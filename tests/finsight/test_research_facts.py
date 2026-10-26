from datetime import date

import pytest

from finsight.evaluation import build_cases, evaluate_run
from finsight.research import demo_content, evidence_for, render_card, research_candidate, validate_content
from finsight.research_facts import render_content
from finsight.service import FinSight


class Answers:
    provider = "ollama"
    model = "fixture-local"
    base_url = "http://fixture"

    def __init__(self, values):
        self.values = iter(values)

    def complete(self, messages, tools, final=False):
        import json
        return {"message": {"role": "assistant", "content": json.dumps(next(self.values), ensure_ascii=False), "tool_calls": []}}


def ready(tmp_path):
    service = FinSight(tmp_path)
    snapshot = service.sync("demo", date(2025, 12, 31), date(2024, 1, 2))["snapshot_id"]
    result = service.screen(snapshot)
    candidate = result.candidates[0]
    return service, result, candidate, evidence_for(result, candidate)


def test_typed_facts_bound_to_snapshot_with_units_sources_and_program_render(tmp_path):
    service, result, candidate, facts = ready(tmp_path)
    assert result.schema_version == 2
    assert facts["financials.roe"]["unit"] == "%"
    assert facts["financials.roe"]["snapshot_id"] == result.snapshot_id
    assert facts["financials.roe"]["known_at"] == candidate["available_on"]
    assert facts["valuation.pe_ttm"]["source"] == candidate["valuation_source"]
    card = research_candidate(result, candidate, service.store, demo=True)
    assert card.facts == facts
    assert "| financials.roe |" in render_card(card)


def test_metric_and_unit_mismatch_rejected_even_if_number_exists_elsewhere(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "净资产收益率上升。", "evidence_ids": ["financials.revenue_growth"]}
    with pytest.raises(ValueError, match="field/evidence mismatch: roe"):
        validate_content(payload, facts)
    payload["reasons"][0] = {"text": "净资产收益率为百分之二十。", "evidence_ids": ["financials.roe"]}
    with pytest.raises(ValueError, match="narrative contains a number"):
        validate_content(payload, facts)
    payload["reasons"][0]["text"] = "净资产收益率为20元。"
    with pytest.raises(ValueError, match="narrative contains a number"):
        validate_content(payload, facts)


def test_unsupported_valuation_and_mismatched_research_binding(tmp_path):
    service, result, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "估值合理。", "evidence_ids": ["valuation.pe_ttm"]}
    with pytest.raises(ValueError, match="无法判断"):
        validate_content(payload, facts)
    good = demo_content(candidate, facts).model_dump()
    good["information_gaps"].append("缺少行业平均估值数据。")
    assert validate_content(good, facts)
    adapter = Answers([good])
    first = research_candidate(result, candidate, service.store, adapter)
    assert first.status == "completed"
    another = result.model_copy(update={"run_id": "another-screen-run"})
    second = research_candidate(another, candidate, service.store, adapter)
    assert second.cache_hit and second.run_id == another.run_id and second.facts == first.facts


def test_invalid_field_response_repairs_within_three_requests(tmp_path):
    service, result, candidate, facts = ready(tmp_path)
    good = demo_content(candidate, facts).model_dump()
    bad = demo_content(candidate, facts).model_dump()
    bad["reasons"][0] = {"text": "营收增长。", "evidence_ids": ["financials.roe"]}
    card = research_candidate(result, candidate, service.store, Answers([bad, good]))
    assert card.status == "completed" and card.llm_calls == 2


def test_thirty_case_suite_requires_real_candidates_and_tracks_synthetic_changes(tmp_path):
    service, result, _, _ = ready(tmp_path)
    with pytest.raises(ValueError, match="real candidates"):
        build_cases(result)
    cases = build_cases(result, allow_demo=True)
    assert len(cases) == 30
    assert sum(case["scenario"] == "actual_candidate" for case in cases) == 10
    assert sum(case["synthetic_modification"] for case in cases) == 20
    report = evaluate_run(service.store, result.run_id, allow_demo=True)
    assert report["case_count"] == 30 and report["models"] == {}


def test_program_fills_only_the_matching_cited_field_and_unit(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "净资产收益率为{{financials.roe}}。", "evidence_ids": ["financials.roe"]}
    content = validate_content(payload, facts)
    rendered = render_content(content, facts)
    assert "{{" not in rendered.reasons[0].text and "%" in rendered.reasons[0].text
    payload["reasons"][0]["text"] = "净资产收益率为{{financials.roe}}%。"
    assert render_content(validate_content(payload, facts), facts).reasons[0].text.count("%") == 1
    payload["reasons"][0]["text"] = "净资产收益率为{{valuation.pb}}。"
    with pytest.raises(ValueError, match="matching field"):
        validate_content(payload, facts)
    payload["reasons"][0] = {"text": "净资产收益率为{{valuation.pb}}。", "evidence_ids": ["valuation.pb"]}
    with pytest.raises(ValueError, match="field/evidence mismatch"):
        validate_content(payload, facts)
    payload["reasons"][0] = {"text": "净资产收益率为{{financials.roe}}元。", "evidence_ids": ["financials.roe"]}
    with pytest.raises(ValueError, match="repeated unit"):
        validate_content(payload, facts)


def test_empty_quantities_and_unprovided_market_cap_are_not_valid_reports(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "策略排名第 名。", "evidence_ids": ["selection.rank"]}
    with pytest.raises(ValueError, match="unfilled numeric"):
        validate_content(payload, facts)
    payload = demo_content(candidate, facts).model_dump()
    payload["summary"] = "分析公司市值表现。"
    with pytest.raises(ValueError, match="not in this snapshot"):
        validate_content(payload, facts)
