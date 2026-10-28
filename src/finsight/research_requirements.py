"""Required disclosures from snapshot fields, independent of model/provider."""
from __future__ import annotations

import math
import re
from datetime import date

MISSING = re.compile(r"未提供|缺少|缺失|缺乏|不可用|无效")
DECLINE = re.compile(r"负增长|(?:营收|营业收入|收入).{0,8}(?:下滑|下降|负值)")
STALE = re.compile(r"陈旧|过期|过时|时效.{0,8}(?:不足|风险|无法判断)")
LABELS = {"financials.roe": ("ROE", "净资产收益率"),
          "financials.revenue_growth": ("营收增长", "营收", "营业收入增长"),
          "financials.report_period": ("年报报告期", "报告期", "财报期")}
RISK_DOMAINS = {"financials": ("financials.roe", "financials.revenue_growth", "financials.report_period"),
               "valuation": ("valuation.pe_ttm", "valuation.pb"),
               "market": ("market.momentum_60", "market.ma_ratio")}
DOMAIN_LABELS = {"financials": ("财务", "ROE", "净资产收益率", "营收", "收入", "年报", "报告期"),
                 "valuation": ("估值", "PE", "PB", "市盈率", "市净率"),
                 "market": ("趋势", "动量", "均线", "历史收益")}
RISK_CUE = re.compile(r"风险|需|无法|不能|不足|不保证|不代表|不等于|不确定|局限|波动|回落|反转|陈旧|过期|过时|下滑|下降|负增长|持续性")


def _cutoff(facts, as_of):
    if as_of is not None:
        return as_of
    value = facts.get("selection.rank", {}).get("known_at") or facts.get("market.trade_date", {}).get("value")
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def risk_domain_fields(facts, as_of=None):
    as_of = _cutoff(facts, as_of)
    usable = {}
    for domain, fields in RISK_DOMAINS.items():
        usable[domain] = []
        for field in fields:
            value = facts.get(field, {}).get("value")
            valid = number(value)
            if field == "financials.report_period":
                try:
                    period = date.fromisoformat(str(value))
                    valid = as_of is not None and period <= as_of
                except ValueError:
                    valid = False
            if valid:
                usable[domain].append(field)
    return usable


def risk_domain_coverage(content, facts, as_of=None):
    usable = risk_domain_fields(facts, as_of)
    covered = {domain: any(set(claim.evidence_ids) & set(fields) and RISK_CUE.search(claim.text) and
        any(label.lower() in claim.text.lower() for label in DOMAIN_LABELS[domain]) for claim in content.risks)
        for domain, fields in usable.items() if fields}
    missing = [domain for domain, present in covered.items() if not present]
    unavailable = [domain for domain, fields in usable.items() if not fields]
    undisclosed = [domain for domain in unavailable if not any(
        "无法判断" in text and MISSING.search(text) and any(label.lower() in text.lower() for label in DOMAIN_LABELS[domain])
        for text in content.information_gaps)]
    return {"covered": covered, "missing_domains": missing, "unavailable_domains": unavailable,
            "undisclosed_unavailable_domains": undisclosed}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def hypothetical(text: str, match: re.Match) -> bool:
    prefix = re.split(r"[。；;，,]", text[:match.start()])[-1]
    if re.search(r"目前|当前|已经|已出现|现有|实际", prefix):
        return False
    return bool(re.search(r"若|如果|未来|可能|能否|假如|需警惕|需防范", prefix) or
                re.match(r"(?:的)?(?:风险|可能性)", text[match.end():]))


