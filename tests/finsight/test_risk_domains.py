import copy
import json
from datetime import date

import pytest

from finsight.models import ResearchCard
from finsight.research import (
    ASSISTED_SUMMARY,
    FIELD_PROMPT_VERSION,
    assisted_repair_schema,
    demo_content,
    evidence_for,
    finalize_content,
    required_risk_choices,
    research_candidate,
)
from finsight.research_facts import render_content
from finsight.research_requirements import risk_domain_coverage
from finsight.service import FinSight


@pytest.fixture
def case(tmp_path):
    s = FinSight(tmp_path)
    snap = s.sync("demo", date(2025, 12, 31), date(2024, 1, 2))["snapshot_id"]
    r = s.screen(snap)
    c = copy.deepcopy(r.candidates[0])
    return s, r, c, evidence_for(r, c)


def valid(c, facts):
    return demo_content(c, facts).model_dump()


@pytest.mark.parametrize("domain", ["financials", "valuation", "market"])
def test_each_available_domain_must_appear_in_risks_not_only_reasons(case, domain):
    _, result, c, facts = case
    value = valid(c, facts)
    removed = [claim for claim in value["risks"] if claim["evidence_ids"][0].startswith(domain + ".")]
    value["risks"] = [claim for claim in value["risks"] if claim not in removed]
    value["reasons"] += removed
    with pytest.raises(ValueError, match="missing domain " + domain):
        finalize_content(value, facts, result.as_of)


def test_liquidity_and_rank_citations_do_not_substitute_for_trend_risk(case):
    _, result, c, facts = case
    value = valid(c, facts)
    value["risks"] = [claim for claim in value["risks"] if not claim["evidence_ids"][0].startswith("market.")]
    value["risks"] += [{"text": "成交额可能波动，需核实流动性。", "evidence_ids": ["market.amount_20"]},
        {"text": "策略排名不能保证未来表现。", "evidence_ids": ["selection.rank"]}]
    with pytest.raises(ValueError, match="missing domain market"):
        finalize_content(value, facts, result.as_of)


def test_positive_factor_description_and_irrelevant_risk_text_do_not_count(case):
    _, result, c, facts = case
    value = valid(c, facts)
    risk = next(claim for claim in value["risks"] if claim["evidence_ids"][0].startswith("financials."))
    risk.update(text="净资产收益率参与财务质量评分。", evidence_ids=["financials.roe"])
    with pytest.raises(ValueError, match="missing domain financials"):
        finalize_content(value, facts, result.as_of)
    risk.update(text="估值缺少比较依据，无法判断是否合理。", evidence_ids=["financials.roe"])
    with pytest.raises(ValueError):
        finalize_content(value, facts, result.as_of)


@pytest.mark.parametrize("domain,fields,label", [
    ("valuation", ("pe_ttm", "pb"), "估值"),
    ("market", ("momentum_60", "ma_ratio"), "趋势"),
    ("financials", ("roe", "revenue_growth", "report_period"), "财务")])
def test_unavailable_domains_require_unknown_disclosure_without_invented_citations(case, domain, fields, label):
    _, result, c, _ = case
    for field in fields:
        c.pop(field, None)
    facts = evidence_for(result, c)
    value = valid(c, facts)
    content = finalize_content(value, facts, result.as_of)
    coverage = risk_domain_coverage(content, facts, result.as_of)
    assert domain in coverage["unavailable_domains"] and not coverage["missing_domains"]
    assert any(label in gap and "无法判断" in gap for gap in content.information_gaps)
    value["information_gaps"] = ["未接入新闻，无法判断最新事件。"]
    with pytest.raises(ValueError, match="unavailable domain " + domain):
        finalize_content(value, facts, result.as_of)


def test_combined_negative_growth_and_staleness_fit_four_risks_without_losing_other_domains(case):
    _, result, c, _ = case
    c.update(revenue_growth=-5., report_period="2022-12-31")
    facts = evidence_for(result, c)
    schema, choices = assisted_repair_schema(facts)
    mandatory = required_risk_choices(facts, choices)
    assert len(mandatory) == 4 and schema["properties"]["risks"]["maxItems"] == 4
    assert schema["properties"]["risks"]["minItems"] == 4
    value = {"summary": ASSISTED_SUMMARY, "reasons": [choices[0]], "risks": mandatory,
        "information_gaps": ["需核实数据。"], "human_checks": ["核实后续报告。"]}
    content = finalize_content(value, facts, result.as_of)
    assert not risk_domain_coverage(content, facts, result.as_of)["missing_domains"]
    assert any("负增长" in claim.text for claim in content.risks) and any("陈旧" in claim.text for claim in content.risks)


