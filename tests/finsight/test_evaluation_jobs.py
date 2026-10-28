import fcntl
import json
import os
from datetime import date

import pytest

import finsight.evaluation_jobs as jobs
from finsight.cli import main
from finsight.models import ResearchCard
from finsight.research import ASSISTED_SUMMARY, SCOPE_SUMMARY, demo_content
from finsight.research_facts import facts_for, render_content
from finsight.service import FinSight


@pytest.fixture
def suite(tmp_path):
    service = FinSight(tmp_path)
    snapshot = service.sync("demo", date(2025, 2, 28), date(2024, 1, 2))["snapshot_id"]
    result = service.screen(snapshot)
    return service, result


@pytest.fixture
def local(monkeypatch):
    calls, closed = [], []
    monkeypatch.setattr(jobs, "_model_identities", lambda models: {
        model: {"endpoint": "http://fixture", "digest": "fixed-" + model} for model in models})
    class Adapter:
        def __init__(self, provider, model, base_url=None):
            assert provider == "ollama"
            self.model = model
        def close(self):
            closed.append(self.model)
    monkeypatch.setattr(jobs, "ChatAdapter", Adapter)
    def generate(result, candidate, store, adapter, **kwargs):
        calls.append({"model": adapter.model, "symbol": candidate["symbol"], **kwargs})
        facts = facts_for(result, candidate)
        content = render_content(demo_content(candidate, facts), facts)
        content.summary = SCOPE_SUMMARY
        return ResearchCard(run_id=result.run_id, symbol=candidate["symbol"], model=adapter.model,
            prompt_version=jobs.FIELD_PROMPT_VERSION, status="completed", content=content,
            facts=facts, evidence=facts, llm_calls=1, input_tokens=11, output_tokens=7, elapsed_seconds=2)
    monkeypatch.setattr(jobs, "research_candidate", generate)
    return calls, closed, generate


def prepare(suite, **kwargs):
    service, result = suite
    report = jobs.prepare_suite(service.store, result.run_id, allow_demo=True, **kwargs)
    return service.store, report["suite_id"], service.store.artifact_dir(report["suite_id"])


