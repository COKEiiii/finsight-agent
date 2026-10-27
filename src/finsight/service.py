from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from finsight.backtest import run_backtest
from finsight.data.demo import make_demo
from finsight.data.quality import audit, missing_sessions
from finsight.data.store import Store, code_version
from finsight.models import ResearchCard, ScreeningResult, StrategyConfig
from finsight.portfolio import journal_template, portfolio_plan, reconcile
from finsight.research import ChatAdapter, render_card, research_candidate
from finsight.screening import screen


class FinSight:
    def __init__(self, root: str | Path = "data/finsight", config: StrategyConfig | None = None):
        self.store = Store(root)
        self.config = config or StrategyConfig()

    def sync(self, provider: str = "demo", as_of: date | None = None, start: date | None = None,
             symbols: list[str] | None = None, mainboard: bool = False,
             timeout: float = 30, retries: int = 2, *, resume: str | None = None,
             background: bool = False, limit: int | None = None, workers: int = 4,
             interval: float = 1, api=None, clock=None) -> dict:
        if provider == "demo" and resume is None:
            from finsight.data.universe import current_members, membership_id
            bundle = make_demo(as_of or date(2025, 12, 31), start or date(2022, 1, 3))
            members = current_members(bundle["universe"])
            bundle.meta.schema_version = 2
            bundle.meta.universe_id = "demo-" + membership_id(members)
            bundle.meta.membership_id, bundle.meta.membership_symbols = membership_id(members), members
            return self._save_bundle(bundle)
        from finsight.data.akshare_provider import normalize_symbol
        from finsight.data.demo import SAMPLE_CODES
        from finsight.data.universe import universe_id
        from finsight.jobs import Jobs
        if provider != "akshare" and resume is None:
            raise ValueError("unknown data provider")
        if timeout <= 0 or not 1 <= workers <= 4 or retries not in {0, 1, 2} or interval < 0:
            raise ValueError("invalid collector timeout/concurrency/retry/rate settings")
        jobs = Jobs(self.store)
        if resume:
            job = jobs.resume(resume)
        else:
            members = [normalize_symbol(s) for s in symbols] if symbols else (None if mainboard else [normalize_symbol(s) for s in SAMPLE_CODES])
            uid = universe_id(mainboard, members, limit)
            previous = self.store.latest(uid)
            from finsight.data.seeds import select_seeds
            now = clock() if clock else datetime.now(ZoneInfo("Asia/Shanghai"))
            cutoff = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
            if previous and previous.job_id and start is None and any(
                "no stock state known at cutoff" in issue for issue in previous.quality_issues
            ):
                calendar = self.store.load_snapshot(previous.snapshot_id, table_names=("calendar",))["calendar"]
                requested = str(min(as_of or cutoff, cutoff))
                known_sessions = calendar.loc[calendar["trade_date"] <= requested, "trade_date"]
                if (not known_sessions.empty and requested <= calendar["trade_date"].max() and
                        known_sessions.max() <= str(previous.as_of)):
                    prior_job = jobs.get(previous.job_id)
                    if prior_job["progress"].get("phase") == "done" and not prior_job["requests"]["failed"]:
                        from finsight.collection_status import recovery_status
                        raise ValueError(recovery_status(self.store, prior_job)["message"])
            seeds = select_seeds(self.store, uid, min(as_of or cutoff, cutoff), now) if previous is None else []
            config = {"as_of": str(as_of) if as_of else None, "start": str(start) if start else None,
                "symbols": symbols, "mainboard": mainboard, "limit": limit,
                "timeout": timeout, "retries": retries, "workers": workers, "interval": interval,
                "universe_id": uid, "base_snapshot_id": previous.snapshot_id if previous else None,
                "seed_snapshot_ids": seeds,
                "strategy": self.config.model_dump()}
            self.store.register_strategy(self.config)
            job = jobs.create(config)
        if background:
            return jobs.launch(job["job_id"])
        if job["status"] == "running" and jobs.alive(job["pid"]):
            return {"job_id": job["job_id"], "publishable": False, "status": "already_running"}
        return self.run_job(job["job_id"], api=api, clock=clock)

    def _save_bundle(self, bundle) -> dict:
        quality = audit(bundle, self.config)
        quality["config_id"] = self.config.config_id
        saved = {str(s.as_of) for s in self.store.snapshots() if s.universe_id == bundle.meta.universe_id}
        quality["missing_snapshot_dates"] = missing_sessions(bundle, saved | {str(bundle.meta.as_of)})
        if quality["missing_snapshot_dates"]:
            quality["catchup_note"] = "已补采行情；缺失日期的历史股票状态和财报版本不能事后伪造。"
        self.store.save_snapshot(bundle)
        folder = self.store.root / "snapshots" / bundle.meta.snapshot_id
        (folder / "quality.json").write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
        return quality

    def run_job(self, job_id: str, *, api=None, clock=None) -> dict:
        import fcntl
        import os
        import signal
        import threading
        import time

        from finsight.data.collector import Collector
        from finsight.data.requests import RequestBroker
        from finsight.jobs import JobCancelled, Jobs
        jobs = Jobs(self.store)
        row = jobs.get(job_id)
        config = row["config"]
        self.config = StrategyConfig.model_validate(config["strategy"])
        started = time.perf_counter()
        broker = None
        previous_handler = None
        with (self.store.root / ".collector.lock").open("a+") as lock:
            # Different profiles can queue; only one coordinator consumes the root's request budget.
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                current = jobs.get(job_id)
                if current["status"] in {"completed", "partial"} and current["snapshot_id"]:
                    saved = self.store.root / "snapshots" / current["snapshot_id"] / "quality.json"
                    quality = json.loads(saved.read_text(encoding="utf-8"))
                    return {**quality, "job_id": job_id, "screen_run_id": current["progress"].get("screen_run_id")}
                jobs.check_cancelled(job_id)
                jobs.update(job_id, status="running", pid=os.getpid(), progress={"phase": "starting"})
                broker = RequestBroker(self.store, job_id, config["workers"], config["interval"],
                                       config["timeout"], config["retries"], api=api, clock=clock)
                if threading.current_thread() is threading.main_thread():
                    previous_handler = signal.getsignal(signal.SIGTERM)
                    signal.signal(signal.SIGTERM, lambda *_: broker.cancel_event.set())
                base = self.store.load_snapshot(config["base_snapshot_id"]) if config.get("base_snapshot_id") else None
                seeds = [self.store.load_snapshot(identifier, table_names=("prices", "valuations", "financials"))
                         for identifier in config.get("seed_snapshot_ids", [])]
                bundle = Collector(broker, config, base, seeds=seeds).fetch()
                jobs.update(job_id, progress={"phase": "quality_and_snapshot"})
                quality = self._save_bundle(bundle)
                result = self.screen(bundle.meta.snapshot_id, preview=not quality["publishable"])
                quality.update(job_id=job_id, screen_run_id=result.run_id)
                jobs.update(job_id, status="completed" if quality["publishable"] else "partial", pid=None,
                    snapshot_id=bundle.meta.snapshot_id, progress={"phase": "done", "coverage": bundle.meta.coverage,
                    "screen_run_id": result.run_id, "elapsed_seconds": time.perf_counter() - started,
                    "publication_status": result.publication_status})
                if bundle.meta.universe_complete and not bundle.meta.synthetic:
                    if quality["publishable"]:
                        with self.store.connect() as db:
                            db.execute("INSERT OR REPLACE INTO publications VALUES (?,?,?,?,?)", (bundle.meta.universe_id,
                                str(bundle.meta.as_of), bundle.meta.snapshot_id, result.run_id, quality["coverage"]["coverage"]))
                    if api is None:
                        from finsight.scheduler import update_collection_rollout
                        try:
                            quality["rollout"] = update_collection_rollout(self.store)
                        except (OSError, ValueError) as exc:
                            quality["schedule_issue"] = str(exc)
                            jobs.update(job_id, progress={"schedule_issue": str(exc)})
                try:
                    report = self.collection_report(job_id, export=True)
                    quality.update(collection_report_run_id=report["run_id"], collection_report_exports=report["exports"])
                    jobs.update(job_id, progress={"collection_report_run_id": report["run_id"]})
                except (OSError, ValueError) as exc:
                    quality["report_issue"] = str(exc)
                    jobs.update(job_id, progress={"report_issue": str(exc)})
                return quality
            except JobCancelled as exc:
                jobs.update(job_id, status="cancelled", error=str(exc), pid=None, progress={"phase": "cancelled"})
                raise ValueError(str(exc)) from exc
            except Exception as exc:
                jobs.update(job_id, status="failed", error=f"{type(exc).__name__}: {str(exc)[:500]}", pid=None,
                            progress={"phase": "failed", "elapsed_seconds": time.perf_counter() - started})
                raise
            finally:
                if previous_handler is not None:
                    signal.signal(signal.SIGTERM, previous_handler)
                if broker:
                    broker.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    def screen(self, snapshot_id: str | None = None, as_of: date | None = None,
               capital: float = 100_000, preview: bool = False) -> ScreeningResult:
        if snapshot_id is None:
            latest = self.store.latest(published_only=not preview)
            if latest is None:
                raise ValueError("no published snapshots; select a preview snapshot explicitly")
            snapshot_id = latest.snapshot_id
        full = self.store.load_snapshot(snapshot_id, table_names=("calendar",))
        if as_of and as_of > full.meta.as_of:
            raise ValueError("selection date exceeds snapshot cutoff")
        day = as_of or full.meta.as_of
        sessions = sorted(full["calendar"]["trade_date"].loc[full["calendar"]["trade_date"] <= str(day)])
        lower = sessions[max(0, len(sessions) - 61)] if sessions else str(day)
        bundle = self.store.load_snapshot(snapshot_id, date_range=(lower, str(day)))
        quality_file = self.store.root / "snapshots" / snapshot_id / "quality.json"
        saved_quality = json.loads(quality_file.read_text(encoding="utf-8")) if quality_file.exists() else {}
        if day != bundle.meta.as_of or saved_quality.get("config_id") != self.config.config_id:
            audit(bundle, self.config)
        result = screen(bundle, self.config, as_of, preview=preview, store=self.store)
        payload = result.model_dump(mode="json")
        self.store.save_run("screen", result.snapshot_id, self.config, payload, result.run_id)
        folder = self.store.artifact_dir(result.run_id)
        (folder / "screening.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        pd.DataFrame(result.candidates).to_csv(folder / "candidates.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(result.exclusions).to_csv(folder / "exclusions.csv", index=False, encoding="utf-8-sig")
        (folder / "journal_template.csv").write_text(journal_template(result), encoding="utf-8-sig")
        plan = portfolio_plan(result, self.config, capital, bundle["calendar"])
        (folder / "portfolio_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def collection_report(self, job_id: str, *, export: bool = False, now: datetime | None = None) -> dict:
        from finsight.collection_status import build_report, render_report
        from finsight.jobs import Jobs
        job = Jobs(self.store).get(job_id)
        report = build_report(self.store, job, now)
        if export:
            config = StrategyConfig.model_validate(job["config"].get("strategy", self.config.model_dump()))
            run_id = self.store.save_run("collection_report", job["snapshot_id"] or "", config, report)
            folder = self.store.artifact_dir(run_id)
            report.update(run_id=run_id, exports=str(folder))
            (folder / "collection_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "collection_report.md").write_text(render_report(report), encoding="utf-8")
            pd.DataFrame(report["methods"]).to_csv(folder / "requests.csv", index=False, encoding="utf-8-sig")
        return report

    def research(self, run_id: str, provider: str = "ollama", model: str | None = None,
                 limit: int = 10, adapter=None) -> list[ResearchCard]:
        if limit < 1 or limit > 100:
            raise ValueError("research limit must be in 1..100")
        record = self.store.run(run_id)
        if record["kind"] != "screen":
            raise ValueError("research requires a screening run")
        result = ScreeningResult.model_validate(record["payload"])
        own_adapter = provider != "demo" and adapter is None
        if own_adapter:
            adapter = ChatAdapter(provider, model)
        try:
            cards = [research_candidate(result, c, self.store, adapter, demo=provider == "demo")
                     for c in result.candidates[:limit]]
        finally:
            if own_adapter:
                adapter.close()
        payload = {"screen_run_id": run_id, "provider": provider, "cards": [c.model_dump(mode="json") for c in cards],
                   "code_version": code_version()}
        research_id = self.store.save_run("research", result.snapshot_id, StrategyConfig.model_validate(record["config"]), payload)
        folder = self.store.artifact_dir(research_id)
        (folder / "research.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "research.md").write_text("# FinSight 研究卡\n\n" + "\n\n---\n\n".join(render_card(c) for c in cards), encoding="utf-8")
        return cards

    def backtest(self, snapshot_id: str | None = None, start: date = date(2023, 1, 1),
                 end: date = date(2024, 12, 31), capital: float = 100_000) -> dict:
        bundle = self.store.load_snapshot(snapshot_id)
        audit(bundle)
        payload = run_backtest(bundle, self.config, start, end, capital)
        payload.update(code_version=code_version(), strategy_config=self.config.model_dump())
        run_id = self.store.save_run("backtest", bundle.meta.snapshot_id, self.config, payload)
        payload["run_id"] = run_id
        folder = self.store.artifact_dir(run_id)
        (folder / "backtest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        curve = pd.DataFrame(payload["strategy"]["curve"])
        curve = curve.merge(pd.DataFrame(payload["benchmark"]), on="trade_date")
        curve["momentum_nav"] = [p["nav"] for p in payload["momentum_baseline"]["curve"]]
        curve.to_csv(folder / "equity_curve.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(payload["strategy"]["trades"]).to_csv(folder / "trades.csv", index=False, encoding="utf-8-sig")
        (folder / "backtest.md").write_text("# FinSight 回测\n\n" + payload["status"] + "\n\n```json\n" +
            json.dumps(payload["strategy"]["metrics"], ensure_ascii=False, indent=2) + "\n```\n\n" +
            "\n".join("- " + s for s in payload["assumptions"]), encoding="utf-8")
        return payload

    def portfolio_report(self, snapshot_id: str | None = None, capital: float = 100_000,
                         account: str = "paper", actual_cash: float | None = None,
                         actual_holdings: dict[str, int] | None = None) -> dict:
        bundle = self.store.load_snapshot(snapshot_id)
        payload = reconcile(self.store, bundle, capital, account, actual_cash, actual_holdings, self.config)
        payload.update(snapshot_id=bundle.meta.snapshot_id, code_version=code_version())
        run_id = self.store.save_run("portfolio_report", bundle.meta.snapshot_id, self.config, payload)
        payload["run_id"] = run_id
        folder = self.store.artifact_dir(run_id)
        (folder / "portfolio_review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        pd.DataFrame(payload["performance"]["curve"]).to_csv(folder / "portfolio_nav.csv", index=False, encoding="utf-8-sig")
        (folder / "weekly_review.md").write_text(
            "# FinSight 模拟组合复盘\n\n" + f"账户：{account} · 截止：{bundle.meta.as_of}\n\n" +
            "```json\n" + json.dumps({"cash": payload["cash"], "nav": payload["nav"],
                "weekly_return": payload["performance"].get("weekly_return"), "flags": payload["flags"],
                "metrics": payload["performance"].get("metrics")}, ensure_ascii=False, indent=2) + "\n```\n\n" +
            "请逐项复盘：策略表现、数据缺口、实际执行、人为偏离、市场与行业因素。\n", encoding="utf-8")
        return payload
