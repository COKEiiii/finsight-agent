from datetime import date

import pytest

from finsight.evaluation import build_cases, evaluate_run
from finsight.research import (
    ASSISTED_SUMMARY,
    assisted_repair_schema,
    demo_content,
    evidence_for,
    render_card,
    research_candidate,
    validate_content,
)
from finsight.research_facts import explanation_evidence, render_content
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


def test_field_repair_keeps_snapshot_without_repeating_invalid_assistant_json(tmp_path):
    import json

    service, result, candidate, facts = ready(tmp_path)
    good = demo_content(candidate, facts).model_dump()
    bad = demo_content(candidate, facts).model_dump()
    bad["reasons"][0] = {"text": "净资产收益率为999999元。", "evidence_ids": ["financials.roe"]}
    class Seen(Answers):
        def complete(self, messages, tools, final=False):
            self.messages = json.loads(json.dumps(messages))
            return super().complete(messages, tools, final)
    adapter = Seen([bad, good])
    card = research_candidate(result, candidate, service.store, adapter)
    assert card.status == "completed" and card.llm_calls == 2
    assert [item["role"] for item in adapter.messages] == ["system", "user", "user"]
    assert result.snapshot_id in adapter.messages[1]["content"]
    assert "reasons[0]" in adapter.messages[-1]["content"]


def test_last_request_can_select_grounded_templates_with_visible_assistance(tmp_path):
    service, result, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    bad = demo_content(candidate, facts).model_dump()
    bad["reasons"][0] = {"text": "估值合理。", "evidence_ids": ["valuation.pe_ttm"]}
    _, choices = assisted_repair_schema(facts)
    payload.update(summary=ASSISTED_SUMMARY,
                   reasons=[next(item for item in choices if item["evidence_ids"] == ["financials.roe"])],
                   risks=[next(item for item in choices if item["evidence_ids"] == [field])
                          for field in ("financials.revenue_growth", "valuation.pe_ttm", "market.momentum_60")])
    card = research_candidate(result, candidate, service.store, Answers([bad, bad, payload]))
    assert card.status == "completed" and card.llm_calls == 3
    assert "模板辅助修正" in render_card(card) and "{{" not in card.content.reasons[0].text
    assert "{{" in card.content_template.reasons[0].text


