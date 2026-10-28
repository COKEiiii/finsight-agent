"""Field-level, immutable evidence for v0.2 research cards."""
from __future__ import annotations

import re

from finsight.models import ResearchContent, ScreeningResult
from finsight.research_requirements import assisted_information_gaps, hypothetical

FIELD_GROUPS = {
    "selection": ("rank", "score", "industry"),
    "financials": ("roe", "revenue_growth", "report_period", "announcement_date", "available_on"),
    "market": ("momentum_60", "ma_ratio", "amount_20", "close", "trade_date"),
    "valuation": ("pe_ttm", "pb"),
}
UNITS = {"rank": "名", "score": "分", "roe": "%", "revenue_growth": "%",
         "momentum_60": "小数收益率", "ma_ratio": "小数比值", "amount_20": "元/日",
         "close": "元/股", "pe_ttm": "倍", "pb": "倍"}
TERMS = {"roe": ("ROE", "净资产收益率"), "revenue_growth": ("营收", "营业收入", "收入同比", "收入增长"),
         "momentum_60": ("动量", "60 日收益率", "60日收益率"),
         "ma_ratio": ("均线", "MA20", "MA60"),
         "pe_ttm": ("PE", "市盈率"), "pb": ("PB", "市净率"),
         "amount_20": ("成交额",), "close": ("收盘价", "股价"),
         "rank": ("排名", "名次", "位次"), "score": ("得分", "总分", "评分", "分数"),
         "report_period": ("报告期", "报表期", "财报期", "财报", "年报"),
         "announcement_date": ("公告日", "披露日", "公告时间", "披露时间"),
         "available_on": ("首次可见", "可用日"), "trade_date": ("交易日", "行情日", "行情日期"), "industry": ("行业",)}
CLAIM_INDICATORS = {field: terms for field, terms in TERMS.items() if field not in {
    "industry", "report_period", "announcement_date", "available_on", "trade_date"}} | {
    "close": ("收盘价",), "score": ("综合得分", "总分", "综合评分", "选择分数", "策略得分"),
}
NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
CHINESE_QUANTITY = re.compile(r"百分之[零〇一二两三四五六七八九十百千万亿点负]+|[零〇一二两三四五六七八九十百千万亿]+(?:元|倍|万元|亿元|个百分点|名|只|家)")
INDICATOR_LABEL = re.compile(r"60\s*日(?:收益率|动量)|(?:20|60)\s*日均线|20\s*日(?:平均)?成交额|MA(?:20|60)", re.I)
COMPARISON = re.compile(r"(?:估值|市盈率|市净率|\bPE\b|\bPB\b).{0,20}(?:合理|偏低|偏高|较低|较高|便宜|昂贵|高位|低位)|低估|高估|(?:低于|高于|优于|超过|落后于).{0,8}(?:行业|同行)|同行相比", re.I)
BENCHMARK_COMPARISON = re.compile(r"(?:优于|跑赢|超过|落后于|弱于|跑输).{0,8}(?:市场|大盘|基准|指数)")
FACT_TOKEN = re.compile(r"\{\{([a-zA-Z_][a-zA-Z_0-9.]*)\}\}")
TOKEN_WITH_UNIT = re.compile(FACT_TOKEN.pattern + r"\s*(元/股|元/日|元每股|元每天|个百分点|%|％|倍|元|分|名|年|点)")
EMPTY_QUANTITY = re.compile(r"第\s*(?:名|位)|(?:为|达|达到)\s*(?:分|点|倍|元|%|年|名)|年\s*年(?:末|初)")
NEGATIVE_GROWTH = re.compile(r"负增长|营收(?:同比)?(?:下滑|下降|萎缩)|(?:营业)?收入(?:同比)?(?:下滑|下降|收缩)")
POSITIVE_GROWTH = re.compile(r"正增长|增长强劲|稳健增长|持续增长|增长良好|增长显著")
PRICE_COMPARISON = re.compile(r"(?:股价|收盘价|价格).{0,10}(?:较低|偏低|低位|较高|偏高|高位|便宜|昂贵)")
PRICE_MA_COMPARISON = re.compile(r"(?:股价|收盘价|价格)(?:(?!均线|均价|移动平均线)[^。；;，,、]){0,12}(?:位于|站上|站稳|高于|低于|突破|跌破|处于)[^。；;，,、]{0,12}(?:均线|均价|移动平均线)")
UNCERTAIN = re.compile(r"(?:无法|不能).{0,8}(?:判断|推断|确认|确定|认定|评估)|不足以判断|未提供|缺少|缺乏")
MA_ABOVE = re.compile(r"短期(?:均线|平均(?:价格|成本)).{0,6}(?:高于|大于|超过).{0,6}长期")
MA_BELOW = re.compile(r"短期(?:均线|平均(?:价格|成本)).{0,6}(?:低于|小于).{0,6}长期")