def required_coverage(facts, as_of: date | None = None):
    as_of = _cutoff(facts, as_of)
    missing = [field for field in ("financials.roe", "financials.revenue_growth")
               if not number(facts.get(field, {}).get("value"))]
    try:
        report = date.fromisoformat(str(facts.get("financials.report_period", {}).get("value")))
        age = (as_of - report).days if as_of else None
    except ValueError:
        age = None
    if age is None or age < 0:
        missing.append("financials.report_period")
    growth = facts.get("financials.revenue_growth", {}).get("value")
    risks = []
    if number(growth) and growth < 0:
        risks.append("financials.revenue_growth")
    if age is not None and age > 550:
        risks.append("financials.report_period")
    domains = risk_domain_fields(facts, as_of)
    return {"missing_financial_fields": missing, "required_risk_fields": risks,
            "required_risk_domains": [domain for domain, fields in domains.items() if fields],
            "unavailable_risk_domains": [domain for domain, fields in domains.items() if not fields],
            "negative_revenue_growth": "financials.revenue_growth" in risks,
            "annual_report_stale": "financials.report_period" in risks}


def discloses_missing(text, field):
    labels = "(?:" + "|".join(re.escape(label) for label in LABELS[field]) + ")"
    absent = "(?:" + MISSING.pattern + ")"
    # A missing industry benchmark elsewhere cannot stand in for a missing ROE.
    pattern = absent + r"[^。；;，,]{0,24}" + labels + "|" + labels + r"[^。；;，,]{0,12}" + absent
    return "无法判断" in text and bool(re.search(pattern, text, re.I))


def observed_decline(claim):
    return "financials.revenue_growth" in claim.evidence_ids and any(
        not hypothetical(claim.text, match) for match in DECLINE.finditer(claim.text))


def warns_stale(claim):
    if "financials.report_period" not in claim.evidence_ids or not re.search(r"年报|财报|报告期", claim.text):
        return False
    if re.search(r"不陈旧|未过期|未过时|不再过时|没有过时", claim.text):
        return False
    for match in STALE.finditer(claim.text):
        prefix = re.split(r"[。；;，,]", claim.text[:match.start()])[-1]
        if not re.search(r"未来|如果|假如|倘若|若", prefix) or re.search(r"当前|目前|已经", prefix):
            return True
    return False


def check_required_coverage(content, facts, as_of=None):
    needs = required_coverage(facts, as_of)
    errors = []
    for field in needs["missing_financial_fields"]:
        if not any(discloses_missing(text, field) for text in content.information_gaps):
            errors.append(f"information_gaps: missing {field}; 明确写未提供{LABELS[field][0]}，无法判断该指标")
    if needs["negative_revenue_growth"] and not any(observed_decline(claim) for claim in content.risks):
        errors.append("risks: observed negative revenue growth missing; 引用financials.revenue_growth并说明当前营收负增长，不能仅写入选理由或未来条件")
    if needs["annual_report_stale"] and not any(warns_stale(claim) for claim in content.risks):
        errors.append("risks: stale annual report warning missing; 引用financials.report_period并明确年报数据陈旧或过时")
    coverage = risk_domain_coverage(content, facts, as_of)
    for domain in coverage["missing_domains"]:
        errors.append(f"risks: missing domain {domain}; 风险必须引用该方面可用字段并说明局限或风险，不能仅列入选理由或正面指标")
    for domain in coverage["undisclosed_unavailable_domains"]:
        errors.append(f"information_gaps: unavailable domain {domain}; 明确说明该方面指标未提供，无法判断")
    if errors:
        raise ValueError(" | ".join(errors))


def assisted_information_gaps(facts):
    needs = required_coverage(facts)
    gaps = ["未接入新闻或公告正文，无法判断最新事件。", "缺少行业比较基准，无法判断估值是否合理。"]
    gaps.extend(f"未提供有效{LABELS[field][0]}，无法判断该指标。" for field in needs["missing_financial_fields"])
    if needs["annual_report_stale"]:
        gaps.append("年报数据陈旧，无法判断最新财务表现。")
    for domain in needs["unavailable_risk_domains"]:
        gaps.append(f"未提供有效{'财务' if domain == 'financials' else '估值' if domain == 'valuation' else '趋势'}指标，无法判断该方面风险。")
    return gaps
