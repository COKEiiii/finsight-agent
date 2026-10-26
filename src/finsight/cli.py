from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from finsight.config import load_strategy
from finsight.portfolio import import_journal, journal_entries
from finsight.service import FinSight


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="finsight", description="FinSight A 股选股与 AI 研究")
    p.add_argument("--data-dir", default=os.getenv("FINSIGHT_DATA_DIR", "data/finsight"))
    p.add_argument("--config", help="strategy JSON; parameter changes require a new version")
    commands = p.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="append an immutable data snapshot")
    sync.add_argument("--provider", choices=["demo", "akshare"], default="demo")
    sync.add_argument("--as-of", type=date.fromisoformat)
    sync.add_argument("--start", type=date.fromisoformat)
    sync.add_argument("--symbols", help="comma-separated mainland stock codes; default: 20-stock sample")
    sync.add_argument("--universe", choices=["sample", "mainboard"], default="sample")
    sync.add_argument("--request-timeout", type=float, default=30)
    sync.add_argument("--retries", type=int, choices=[0, 1, 2], default=1)
    screen = commands.add_parser("screen", help="deterministic ranking and portfolio reference plan")
    screen.add_argument("--snapshot")
    screen.add_argument("--as-of", type=date.fromisoformat)
    screen.add_argument("--capital", type=float, default=100_000)
    research = commands.add_parser("research", help="generate research cards; default: local Ollama")
    research.add_argument("--run", required=True)
    research.add_argument("--provider", choices=["ollama", "deepseek", "demo"], default=os.getenv("FINSIGHT_PROVIDER", "ollama"))
    research.add_argument("--model")
    research.add_argument("--limit", type=int, default=10)
    backtest = commands.add_parser("backtest", help="daily ledger and comparable momentum baseline")
    backtest.add_argument("--snapshot")
    backtest.add_argument("--start", type=date.fromisoformat, default=date(2023, 1, 1))
    backtest.add_argument("--end", type=date.fromisoformat, default=date(2024, 12, 31))
    backtest.add_argument("--capital", type=float, default=100_000)
    commands.add_parser("status", help="list snapshot quality and runs")
    journal = commands.add_parser("journal", help="manual simulation trade journal")
    journal_cmd = journal.add_subparsers(dest="journal_command", required=True)
    imp = journal_cmd.add_parser("import")
    imp.add_argument("path", type=Path)
    show = journal_cmd.add_parser("export")
    show.add_argument("--account", default="paper")
    report = journal_cmd.add_parser("report")
    report.add_argument("--account", default="paper")
    report.add_argument("--snapshot")
    report.add_argument("--capital", type=float, required=True)
    report.add_argument("--actual-cash", type=float)
    report.add_argument("--actual-holdings", type=Path, help="JSON object of symbol -> shares")
    watch = commands.add_parser("watch", help="run after 18:00 China time; market history catch-up, no fabricated snapshots")
    watch.add_argument("--once", action="store_true", help="check once and exit; suitable for launchd/cron")
    watch.add_argument("--universe", choices=["sample", "mainboard"], default="sample")
    watch.add_argument("--poll-seconds", type=int, default=60)
    demo = commands.add_parser("demo", help="offline end-to-end demonstration (no model requests)")
    demo.add_argument("--as-of", type=date.fromisoformat, default=date(2025, 12, 31))
    commands.add_parser("dashboard", help="launch the local Streamlit interface")
    schedule = commands.add_parser("schedule", help="macOS daily data collector; no model calls")
    schedule.add_argument("action", choices=["install", "status"])
    return p


