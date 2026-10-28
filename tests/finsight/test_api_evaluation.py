import json
from datetime import date

import httpx
import pytest

import finsight.evaluation_jobs as jobs
from finsight.cli import main
from finsight.evaluation_review import completed_suites, review_packet, review_status
from finsight.models import ResearchCard
from finsight.research import FIELD_PROMPT_VERSION, demo_content, evidence_for, finalize_content
from finsight.service import FinSight


@pytest.fixture
def api(tmp_path, monkeypatch):
    service = FinSight(tmp_path)
    snap = service.sync("demo", date(2025, 12, 31), date(2024, 1, 2))["snapshot_id"]
    result = service.screen(snap)
    for provider in ("deepseek", "qwen"):
        monkeypatch.setenv(provider.upper() + "_API_KEY", "test-only")
        monkeypatch.setenv(provider.upper() + "_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setattr(jobs, "_model_identities", lambda *a: pytest.fail("API evaluation must not contact Ollama"))
    calls, closed = [], []
    class Adapter:
        def __init__(self, provider, model, base_url):
            self.provider, self.model, self.base_url = provider, model, base_url
        def close(self):
            closed.append(self.provider)
    monkeypatch.setattr(jobs, "ChatAdapter", Adapter)
    def generate(result, c, store, adapter, **options):
        calls.append({"provider": adapter.provider, "model": adapter.model, **options})
        facts = evidence_for(result, c)
        template = demo_content(c, facts)
        return ResearchCard(run_id=result.run_id, symbol=c["symbol"], model=adapter.model,
            prompt_version=FIELD_PROMPT_VERSION, status="completed", content=finalize_content(template.model_dump(), facts, result.as_of),
            content_template=template, facts=facts, evidence=facts, llm_calls=1, input_tokens=11, output_tokens=7, elapsed_seconds=2)
    monkeypatch.setattr(jobs, "research_candidate", generate)
    return service, result, calls, closed, generate


def prepare(api, provider="deepseek", **options):
    s, result, *_ = api
    return jobs.prepare_suite(s.store, result.run_id, provider=provider, models=(provider + "-fixture",), allow_demo=True, **options)


@pytest.mark.parametrize("provider", ["deepseek", "qwen"])
def test_full_api_suite_accounting_and_human_review_stay_separate_from_production(api, provider):
    s, result, calls, closed, _ = api
    report = jobs.run_suite(s.store, prepare(api, provider)["suite_id"])
    assert report["status"] == "completed" and report["provider"] == provider
    assert report["budget"]["requests_used_or_reserved"] == 30
    assert report["budget"]["reported_tokens"] == 540 and not report["budget"]["unknown_usage_cases"]
    model = report["models"][provider + "-fixture"]
    assert model["automated_gate_passed"] and not report["quality_gate_passed"]
    assert len(calls) == 30 and closed == [provider] and all(call["max_llm_calls"] == 3 for call in calls)
    assert not s.saved_research(result.run_id)["groups"] and not s.store.runs("research")
    usage = s.research_usage(provider)
    assert usage["reported_tokens"] == 540 and usage["by_model"][0]["evaluation_case_records"] == 30
    assert s.research_usage(provider) == usage and not usage["evaluation_usage_issues"]
    other = "qwen" if provider == "deepseek" else "deepseek"
    assert s.research_usage(other)["reported_tokens"] == 0
    assert review_packet(s.store, report["suite_id"])["model_calls"] == 0
    assert not review_status(s.store, report["suite_id"])["quality_gate_passed"]
    assert report["suite_id"] in completed_suites(s.store)
    assert jobs.run_suite(s.store, report["suite_id"]) == report and len(calls) == 30


def test_request_budget_is_not_reset_on_resume_and_caps_last_card_requests(api, monkeypatch):
    s, _, calls, _, generate = api
    def consume(*args, **kw):
        card = generate(*args, **kw)
        card.llm_calls = kw["max_llm_calls"]
        return card
    monkeypatch.setattr(jobs, "research_candidate", consume)
    suite = prepare(api, request_limit=5)
    report = jobs.run_suite(s.store, suite["suite_id"])
    assert report["status"] == "budget_stopped" and [c["max_llm_calls"] for c in calls] == [3, 2]
    assert report["budget"]["requests_used_or_reserved"] == 5
    assert jobs.run_suite(s.store, suite["suite_id"]) == report and len(calls) == 2
    assert suite["suite_id"] not in completed_suites(s.store)


def test_reported_token_threshold_stops_before_next_case_and_can_overshoot_in_current_case(api):
    s, _, calls, *_ = api
    report = jobs.run_suite(s.store, prepare(api, reported_token_stop=17)["suite_id"])
    assert report["status"] == "budget_stopped" and len(calls) == 1
    assert report["budget"]["reported_tokens"] == 18
    assert "current case may exceed" in report["budget"]["note"]


def test_interrupted_case_reserves_its_allowance_and_is_never_replayed(api, monkeypatch):
    s, _, calls, _, generate = api
    prepared = prepare(api, request_limit=5)
    def interrupted(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(jobs, "research_candidate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        jobs.run_suite(s.store, prepared["suite_id"])
    state = json.loads((s.store.artifact_dir(prepared["suite_id"])/"state.json").read_text())
    assert state["in_flight"]["reserved_requests"] == 3
    monkeypatch.setattr(jobs, "research_candidate", generate)
    report = jobs.run_suite(s.store, prepared["suite_id"])
    assert report["status"] == "budget_stopped" and len(calls) == 2
    assert calls[0]["max_llm_calls"] == 2 and calls[1]["max_llm_calls"] == 1
    assert report["budget"]["unknown_usage_cases"] == 1 and report["budget"]["requests_used_or_reserved"] == 5
    assert s.research_usage()["evaluation_usage_unknown_cases"] == 1


@pytest.mark.parametrize("change", ["endpoint", "budget", "key"])
def test_changed_identity_budget_or_missing_key_stops_before_requests(api, monkeypatch, change):
    s, _, calls, *_ = api
    prepared = prepare(api)
    folder, state, _ = jobs._load(s.store, prepared["suite_id"])
    if change == "endpoint":
        state["model_identities"] = {"deepseek-fixture": {"endpoint": "https://old.invalid", "model": "deepseek-fixture", "weights_digest": None}}
    elif change == "budget":
        state["request_limit"] = 90_000
    else:
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    jobs._save(folder, state)
    with pytest.raises(ValueError):
        jobs.run_suite(s.store, prepared["suite_id"])
    assert not calls


@pytest.mark.parametrize("options", [{"request_limit":0}, {"request_limit":91}, {"reported_token_stop":0}, {"request_limit":True}])
def test_invalid_budget_creates_no_suite(api, options):
    s, *_ = api
    before = set((s.store.root/"exports").iterdir())
    with pytest.raises(ValueError):
        prepare(api, **options)
    assert set((s.store.root/"exports").iterdir()) == before


def test_one_safe_api_model_per_suite_and_explicit_provider_required(api):
    s, result, *_ = api
    for models in (("../model",), ("first", "second")):
        with pytest.raises(ValueError):
            jobs.prepare_suite(s.store, result.run_id, provider="qwen", models=models, allow_demo=True)
    with pytest.raises(ValueError):
        jobs.prepare_suite(s.store, result.run_id, models=("qwen-fixture",), allow_demo=True)


def test_corrupt_usage_is_visible_and_copies_do_not_double_count(api):
    s, *_ = api
    prepared = prepare(api, limit=1)
    report = jobs.run_suite(s.store, prepared["suite_id"])
    folder = s.store.artifact_dir(report["suite_id"])
    assert s.research_usage()["reported_tokens"] == 18
    state = json.loads((folder/"state.json").read_text())
    state["results"]["deepseek-fixture"][0]["card"]["input_tokens"] = 99
    jobs._json(folder/"state.json",state)
    usage = s.research_usage()
    assert usage["reported_tokens"] == 0 and usage["evaluation_usage_issues"]


def test_cli_read_only_modes_cannot_override_saved_budgets(api, capsys):
    s, *_ = api
    suite = prepare(api)
    assert main(["--data-dir",str(s.store.root),"evaluate","--status",suite["suite_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["provider"] == "deepseek"
    for mode in ("--status", "--resume", "--review-template"):
        assert main(["--data-dir",str(s.store.root),"evaluate",mode,suite["suite_id"],"--request-limit","90"]) == 2


def test_qwen_and_deepseek_usage_is_not_mixed(api):
    s, *_ = api
    for provider in ("qwen", "deepseek"):
        jobs.run_suite(s.store, prepare(api,provider,limit=1)["suite_id"])
    assert s.research_usage("qwen")["reported_tokens"] == s.research_usage("deepseek")["reported_tokens"] == 18
    assert s.research_usage("all")["reported_tokens"] == 36


def test_cli_explicit_qwen_launch_routes_frozen_budget_to_api_suite(api, monkeypatch, capsys):
    s, result, calls, *_ = api
    original = jobs.prepare_suite
    def prepare_fixture(*a, **kw):
        return original(*a, **kw, allow_demo=True)
    monkeypatch.setattr(jobs, "prepare_suite", prepare_fixture)
    assert main(["--data-dir",str(s.store.root),"evaluate","--run",result.run_id,"--provider","qwen",
        "--models","qwen-fixture","--limit","2","--request-limit","1","--reported-token-stop","120000"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "budget_stopped" and report["provider"] == "qwen"
    assert report["budget"]["request_limit"] == 1 and len(calls) == 1


def test_success_after_unreported_api_failure_still_marks_usage_unknown(api, monkeypatch):
    from finsight.research import ChatAdapter, research_candidate

    s, result, *_ = api
    facts = evidence_for(result, result.candidates[0])
    response = demo_content(result.candidates[0], facts).model_dump()
    attempts = []
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, json={"error":{"code":"temporary"}})
        return httpx.Response(200, json={"choices":[{"message":{"content":json.dumps(response)},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":11,"completion_tokens":7}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(jobs,"ChatAdapter",lambda provider,model,base_url: ChatAdapter(provider,model,client,base_url))
        monkeypatch.setattr(jobs,"research_candidate",research_candidate)
        report = jobs.run_suite(s.store,prepare(api,"qwen",limit=1)["suite_id"])
    assert report["models"]["qwen-fixture"]["completed"] == 1 and report["budget"]["unknown_usage_cases"] == 1
    assert len(attempts) == 2 and s.research_usage("qwen")["evaluation_usage_unknown_cases"] == 1