def quantitative_text(text: str):
    remainder = FACT_TOKEN.sub("字段", INDICATOR_LABEL.sub("指标", text))
    return NUMBER.search(remainder) or CHINESE_QUANTITY.search(remainder)


def term_matches(text: str, term: str):
    pattern = re.escape(term)
    if term.isascii():
        pattern = r"(?<![A-Za-z0-9_])" + pattern + r"(?![A-Za-z0-9_])"
    return re.finditer(pattern, text, re.I)


def check_comparison(text: str, *, verification: bool = False):
    verify_scope = False
    for clause in re.split(r"[。；;，,]", text):
        own_verb = bool(re.search(r"核实|核对|检查|确认|验证|评估|判断", clause))
        connected = bool(re.match(r"\s*(?:以及|并|同时|及)", clause))
        comparison = COMPARISON.search(clause) or BENCHMARK_COMPARISON.search(clause)
        if comparison and not UNCERTAIN.search(clause):
            own_question = own_verb and ("是否" in clause or "合理性" in clause)
            shared_question = verify_scope and connected and "是否" in clause
            if not verification or not (own_question or shared_question):
                raise ValueError(f"unsupported valuation or industry comparison must say 无法判断: {text[:120]}")
        # A disclaimer about missing news cannot excuse an assertion elsewhere;
        # shared verification verbs apply only to explicitly connected questions.
        verify_scope = own_verb or (verify_scope and connected)


def check_unprovided_metrics(text: str):
    for clause in re.split(r"[。；;，,]", text):
        for word in ("市值", "净利润", "净利率", "毛利率", "负债率", "资金流向", "护城河"):
            if word not in clause or any(term in clause for term in (
                "无法判断", "未提供", "缺少", "缺乏", "核实", "核对", "确认", "无法核验")):
                continue
            prefix = clause[:clause.index(word)]
            if re.search(r"(?:不代表|不等于|(?:不能|无法).{0,8}(?:推断|判断|证明|说明)).{0,12}$", prefix):
                continue
            raise ValueError(f"metric is not in this snapshot evidence: {clause[:120]}")


def compatible_unit(expected: str, suffix: str) -> bool:
    if suffix == expected or (suffix == "％" and expected == "%"):
        return True
    return (suffix == "元" and expected.startswith("元/")) or (expected, suffix) in {
        ("元/股", "元每股"), ("元/日", "元每天")}


def check_financial_direction(text: str, facts: dict[str, dict], evidence_ids: list[str]):
    key = "financials.revenue_growth"
    if (NEGATIVE_GROWTH.search(text) or POSITIVE_GROWTH.search(text)) and key not in evidence_ids:
        raise ValueError("growth direction must cite financials.revenue_growth, not industry or another field")
    if key in evidence_ids:
        value = float(facts[key]["value"])
        pattern = NEGATIVE_GROWTH if value >= 0 else POSITIVE_GROWTH
        for match in pattern.finditer(text):
            if not hypothetical(text, match):
                raise ValueError(f"growth direction contradicts {key} ({value:g}%): {text[:150]}")
        if value == 0 and POSITIVE_GROWTH.search(text):
            raise ValueError(f"zero revenue growth is not positive growth: {text[:150]}")
    if "financials.roe" in evidence_ids and re.search(r"(?:ROE|净资产收益率).{0,12}(?:高位|低位|偏高|偏低)", text, re.I) and not any(
        word in text for word in ("无法判断", "不能判断", "缺少", "缺乏")):
        raise ValueError("ROE level comparison has no benchmark")
    ma_key = "market.ma_ratio"
    if ma_key in evidence_ids:
        ratio = float(facts[ma_key]["value"])
        contradiction = MA_BELOW.search(text) if ratio > 0 else MA_ABOVE.search(text) if ratio < 0 else (MA_BELOW.search(text) or MA_ABOVE.search(text))
        if contradiction and not hypothetical(text, contradiction) and not UNCERTAIN.search(text):
            raise ValueError("moving-average direction contradicts market.ma_ratio")
        if re.search(r"(?:比率|比值|比例).{0,6}(?:大于|小于|高于|低于).{0,6}(?:长期|短期)(?:均线|平均成本)", text):
            raise ValueError("compare two averages, not a dimensionless ratio against an average")
    if PRICE_COMPARISON.search(text) and not UNCERTAIN.search(text):
        raise ValueError(f"price-level comparison has no benchmark: {text[:150]}")
    if PRICE_MA_COMPARISON.search(text) and not UNCERTAIN.search(text):
        raise ValueError("MA20/MA60 minus one compares two averages, not the closing price versus an average")


