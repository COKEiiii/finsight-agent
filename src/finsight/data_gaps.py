"""Explain saved collection gaps without fetching, imputing or republishing data."""
from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd

from finsight.data.store import Store, code_version
from finsight.factors import PRICE_FIELDS, coverage_report, evaluate, invalid_price_mask
from finsight.models import StrategyConfig


def records(frame: pd.DataFrame) -> list[dict]:
    # JSON null means unavailable; never turn a missing observation into zero.
    frame = frame.replace([np.inf, -np.inf], np.nan).astype(object)
    return frame.where(pd.notna(frame), None).to_dict("records")


def build_gap_report(store: Store, snapshot_id: str, config: StrategyConfig) -> dict:
    calendar = store.load_snapshot(snapshot_id, table_names=("calendar",))
    day = str(calendar.meta.as_of)
    sessions = sorted(calendar["calendar"].loc[calendar["calendar"]["trade_date"] <= day, "trade_date"])
    expected = sessions[-61:]
    lower = expected[0] if expected else day
    bundle = store.load_snapshot(snapshot_id, date_range=(lower, day),
        table_names=("calendar", "universe", "prices", "financials", "valuations"))
    frame, exclusions = evaluate(bundle, bundle.meta.as_of, config)
    coverage = coverage_report(bundle, frame, exclusions)
    prices = bundle["prices"].sort_values(["symbol", "trade_date"], kind="stable")
    prices = prices.groupby("symbol", sort=False).tail(61)
    bad = prices.loc[invalid_price_mask(prices)].copy()
    bad["missing_fields"] = bad[list(PRICE_FIELDS)].apply(pd.to_numeric, errors="coerce").apply(
        lambda row: [field for field in PRICE_FIELDS if not np.isfinite(row[field])], axis=1)
    fields = ["symbol", "trade_date", *PRICE_FIELDS, "suspended", "missing_fields", "source", "fetched_at"]
    bad = bad.reindex(columns=fields)
    financials = bundle["financials"]
    financials = financials.loc[(financials["available_on"] <= day) &
        (financials["announcement_date"] < day) & financials["report_period"].str.endswith("12-31")]
    details = []
    for missing in coverage["missing"]:
        symbol = missing["symbol"]
        bars = bad.loc[bad["symbol"] == symbol]
        current = prices.loc[(prices["symbol"] == symbol) & (prices["trade_date"] == day)]
        fin = financials.loc[financials["symbol"] == symbol]
        if not fin.empty:
            # Keep all observable versions of the latest period, without
            # asserting that a different ROE definition can replace the input.
            fin = fin.loc[fin["report_period"] == fin["report_period"].max()]
        vals = bundle["valuations"]
        vals = vals.loc[(vals["symbol"] == symbol) & (vals["trade_date"] == day)]
        dates = set(prices.loc[prices["symbol"] == symbol, "trade_date"])
        details.append({**missing, "missing_sessions": sorted(set(expected) - dates),
            "invalid_bars": records(bars), "current_bar": records(current),
            "latest_available_annual_records": records(fin), "current_valuation": records(vals)})
    missing_symbols = {item["symbol"] for item in details}
    bad = bad.loc[bad["symbol"].isin(missing_symbols)]
    return {"protocol": "collection_gaps_v1", "snapshot_id": snapshot_id, "as_of": day,
        "source": bundle.meta.source, "synthetic": bundle.meta.synthetic,
        "historical_validation_passed": bundle.meta.research_eligible,
        "report_code_version": code_version(), "config_id": config.config_id,
        "strategy": config.model_dump(), "snapshot_table_hashes": bundle.meta.table_hashes,
        "universe_id": bundle.meta.universe_id, "membership_id": bundle.meta.membership_id,
        "window": {"first_session": lower, "last_session": day, "sessions": len(expected)},
        "saved_publication_status": bundle.meta.publication_status,
        "saved_coverage": bundle.meta.coverage,
        "coverage": {key: value for key, value in coverage.items() if key != "missing"},
        "missing_reason_counts": dict(Counter(code for item in details for code in item["codes"])),
        "invalid_bar_count": len(bad),
        "invalid_bars_with_confirmed_suspension": int(bad["suspended"].eq(True).fillna(False).sum()),
        "details": details,
        "notes": ["按选股使用的最近 61 个交易日窗口重算；不修改快照、排名或原发布状态。",
            "停牌记录缺少成交量或成交额仍为数据缺口，不推定为零；负估值也不能掩盖数据缺口。",
            "报告仅检查已保存的处理后快照；未重新请求上游，也未认证历史修订或原始报文字段。",
            "优先核实所列股票、日期与字段；缺少年报加权 ROE 时，不以平均 ROE 或其他期间替代。"]}


def render_gap_report(report: dict) -> str:
    coverage = report["coverage"]
    lines = ["# FinSight 数据缺口核查", f"快照：{report['snapshot_id']} · 截止 {report['as_of']}",
        "数据类型：" + ("离线演示" if report["synthetic"] else "已保存的真实采集快照"),
        "历史验证：" + ("通过" if report["historical_validation_passed"] else "未通过"),
        f"覆盖率：{coverage['coverage']:.2%} · 数据缺口 {coverage['missing_count']} 只 · 已确认规则排除 {coverage['confirmed_exclusions']} 只",
        f"异常行情 {report['invalid_bar_count']} 行，其中确认停牌 {report['invalid_bars_with_confirmed_suspension']} 行。",
        "", "| 股票 | 未满足的判定 | 异常行情行数 | 缺失交易日数 |", "|---|---|---:|---:|"]
    lines += [f"| {item['symbol']} | {', '.join(item['codes'])} | {len(item['invalid_bars'])} | {len(item['missing_sessions'])} |"
        for item in report["details"]]
    lines += ["", "逐行字段、数值、日期、来源及首次抓取时间见 data_gaps.json 和 invalid_bars.csv。",
        "", *[f"- {note}" for note in report["notes"]]]
    return "\n".join(lines) + "\n"
