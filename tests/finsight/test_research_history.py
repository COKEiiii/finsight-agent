import json
from datetime import date
from pathlib import Path

import pytest

from finsight.models import ResearchCard
from finsight.research import FIELD_PROMPT_VERSION, ChatAdapter, demo_content, evidence_for
from finsight.research_facts import render_content
from finsight.research_history import report_group
from finsight.service import FinSight


@pytest.fixture
def history(tmp_path, monkeypatch):
    s = FinSight(tmp_path)
    snapshot = s.sync("demo", date(2025, 2, 28), date(2024, 1, 2))["snapshot_id"]
    result = s.screen(snapshot)
    monkeypatch.setattr(ChatAdapter, "complete", lambda *a, **kw: pytest.fail("history must not request a model"))
    return s, result


def save(s, result, *, limit=3, provider="deepseek", model="history-fixture", version=FIELD_PROMPT_VERSION, status="completed"):
    cards = []
    for c in result.candidates[:limit]:
        facts = evidence_for(result, c)
        template = demo_content(c, facts)
        cards.append(ResearchCard(run_id=result.run_id, symbol=c["symbol"], model=model, prompt_version=version,
            status=status, content=render_content(template, facts) if status == "completed" else None,
            facts=facts, evidence=facts, content_template=template, error="fixture timeout" if status == "failed" else None))
    run = s.store.save_run("research", result.snapshot_id, s.config, {"screen_run_id": result.run_id,
        "provider": provider, "model": model, "cards": [c.model_dump(mode="json") for c in cards]})
    return run


def test_partial_same_version_batches_merge_by_rank_and_keep_provenance(history):
    s, result = history
    first = save(s, result, limit=3)
    latest = save(s, result, limit=1)
    view = s.saved_research(result.run_id)
    assert view["model_calls"] == 0 and not view["rejected"]
    group = view["groups"][0]
    assert group["saved_symbols"] == 3 and group["candidate_count"] == 10
    assert [r["card"]["symbol"] for r in group["cards"]] == [c["symbol"] for c in result.candidates[:3]]
    assert [r["research_run_id"] for r in group["cards"]] == [latest, first, first]


def test_latest_failure_stays_failed_and_exposes_previous_success_separately(history):
    s, result = history
    first = save(s, result)
    latest = save(s, result, limit=1, status="failed")
    row = s.saved_research(result.run_id)["groups"][0]["cards"][0]
    assert row["card"]["status"] == "failed" and row["research_run_id"] == latest
    assert row["previous_completed"]["card"]["status"] == "completed"
    assert row["previous_completed"]["research_run_id"] == first


def test_models_providers_and_prompt_versions_are_separate_groups(history):
    s, result = history
    for kwargs in ({}, {"provider": "ollama"}, {"model": "another"}, {"version": "old"}):
        save(s, result, **kwargs)
    groups = s.saved_research(result.run_id)["groups"]
    assert len(groups) == 4 and len({g["group_id"] for g in groups}) == 4
    assert all(g["saved_symbols"] == 3 for g in groups)


@pytest.mark.parametrize("change", ["stock", "signal", "unit", "value", "date", "snapshot", "config", "model", "duplicates", "content"])
def test_wrong_bindings_and_corrupt_batches_are_not_displayed(history, change):
    s, result = history
    run_id = save(s, result, limit=1)
    raw = s.store.run(run_id)["payload"]
    card = raw["cards"][0]
    if change == "stock":
        card["symbol"] = "600999.SH"
    elif change == "signal":
        card["run_id"] = "other-screen"
    elif change in {"unit", "value", "date"}:
        card["evidence"]["financials.roe"][{"unit": "unit", "value": "value", "date": "date"}[change]] = "wrong"
    elif change == "model":
        raw["model"] = "other-model"
    elif change == "duplicates":
        raw["cards"].append(card)
    elif change == "content":
        card["content"] = None
    with s.store.connect() as db:
        db.execute("UPDATE runs SET payload=?,snapshot_id=?,config=? WHERE run_id=?", (json.dumps(raw),
            "wrong" if change == "snapshot" else result.snapshot_id,
            s.config.model_copy(update={"version": "wrong"}).model_dump_json() if change == "config" else s.config.model_dump_json(),run_id))
    view = s.saved_research(result.run_id)
    assert not view["groups"] and len(view["rejected"]) == 1


def test_other_screens_and_experiment_records_are_never_merged(history):
    s, result = history
    run = save(s, result)
    another = s.screen(result.snapshot_id)
    assert not s.saved_research(another.run_id)["groups"]
    with s.store.connect() as db:
        db.execute("UPDATE runs SET kind='research_experiment' WHERE run_id=?", (run,))
    assert not s.saved_research(result.run_id)["groups"]


def test_legacy_batch_without_new_metadata_remains_readable(history):
    s, result = history
    run = save(s, result)
    payload = s.store.run(run)["payload"]
    payload.pop("model")
    with s.store.connect() as db:
        db.execute("UPDATE runs SET payload=? WHERE run_id=?", (json.dumps(payload), run))
    assert s.saved_research(result.run_id)["groups"][0]["saved_symbols"] == 3


def test_invalid_batch_json_shape_is_reported_without_crashing(history):
    s, result = history
    run = save(s, result)
    for text in ("[]", "{broken"):
        with s.store.connect() as db:
            db.execute("UPDATE runs SET payload=? WHERE run_id=?", (text,run))
        assert s.saved_research(result.run_id)["rejected"]


def test_demo_service_partial_generation_does_not_hide_other_cards(history):
    s, result = history
    s.research(result.run_id, "demo", limit=3)
    s.research(result.run_id, "demo", limit=1)
    group = s.saved_research(result.run_id)["groups"][0]
    assert group["saved_symbols"] == 3
    assert group["cards"][0]["card"]["cache_hit"]
    assert s.research_usage("demo")["reported_tokens"] == 0


def test_dashboard_can_choose_saved_model_versions_and_keep_partial_cards(history, monkeypatch):
    from streamlit.testing.v1 import AppTest

    s, result = history
    save(s, result)
    save(s, result, limit=1, status="failed")
    save(s, result, model="another", limit=2)
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(s.store.root))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2]/"src/finsight/dashboard.py"), default_timeout=30).run()
    assert not app.exception
    selector = next(item for item in app.selectbox if item.label == "查看已保存报告")
    selector.select(report_group("deepseek", "history-fixture", FIELD_PROMPT_VERSION)).run()
    assert not app.exception
    assert any("已保存 3/10" in item.value for item in app.caption)
    assert any(item.label == "上一次成功卡（历史，最新生成仍为失败）" for item in app.expander)
    before = len(s.store.runs("research"))
    app.run()
    assert not app.exception and len(s.store.runs("research")) == before
