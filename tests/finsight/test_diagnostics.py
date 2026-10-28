import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from finsight.cli import main
from finsight.diagnostics import diagnose_run
from finsight.factors import evaluate
from finsight.models import FACTORS, StrategyConfig
from finsight.research import ChatAdapter
from finsight.screening import rank_factors
from finsight.service import FinSight


@pytest.fixture
def screened(tmp_path, monkeypatch):
    s = FinSight(tmp_path)
    snapshot = s.sync("demo", date(2025, 2, 28), date(2024, 1, 2))["snapshot_id"]
    result = s.screen(snapshot)
    monkeypatch.setattr(ChatAdapter, "complete", lambda *a, **k: pytest.fail("diagnostics cannot call a model"))
    return s, result


def test_score_contributions_match_saved_ranking_and_use_entire_eligible_pool(screened):
    s, result = screened
    report = s.diagnose(result.run_id)
    assert report["model_calls"] == 0 and report["synthetic"]
    assert report["eligible_count"] == result.eligible_count > len(result.candidates)
    assert report["screen_run_id"] == result.run_id and report["config_id"] == result.config_id
    for row, candidate in zip(report["factor_contributions"], result.candidates):
        assert row["symbol"] == candidate["symbol"] and row["score"] == candidate["score"]
        assert sum(row[f + "_contribution"] for f in FACTORS) == pytest.approx(candidate["score"])
        for f in FACTORS:
            assert row[f + "_contribution"] == candidate[f + "_score"] * s.config.weights[f]
    out = Path(report["exports"])
    assert {p.name for p in out.iterdir()} == {"diagnostics.json", "diagnostics.md", "factor_contributions.csv",
        "industry_exposure.csv", "factor_rank_correlation.csv"}
    assert "不是收益归因" in (out / "diagnostics.md").read_text()
    saved = s.store.run(report["run_id"])
    assert saved["kind"] == "diagnostics" and saved["config"] == s.store.run(result.run_id)["config"]


def test_correlations_are_ranked_with_ties_and_inverse_valuation_direction():
    frame = pd.DataFrame({"symbol": ["600003.SH", "600002.SH", "600001.SH"],
        "roe": [10., 10., 20.], "revenue_growth": [1., 1., 2.], "momentum_60": [.1, .1, .2],
        "ma_ratio": [.1, .1, .2], "pe_ttm": [20., 20., 10.], "pb": [2., 2., 1.]})
    before = frame.copy(deep=True)
    ranked = rank_factors(frame, StrategyConfig())
    pd.testing.assert_frame_equal(frame, before)
    assert list(ranked["symbol"]) == ["600001.SH", "600002.SH", "600003.SH"]
    assert ranked.iloc[1].roe_score == 50 and ranked.iloc[1].pe_ttm_score == 50
    assert ranked.iloc[0].score == 100
    baseline = rank_factors(frame, StrategyConfig(), baseline=True)
    assert list(baseline["score"]) == list(ranked["momentum_60_score"])


def test_correlation_matches_all_eligible_rank_scores_not_top_ten(screened):
    s, result = screened
    report = s.diagnose(result.run_id)
    bundle = s.store.load_snapshot(result.snapshot_id)
    raw, _ = evaluate(bundle, result.as_of, s.config)
    full = rank_factors(raw, s.config)
    expected = full[[f + "_score" for f in FACTORS]].corr()
    for row in report["factor_rank_correlation"]:
        for other in FACTORS:
            assert row[other] == pytest.approx(expected.loc[row["factor"] + "_score", other + "_score"])


def test_industry_counts_and_target_caps_are_reference_only(screened):
    s, result = screened
    report = s.diagnose(result.run_id)
    industries = report["industry_exposure"]
    assert sum(row["eligible_names"] for row in industries) == result.eligible_count
    assert sum(row["candidate_names"] for row in industries) == len(result.candidates)
    assert sum(row["eligible_share"] for row in industries) == pytest.approx(1)
    assert sum(row["candidate_share"] for row in industries) == pytest.approx(1)
    assert max(row["target_names"] for row in industries) <= 3
    assert sum(row["reference_target_weight"] for row in industries) <= .8 + 1e-12
    assert report["reference_target_cash_weight"] >= .2 - 1e-12
    assert not s.store.runs("rebalance") and not s.store.runs("research")
    assert "不是实际行业暴露" in " ".join(report["limitations"])


