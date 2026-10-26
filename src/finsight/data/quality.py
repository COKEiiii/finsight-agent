from __future__ import annotations

from datetime import date

from finsight.data.store import TABLES, SnapshotBundle
from finsight.models import RESEARCH_CHECKS

REQUIRED = {
    "universe": {"symbol", "name", "industry", "listed_on", "board", "is_st", "is_delisting", "known_on"},
    "prices": {"symbol", "trade_date", "open", "high", "low", "close", "adj_close", "amount", "volume", "suspended"},
    "financials": {"symbol", "report_period", "announcement_date", "available_on", "roe", "revenue_growth"},
    "valuations": {"symbol", "trade_date", "pe_ttm", "pb"},
    "calendar": {"trade_date"}, "benchmark": {"trade_date", "close"},
    "actions": {"symbol", "ex_date", "pay_date", "split_ratio", "cash_per_share"},
}


def audit(bundle: SnapshotBundle) -> dict:
    fatal, issues = [], list(bundle.meta.quality_issues)
    for key in TABLES:
        if key not in bundle.tables or not REQUIRED[key].issubset(bundle[key].columns):
            fatal.append(f"{key}: required columns missing")
    if not fatal:
        day = str(bundle.meta.as_of)
        for key, fields in (("prices", ["symbol", "trade_date"]), ("valuations", ["symbol", "trade_date"]),
                            ("calendar", ["trade_date"]), ("universe", ["symbol", "known_on"])):
            if bundle[key].duplicated(fields).any():
                fatal.append(f"{key}: duplicate keys")
        for key in ("universe", "prices", "financials", "valuations", "benchmark"):
            if bundle[key].empty:
                fatal.append(f"{key}: empty batch")
        if day not in set(bundle["calendar"]["trade_date"].astype(str)):
            fatal.append("snapshot date is absent from the trading calendar")
        if day not in set(bundle["prices"]["trade_date"].astype(str)):
            fatal.append("no prices for snapshot date; stale batch")
        if day not in set(bundle["valuations"]["trade_date"].astype(str)):
            fatal.append("no valuations for snapshot date; stale batch")
        for key in ("prices", "valuations", "benchmark"):
            if (bundle[key]["trade_date"].astype(str) > day).any():
                fatal.append(f"{key}: observations after snapshot date")
        latest = bundle["prices"][bundle["prices"]["trade_date"] == day]
        observed = set(latest["symbol"])
        missing = set(bundle["universe"]["symbol"]) - observed
        if missing:
            issues.append(f"{len(missing)} symbols have no bar on the snapshot date; excluded")
        bad = bundle["prices"]
        bad = bad[(bad["close"] <= 0) | (bad["open"] <= 0) | (bad["amount"] < 0) |
                  (bad["high"] < bad[["open", "close"]].max(axis=1)) |
                  (bad["low"] > bad[["open", "close"]].min(axis=1))]
        if not bad.empty:
            fatal.append(f"{len(bad)} invalid OHLC/amount observations")
    bundle.meta.publishable = bundle.meta.publishable and not fatal
    bundle.meta.quality_issues = list(dict.fromkeys(issues + fatal))
    return {
        "snapshot_id": bundle.meta.snapshot_id, "as_of": str(bundle.meta.as_of),
        "source": bundle.meta.source, "synthetic": bundle.meta.synthetic,
        "universe_scope": bundle.meta.universe_scope,
        "publishable": bundle.meta.publishable, "research_eligible": bundle.meta.research_eligible,
        "research_status": "historical_checks_passed" if bundle.meta.research_eligible else "研究演示：历史验证未通过",
        "table_rows": {k: len(bundle.tables.get(k, [])) for k in TABLES},
        "issues": bundle.meta.quality_issues,
        "missing_research_checks": [k for k in RESEARCH_CHECKS if not bundle.meta.research_checks.get(k, False)],
    }


def missing_sessions(bundle: SnapshotBundle, saved_dates: set[str], start: date | None = None) -> list[str]:
    dates = bundle["calendar"]["trade_date"].astype(str)
    lower = str(start) if start else min(saved_dates, default=str(bundle.meta.as_of))
    return sorted(d for d in dates if lower <= d <= str(bundle.meta.as_of) and d not in saved_dates)