def test_future_or_invalid_report_period_is_not_available_financial_risk_evidence(case):
    _, result, c, _ = case
    c.update(roe=None, revenue_growth=None, report_period="2099-12-31")
    facts = evidence_for(result, c)
    content = finalize_content(valid(c, facts), facts, result.as_of)
    assert "financials" in risk_domain_coverage(content, facts, result.as_of)["unavailable_domains"]
    assert all("financials.report_period" not in claim.evidence_ids for claim in content.risks)


def test_no_domain_data_retains_only_available_evidence_and_unknown_disclosures(case):
    _, result, c, _ = case
    for field in ("roe", "revenue_growth", "report_period", "pe_ttm", "pb", "momentum_60", "ma_ratio"):
        c.pop(field, None)
    facts = evidence_for(result, c)
    content = finalize_content(valid(c, facts), facts, result.as_of)
    coverage = risk_domain_coverage(content, facts, result.as_of)
    assert set(coverage["unavailable_domains"]) == {"financials", "valuation", "market"}
    assert not coverage["undisclosed_unavailable_domains"]
    assert all(key in facts for claim in content.reasons + content.risks for key in claim.evidence_ids)


class Replies:
    provider, model, base_url = "ollama", "risk-domain-fixture", "http://fixture"
    def __init__(self, values):
        self.values, self.calls = iter(values), 0
    def complete(self, messages, tools, final=False):
        self.calls += 1
        return {"message": {"content": json.dumps(next(self.values), ensure_ascii=False)}, "input_tokens": 10, "output_tokens": 5}


def test_domain_gap_is_repaired_within_budget_and_never_inserts_a_fake_success(case):
    s, result, c, facts = case
    good = valid(c, facts)
    bad = copy.deepcopy(good)
    bad["risks"] = [claim for claim in bad["risks"] if not claim["evidence_ids"][0].startswith("financials.")]
    fixed = research_candidate(result, c, s.store, Replies([bad, good]))
    assert fixed.status == "completed" and fixed.llm_calls == 2 and fixed.prompt_version == FIELD_PROMPT_VERSION
    isolated = result.model_copy(update={"config_id": "different"})
    failed = research_candidate(isolated, c, s.store, Replies([bad, bad, bad]))
    assert failed.status == "failed" and failed.content is None and failed.llm_calls == 3
    assert "missing domain financials" in failed.error


def test_history_rechecks_old_saved_response_without_rewriting_original_status(case):
    s, result, c, facts = case
    value = valid(c, facts)
    value["risks"] = [claim for claim in value["risks"] if not claim["evidence_ids"][0].startswith("financials.")]
    card = ResearchCard(run_id=result.run_id, symbol=c["symbol"], model="fixture", prompt_version="old",
        status="completed", content=value, content_template=value, facts=facts, evidence=facts)
    run = s.store.save_run("research", result.snapshot_id, s.config, {"screen_run_id": result.run_id,
        "provider": "ollama", "cards": [card.model_dump(mode="json")]})
    before = s.store.run(run)
    group = s.saved_research(result.run_id)["groups"][0]
    assert group["cards"][0]["card"]["status"] == "completed"
    assert group["current_validated_cards"] == 0
    assert "missing domain financials" in group["cards"][0]["current_validation"]["issue"]
    assert s.store.run(run) == before


@pytest.mark.parametrize("change", ["risk_omitted", "displayed_content_changed"])
def test_current_version_saved_success_with_invalid_content_is_pending_and_read_only(case, monkeypatch, change):
    from finsight.research import ChatAdapter

    s, result, c, facts = case
    template = demo_content(c, facts)
    if change == "risk_omitted":
        template.risks = [claim for claim in template.risks if not claim.evidence_ids[0].startswith("financials.")]
    displayed = render_content(template, facts)
    if change == "displayed_content_changed":
        displayed.risks[0].text = "原报告正文已被替换。"
    card = ResearchCard(run_id=result.run_id, symbol=c["symbol"], model="fixture", prompt_version=FIELD_PROMPT_VERSION,
        status="completed", content=displayed, content_template=template, facts=facts, evidence=facts)
    run = s.store.save_run("research", result.snapshot_id, s.config, {"screen_run_id": result.run_id,
        "provider": "ollama", "cards": [card.model_dump(mode="json")]})
    before = s.store.run(run)
    monkeypatch.setattr(ChatAdapter, "__init__", lambda *a, **kw: pytest.fail("read-only checks cannot create a client"))
    group = s.saved_research(result.run_id)["groups"][0]
    assert group["current_validated_cards"] == 0 and group["cards"][0]["card"]["status"] == "completed"
    plan = s.research_plan(result.run_id, "ollama", "fixture", symbols=[c["symbol"]], missing_only=True)
    assert plan["symbols"] == [c["symbol"]] and not plan["skipped_successful_symbols"]
    assert s.store.run(run) == before and not s.store.runs("research_batch")