@pytest.mark.parametrize("change", ["score", "exclusion", "config", "snapshot", "kind"])
def test_diagnostics_rejects_changed_source_binding_before_export(screened, change):
    s, result = screened
    source = s.store.run(result.run_id)
    payload = source["payload"]
    if change == "score":
        payload["candidates"][0]["score"] += 1
    elif change == "exclusion":
        payload["exclusions"].append({"symbol": "600000.SH", "industry": "fixture", "reasons": ["changed reason"],
            "codes": ["changed"], "category": "data"})
    elif change == "config":
        payload["config_id"] = "wrong-config"
    elif change == "snapshot":
        payload["snapshot_id"] = "different-snapshot"
    with s.store.connect() as db:
        db.execute("UPDATE runs SET payload=?,kind=? WHERE run_id=?", (json.dumps(payload), "research" if change == "kind" else "screen", result.run_id))
    with pytest.raises(ValueError):
        s.diagnose(result.run_id)
    assert not s.store.runs("diagnostics")


def test_source_configuration_is_used_instead_of_current_service_config(screened):
    s, result = screened
    s.config = StrategyConfig(version="strategy_shadow", weights={f: 1. if f == "momentum_60" else 0. for f in FACTORS})
    report = s.diagnose(result.run_id)
    assert report["factor_weights"] == StrategyConfig().weights
    assert report["strategy_version"] == result.strategy_version


def test_cli_and_repeated_diagnostics_do_not_change_screening_or_publication(screened, capsys):
    s, result = screened
    before = s.store.run(result.run_id)
    assert main(["--data-dir", str(s.store.root), "diagnose", "--run", result.run_id]) == 0
    report = json.loads(capsys.readouterr().out)
    other = diagnose_run(s.store, result.run_id)
    for key in ("factor_contributions", "factor_rank_correlation", "industry_exposure", "source_fingerprint"):
        assert report[key] == other[key]
    assert s.store.run(result.run_id) == before
    with s.store.connect() as db:
        assert db.execute("SELECT count(*) FROM publications").fetchone()[0] == 0


def test_empty_eligible_pool_outputs_unknown_correlations(tmp_path):
    config = StrategyConfig(version="empty_pool", min_amount_20=1e20)
    s = FinSight(tmp_path, config)
    snapshot = s.sync("demo", date(2025, 2, 28), date(2024, 1, 2))["snapshot_id"]
    result = s.screen(snapshot)
    report = s.diagnose(result.run_id)
    assert report["eligible_count"] == 0 and not report["industry_exposure"]
    assert not report["factor_contributions"] and report["reference_target_cash_weight"] == 1
    assert all(row[f] is None for row in report["factor_rank_correlation"] for f in FACTORS)
    assert not report["high_correlation_pairs"]


def test_constant_factor_correlations_are_unknown(tmp_path):
    from finsight.data.demo import make_demo

    s = FinSight(tmp_path)
    b = make_demo(date(2025, 2, 28), date(2024, 1, 2))
    b.tables["financials"]["roe"] = 10.
    s.store.save_snapshot(b)
    result = s.screen(b.meta.snapshot_id)
    report = s.diagnose(result.run_id)
    roe = next(row for row in report["factor_rank_correlation"] if row["factor"] == "roe")
    assert all(roe[f] is None for f in FACTORS)
    assert all(row["roe"] is None for row in report["factor_rank_correlation"])


def test_dashboard_can_generate_and_read_diagnostics_without_model_calls(screened, monkeypatch):
    from streamlit.testing.v1 import AppTest

    s, result = screened
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(s.store.root))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    next(button for button in app.button if button.label == "生成选股诊断").click().run()
    assert not app.exception and len(s.store.runs("diagnostics")) == 1
    assert any("MA20/MA60" in caption.value for caption in app.caption)
    app.run()
    assert not app.exception and len(s.store.runs("diagnostics")) == 1
    assert not s.store.runs("research") and len(s.store.runs("screen")) == 1
