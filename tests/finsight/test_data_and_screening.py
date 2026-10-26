from datetime import date

import pandas as pd
import pytest

from finsight.data.quality import audit, missing_sessions
from finsight.models import StrategyConfig
from finsight.screening import screen


def test_snapshot_roundtrip_immutable_and_tamper_detection(bundle, store):
    store.save_snapshot(bundle)
    loaded = store.load_snapshot(bundle.meta.snapshot_id)
    pd.testing.assert_frame_equal(loaded["prices"], bundle["prices"])
    assert loaded.meta.synthetic and not loaded.meta.research_eligible
    with pytest.raises(ValueError, match="immutable"):
        store.save_snapshot(bundle)
    path = store.root / "snapshots" / bundle.meta.snapshot_id / "prices.parquet"
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="integrity"):
        store.load_snapshot(bundle.meta.snapshot_id)


def test_strategy_change_requires_new_version(store, config):
    store.register_strategy(config)
    store.register_strategy(StrategyConfig.model_validate_json(config.model_dump_json()))
    with pytest.raises(ValueError, match="new strategy version"):
        store.register_strategy(config.model_copy(update={"top_n": 9}))
    store.register_strategy(StrategyConfig(version="strategy_v0.2", top_n=9))


def test_repeatability_and_hand_computed_percentiles(bundle, config):
    # Equal trend across all companies gives average percentile (20+1)/(2*20).
    bundle.tables["prices"]["adj_close"] = 10.0
    first, second = screen(bundle, config), screen(bundle, config)
    assert first.candidates == second.candidates
    row = first.candidates[0]
    assert row["momentum_60"] == 0
    assert row["ma_ratio"] == 0
    assert row["momentum_60_score"] == pytest.approx(52.5)
    assert row["score"] == pytest.approx(sum(config.weights[f] * row[f"{f}_score"] for f in config.weights))
    assert row["roe_score"] > row["pe_ttm_score"]  # better quality, more expensive in synthetic fixtures


def test_identical_factors_tie_break_by_code(bundle):
    for col in ["roe", "revenue_growth"]:
        bundle.tables["financials"][col] = 10.0
    for col in ["pe_ttm", "pb"]:
        bundle.tables["valuations"][col] = 10.0
    bundle.tables["prices"]["adj_close"] = 10.0
    result = screen(bundle)
    assert [r["symbol"] for r in result.candidates] == sorted(bundle["universe"]["symbol"])[:10]


@pytest.mark.parametrize("field,value,reason", [
    ("is_st", True, "risk warning"), ("is_delisting", True, "delisting"),
    ("listed_on", "2024-01-01", "two years"), ("industry", None, "industry missing"),
    ("known_on", "2025-03-01", "not known"),
])
def test_universe_filters(bundle, field, value, reason):
    symbol = bundle["universe"].iloc[0]["symbol"]
    bundle.tables["universe"].loc[0, field] = value
    result = screen(bundle)
    excluded = next(e for e in result.exclusions if e["symbol"] == symbol)
    assert any(reason in r for r in excluded["reasons"])


def test_negative_growth_valid_but_negative_valuation_excluded(bundle):
    symbol = bundle["universe"].iloc[0]["symbol"]
    result = screen(bundle)
    assert result.eligible_count == 20  # first synthetic company's growth is -5%, retained
    mask = bundle["valuations"]["symbol"] == symbol
    bundle.tables["valuations"].loc[mask, "pe_ttm"] = -2
    result = screen(bundle)
    assert result.eligible_count == 19
    assert "nonpositive" in next(e for e in result.exclusions if e["symbol"] == symbol)["reasons"][0]


def test_future_and_revised_financials_not_used(bundle):
    symbol = bundle["universe"].iloc[0]["symbol"]
    original = bundle["financials"].query("symbol == @symbol and report_period == '2023-12-31'").iloc[0].copy()
    original["roe"] = 999
    original["available_on"] = "2025-03-03"
    bundle.tables["financials"] = pd.concat([bundle["financials"], pd.DataFrame([original])], ignore_index=True)
    result = screen(bundle, StrategyConfig(top_n=20))
    assert next(c for c in result.candidates if c["symbol"] == symbol)["roe"] != 999
    # With no older available version, exclude rather than borrowing from the future.
    bundle.tables["financials"] = bundle["financials"][
        (bundle["financials"]["symbol"] != symbol) | (bundle["financials"]["available_on"] > "2025-02-28")]
    assert screen(bundle).eligible_count == 19


def test_missing_price_session_and_nan_factor_excluded(bundle):
    symbol = bundle["universe"].iloc[0]["symbol"]
    day = bundle["prices"].query("symbol == @symbol").iloc[-5]["trade_date"]
    bundle.tables["prices"] = bundle["prices"][~((bundle["prices"]["symbol"] == symbol) & (bundle["prices"]["trade_date"] == day))]
    result = screen(bundle)
    assert result.eligible_count == 19
    assert any("missing sessions" in reason for e in result.exclusions for reason in e["reasons"])


def test_batch_failure_blocks_ranking_and_future_cutoff_rejected(bundle):
    bundle.tables["valuations"] = bundle["valuations"].iloc[:0]
    assert not audit(bundle)["publishable"]
    with pytest.raises(ValueError, match="formal ranking"):
        screen(bundle)


def test_asof_cannot_cross_snapshot(bundle):
    with pytest.raises(ValueError, match="cutoff"):
        screen(bundle, as_of=date(2025, 3, 1))


def test_calendar_gaps_remain_visible(bundle):
    gaps = missing_sessions(bundle, {"2025-02-25", "2025-02-28"})
    assert gaps == ["2025-02-26", "2025-02-27"]


def test_first_capture_cannot_publish_a_prior_session_ranking(bundle):
    bundle.tables["universe"]["known_on"] = "2025-03-03"
    assert not audit(bundle)["publishable"]
    with pytest.raises(ValueError, match="formal ranking"):
        screen(bundle)
