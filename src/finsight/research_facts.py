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
         "amount_20": ("成交额",), "close": ("收盘价", "股价"),
         "rank": ("排名", "名次", "位次"), "score": ("得分", "总分", "评分", "分数"),
         "report_period": ("报告期", "报表期", "财报期", "财报", "年报"),
         "announcement_date": ("公告日", "披露日", "公告时间", "披露时间"),
         "available_on": ("首次可见", "可用日"), "trade_date": ("交易日", "行情日", "行情日期"), "industry": ("行业",)}
CLAIM_INDICATORS = {field: terms for field, terms in TERMS.items() if field != "industry"} | {
    "close": ("收盘价",), "score": ("综合得分", "总分", "综合评分", "选择分数", "策略得分"),
    "report_period": ("报告期", "报表期", "财报期")}
NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
CHINESE_QUANTITY = re.compile(r"百分之[零〇一二两三四五六七八九十百千万亿点负]+|[零〇一二两三四五六七八九十百千万亿]+(?:元|倍|万元|亿元|个百分点|名|只|家)")
INDICATOR_LABEL = re.compile(r"60\s*日(?:收益率|动量)|(?:20|60)\s*日均线|20\s*日(?:平均)?成交额|MA(?:20|60)", re.I)
COMPARISON = re.compile(r"估值(?:合理|偏低|偏高|较低|较高|便宜|昂贵)|低估|高估|(?:低于|高于|优于|超过|落后于).{0,8}(?:行业|同行)|同行相比")
FACT_TOKEN = re.compile(r"\{\{([a-zA-Z_][a-zA-Z_0-9.]*)\}\}")
TOKEN_WITH_UNIT = re.compile(FACT_TOKEN.pattern + r"\s*(元/股|元/日|元每股|元每天|个百分点|%|％|倍|元|分|名|年|点)")
EMPTY_QUANTITY = re.compile(r"第\s*(?:名|位)|(?:为|达|达到)\s*(?:分|点|倍|元|%|年|名)|年\s*年(?:末|初)")


def quantitative_text(text: str):
    remainder = FACT_TOKEN.sub("字段", INDICATOR_LABEL.sub("指标", text))
    return NUMBER.search(remainder) or CHINESE_QUANTITY.search(remainder)


def check_comparison(text: str):
    if COMPARISON.search(text) and not any(word in text for word in ("无法判断", "不能判断", "无法评估", "不能认定", "不足以判断")):
        raise ValueError(f"unsupported valuation or industry comparison must say 无法判断: {text[:120]}")


def check_unprovided_metrics(text: str):
    if any(word in text for word in ("市值", "净利润", "净利率", "毛利率", "负债率", "资金流向")) and not any(
        word in text for word in ("无法判断", "未提供", "缺少", "缺乏", "核实", "核对", "确认", "无法核验")):
        raise ValueError(f"metric is not in this snapshot evidence: {text[:120]}")


def compatible_unit(expected: str, suffix: str) -> bool:
    if suffix == expected or (suffix == "％" and expected == "%"):
        return True
    return (suffix == "元" and expected.startswith("元/")) or (expected, suffix) in {
        ("元/股", "元每股"), ("元/日", "元每天")}


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
                                            "valuation_source" if group == "valuation" else
                                            "market_source" if group == "market" else "source", "snapshot")),
                "version": str(candidate.get("financial_version") if group == "financials" else
                               candidate.get("market_version") if group == "market" else result.snapshot_id),
                "snapshot_id": result.snapshot_id,
            }
    return facts


def check_field_claims(content: ResearchContent, facts: dict[str, dict]):
    for text in [content.summary, *content.information_gaps, *content.human_checks]:
        if FACT_TOKEN.search(text):
            raise ValueError("place field values only in cited reasons or risks; keep summary and checks qualitative")
        if EMPTY_QUANTITY.search(text):
            raise ValueError(f"unfilled numeric sentence: {text[:120]}")
        match = quantitative_text(text)
        if match:
            raise ValueError(f"model narrative contains a number ({match.group()}); omit quantities and dates from: {text[:120]}")
        check_comparison(text)
        check_unprovided_metrics(text)
    for claim in content.reasons + content.risks:
        if any(key not in facts for key in claim.evidence_ids):
            raise ValueError("research card cites unknown field evidence")
        text = claim.text
        for token in FACT_TOKEN.finditer(text):
            key = token.group(1)
            if key not in claim.evidence_ids:
                raise ValueError(f"numeric template must cite its matching field: {key}")
            prefix = FACT_TOKEN.sub("", text[:token.start()])
            mentions = [(match.start(), field) for field, terms in TERMS.items() for term in terms
                        for match in re.finditer(re.escape(term), prefix, re.I)]
            if mentions and max(mentions)[1] != facts[key]["field"]:
                raise ValueError(f"field/evidence mismatch: label {max(mentions)[1]} versus {key}; place each value next to its own metric name")
            if not mentions and len(claim.evidence_ids) > 1:
                raise ValueError(f"name the metric immediately before its field placeholder: {key}")
        if "{{" in FACT_TOKEN.sub("", text) or "}}" in FACT_TOKEN.sub("", text):
            raise ValueError("invalid field placeholder")
        if EMPTY_QUANTITY.search(text):
            raise ValueError(f"unfilled numeric sentence; use a matching field placeholder: {text[:120]}")
        for match in TOKEN_WITH_UNIT.finditer(text):
            if not compatible_unit(facts[match.group(1)]["unit"], match.group(2)):
                raise ValueError(f"field placeholder has an incompatible repeated unit: {match.group()}")
        match = quantitative_text(text)
        if match:
            raise ValueError(f"model narrative contains a number ({match.group()}); remove quantities from: {text[:120]}")
        narrative = FACT_TOKEN.sub("", text)
        for field, terms in CLAIM_INDICATORS.items():
            if any(term.lower() in narrative.lower() for term in terms):
                if not any(facts[key]["field"] == field for key in claim.evidence_ids):
                    raise ValueError(f"field/evidence mismatch: {field}")
        check_comparison(text)
        check_unprovided_metrics(text)


def render_value(fact: dict) -> str:
    value, unit = fact["value"], fact["unit"]
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    if unit in {"日期", "文本"}:
        return text
    if unit in {"小数收益率", "小数比值"}:
        return f"{text}（{unit}）"
    return f"{text}{unit}"


def render_content(template: ResearchContent, facts: dict[str, dict]) -> ResearchContent:
    check_field_claims(template, facts)
    def replace(text: str):
        text = TOKEN_WITH_UNIT.sub(lambda match: "{{" + match.group(1) + "}}", text)
        return FACT_TOKEN.sub(lambda match: render_value(facts[match.group(1)]), text)
    value = template.model_dump()
    value["summary"] = replace(value["summary"])
    for section in ("reasons", "risks"):
        for item in value[section]:
            item["text"] = replace(item["text"])
    for section in ("information_gaps", "human_checks"):
        value[section] = [replace(item) for item in value[section]]
    return ResearchContent.model_validate(value)
