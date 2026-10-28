import copy
import json
from datetime import date, timedelta

import pytest

from finsight.models import ResearchCard
from finsight.research import (
    ASSISTED_SUMMARY,
    FIELD_PROMPT_VERSION,
    SCOPE_SUMMARY,
    assisted_repair_schema,
    evidence_for,
    finalize_content,
    research_candidate,
)
from finsight.research_requirements import required_coverage
from finsight.service import FinSight


@pytest.fixture
def research_case(tmp_path):
    service = FinSight(tmp_path)
    snapshot = service.sync("demo", date(2025,12,31), date(2024,1,2))["snapshot_id"]
    result = service.screen(snapshot)
    return service, result, copy.deepcopy(result.candidates[0])


def payload():
    return {"summary": SCOPE_SUMMARY,
        "reasons": [{"text": "固定规则确定候选名单。", "evidence_ids": ["selection.rank"]}],
        "risks": [
            {"text": "营收增长的持续性需核实。", "evidence_ids": ["financials.revenue_growth"]},
            {"text": "缺少比较依据，无法判断估值是否合理。", "evidence_ids": ["valuation.pe_ttm"]},
            {"text": "历史动量不能保证趋势延续。", "evidence_ids": ["market.momentum_60"]}],
        "information_gaps": ["未接入新闻和公告正文，无法判断最新事件。"],
        "human_checks": ["核实年报与交易状态。"]}


class Responses:
    provider, model, base_url = "ollama", "requirements-fixture", "http://fixture"
    def __init__(self, values):
        self.values, self.messages = iter(values), []
    def complete(self, messages, tools, final=False):
        self.messages.append(copy.deepcopy(messages))
        return {"message": {"content": json.dumps(next(self.values), ensure_ascii=False)},
                "input_tokens": 12, "output_tokens": 8}


@pytest.mark.parametrize("value", [None, "absent"])
def test_missing_roe_must_be_explicit_in_information_gaps(research_case, value):
    _, result, candidate = research_case
    if value == "absent":
        candidate.pop("roe")
    else:
        candidate["roe"] = value
    facts = evidence_for(result, candidate)
    with pytest.raises(ValueError, match="missing financials.roe"):
        finalize_content(payload(), facts, result.as_of)
    good = payload()
    good["information_gaps"].append("未提供净资产收益率，无法判断该指标。")
    assert finalize_content(good, facts, result.as_of)
    good["information_gaps"] = ["ROE 可以用于分析，缺少行业比较依据，无法判断估值。"]
    with pytest.raises(ValueError, match="missing financials.roe"):
        finalize_content(good, facts, result.as_of)


@pytest.mark.parametrize("text", ["未来若营收出现负增长，需防范风险。", "未来若营收增长未能改善，可能影响财务质量。"])
def test_negative_growth_in_reasons_or_future_conditions_cannot_replace_observed_risk(research_case, text):
    _, result, candidate = research_case
    candidate["revenue_growth"] = -5
    facts = evidence_for(result, candidate)
    bad = payload()
    bad["reasons"].append({"text": "当前营收负增长。", "evidence_ids": ["financials.revenue_growth"]})
    bad["risks"] = [{"text": text, "evidence_ids": ["financials.revenue_growth"]}] + payload()["risks"][1:]
    with pytest.raises(ValueError, match="observed negative"):
        finalize_content(bad, facts)
    bad["risks"][0]["text"] = "当前营收出现负增长，应核实原因。"
    assert finalize_content(bad, facts)


def test_stale_annual_report_needs_a_cited_risk_not_only_a_gap(research_case):
    _, result, candidate = research_case
    candidate["report_period"] = "2022-12-31"
    facts = evidence_for(result, candidate)
    bad = payload()
    bad["information_gaps"].append("年报信息已过时，无法判断最新财务表现。")
    with pytest.raises(ValueError, match="stale annual"):
        finalize_content(bad, facts)
    bad["risks"] = [{"text": "年报信息可能已过时，需核实最新财务表现。", "evidence_ids": ["financials.report_period"]}] + payload()["risks"][1:]
    assert finalize_content(bad, facts)
    schema, choices = assisted_repair_schema(facts)
    assert any(choice["evidence_ids"] == ["financials.report_period"] for choice in choices)
    assert schema["properties"]["risks"]["minItems"] >= 1


@pytest.mark.parametrize("age,stale", [(550,False),(551,True)])
def test_freshness_boundary_and_zero_growth_are_deterministic(research_case, age, stale):
    _, result, candidate = research_case
    candidate.update(report_period=str(result.as_of-timedelta(days=age)), revenue_growth=0)
    needs = required_coverage(evidence_for(result,candidate))
    assert needs["annual_report_stale"] == stale and not needs["negative_revenue_growth"]