def explanation_evidence(facts: dict[str, dict]) -> dict:
    """Keep exact quantities in the immutable card; give the model renderable IDs and signs."""
    definitions = {"market.momentum_60": "最近六十个交易日的复权价格收益率",
                   "market.ma_ratio": "短期均线除以长期均线后减一；正值表示短期均线高于长期均线，不能推断股价与均线的位置"}
    output = {}
    for key, fact in facts.items():
        value = fact["value"]
        item = {"unit": fact["unit"], "value": "{{" + key + "}}"}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            item["observed_sign"] = "正值" if value > 0 else "负值" if value < 0 else "零值"
        elif fact["unit"] == "文本":
            item["value"] = str(value)
        if key in definitions:
            item["definition"] = definitions[key]
        output[key] = item
    return output


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
    # Report independent failures together so bounded repair can fix the full card,
    # instead of spending one request discovering each successive bad sentence.
    errors = []
    parts = [("scope", content.model_copy(update={"reasons": [], "risks": []}))]
    for section in ("reasons", "risks"):
        for index, claim in enumerate(getattr(content, section)):
            parts.append((f"{section}[{index}]", content.model_copy(update={
                "summary": "", "information_gaps": [], "human_checks": [],
                "reasons": [claim], "risks": []})))
    for label, part in parts:
        try:
            _check_field_claims(part, facts)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    if errors:
        raise ValueError(" | ".join(errors[:6]))


def _check_field_claims(content: ResearchContent, facts: dict[str, dict]):
    texts = [(content.summary, False), *[(text, False) for text in content.information_gaps],
             *[(text, True) for text in content.human_checks]]
    for text, verification in texts:
        if FACT_TOKEN.search(text):
            raise ValueError("place field values only in cited reasons or risks; keep summary and checks qualitative")
        if EMPTY_QUANTITY.search(text):
            raise ValueError(f"unfilled numeric sentence: {text[:120]}")
        match = quantitative_text(text)
        if match:
            raise ValueError(f"model narrative contains a number ({match.group()}); omit quantities and dates from: {text[:120]}")
        check_comparison(text, verification=verification)
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
                        for match in term_matches(prefix, term)]
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
            if any(next(term_matches(narrative, term), None) is not None for term in terms):
                if not any(facts[key]["field"] == field for key in claim.evidence_ids):
                    raise ValueError(f"field/evidence mismatch: {field}; cited {claim.evidence_ids}; sentence: {text[:150]}")
        check_financial_direction(text, facts, claim.evidence_ids)
        check_comparison(text)
        check_unprovided_metrics(text)


def format_fact_value(value) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def render_value(fact: dict) -> str:
    text, unit = format_fact_value(fact["value"]), fact["unit"]
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
    if "本卡采用模板辅助修正" in template.summary:
        # Field presence is known by the program; a model must not invent missing
        # dates while the original dates remain visible in the evidence table.
        value["information_gaps"] = assisted_information_gaps(facts)
    value["summary"] = replace(value["summary"])
    for section in ("reasons", "risks"):
        for item in value[section]:
            item["text"] = replace(item["text"])
    for section in ("information_gaps", "human_checks"):
        value[section] = [replace(item) for item in value[section]]
    return ResearchContent.model_validate(value)
