"""Field-level, immutable evidence for v0.2 research cards."""
from __future__ import annotations

import re

from finsight.models import ResearchContent, ScreeningResult

FIELD_GROUPS = {
    "selection": ("rank", "score", "industry"),
    "financials": ("roe", "revenue_growth", "report_period", "announcement_date", "available_on"),
    "market": ("momentum_60", "ma_ratio", "amount_20", "close", "trade_date"),
    "valuation": ("pe_ttm", "pb"),
}
UNITS = {"rank": "名", "score": "分", "roe": "%", "revenue_growth": "%",
         "momentum_60": "小数收益率", "ma_ratio": "小数比值", "amount_20": "元/日",
         "close": "元/股", "pe_ttm": "倍", "pb": "倍"}
TERMS = {"roe": ("ROE", "净资产收益率"), "revenue_growth": ("营收", "收入同比"),
         "momentum_60": ("动量", "60 日收益率", "60日收益率"),
         "ma_ratio": ("均线", "MA20", "MA60"),
         "pe_ttm": ("PE", "市盈率"), "pb": ("PB", "市净率"),
         "amount_20": ("成交额",), "close": ("收盘价",),
         "rank": ("排名", "名次"), "score": ("得分", "总分")}
NUMBER = re.compile(r"(?<![A-Za-z0-9_.])[-+]?\d+(?:\.\d+)?")


def facts_for(result: ScreeningResult, candidate: dict) -> dict[str, dict]:
    facts = {}
    for group, fields in FIELD_GROUPS.items():
        for field in fields:
            if field not in candidate:
                continue
            value = candidate[field]
            facts[f"{group}.{field}"] = {
                "field": field, "value": value, "unit": UNITS.get(field, "日期" if "date" in field or field in {"report_period", "available_on", "trade_date"} else "文本"),
                "date": str(candidate.get("trade_date") if group in {"market", "valuation", "selection"} else candidate.get("report_period")),
                "known_at": str(candidate.get("available_on") if group == "financials" else result.as_of),
                "source": str(candidate.get("financial_source" if group == "financials" else
                                            "valuation_source" if group == "valuation" else "source", "snapshot")),
                "version": str(candidate.get("financial_version") if group == "financials" else result.snapshot_id),
                "snapshot_id": result.snapshot_id,
            }
    return facts


def check_field_claims(content: ResearchContent, facts: dict[str, dict]):
    for claim in content.reasons + content.risks:
        if any(key not in facts for key in claim.evidence_ids):
            raise ValueError("research card cites unknown field evidence")
        text = claim.text
        if NUMBER.search(text):
            raise ValueError("model narrative contains a number; the program renders field facts")
        for field, terms in TERMS.items():
            if any(term.lower() in text.lower() for term in terms):
                if not any(facts[key]["field"] == field for key in claim.evidence_ids):
                    raise ValueError(f"field/evidence mismatch: {field}")
        if any(term in text for term in ("估值合理", "明显低估", "低估", "高估", "行业平均", "同行相比")) and "无法判断" not in text:
            raise ValueError("unsupported valuation or industry comparison must say 无法判断")
    if NUMBER.search(content.summary):
        raise ValueError("model summary contains a number; the program renders field facts")
