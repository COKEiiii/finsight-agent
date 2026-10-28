import hashlib
import json
from datetime import date

import pytest

from finsight.cli import main
from finsight.evaluation_audit import audit_suite, scenario_check
from finsight.evaluation_jobs import _load, _save, prepare_suite
from finsight.models import ResearchCard, ResearchContent
from finsight.research import ASSISTED_SUMMARY, SCOPE_SUMMARY, ChatAdapter, assisted_repair_schema
from finsight.research_facts import facts_for, render_content
from finsight.service import FinSight


def content(risk="数据存在局限。", gaps=None):
    return ResearchContent(summary=SCOPE_SUMMARY,
        reasons=[{"text": "策略排名来自固定规则。", "evidence_ids": ["selection.rank"]}],
        risks=[{"text": risk, "evidence_ids": ["financials.revenue_growth"]}],
        information_gaps=gaps or ["未接新闻或公告正文，无法判断最新事件。"], human_checks=["核实年报。"])


@pytest.fixture
def recorded(tmp_path):
    service = FinSight(tmp_path)
    snapshot = service.sync("demo", date(2025,2,28), date(2024,1,2))["snapshot_id"]
    result = service.screen(snapshot)
    prepared = prepare_suite(service.store, result.run_id, models=("qwen3:8b",), limit=5, allow_demo=True)
    folder, state, cases = _load(service.store, prepared["suite_id"])
    rows = []
    for case in cases[:5]:
        facts = facts_for(result, case["candidate"])
        template = content()
        if case["scenario"] == "actual_candidate":
            _, choices = assisted_repair_schema(facts)
            template = ResearchContent(summary=ASSISTED_SUMMARY, reasons=[choices[0]], risks=[choices[-1]],
                information_gaps=["未接新闻，无法判断。"],
                human_checks=["需核实营收增长是否可持续，以及市盈率和市净率是否合理。"])
        elif case["scenario"] == "missing_roe":
            template.information_gaps = ["未提供净资产收益率，无法判断该指标。"]
        elif case["scenario"] == "stale_report":
            template.risks[0].text = "年报信息可能已过时，需核实。"
            template.risks[0].evidence_ids = ["financials.report_period"]
        elif case["scenario"] == "negative_growth":
            template.risks[0].text = "当前营收出现负增长，需核实其原因。"
        old_failed = case["scenario"] == "actual_candidate"
        card = ResearchCard(run_id=result.run_id, symbol=case["symbol"], model="qwen3:8b", prompt_version="fixture",
            status="failed" if old_failed else "completed", content_template=template,
            content=None if old_failed else render_content(template, facts), facts=facts, evidence=facts)
        rows.append({"case_id": case["case_id"], "scenario": case["scenario"], "status": card.status,
            "passed": not old_failed, "cache_hit": False, "mode": "failed" if old_failed else "freeform",
            "elapsed_seconds": 1, "llm_calls": 1, "input_tokens": 10, "output_tokens": 5,
            "card": card.model_dump(mode="json")})
    state.update(status="completed", results={"qwen3:8b": rows})
    _save(folder, state)
    return service, prepared["suite_id"], folder, state


def test_offline_cli_audit_preserves_originals_and_never_calls_a_model(recorded, monkeypatch, capsys):
    service, suite_id, folder, _ = recorded
    def no_network(*args, **kwargs):
        pytest.fail("offline audit must not contact a model")
    monkeypatch.setattr(ChatAdapter, "complete", no_network)
    monkeypatch.setattr("finsight.evaluation_jobs._model_identities", no_network)
    before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    assert main(["--data-dir", str(service.store.root), "evaluate", "--audit", suite_id]) == 0
    report = json.loads(capsys.readouterr().out)
    model = report["models"]["qwen3:8b"]
    assert model["original_completed"] == 4 and model["saved_responses_valid_now"] == 5
    assert model["recoverable_saved_responses"] == 1 and model["scenario_passed_now"] == 5
    assert report["model_calls"] == 0 and not report["quality_gate_passed"]
    assert before == {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    out = service.store.artifact_dir(report["run_id"])
    assert all((out / name).exists() for name in ("audit.md", "audit.json", "case_review.csv"))


@pytest.mark.parametrize("changed", ["binding", "unit", "date", "extra_field", "missing_template"])
def test_audit_rejects_wrong_binding_facts_or_missing_original_template(recorded, changed):
    service, suite_id, folder, state = recorded
    card = state["results"]["qwen3:8b"][0]["card"]
    if changed == "binding":
        card["run_id"] = "different-signal"
    elif changed == "unit":
        card["evidence"]["market.amount_20"]["unit"] = "万元/日"
    elif changed == "date":
        card["facts"]["financials.roe"]["known_at"] = "2099-01-01"
    elif changed == "extra_field":
        card["facts"]["invented.metric"] = {"value": 1}
    else:
        card["content_template"] = None
    _save(folder, state)
    report = audit_suite(service.store, suite_id)
    assert report["models"]["qwen3:8b"]["saved_responses_valid_now"] == 4
    assert not report["quality_gate_passed"]


def test_audit_requires_a_complete_case_set_and_completed_suite(recorded):
    service, suite_id, folder, state = recorded
    state["status"] = "running"
    _save(folder, state)
    with pytest.raises(ValueError, match="completed"):
        audit_suite(service.store, suite_id)
    state["status"] = "completed"
    state["results"]["qwen3:8b"][1] = state["results"]["qwen3:8b"][0]
    _save(folder, state)
    with pytest.raises(ValueError, match="duplicated"):
        audit_suite(service.store, suite_id)


@pytest.mark.parametrize("text,passed", [("年报信息可能已过时，需核实。", True),
    ("年报尚未过期。", False), ("请核实最新股价。", False)])
def test_report_freshness_check_is_specific_and_accepts_outdated_synonym(text, passed):
    assert bool(scenario_check("stale_report", content(risk=text), {})) == passed


@pytest.mark.parametrize("text,passed", [("未来若营收出现负增长，需要防范风险。", False),
    ("当前营收负增长，需要核实。", True), ("未来若营收增长未能改善，需要核实。", False)])
def test_observed_negative_growth_must_be_in_risks_not_just_a_future_condition(text, passed):
    assert bool(scenario_check("negative_growth", content(risk=text), {})) == passed
