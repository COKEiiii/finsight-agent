from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from finsight.cli import main
from finsight.jobs import Jobs
from finsight.models import StrategyConfig
from finsight.service import FinSight


def saved_job(service, bundle, config=None):
    config = config or service.config
    service._save_bundle(bundle)
    job = Jobs(service.store).create({"strategy": config.model_dump(), "case": "gaps"})
    Jobs(service.store).update(job["job_id"], status="completed", snapshot_id=bundle.meta.snapshot_id)
    return job["job_id"]


def test_gap_free_snapshot_exports_empty_csv_without_requests(bundle, tmp_path, capsys):
    service = FinSight(tmp_path)
    job_id = saved_job(service, bundle)
    assert main(["--data-dir", str(tmp_path), "jobs", "gaps", job_id]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["coverage"]["missing_count"] == 0 and result["invalid_bar_count"] == 0
    folder = Path(result["exports"])
    report = json.loads((folder / "data_gaps.json").read_text())
    assert report["details"] == []
    assert report["synthetic"] is True and not report["historical_validation_passed"]
    assert pd.read_csv(folder / "invalid_bars.csv").empty
    assert "数据缺口 0 只" in (folder / "data_gaps.md").read_text()
    assert "离线演示" in (folder / "data_gaps.md").read_text()
    assert Jobs(service.store).get(job_id)["requests"]["network_attempts"] == 0


def test_historical_halt_missing_turnover_is_not_imputed_or_hidden(bundle, tmp_path):
    service = FinSight(tmp_path)
    symbol = bundle["universe"].iloc[0]["symbol"]
    historical = bundle["prices"].loc[bundle["prices"]["symbol"] == symbol].iloc[-5].name
    day = bundle["prices"].loc[historical, "trade_date"]
    bundle.tables["prices"].loc[historical, ["amount", "volume"]] = np.nan
    bundle.tables["prices"].loc[historical, "suspended"] = True
    bundle.tables["valuations"].loc[bundle["valuations"]["symbol"] == symbol, "pe_ttm"] = -2.
    job_id = saved_job(service, bundle)
    before = service.store.load_snapshot(bundle.meta.snapshot_id)
    with service.store.connect() as db:
        counts = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("publications", "job_requests")}
    report = service.collection_gaps(job_id)
    assert report["coverage"]["missing_count"] == 1
    assert report["invalid_bar_count"] == report["invalid_bars_with_confirmed_suspension"] == 1
    detail = report["details"][0]
    assert detail["symbol"] == symbol and detail["category"] == "data"
    assert {"invalid_price", "amount_missing", "nonpositive_valuation"} <= set(detail["codes"])
    bar = detail["invalid_bars"][0]
    assert bar["trade_date"] == day and bar["amount"] is None and bar["volume"] is None
    assert bar["suspended"] is True and set(bar["missing_fields"]) == {"amount", "volume"}
    assert detail["current_bar"][0]["suspended"] is False
    assert report["snapshot_table_hashes"] == before.meta.table_hashes
    after = service.store.load_snapshot(bundle.meta.snapshot_id)
    assert before.meta == after.meta
    pd.testing.assert_frame_equal(before["prices"], after["prices"])
    with service.store.connect() as db:
        assert counts == {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counts}
    assert service.collection_gaps(job_id)["details"] == report["details"]


def test_confirmed_current_suspension_is_separate_from_data_gaps(bundle, tmp_path):
    service = FinSight(tmp_path)
    index = bundle["prices"].index[-1]
    bundle.tables["prices"].loc[index, ["amount", "volume"]] = np.nan
    bundle.tables["prices"].loc[index, "suspended"] = True
    report = service.collection_gaps(saved_job(service, bundle))
    assert report["coverage"]["confirmed_exclusions"] == 1
    assert report["coverage"]["missing_count"] == report["invalid_bar_count"] == 0


def test_financial_evidence_preserves_missing_weighted_roe_and_excludes_future(bundle, tmp_path):
    service = FinSight(tmp_path)
    symbol = bundle["universe"].iloc[0]["symbol"]
    fin = bundle.tables["financials"]
    fin.loc[fin["symbol"] == symbol, "roe"] = np.nan
    future = fin.loc[fin["symbol"] == symbol].iloc[-1].copy()
    future["report_period"] = "2024-12-31"
    future["announcement_date"], future["available_on"], future["roe"] = "2025-03-20", "2025-03-21", 99.
    bundle.tables["financials"] = pd.concat([fin, future.to_frame().T], ignore_index=True)
    report = service.collection_gaps(saved_job(service, bundle))
    detail = report["details"][0]
    assert "financial_value_missing" in detail["codes"]
    evidence = detail["latest_available_annual_records"]
    assert evidence and all(row["roe"] is None for row in evidence)
    assert all(row["announcement_date"] < str(bundle.meta.as_of) for row in evidence)


def test_gap_diagnostic_uses_exact_exchange_calendar_not_calendar_days(bundle, tmp_path):
    service = FinSight(tmp_path)
    symbol = bundle["universe"].iloc[0]["symbol"]
    sessions = sorted(bundle["calendar"].loc[bundle["calendar"]["trade_date"] <= str(bundle.meta.as_of), "trade_date"])[-61:]
    prices = bundle["prices"]
    # Outside-window corruption cannot explain a current selection failure.
    outside = prices.index[(prices["symbol"] == symbol) & (prices["trade_date"] < sessions[0])][-1]
    bundle.tables["prices"].loc[outside, "amount"] = np.nan
    absent = sessions[-10]
    bundle.tables["prices"] = prices.loc[~((prices["symbol"] == symbol) & (prices["trade_date"] == absent))]
    report = service.collection_gaps(saved_job(service, bundle))
    assert report["window"] == {"first_session": sessions[0], "last_session": sessions[-1], "sessions": 61}
    assert report["details"][0]["missing_sessions"] == [absent]
    assert report["invalid_bar_count"] == 0


def test_job_strategy_is_used_instead_of_current_cli_configuration(bundle, tmp_path):
    service = FinSight(tmp_path)
    config = StrategyConfig(version="experiment_high_liquidity", min_amount_20=1e15)
    job_id = saved_job(service, bundle, config)
    report = service.collection_gaps(job_id)
    assert report["config_id"] == config.config_id
    assert report["coverage"]["eligible"] == 0 and report["coverage"]["missing_count"] == 0


def test_unfinished_job_has_no_snapshot_and_does_not_start_collection(tmp_path):
    service = FinSight(tmp_path)
    job = Jobs(service.store).create({"case": "not-started"})
    with pytest.raises(ValueError, match="no saved snapshot"):
        service.collection_gaps(job["job_id"])
    assert Jobs(service.store).get(job["job_id"])["status"] == "queued"


def test_snapshot_integrity_failure_blocks_gap_report(bundle, tmp_path):
    service = FinSight(tmp_path)
    job_id = saved_job(service, bundle)
    (service.store.root / "snapshots" / bundle.meta.snapshot_id / "prices.parquet").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        service.collection_gaps(job_id)
