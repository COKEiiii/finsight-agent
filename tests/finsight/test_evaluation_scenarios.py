from datetime import date

import pytest

from finsight.evaluation import _scenario_pass, build_cases, scenario_check
from finsight.evaluation_audit import scenario_check as offline_check
from finsight.models import EvidenceClaim, ResearchCard, ResearchContent
from finsight.research import demo_content, evidence_for, finalize_content
from finsight.service import FinSight


def response(text, field="financials.report_period", section="risks", facts=None):
    value = ResearchContent(summary="仅依据快照。",
        reasons=[{"text": "该股进入候选名单。", "evidence_ids": ["selection.rank"]}],
        risks=[{"text": "历史趋势不能保证未来表现。", "evidence_ids": ["market.momentum_60"]}],
        information_gaps=["未接新闻，无法判断事件影响。"], human_checks=["核实后续披露。"])
    if section == "information_gaps":
        value.information_gaps.append(text)
    else:
        getattr(value, section).append(EvidenceClaim(text=text, evidence_ids=[field]))
    return ResearchCard(run_id="signal", symbol="600000.SH", model="fixture", prompt_version="fixture",
        status="completed", content=value, facts=facts or {})


@pytest.mark.parametrize("scenario,text,field,section,passed", [
    ("missing_roe", "未提供净资产收益率，无法判断该指标。", "", "information_gaps", True),
    ("missing_roe", "ROE 缺失，无法判断。", "", "information_gaps", True),
    ("missing_roe", "未提供 roe，无法判断。", "", "information_gaps", True),
    ("missing_roe", "未提供新闻，无法判断事件。净资产收益率已列出。", "", "information_gaps", False),
    ("missing_roe", "未提供净资产收益率，无法判断。", "selection.rank", "risks", False),
    ("stale_report", "年报数据已过时，无法判断最新财务表现。", "financials.report_period", "risks", True),
    ("stale_report", "财报陈旧，需核实后续数据。", "financials.report_period", "risks", True),
    ("stale_report", "未来若年报过期，需要核实。", "financials.report_period", "risks", False),
    ("stale_report", "年报尚未过期。", "financials.report_period", "risks", False),
    ("stale_report", "年报已过时。", "market.trade_date", "risks", False),
    ("stale_report", "年报数据陈旧，需核实。", "", "information_gaps", False),
    ("negative_growth", "当前营收出现负增长，需核实原因。", "financials.revenue_growth", "risks", True),
    ("negative_growth", "未来若营收负增长，需核实。", "financials.revenue_growth", "risks", False),
    ("negative_growth", "当前营收负增长。", "financials.roe", "risks", False),
    ("negative_growth", "当前营收负增长。", "financials.revenue_growth", "reasons", False),
    ("unknown_scenario", "无需判断。", "selection.rank", "risks", False),
])
def test_live_and_offline_scoring_use_specific_cited_disclosures(scenario, text, field, section, passed):
    card = response(text, field, section)
    assert _scenario_pass(scenario, card) is passed
    assert offline_check(scenario, card.content, card.facts) is passed


@pytest.mark.parametrize("value,unit,passed", [(10000., "元/日", True), (10000., "万元/日", False),
    (None, "元/日", False), (True, "元/日", False), (float("nan"), "元/日", False)])
def test_conversion_scenario_requires_a_finite_value_with_the_original_unit(value, unit, passed):
    card = response("需核实流动性。", "market.amount_20", facts={"market.amount_20": {"value": value, "unit": unit}})
    assert _scenario_pass("unit_conversion", card) is passed
    assert offline_check("unit_conversion", card.content, card.facts) is passed


@pytest.mark.parametrize("status", ["failed", "demo"])
def test_non_completed_card_cannot_pass_even_with_correct_disclosure(status):
    card = response("未提供净资产收益率，无法判断。", section="information_gaps")
    card.status = status
    assert not _scenario_pass("missing_roe", card)
    card.status, card.content = "completed", None
    assert not _scenario_pass("actual_candidate", card)


@pytest.mark.parametrize("scenario", ["missing_roe", "stale_report"])
def test_generation_valid_chinese_response_is_not_falsely_failed_by_the_evaluator(tmp_path, scenario):
    service = FinSight(tmp_path)
    snapshot = service.sync("demo", date(2025, 12, 31), date(2024, 1, 2))["snapshot_id"]
    result = service.screen(snapshot)
    case = next(case for case in build_cases(result, allow_demo=True) if case["scenario"] == scenario)
    facts = evidence_for(result, case["candidate"])
    template = demo_content(case["candidate"], facts)
    if scenario == "missing_roe":
        template.information_gaps = [text.replace("ROE", "净资产收益率") for text in template.information_gaps]
    else:
        for claim in template.risks:
            claim.text = claim.text.replace("陈旧", "过时")
    content = finalize_content(template.model_dump(), facts, result.as_of)
    card = ResearchCard(run_id=result.run_id, symbol=case["symbol"], model="fixture", prompt_version="fixture",
        status="completed", content=content, facts=facts)
    assert _scenario_pass(scenario, card) and scenario_check(scenario, content, facts)
