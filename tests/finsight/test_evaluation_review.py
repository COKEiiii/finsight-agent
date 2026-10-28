import csv
import hashlib
import io
import json
from datetime import date

import pytest

from finsight.cli import main
from finsight.evaluation_jobs import _load, _save, prepare_suite
from finsight.evaluation_review import (
    FIELDS,
    _report,
    completed_suites,
    export_packet,
    import_review,
    review_packet,
    review_status,
)
from finsight.models import ResearchCard, ResearchContent
from finsight.research import SCOPE_SUMMARY, ChatAdapter
from finsight.research_facts import facts_for
from finsight.service import FinSight


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    service = FinSight(tmp_path)
    snapshot = service.sync("demo", date(2025, 2, 28), date(2024, 1, 2))["snapshot_id"]
    result = service.screen(snapshot)
    prepared = prepare_suite(service.store, result.run_id, models=("qwen3:8b", "qwen3.5:4b"), allow_demo=True)
    folder, state, cases = _load(service.store, prepared["suite_id"])
    for model in state["models"]:
        rows = []
        for case in cases:
            facts = facts_for(result, case["candidate"])
            content = ResearchContent(summary=SCOPE_SUMMARY,
                reasons=[{"text": "入选。", "evidence_ids": ["selection.rank"]}],
                risks=[{"text": "核对风险。", "evidence_ids": ["market.amount_20"]}],
                information_gaps=["未接新闻。"], human_checks=["核对年报。"])
            card = ResearchCard(run_id=result.run_id, symbol=case["symbol"], model=model,
                prompt_version=state["prompt_version"], status="completed", content=content, facts=facts, evidence=facts)
            rows.append({"case_id": case["case_id"], "scenario": case["scenario"], "symbol": case["symbol"],
                "status": "completed", "passed": True, "mode": "freeform", "card": card.model_dump(mode="json")})
        state["results"][model] = rows
    state["status"] = "completed"
    _save(folder, state)
    monkeypatch.setattr(ChatAdapter, "complete", lambda *a, **kw: pytest.fail("human review must be offline"))
    monkeypatch.setattr("finsight.evaluation_jobs._model_identities", lambda *a: pytest.fail("no model identity query"))
    return service, state["suite_id"], folder, state


def fill(packet, count=None, change=None):
    rows = list(csv.DictReader(io.StringIO(packet["template_csv"].lstrip("\ufeff"))))
    for row in rows[:count]:
        row.update(reviewer="测试审阅人", serious_factual_errors="0", evidence_complete="true",
            manual_revision_count="1", review_seconds="30", notes="")
    if change:
        change(rows)
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def test_review_packet_and_import_keep_all_baselines_immutable(recorded):
    service, suite_id, folder, _ = recorded
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    packet = review_packet(service.store, suite_id)
    assert packet["model_calls"] == 0 and "变造输入：是，仅供评估" in packet["cards_markdown"]
    assert len(list(csv.DictReader(io.StringIO(packet["template_csv"].lstrip("\ufeff"))))) == 60
    report = import_review(service.store, suite_id, fill(packet))
    assert report["review_complete"] and not report["quality_gate_passed"]  # Demo source cannot certify live quality.
    model = report["models"]["qwen3:8b"]
    assert model["human_review_passed"] and model["automated_gate_passed"]
    assert model["manual_revision_count"] == 30 and model["review_seconds"] == 900 and model["mean_review_seconds"] == 30
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    assert service.store.run(report["run_id"])["kind"] == "evaluation_review"
    assert review_status(service.store, suite_id)["run_id"] == report["run_id"]


def test_partial_progress_missing_models_and_saved_csv_roundtrip(recorded):
    service, suite_id, _, _ = recorded
    pending = review_status(service.store, suite_id)
    assert pending["human_review"] == "pending"
    assert pending["models"]["qwen3:8b"]["serious_factual_errors"] is None
    assert pending["models"]["qwen3:8b"]["manual_revision_count"] is None
    report = import_review(service.store, suite_id, fill(review_packet(service.store, suite_id), 2))
    assert not report["review_complete"] and not report["quality_gate_passed"]
    assert report["models"]["qwen3:8b"]["reviewed"] == 2
    assert report["models"]["qwen3.5:4b"]["reviewed"] == 0
    saved = (service.store.artifact_dir(report["run_id"]) / "review.csv").read_text()
    again = import_review(service.store, suite_id, saved)
    assert again["models"] == report["models"] and len(service.store.runs("evaluation_review")) == 2
    assert len(list(csv.DictReader(io.StringIO(saved.lstrip("\ufeff"))))) == 60


@pytest.mark.parametrize("field,value", [("serious_factual_errors", "-1"), ("manual_revision_count", "1.2"),
    ("review_seconds", "nan"), ("review_seconds", "inf"), ("review_seconds", "0"),
    ("evidence_complete", "yes"), ("reviewer", ""), ("suite_id", "wrong-suite"),
    ("response_fingerprint", "changed"), ("checkpoint_fingerprint", "changed"), ("synthetic_modification", "false")])
def test_invalid_or_unbound_ratings_fail_before_any_write(recorded, field, value):
    service, suite_id, _, _ = recorded
    # The second case is modified, so changing its modification marker is also rejected.
    text = fill(review_packet(service.store, suite_id), 2, lambda rows: rows[1].update({field: value}))
    with pytest.raises(ValueError):
        import_review(service.store, suite_id, text)
    assert not service.store.runs("evaluation_review")


