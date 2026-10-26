from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from finsight.backtest import run_backtest
from finsight.data.akshare_provider import AKShareProvider
from finsight.data.demo import make_demo
from finsight.data.quality import audit, missing_sessions
from finsight.data.store import Store, code_version
from finsight.models import ResearchCard, ScreeningResult, StrategyConfig
from finsight.portfolio import journal_template, portfolio_plan
from finsight.research import ChatAdapter, render_card, research_candidate
from finsight.screening import screen


class FinSight:
    def __init__(self, root: str | Path = "data/finsight", config: StrategyConfig | None = None):
        self.store = Store(root)
        self.config = config or StrategyConfig()

    def sync(self, provider: str = "demo", as_of: date | None = None, start: date | None = None,
             symbols: list[str] | None = None, mainboard: bool = False,
             timeout: float = 30, retries: int = 1) -> dict:
        if provider == "demo":
            bundle = make_demo(as_of or date(2025, 12, 31), start or date(2022, 1, 3))
        elif provider == "akshare":
            bundle = AKShareProvider(self.store.root, timeout, retries).fetch(as_of, start, symbols, mainboard)
            # Preserve first-observed financial values and historical stock states across daily snapshots.
            previous = next((s for s in self.store.snapshots() if s.source == "akshare" and
                             s.universe_scope == bundle.meta.universe_scope and s.as_of <= bundle.meta.as_of), None)
            if previous:
                old = self.store.load_snapshot(previous.snapshot_id)
                states = pd.concat([old["universe"], bundle["universe"]], ignore_index=True)
                bundle.tables["universe"] = states.drop_duplicates(["symbol", "known_on"], keep="last")
                fin = pd.concat([old["financials"], bundle["financials"]], ignore_index=True)
                identity = ["symbol", "report_period", "announcement_date", "roe", "revenue_growth"]
                bundle.tables["financials"] = fin.sort_values("available_on").drop_duplicates(identity, keep="first")
        else:
            raise ValueError("unknown data provider")
        quality = audit(bundle)
        saved = {str(s.as_of) for s in self.store.snapshots()
                 if s.source == bundle.meta.source and s.universe_scope == bundle.meta.universe_scope}
        quality["missing_snapshot_dates"] = missing_sessions(bundle, saved | {str(bundle.meta.as_of)})
        if quality["missing_snapshot_dates"]:
            quality["catchup_note"] = "行情历史已补采；缺失日未保存的估值/财务/股票状态版本不可重建，不伪造当日快照。"
        self.store.save_snapshot(bundle)
        folder = self.store.root / "snapshots" / bundle.meta.snapshot_id
        (folder / "quality.json").write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
        return quality

    def screen(self, snapshot_id: str | None = None, as_of: date | None = None,
               capital: float = 100_000) -> ScreeningResult:
        bundle = self.store.load_snapshot(snapshot_id)
        audit(bundle)
        result = screen(bundle, self.config, as_of)
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