def test_assisted_information_gaps_come_from_actual_field_presence(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["summary"] = ASSISTED_SUMMARY
    payload["information_gaps"] = ["未提供财务报告具体日期，无法判断年报时效性。"]
    template = validate_content(payload, facts)
    rendered = render_content(template, facts)
    assert not any("未提供财务报告具体日期" in item for item in rendered.information_gaps)
    facts.pop("financials.roe")
    payload["reasons"] = [{"text": "营收增长参与财务质量评分。", "evidence_ids": ["financials.revenue_growth"]}]
    rendered = render_content(validate_content(payload, facts), facts)
    assert any("ROE" in item and "无法判断" in item for item in rendered.information_gaps)


def test_assisted_schema_choices_are_valid_for_negative_and_positive_facts(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    for growth in (-5., 0., 12.):
        facts["financials.revenue_growth"]["value"] = growth
        schema, choices = assisted_repair_schema(facts)
        assert schema["properties"]["summary"]["enum"] == [ASSISTED_SUMMARY]
        for choice in choices:
            payload = demo_content(candidate, facts).model_dump()
            payload["reasons"] = [choice]
            validate_content(payload, facts)


def test_thirty_case_suite_requires_real_candidates_and_tracks_synthetic_changes(tmp_path):
    service, result, _, _ = ready(tmp_path)
    with pytest.raises(ValueError, match="real candidates"):
        build_cases(result)
    cases = build_cases(result, allow_demo=True)
    assert len(cases) == 30
    assert sum(case["scenario"] == "actual_candidate" for case in cases) == 10
    assert sum(case["synthetic_modification"] for case in cases) == 20
    assert all(case["candidate"]["report_period"].endswith("12-31") for case in cases if case["scenario"] == "stale_report")
    report = evaluate_run(service.store, result.run_id, allow_demo=True)
    assert report["case_count"] == 30 and report["models"] == {}


def test_evaluation_cache_scope_does_not_reuse_or_replace_product_cards(tmp_path):
    service, result, candidate, facts = ready(tmp_path)
    value = demo_content(candidate, facts).model_dump()
    adapter = Answers([value, value, value])
    product = research_candidate(result, candidate, service.store, adapter)
    scoped = research_candidate(result, candidate, service.store, adapter, cache_scope="evaluation:suite-a")
    assert not product.cache_hit and not scoped.cache_hit
    assert research_candidate(result, candidate, service.store, adapter).cache_hit
    assert research_candidate(result, candidate, service.store, adapter, cache_scope="evaluation:suite-a").cache_hit
    other = research_candidate(result, candidate, service.store, adapter, cache_scope="evaluation:suite-b")
    assert not other.cache_hit and other.status == "completed"


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


def test_report_period_context_does_not_require_a_separate_date_claim(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "截至报告期末，净资产收益率为{{financials.roe}}。",
                             "evidence_ids": ["financials.roe"]}
    assert validate_content(payload, facts)
    payload["reasons"][0] = {"text": "报告期字段 report_period 需要复核。", "evidence_ids": ["financials.report_period"]}
    assert validate_content(payload, facts)  # 'PE' inside 'period' is not a valuation claim.
    payload["reasons"][0] = {"text": "报告期为{{financials.announcement_date}}。", "evidence_ids": ["financials.announcement_date"]}
    with pytest.raises(ValueError, match="field/evidence mismatch"):
        validate_content(payload, facts)


@pytest.mark.parametrize("growth,text", [(15.7, "营业收入出现负增长趋势。"),
                                       (-5., "营收保持正增长。"), (0., "营收保持正增长。")])
def test_growth_description_must_match_the_observed_sign(tmp_path, growth, text):
    _, _, candidate, facts = ready(tmp_path)
    facts["financials.revenue_growth"]["value"] = growth
    payload = demo_content(candidate, facts).model_dump()
    payload["risks"][0] = {"text": text, "evidence_ids": ["financials.revenue_growth"]}
    with pytest.raises(ValueError, match="growth"):
        validate_content(payload, facts)
    payload["risks"][0]["text"] = "未来若营收出现负增长，业绩风险需核实。"
    assert validate_content(payload, facts)
    payload["risks"][0]["text"] = "营收同比增长为{{financials.revenue_growth}}，可持续性需核实。"
    rendered = render_content(validate_content(payload, facts), facts)
    assert "%" in rendered.risks[0].text


def test_absolute_price_does_not_prove_a_low_price_level(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["risks"][0] = {"text": "当前股价处于较低水平。", "evidence_ids": ["market.close"]}
    with pytest.raises(ValueError, match="no benchmark"):
        validate_content(payload, facts)
    payload["risks"][0]["text"] = "缺少历史价格位置比较，无法判断股价处于低位。"
    assert validate_content(payload, facts)
    payload["risks"][0]["text"] = "无法据此推断股价具体处于高位或低位。"
    assert validate_content(payload, facts)


def test_average_ratio_does_not_establish_price_above_an_average(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "股价位于移动平均线上方。", "evidence_ids": ["market.ma_ratio"]}
    with pytest.raises(ValueError, match="compares two averages"):
        validate_content(payload, facts)
    payload["reasons"][0]["text"] = "短期均线高于长期均线，但后续趋势仍需核实。"
    assert validate_content(payload, facts)
    facts["market.ma_ratio"]["value"] = .04
    payload["reasons"][0]["text"] = "短期均线高于长期均线，表明短期平均成本低于长期平均成本。"
    with pytest.raises(ValueError, match="direction contradicts"):
        validate_content(payload, facts)
    payload["reasons"][0]["text"] = "短期均线比率大于长期均线。"
    with pytest.raises(ValueError, match="dimensionless ratio"):
        validate_content(payload, facts)


def test_model_receives_field_tokens_and_signs_but_card_keeps_original_values(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    facts["financials.revenue_growth"]["value"] = -5.5
    model_facts = explanation_evidence(facts)
    assert model_facts["financials.revenue_growth"]["value"] == "{{financials.revenue_growth}}"
    assert model_facts["financials.revenue_growth"]["observed_sign"] == "负值"
    assert facts["financials.revenue_growth"]["value"] == -5.5
    assert "不能推断股价" in model_facts["market.ma_ratio"]["definition"]
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "净资产收益率为{{financials.roe}}。", "evidence_ids": ["financials.roe"]}
    assert f"{facts['financials.roe']['value']:.4f}".rstrip("0").rstrip(".") in render_content(validate_content(payload, facts), facts).reasons[0].text


def test_failed_structured_output_is_retained_for_diagnosis(tmp_path):
    service, result, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["risks"][0] = {"text": "营业收入出现负增长趋势。", "evidence_ids": ["financials.revenue_growth"]}
    card = research_candidate(result, candidate, service.store, Answers([payload] * 3))
    assert card.status == "failed" and card.content is None and card.content_template is not None
    assert card.llm_calls == 3 and "growth direction contradicts" in card.error
    assert "营收增长方向" in render_card(card) and "ValueError" not in render_card(card)


def test_growth_cannot_be_inferred_from_an_industry_label(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "公司所属行业具备正增长趋势。", "evidence_ids": ["selection.industry"]}
    with pytest.raises(ValueError, match="must cite financials.revenue_growth"):
        validate_content(payload, facts)


def test_old_cached_narrative_is_revalidated_before_reuse(tmp_path, monkeypatch):
    service, result, candidate, facts = ready(tmp_path)
    good = demo_content(candidate, facts).model_dump()
    original = research_candidate(result, candidate, service.store, Answers([good]))
    stale = original.model_dump(mode="json")
    stale["content_template"]["risks"][0] = {"text": "营业收入出现负增长趋势。", "evidence_ids": ["financials.revenue_growth"]}
    monkeypatch.setattr(service.store, "cached", lambda key: stale)
    regenerated = research_candidate(result, candidate, service.store, Answers([good]))
    assert regenerated.status == "completed" and not regenerated.cache_hit and regenerated.llm_calls == 1


def test_repair_reports_all_unsupported_comparisons_in_one_request(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "市场动量为正，股价表现优于市场趋势。", "evidence_ids": ["market.momentum_60"]}
    payload["risks"][0] = {"text": "市盈率和市净率处于较高水平。", "evidence_ids": ["valuation.pe_ttm", "valuation.pb"]}
    with pytest.raises(ValueError) as exc:
        validate_content(payload, facts)
    assert "reasons[0]" in str(exc.value) and "risks[0]" in str(exc.value)
    payload["reasons"][0]["text"] = "缺少基准收益数据，无法判断是否优于市场。"
    payload["risks"][0]["text"] = "缺少估值比较依据，无法判断市盈率和市净率是否偏高。"
    assert validate_content(payload, facts)


@pytest.mark.parametrize("check", ["结合行业与同业数据人工判断估值是否合理。", "核实市盈率和市净率的合理性。",
    "需核实营收负增长是否由业务变化引起，以及估值合理性是否需进一步比较。",
    "需核实营收增长是否可持续，以及市盈率和市净率是否合理。"])
def test_human_verification_questions_are_not_unsupported_conclusions(tmp_path, check):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["human_checks"] = [check]
    assert validate_content(payload, facts)
    payload["reasons"][0] = {"text": "估值合理。", "evidence_ids": ["valuation.pe_ttm"]}
    with pytest.raises(ValueError, match="comparison"):
        validate_content(payload, facts)


@pytest.mark.parametrize("text", ["估值合理，请核实是否存在其他风险。", "核实营收是否增长，以及市盈率合理。",
    "核实营收是否增长。市盈率合理。", "核实营收是否增长，以及估值合理性已经得到证明。"])
def test_assertion_in_a_check_is_not_legalized_by_a_following_question(tmp_path, text):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["human_checks"] = [text]
    with pytest.raises(ValueError, match="comparison"):
        validate_content(payload, facts)


@pytest.mark.parametrize("text", ["未提供新闻，估值合理。", "无法判断最新事件，市盈率明显低估。"])
def test_unrelated_missing_data_disclaimer_cannot_excuse_a_valuation_assertion(tmp_path, text):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["human_checks"] = [text]
    with pytest.raises(ValueError, match="comparison"):
        validate_content(payload, facts)


def test_explicit_disclaimer_is_not_an_invented_metric(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "策略排名本身不代表市值或绝对优劣。", "evidence_ids": ["selection.rank"]}
    assert validate_content(payload, facts)
    payload["reasons"][0]["text"] += "公司净利润高速增长。"
    with pytest.raises(ValueError, match="not in this snapshot"):
        validate_content(payload, facts)


def test_positive_trend_does_not_assign_the_average_comparison_to_price(tmp_path):
    _, _, candidate, facts = ready(tmp_path)
    payload = demo_content(candidate, facts).model_dump()
    payload["reasons"][0] = {"text": "动量与均线比值均为正，说明历史区间内价格趋势向上、短期均线高于长期均线；后续趋势仍需核实。",
                             "evidence_ids": ["market.momentum_60", "market.ma_ratio"]}
    assert validate_content(payload, facts)
    payload["reasons"][0]["text"] = "价格高于短期均线。"
    with pytest.raises(ValueError, match="compares two averages"):
        validate_content(payload, facts)