def test_duplicates_blank_ratings_and_problem_notes(recorded):
    service, suite_id, _, _ = recorded
    packet = review_packet(service.store, suite_id)
    with pytest.raises(ValueError, match="duplicate"):
        import_review(service.store, suite_id, fill(packet, 1, lambda rows: rows.append(rows[0])))
    with pytest.raises(ValueError, match="no completed human"):
        import_review(service.store, suite_id, packet["template_csv"])
    with pytest.raises(ValueError, match="notes"):
        import_review(service.store, suite_id, fill(packet, 1, lambda rows: rows[0].update(evidence_complete="false")))
    with pytest.raises(ValueError, match="columns"):
        import_review(service.store, suite_id, "model,case_id,reviewer\nqwen3:8b,actual_candidate-01,me\n")


def test_findings_and_automated_gate_both_control_quality_per_model(recorded):
    service, suite_id, folder, state = recorded
    state["allow_demo"] = False  # Isolated fixture exercises the non-demo gate; no real acceptance is created.
    _save(folder, state)
    packet = review_packet(service.store, suite_id)
    report = import_review(service.store, suite_id, fill(packet, change=lambda rows: rows[0].update(
        serious_factual_errors="1", evidence_complete="false", notes="营收指标引用错误，需重写。")))
    assert not report["quality_gate_passed"]
    assert report["models"]["qwen3:8b"]["serious_factual_errors"] == 1
    assert report["models"]["qwen3:8b"]["incomplete_completed_cards"] == 1
    assert report["models"]["qwen3.5:4b"]["human_review_passed"]
    assert not report["models"]["qwen3.5:4b"]["quality_gate_passed"]  # Source remains synthetic even if allow_demo is changed.
    reviews = report["reviews"]
    responses = {(model, row["case_id"]): row for model, rows in state["results"].items() for row in rows}
    assert _report(state, responses, [{**row, "serious_factual_errors": 0, "evidence_complete": True} for row in reviews],
        source_synthetic=False)["quality_gate_passed"]
    for row in state["results"]["qwen3:8b"][:2]:
        row["passed"] = False
    assert not _report(state, responses, [{**row, "serious_factual_errors": 0, "evidence_complete": True} for row in reviews],
        source_synthetic=False)["quality_gate_passed"]


def test_failed_card_review_is_unavailable_not_complete_evidence(recorded):
    service, suite_id, folder, state = recorded
    state["results"]["qwen3:8b"][0].update(status="failed", passed=False)
    _save(folder, state)
    packet = review_packet(service.store, suite_id)
    with pytest.raises(ValueError, match="failed response"):
        import_review(service.store, suite_id, fill(packet, 1))
    report = import_review(service.store, suite_id, fill(packet, change=lambda rows: rows[0].update(
        evidence_complete="false", notes="模型输出校验失败，无成功卡可审阅。")))
    model = report["models"]["qwen3:8b"]
    assert model["unavailable_cards"] == 1 and model["incomplete_completed_cards"] == 0
    assert model["human_review_passed"] and model["automated_gate_passed"]


def test_checkpoint_changes_and_wrong_card_binding_cannot_reuse_ratings(recorded):
    service, suite_id, folder, state = recorded
    text = fill(review_packet(service.store, suite_id), 1)
    import_review(service.store, suite_id, text)
    state["results"]["qwen3:8b"][0]["card"]["content"]["summary"] += " changed"
    _save(folder, state)
    with pytest.raises(ValueError, match="binding changed"):
        import_review(service.store, suite_id, text)
    with pytest.raises(ValueError, match="checkpoint changed"):
        review_status(service.store, suite_id)
    state["results"]["qwen3:8b"][0]["card"]["model"] = "other-model"
    _save(folder, state)
    with pytest.raises(ValueError, match="different screening"):
        review_packet(service.store, suite_id)


def test_cli_packet_import_status_and_misused_options_are_offline(recorded, capsys):
    service, suite_id, _, _ = recorded
    prefix = ["--data-dir", str(service.store.root), "evaluate"]
    assert main(prefix + ["--review-template", suite_id]) == 0
    packet = json.loads(capsys.readouterr().out)
    from pathlib import Path
    path = Path(packet["review_csv"])
    path.write_text(fill(review_packet(service.store, suite_id), 1))
    assert main(prefix + ["--review", suite_id, "--review-file", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["models"]["qwen3:8b"]["reviewed"] == 1
    assert main(prefix + ["--review", suite_id]) == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    assert main(prefix + ["--review-template", suite_id, "--models", "qwen3:8b"]) == 2
    assert main(prefix + ["--status", suite_id, "--review-file", str(path)]) == 2


def test_packet_requires_completed_intact_case_inventory(recorded):
    service, suite_id, folder, state = recorded
    assert completed_suites(service.store) == [suite_id]
    exported = export_packet(service.store, suite_id)
    assert (service.store.artifact_dir(exported["exports"].split("/")[-1]) / "cards.md").exists()
    state["results"]["qwen3:8b"].pop()
    _save(folder, state)
    with pytest.raises(ValueError, match="incomplete"):
        review_packet(service.store, suite_id)
    state["status"] = "running"
    _save(folder, state)
    assert not completed_suites(service.store)
    with pytest.raises(ValueError, match="completed evaluation"):
        review_packet(service.store, suite_id)


def test_dashboard_review_packet_is_read_only(recorded, monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    service, suite_id, _, _ = recorded
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(service.store.root))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    assert not app.exception
    assert next(s for s in app.selectbox if s.label == "待审阅评估套件").value == suite_id
    next(b for b in app.button if b.label == "准备人工审阅包").click().run()
    assert not app.exception and not service.store.runs("evaluation_review")
    assert app.session_state["review_packet"]["suite_id"] == suite_id
    app.run()
    assert not app.exception and not service.store.runs("evaluation_review")