def test_valid_second_response_repairs_required_gap_without_changing_ranking(research_case):
    service,result,candidate = research_case
    original = copy.deepcopy(result.candidates)
    candidate.pop("roe")
    good = payload()
    good["information_gaps"].append("未提供ROE，无法判断该指标。")
    adapter = Responses([payload(),good])
    card = research_candidate(result,candidate,service.store,adapter)
    assert card.status == "completed" and card.llm_calls == 2 and card.tool_calls == 0
    assert card.input_tokens == 24 and result.candidates == original
    context = json.loads(adapter.messages[0][1]["content"])["candidate"]
    assert "financials.roe" in context["required_disclosures"]["missing_financial_fields"]
    assert "missing financials.roe" in adapter.messages[1][-1]["content"]


def assisted(facts, required_risks=()):
    _, choices = assisted_repair_schema(facts)
    fields = list(required_risks) if required_risks else ["financials.roe"]
    risks = [next(c for c in choices if c["evidence_ids"] == [field]) for field in fields + ["valuation.pe_ttm", "market.momentum_60"]]
    return {"summary": ASSISTED_SUMMARY, "reasons": [choices[0]],
        "risks": risks or [next(c for c in choices if c["evidence_ids"] == ["valuation.pe_ttm"])],
        "information_gaps": ["需核实数据。"], "human_checks": ["核实财报。"]}


def test_final_assisted_request_keeps_all_mandatory_risks(research_case):
    service,result,candidate = research_case
    candidate.update(revenue_growth=-5,report_period="2022-12-31")
    facts = evidence_for(result,candidate)
    required = ["financials.revenue_growth","financials.report_period"]
    schema,_ = assisted_repair_schema(facts)
    assert schema["properties"]["risks"]["minItems"] == 4
    adapter = Responses([payload(),payload(),assisted(facts,required)])
    card = research_candidate(result,candidate,service.store,adapter)
    assert card.status == "completed" and card.llm_calls == 3
    instruction = json.loads(adapter.messages[-1][-1]["content"])
    assert {c["evidence_ids"][0] for c in instruction["mandatory_risks"]} == set(required) | {"valuation.pe_ttm", "market.momentum_60"}
    assert "模板辅助修正" in card.content.summary
    assert any("负增长" in c.text for c in card.content.risks) and any("陈旧" in c.text for c in card.content.risks)


def test_exhausted_budget_never_adds_missing_risks_as_a_fake_success(research_case):
    service,result,candidate = research_case
    candidate["revenue_growth"] = -5
    facts = evidence_for(result,candidate)
    card = research_candidate(result,candidate,service.store,Responses([payload(),payload(),assisted(facts)]))
    assert card.status == "failed" and card.content is None and card.content_template is not None
    assert card.llm_calls == 3 and "observed negative" in card.error


def test_old_incomplete_cache_is_not_presented_as_complete(research_case,monkeypatch):
    service,result,candidate = research_case
    candidate.pop("roe")
    facts = evidence_for(result,candidate)
    old = ResearchCard(run_id=result.run_id,symbol=candidate["symbol"],model=Responses.model,
        prompt_version="old",status="completed",content_template=payload(),facts=facts,evidence=facts)
    monkeypatch.setattr(service.store,"cached",lambda key:old.model_dump(mode="json"))
    good = payload()
    good["information_gaps"].append("ROE未提供，无法判断该指标。")
    card = research_candidate(result,candidate,service.store,Responses([good]))
    assert card.status == "completed" and not card.cache_hit and card.llm_calls == 1
    assert card.prompt_version == FIELD_PROMPT_VERSION


def test_offline_demo_also_discloses_missing_fields_and_current_risks(research_case):
    service,result,candidate = research_case
    candidate.pop("roe")
    candidate.update(revenue_growth=-5,report_period="2022-12-31")
    card = research_candidate(result,candidate,service.store,demo=True)
    assert card.status == "demo" and card.llm_calls == 0
    assert any("ROE" in text and "无法判断" in text for text in card.content.information_gaps)
    assert any("负增长" in claim.text for claim in card.content.risks)
    assert any("陈旧" in claim.text for claim in card.content.risks)
    cached = research_candidate(result,candidate,service.store,demo=True)
    assert cached.cache_hit and cached.llm_calls == 0 and cached.content == card.content


def test_missing_growth_and_invalid_period_are_unknown_instead_of_crashing(research_case):
    service,result,candidate = research_case
    candidate.update(revenue_growth=None,report_period=None)
    card = research_candidate(result,candidate,service.store,demo=True)
    assert card.status == "demo" and card.llm_calls == 0
    assert any("营收增长" in text and "无法判断" in text for text in card.content.information_gaps)
    assert any("报告期" in text and "无法判断" in text for text in card.content.information_gaps)


def test_report_age_uses_signal_cutoff_rather_than_an_old_quote_date(research_case):
    _,result,candidate = research_case
    candidate.update(report_period="2022-12-31",trade_date="2023-01-01")
    facts = evidence_for(result,candidate)
    assert required_coverage(facts)["annual_report_stale"]
    _,choices = assisted_repair_schema(facts)
    assert any(choice["evidence_ids"] == ["financials.report_period"] for choice in choices)
