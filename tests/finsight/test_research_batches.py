import fcntl
import json
from datetime import date
from pathlib import Path

import pytest

import finsight.research_batches as batches
from finsight.cli import main
from finsight.models import ResearchCard
from finsight.research import FIELD_PROMPT_VERSION, demo_content, evidence_for
from finsight.research_facts import render_content
from finsight.service import FinSight


@pytest.fixture
def source(tmp_path):
    s = FinSight(tmp_path)
    snapshot = s.sync("demo", date(2025, 2, 28), date(2024, 1, 2))["snapshot_id"]
    return s, s.screen(snapshot)


def saved(s, result, count=3, *, provider="deepseek", model="fixture", version=FIELD_PROMPT_VERSION, status="completed"):
    cards = []
    for c in result.candidates[:count]:
        facts = evidence_for(result, c)
        template = demo_content(c, facts)
        cards.append(ResearchCard(run_id=result.run_id, symbol=c["symbol"], model=model, prompt_version=version,
            status=status, content=render_content(template, facts) if status == "completed" else None, content_template=template,
            facts=facts, evidence=facts).model_dump(mode="json"))
    s.store.save_run("research", result.snapshot_id, s.config, {"screen_run_id": result.run_id, "provider": provider, "cards": cards})


def test_missing_plan_skips_valid_successes_and_applies_limit_after_filtering(source, monkeypatch):
    s, result = source
    saved(s, result)
    monkeypatch.setattr(batches, "ChatAdapter", lambda *a, **kw: pytest.fail("planning must not initialize a provider"))
    plan = s.research_plan(result.run_id, "deepseek", "fixture", 2, missing_only=True)
    assert plan["symbols"] == [c["symbol"] for c in result.candidates[3:5]]
    assert plan["skipped_successful_symbols"] == [c["symbol"] for c in result.candidates[:3]]
    assert plan["maximum_model_requests"] == 6 and plan["model_calls"] == 0
    assert plan["remaining_symbols"] == [c["symbol"] for c in result.candidates[5:]]
    assert not s.store.runs("research_batch")


@pytest.mark.parametrize("different", ["provider", "model", "prompt", "latest_failure"])
def test_other_groups_or_prior_success_before_failure_do_not_hide_missing_cards(source, different):
    s, result = source
    saved(s, result, provider="ollama" if different == "provider" else "deepseek",
        model="another" if different == "model" else "fixture", version="old" if different == "prompt" else FIELD_PROMPT_VERSION)
    if different == "latest_failure":
        saved(s, result, count=1, status="failed")
    plan = s.research_plan(result.run_id, "deepseek", "fixture", 1, missing_only=True)
    assert plan["symbols"] == [result.candidates[0]["symbol"]]


def test_fully_saved_selection_needs_no_adapter_lock_or_empty_record(source, monkeypatch):
    s, result = source
    saved(s, result, count=10)
    monkeypatch.setattr(batches, "ChatAdapter", lambda *a, **kw: pytest.fail("nothing missing must not construct a provider"))
    assert s.research(result.run_id, "deepseek", "fixture", missing_only=True) == []
    assert not s.store.runs("research_batch") and not (s.store.root / ".research.lock").exists()


def test_explicit_symbols_are_normalized_and_ordered_by_source_rank(source):
    s, result = source
    first, third = result.candidates[0]["symbol"], result.candidates[2]["symbol"]
    plan = s.research_plan(result.run_id, "demo", symbols=[third.lower(), first[:6]])
    assert plan["symbols"] == [first, third]
    assert plan["maximum_model_requests"] == 0
    assert s.research_plan(result.run_id, "demo", limit=2)["symbols"] == [c["symbol"] for c in result.candidates[:2]]


@pytest.mark.parametrize("symbols", [[], ["999999.SZ"], ["600000.SZ"], ["bad"], ["600000", "600000.SH"]])
def test_invalid_selection_fails_before_any_request_or_batch(source, monkeypatch, symbols):
    s, result = source
    monkeypatch.setattr(batches, "ChatAdapter", lambda *a, **kw: pytest.fail("invalid input must not construct a provider"))
    with pytest.raises(ValueError):
        s.research(result.run_id, "deepseek", "fixture", symbols=symbols)
    assert not s.store.runs("research_batch") and not s.store.runs("research")


