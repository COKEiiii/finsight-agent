import copy
import json
from datetime import date

import pytest

from finsight.models import StrategyConfig
from finsight.research import RESEARCH_CACHE_VERSION, demo_content, evidence_for, research_candidate
from finsight.service import FinSight


class LocalResponse:
    provider, model, base_url = "ollama", "bound-fixture", "http://fixture"
    def __init__(self, content):
        self.content, self.calls = content, 0
    def complete(self, messages, tools, final=False):
        self.calls += 1
        return {"message": {"content": json.dumps(self.content, ensure_ascii=False)}, "input_tokens": 11, "output_tokens": 7}


@pytest.fixture
def cached(tmp_path):
    s = FinSight(tmp_path)
    snapshot = s.sync("demo", date(2025, 2, 28), date(2024, 1, 2))["snapshot_id"]
    result = s.screen(snapshot)
    c = result.candidates[0]
    adapter = LocalResponse(demo_content(c, evidence_for(result, c)).model_dump())
    first = research_candidate(result, c, s.store, adapter)
    assert first.status == "completed" and adapter.calls == 1
    with s.store.connect() as db:
        row = db.execute("SELECT * FROM research_cache").fetchone()
    return s, result, c, adapter, row["cache_key"], json.loads(row["payload"])


@pytest.mark.parametrize("change", ["stock", "model", "prompt", "status", "fact_value", "unit", "date",
    "provenance", "missing_fact", "extra_fact", "context", "cache_key", "version", "negative_usage", "content", "error", "missing_template"])
def test_damaged_cache_binding_is_regenerated_within_normal_budget(cached, change):
    s, result, c, adapter, key, value = cached
    card = value["card"]
    if change == "stock":
        card["symbol"] = "600999.SH"
    elif change in {"model", "prompt", "status"}:
        card[{"model": "model", "prompt": "prompt_version", "status": "status"}[change]] = {
            "model": "other-model", "prompt": "old-prompt", "status": "failed"}[change]
    elif change == "fact_value":
        card["facts"]["financials.roe"]["value"] += 1
    elif change in {"unit", "date", "provenance"}:
        field = {"unit": "unit", "date": "known_at", "provenance": "source"}[change]
        card["evidence"]["financials.roe"][field] = "changed"
    elif change == "missing_fact":
        card["facts"].pop("financials.roe")
    elif change == "extra_fact":
        card["facts"]["invented.metric"] = {"value": 1}
    elif change == "context":
        value["screen_context"]["config_id"] = "other-config"
    elif change == "cache_key":
        value["cache_key"] = "other-key"
    elif change == "version":
        value["version"] = "legacy"
    elif change == "negative_usage":
        card["input_tokens"] = -100
    elif change == "error":
        card["error"] = "old failure"
    elif change == "missing_template":
        card["content_template"] = None
    else:
        card["content_template"]["reasons"][0] = {"text": "净资产收益率为99999元。", "evidence_ids": ["financials.roe"]}
    s.store.cache(key, value)
    regenerated = research_candidate(result, c, s.store, adapter)
    assert not regenerated.cache_hit and regenerated.status == "completed"
    assert regenerated.llm_calls == 1 and adapter.calls == 2
    assert regenerated.symbol == c["symbol"] and regenerated.model == adapter.model
    assert regenerated.evidence == evidence_for(result, c) == regenerated.facts


@pytest.mark.parametrize("text", ["{broken", "[]", "null", '"text"'])
def test_invalid_json_and_non_object_caches_are_misses(cached, text):
    s, result, c, adapter, key, _ = cached
    with s.store.connect() as db:
        db.execute("UPDATE research_cache SET payload=? WHERE cache_key=?", (text, key))
    assert s.store.cached(key) is None
    card = research_candidate(result, c, s.store, adapter)
    assert card.status == "completed" and not card.cache_hit and adapter.calls == 2


@pytest.mark.parametrize("changed", ["config_id", "strategy_version", "universe_id", "membership_id", "synthetic"])
def test_strategy_and_membership_changes_do_not_reuse_matching_stock_values(cached, changed):
    s, result, c, adapter, _, _ = cached
    another = result.model_copy(update={changed: not result.synthetic if changed == "synthetic" else "different",
        "run_id": "another-run"})
    card = research_candidate(another, c, s.store, adapter)
    assert card.status == "completed" and not card.cache_hit and adapter.calls == 2


def test_same_snapshot_and_configuration_can_rebind_cached_card_to_repeated_screen(cached):
    s, result, c, adapter, key, value = cached
    before = copy.deepcopy(value)
    assert value["version"] == RESEARCH_CACHE_VERSION
    another = result.model_copy(update={"run_id": "repeated-screen"})
    card = research_candidate(another, c, s.store, adapter)
    assert card.cache_hit and card.run_id == another.run_id and adapter.calls == 1
    assert card.llm_calls == card.input_tokens == card.output_tokens == 0
    assert card.generation_usage["input_tokens"] == 11
    assert s.store.cached(key) == before


def test_cloned_strategy_version_with_same_weights_has_an_independent_cache(cached):
    s, result, c, adapter, _, _ = cached
    config = StrategyConfig(version="shadow_strategy")
    another = result.model_copy(update={"run_id": "shadow", "config_id": config.config_id, "strategy_version": config.version})
    assert evidence_for(another, c) == evidence_for(result, c)
    assert not research_candidate(another, c, s.store, adapter).cache_hit
    assert adapter.calls == 2