def test_prepared_status_and_cli_are_offline(suite, monkeypatch, capsys):
    store, suite_id, folder = prepare(suite)
    monkeypatch.setattr(jobs, "_model_identities", lambda *a: pytest.fail("status must be offline"))
    assert jobs.suite_status(store, suite_id)["models"] == {}
    assert main(["--data-dir", str(store.root), "evaluate", "--status", suite_id]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "prepared"
    assert (folder / "evaluation.md").exists() and not (folder / "review_template.csv").exists()


def test_completed_cases_checkpoint_and_modes_remain_separate(suite, local):
    store, suite_id, folder = prepare(suite, models=("qwen3:8b",), limit=3)
    calls, closed, generate = local
    def varied(*args, **kwargs):
        card = generate(*args, **kwargs)
        if len(calls) == 2:
            card.content.summary = ASSISTED_SUMMARY
        elif len(calls) == 3:
            card.cache_hit = True
            card.generation_usage = {"llm_calls": 3, "input_tokens": 900, "output_tokens": 300}
            card.llm_calls = card.input_tokens = card.output_tokens = card.elapsed_seconds = 0
        return card
    from unittest.mock import patch
    with patch.object(jobs, "research_candidate", varied):
        report = jobs.run_suite(store, suite_id)
    model = report["models"]["qwen3:8b"]
    assert model["completed"] == 3 and model["freeform_completed"] == 2 and model["assisted_completed"] == 1
    assert model["cache_hits"] == 1 and model["fresh_completed"] == 2 and model["elapsed_seconds"] == 4
    assert model["llm_calls"] == 2 and model["input_tokens"] == 22
    assert not model["automated_gate_passed"] and not report["quality_gate_passed"]
    assert closed == ["qwen3:8b"] and all(c["cache_scope"].startswith("evaluation:") for c in calls)
    state = json.loads((folder / "state.json").read_text())
    assert len(state["results"]["qwen3:8b"]) == 3 and state["pid"] is None
    rows = state["results"]["qwen3:8b"]
    assert not rows[0]["synthetic_modification"] and rows[1]["synthetic_modification"]
    assert rows[1]["card"]["facts"]["selection.rank"]["synthetic_modification"]
    assert (folder / "review_template.csv").exists()


def test_resume_preserves_completed_cases_and_does_not_repeat_unknown_inflight_budget(suite, local, monkeypatch):
    store, suite_id, folder = prepare(suite, models=("qwen3:8b",), limit=3)
    calls, closed, generate = local
    def interrupted(*args, **kwargs):
        if len(calls) == 1:
            raise KeyboardInterrupt()
        return generate(*args, **kwargs)
    monkeypatch.setattr(jobs, "research_candidate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        jobs.run_suite(store, suite_id)
    checkpoint = json.loads((folder / "state.json").read_text())
    assert len(checkpoint["results"]["qwen3:8b"]) == 1 and checkpoint["in_flight"]["case_id"] == "missing_roe-01"
    (folder / "review_template.csv").write_text("preserve the user's manual review")
    monkeypatch.setattr(jobs, "research_candidate", generate)
    report = jobs.run_suite(store, suite_id)
    assert len(calls) == 2 and report["models"]["qwen3:8b"]["interrupted_cases"] == 1
    assert report["models"]["qwen3:8b"]["evaluated"] == 3 and not report["quality_gate_passed"]
    assert "unknown" in report["usage_scope"]
    assert (folder / "review_template.csv").read_text() == "preserve the user's manual review"
    monkeypatch.setattr(jobs, "_model_identities", lambda *a: pytest.fail("completed resume needs no server"))
    assert jobs.run_suite(store, suite_id)["status"] == "completed" and len(calls) == 2
    assert len(closed) == 2


def test_no_second_evaluator_or_false_live_pid(suite, local):
    store, suite_id, folder = prepare(suite, models=("qwen3:8b",), limit=1)
    state = json.loads((folder / "state.json").read_text())
    state.update(status="running", pid=os.getpid())
    jobs._save(folder, state)
    # A reused/live PID alone does not prove that the evaluator still owns its lock.
    assert jobs.suite_status(store, suite_id)["status"] == "interrupted"
    with (folder / "run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert jobs.suite_status(store, suite_id)["status"] == "running"
        with pytest.raises(ValueError, match="already running"):
            jobs.run_suite(store, suite_id)
    assert not local[0]


@pytest.mark.parametrize("change", ["engine", "cases", "source", "model", "order"])
def test_resume_rejects_changed_inputs_before_model_generation(suite, local, monkeypatch, change):
    store, suite_id, folder = prepare(suite, models=("qwen3:8b",), limit=1)
    models = ()
    if change == "engine":
        monkeypatch.setattr(jobs, "_engine_id", lambda: "changed-engine")
    elif change == "cases":
        cases = json.loads((folder / "cases.json").read_text())
        cases[0]["candidate"]["roe"] = -999
        jobs._json(folder / "cases.json", cases)
    elif change == "source":
        with store.connect() as db:
            db.execute("UPDATE runs SET payload=json_set(payload, '$.eligible_count', 123) WHERE run_id=?", (suite[1].run_id,))
    elif change == "model":
        state = json.loads((folder / "state.json").read_text())
        state["model_identities"] = {"qwen3:8b": {"endpoint": "http://fixture", "digest": "older"}}
        jobs._save(folder, state)
    else:
        models = ("qwen3.5:4b",)
    with pytest.raises(ValueError):
        jobs.run_suite(store, suite_id, models=models)
    assert not local[0]


def test_digest_change_during_case_preserves_the_invalid_result_without_replaying(suite, local, monkeypatch):
    store, suite_id, folder = prepare(suite, models=("qwen3:8b",), limit=2)
    count = 0
    def identity(models):
        nonlocal count
        count += 1
        return {model: {"endpoint": "http://fixture", "digest": "before" if count < 3 else "after"} for model in models}
    monkeypatch.setattr(jobs, "_model_identities", identity)
    with pytest.raises(ValueError, match="identity changed"):
        jobs.run_suite(store, suite_id)
    state = json.loads((folder / "state.json").read_text())
    assert state["results"]["qwen3:8b"][0]["status"] == "identity_changed"
    assert not state["results"]["qwen3:8b"][0]["passed"] and state["in_flight"] is None
    assert len(local[0]) == 1


def test_full_automated_pass_never_claims_human_quality_acceptance(suite, local):
    store, suite_id, _ = prepare(suite, models=("qwen3:8b",), limit=30)
    report = jobs.run_suite(store, suite_id)
    assert report["models"]["qwen3:8b"]["automated_gate_passed"]
    assert not report["quality_gate_passed"] and "pending" in report["human_review"]
    assert len(local[0]) == 30


def test_persistent_provider_failures_stop_without_discarding_case_results(suite, local, monkeypatch):
    store, suite_id, folder = prepare(suite, models=("qwen3:8b",), limit=5)
    def unavailable(*args, **kwargs):
        card = local[2](*args, **kwargs)
        card.status, card.content, card.error, card.llm_calls = "failed", None, "ollama: request timed out or local service unavailable", 3
        return card
    monkeypatch.setattr(jobs, "research_candidate", unavailable)
    with pytest.raises(ValueError, match="persistent local provider"):
        jobs.run_suite(store, suite_id)
    state = json.loads((folder / "state.json").read_text())
    assert len(state["results"]["qwen3:8b"]) == 3 and state["in_flight"] is None
    report = jobs.suite_status(store, suite_id)
    assert report["status"] == "interrupted" and report["models"]["qwen3:8b"]["llm_calls"] == 9


@pytest.mark.parametrize("models,limit", [(("deepseek",),30), (("qwen3:8b","qwen3:8b"),30), ((),0), ((),31)])
def test_invalid_evaluation_request_creates_no_artifact(suite, models, limit):
    before = set((suite[0].store.root / "exports").iterdir())
    with pytest.raises(ValueError):
        prepare(suite, models=models, limit=limit)
    assert set((suite[0].store.root / "exports").iterdir()) == before


def test_corrupt_checkpoint_and_unsafe_identifiers_are_errors(suite):
    store, suite_id, folder = prepare(suite)
    (folder / "state.json").write_text("{not JSON")
    with pytest.raises(ValueError, match="corrupt"):
        jobs.suite_status(store, suite_id)
    with pytest.raises(ValueError, match="identifier"):
        jobs.suite_status(store, "../escape")