def test_research_lock_prevents_a_second_request_stream(source, monkeypatch):
    s, result = source
    monkeypatch.setattr(batches, "ChatAdapter", lambda *a, **kw: pytest.fail("lock failure must not construct a provider"))
    with (s.store.root / ".research.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="already running"):
            s.research(result.run_id, "deepseek", "fixture")
    assert not s.store.runs("research_batch")


def test_each_card_is_saved_before_the_next_model_request_and_interrupt_preserves_it(source, monkeypatch):
    s, result = source
    original = batches.research_candidate
    calls = []
    def generate(result, candidate, store, adapter, **kwargs):
        calls.append(candidate["symbol"])
        if len(calls) == 2:
            assert len(store.runs("research")) == 1
            raise KeyboardInterrupt()
        return original(result, candidate, store, adapter, **kwargs)
    monkeypatch.setattr(batches, "research_candidate", generate)
    with pytest.raises(KeyboardInterrupt):
        s.research(result.run_id, "demo", limit=3)
    assert len(s.store.runs("research")) == 1
    parent = s.store.runs("research_batch")[0]
    folder = s.store.artifact_dir(parent["run_id"])
    manifest = json.loads((folder / "batch.json").read_text())
    assert manifest["status"] == "interrupted" and manifest["error"] == "KeyboardInterrupt"
    assert manifest["completed_symbols"] == [result.candidates[0]["symbol"]]
    assert manifest["active_symbol"] == result.candidates[1]["symbol"] and manifest["pid"] is None
    assert len(json.loads((folder / "research.json").read_text())["cards"]) == 1
    assert s.saved_research(result.run_id)["groups"][0]["saved_symbols"] == 1
    # A separate explicit invocation can fill missing cards. It is not an automatic resume.
    monkeypatch.setattr(batches, "research_candidate", original)
    cards = s.research(result.run_id, "demo", limit=2, missing_only=True)
    assert [c.symbol for c in cards] == [c["symbol"] for c in result.candidates[1:3]]
    assert len(s.store.runs("research")) == 3


def test_completed_batch_exports_and_usage_count_each_card_once(source):
    s, result = source
    class Adapter:
        provider, model, base_url = "deepseek", "batch-fixture", "http://fixture"
        def complete(self, messages, tools, final=False):
            context = json.loads(messages[1]["content"])["candidate"]
            candidate = next(c for c in result.candidates if c["symbol"] == context["symbol"])
            content = demo_content(candidate, evidence_for(result, candidate)).model_dump()
            return {"message": {"content": json.dumps(content, ensure_ascii=False)}, "input_tokens": 13, "output_tokens": 7}
    cards = s.research(result.run_id, "deepseek", limit=2, adapter=Adapter())
    assert len(cards) == 2 and all(c.status == "completed" for c in cards)
    assert len(s.store.runs("research")) == 2 and len(s.store.runs("research_batch")) == 1
    parent = s.store.runs("research_batch")[0]
    folder = s.store.artifact_dir(parent["run_id"])
    manifest = json.loads((folder / "batch.json").read_text())
    assert manifest["status"] == "completed" and manifest["active_symbol"] is None
    assert manifest["model_calls"] == 2 and manifest["input_tokens"] == 26 and manifest["output_tokens"] == 14
    assert not manifest["in_flight_usage_unknown"] and len(manifest["card_run_ids"]) == 2
    assert s.research_usage("deepseek")["reported_tokens"] == 40
    assert len(json.loads((folder / "research.json").read_text())["cards"]) == 2
    assert (folder / "research.md").exists()


def test_returned_response_file_survives_a_database_publication_failure(source, monkeypatch):
    s, result = source
    save_run = s.store.save_run
    def failing(kind, *args, **kwargs):
        if kind == "research":
            raise OSError("fixture database unavailable")
        return save_run(kind, *args, **kwargs)
    monkeypatch.setattr(s.store, "save_run", failing)
    with pytest.raises(OSError):
        s.research(result.run_id, "demo", limit=1)
    parent = s.store.runs("research_batch")[0]
    manifest = json.loads((s.store.artifact_dir(parent["run_id"]) / "batch.json").read_text())
    response = json.loads((s.store.artifact_dir(manifest["active_card_run_id"]) / "research.json").read_text())
    assert manifest["status"] == "interrupted" and not s.store.runs("research")
    assert response["cards"][0]["symbol"] == result.candidates[0]["symbol"]
    assert not manifest["in_flight_usage_unknown"]


def test_interrupted_provider_usage_is_unknown_and_owned_adapter_is_closed(source, monkeypatch):
    s, result = source
    closed = []
    class Adapter:
        provider, model, base_url = "deepseek", "fixture", "http://fixture"
        def __init__(self, *a, **k):
            pass
        def complete(self, *a, **kw):
            raise KeyboardInterrupt()
        def close(self):
            closed.append(True)
    monkeypatch.setattr(batches, "ChatAdapter", Adapter)
    with pytest.raises(KeyboardInterrupt):
        s.research(result.run_id, "deepseek", "fixture", limit=1)
    parent = s.store.runs("research_batch")[0]
    manifest = json.loads((s.store.artifact_dir(parent["run_id"]) / "batch.json").read_text())
    assert manifest["status"] == "interrupted" and manifest["in_flight_usage_unknown"]
    assert closed == [True] and not s.store.runs("research")


def test_initial_database_failure_never_starts_the_provider(source, monkeypatch):
    s, result = source
    monkeypatch.setattr(batches, "ChatAdapter", lambda *a, **kw: pytest.fail("database failure must not start a provider"))
    def failed(*a, **kw):
        raise OSError("fixture database unavailable")
    monkeypatch.setattr(s.store, "save_run", failed)
    with pytest.raises(OSError):
        s.research(result.run_id, "deepseek", "fixture", limit=1)
    paths = list((s.store.root / "exports").glob("research-batch-*/batch.json"))
    assert len(paths) == 1
    manifest = json.loads(paths[0].read_text())
    assert manifest["status"] == "interrupted" and manifest["model_calls"] == 0
    assert manifest["active_symbol"] is None and not manifest["in_flight_usage_unknown"]


def test_cli_plan_is_offline_and_outputs_real_remaining_request_ceiling(source, monkeypatch, capsys):
    s, result = source
    saved(s, result, count=3)
    monkeypatch.setattr(batches, "ChatAdapter", lambda *a, **kw: pytest.fail("CLI plan must not contact a provider"))
    assert main(["--data-dir", str(s.store.root), "research", "--run", result.run_id, "--provider", "deepseek",
        "--model", "fixture", "--missing-only", "--plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["symbols"]) == 7 and plan["maximum_model_requests"] == 21
    assert not s.store.runs("research_batch")


def test_dashboard_disables_generation_when_current_demo_cards_are_complete(source, monkeypatch):
    from streamlit.testing.v1 import AppTest

    s, result = source
    s.research(result.run_id, "demo", limit=10)
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(s.store.root))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    next(item for item in app.selectbox if item.label == "研究模型来源").select("demo").run()
    assert not app.exception and next(b for b in app.button if b.label == "生成研究卡").disabled
    next(item for item in app.checkbox if item.label == "仅生成缺少的当前版本报告").uncheck().run()
    assert not app.exception and not next(b for b in app.button if b.label == "生成研究卡").disabled


def test_dashboard_replans_after_filling_the_last_missing_card(source, monkeypatch):
    from streamlit.testing.v1 import AppTest

    s, result = source
    s.research(result.run_id, "demo", limit=9)
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(s.store.root))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    next(item for item in app.selectbox if item.label == "研究模型来源").select("demo").run()
    next(button for button in app.button if button.label == "生成研究卡").click().run()
    assert not app.exception and next(button for button in app.button if button.label == "生成研究卡").disabled
    assert any("已保存 10/10" in item.value for item in app.caption)
    assert any("1 张研究卡已保存" in item.value for item in app.success)


def test_dashboard_generation_error_keeps_existing_cards_visible(source, monkeypatch):
    from streamlit.testing.v1 import AppTest

    s, result = source
    s.research(result.run_id, "demo", limit=1)
    def interrupted(*a, **kw):
        raise OSError("fixture interrupted")
    monkeypatch.setattr(FinSight, "research", interrupted)
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(s.store.root))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    next(item for item in app.selectbox if item.label == "研究模型来源").select("demo").run()
    next(button for button in app.button if button.label == "生成研究卡").click().run()
    assert not app.exception and any("已保存卡片和选股结果保留" in item.value for item in app.error)
    assert any("已保存 1/10" in item.value for item in app.caption)