def check_daily(service: FinSight, universe: str, now: datetime | None = None) -> dict:
    from finsight.data.akshare_provider import AKShareProvider
    now = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
    cutoff = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
    provider = AKShareProvider(service.store.root)
    calendar = provider.calendar()
    if calendar.empty or calendar["trade_date"].max() < str(cutoff):
        raise ValueError("trading calendar does not cover the collection cutoff")
    sessions = calendar[calendar["trade_date"] <= str(cutoff)]
    if sessions.empty:
        raise ValueError("no completed sessions in exchange calendar")
    day = date.fromisoformat(sessions.iloc[-1]["trade_date"])
    scope = "current mainboard" if universe == "mainboard" else "sample:20 current mainboard stocks"
    saved = [s for s in service.store.snapshots() if s.source == "akshare" and s.universe_scope == scope]
    if any(s.as_of == day and s.publishable for s in saved):
        return {"status": "up_to_date", "as_of": str(day)}
    failed = next((s for s in saved if s.as_of == day), None)
    if failed and (now - failed.captured_at).total_seconds() < 3600:
        return {"status": "waiting_retry", "as_of": str(day), "note": "last failed batch is retained; retry cooldown is one hour"}
    return service.sync("akshare", day, mainboard=universe == "mainboard")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    try:
        service = FinSight(args.data_dir, load_strategy(args.config))
        if args.command == "sync":
            if args.request_timeout <= 0:
                raise ValueError("request-timeout must be positive")
            result = service.sync(args.provider, args.as_of, args.start,
                                  args.symbols.split(",") if args.symbols else None,
                                  args.universe == "mainboard", args.request_timeout, args.retries)
            output(result)
            return 0 if result["publishable"] else 2
        if args.command == "screen":
            result = service.screen(args.snapshot, args.as_of, args.capital)
            output({"run_id": result.run_id, "eligible_count": result.eligible_count,
                    "candidates": result.candidates, "exports": str(service.store.artifact_dir(result.run_id))})
        elif args.command == "research":
            cards = service.research(args.run, args.provider, args.model, args.limit)
            output([c.model_dump(mode="json") for c in cards])
            return 2 if any(c.status == "failed" for c in cards) else 0
        elif args.command == "backtest":
            result = service.backtest(args.snapshot, args.start, args.end, args.capital)
            output({k: result[k] for k in ("run_id", "status", "start", "end", "synthetic") } |
                   {"metrics": result["strategy"]["metrics"], "exports": str(service.store.artifact_dir(result["run_id"]))})
        elif args.command == "status":
            output({"snapshots": [s.model_dump(mode="json") for s in service.store.snapshots()],
                    "runs": [{k: row[k] for k in ("run_id", "kind", "snapshot_id", "created_at")} for row in service.store.runs()]})
        elif args.command == "journal":
            if args.journal_command == "import":
                output(import_journal(service.store, args.path.read_text(encoding="utf-8-sig")))
            elif args.journal_command == "export":
                output([e.model_dump(mode="json") for e in journal_entries(service.store, args.account)])
            else:
                actual = json.loads(args.actual_holdings.read_text()) if args.actual_holdings else None
                output(service.portfolio_report(args.snapshot, args.capital, args.account, args.actual_cash, actual))
        elif args.command == "watch":
            import time
            if args.poll_seconds < 10:
                raise ValueError("poll interval must be at least 10 seconds")
            while True:
                try:
                    output(check_daily(service, args.universe))
                except (ValueError, OSError) as exc:
                    if args.once:
                        raise
                    output({"status": "sync_failed", "error": str(exc)})
                if args.once:
                    break
                time.sleep(args.poll_seconds)
        elif args.command == "demo":
            quality = service.sync("demo", args.as_of)
            result = service.screen(quality["snapshot_id"])
            cards = service.research(result.run_id, "demo")
            bt = service.backtest(quality["snapshot_id"], date(2025, 1, 1), min(args.as_of, date(2025, 12, 31)))
            output({"snapshot_id": quality["snapshot_id"], "screen_run_id": result.run_id,
                    "candidates": len(result.candidates), "research_cards": len(cards),
                    "backtest_run_id": bt["run_id"], "status": bt["status"],
                    "exports": str(service.store.root / "exports")})
        elif args.command == "dashboard":
            import subprocess
            path = Path(__file__).with_name("dashboard.py")
            return subprocess.call([sys.executable, "-m", "streamlit", "run", str(path),
                                    "--server.address", "127.0.0.1", "--", "--data-dir", str(service.store.root)])
        elif args.command == "schedule":
            from finsight.scheduler import install, status
            output(install(service.store.root) if args.action == "install" else status())
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"FinSight: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
