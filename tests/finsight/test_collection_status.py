from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from test_v2_pipeline import IncrementalAPI

from finsight.cli import main
from finsight.collection_status import elapsed_seconds, recovery_status, request_profile
from finsight.data.requests import RequestBroker, RequestFailure
from finsight.data.store import Store
from finsight.data.universe import current_members, membership_id, universe_id
from finsight.jobs import Jobs
from finsight.service import FinSight

ZONE = ZoneInfo("Asia/Shanghai")


def test_dead_coordinator_is_observed_as_interrupted_without_rewriting_storage(tmp_path, monkeypatch):
    service = FinSight(tmp_path)
    jobs = Jobs(service.store)
    job = jobs.create({"case": "lost-database-at-exit"})
    jobs.update(job["job_id"], status="running", pid=999999999,
                progress={"symbols_completed": 734, "symbols_total": 3194})
    with service.store.connect() as db:
        db.execute("UPDATE jobs SET created_at=?,updated_at=? WHERE job_id=?",
            ("2025-03-03T09:00:00+08:00", "2025-03-03T09:30:00+08:00", job["job_id"]))
    monkeypatch.setattr(Jobs, "alive", staticmethod(lambda pid: False))
    observed = jobs.get(job["job_id"])
    assert observed["status"] == "running" and observed["observed_status"] == "interrupted"
    assert observed["interrupted"] and not observed["process_alive"]
    assert elapsed_seconds(observed, datetime(2025,3,4,20,tzinfo=ZONE)) == 1800
    report = service.collection_report(job["job_id"])
    assert report["status"] == "interrupted" and report["stored_status"] == "running"
    assert report["recovery"]["can_resume"] and report["recovery"]["reason"] == "coordinator_exited"
    assert jobs.resume(job["job_id"])["observed_status"] == "queued"
    assert jobs.get(job["job_id"])["progress"]["symbols_completed"] == 734


def test_live_coordinator_and_unstarted_queue_are_not_restarted(store):
    jobs = Jobs(store)
    job = jobs.create({"case": "queued"})
    assert job["observed_status"] == "queued" and not job["interrupted"]
    jobs.update(job["job_id"], status="running", pid=os.getpid())
    observed = jobs.get(job["job_id"])
    assert observed["observed_status"] == "running" and observed["process_alive"]
    assert jobs.resume(job["job_id"])["pid"] == os.getpid()
    jobs.update(job["job_id"], pid=None)
    assert jobs.get(job["job_id"])["observed_status"] == "interrupted"


def test_dashboard_labels_a_dead_job_as_interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(tmp_path))
    service = FinSight(tmp_path)
    service.sync("demo", date(2025,2,28), date(2024,1,2))
    jobs = Jobs(service.store)
    job = jobs.create({"universe_id": "fixture-universe", "as_of": "2025-02-28"})
    jobs.update(job["job_id"], status="running", pid=999999999)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    assert not app.exception
    choices = next(box for box in app.selectbox if box.label == "采集任务").options
    assert any("已中断" in label for label in choices)
    assert any("采集进程已停止" in warning.value for warning in app.warning)
    assert jobs.get(job["job_id"])["status"] == "running"


def finalized_before_cutoff(service, bundle):
    bundle.meta.synthetic = False
    bundle.meta.source = "akshare"
    bundle.meta.schema_version = 2
    bundle.meta.universe_complete = True
    bundle.meta.universe_id = universe_id(True)
    bundle.meta.membership_symbols = current_members(bundle["universe"])
    bundle.meta.membership_id = membership_id(bundle.meta.membership_symbols)
    bundle.meta.captured_at = datetime(2025, 3, 3, 9, tzinfo=ZONE)
    bundle.tables["universe"]["known_on"] = "2025-03-03"
    bundle.tables["calendar"] = pd.concat([bundle["calendar"], pd.DataFrame({"trade_date": ["2025-03-03"]})], ignore_index=True)
    service._save_bundle(bundle)
    jobs = Jobs(service.store)
    job = jobs.create({"mainboard": True, "universe_id": universe_id(True)})
    jobs.update(job["job_id"], status="partial", snapshot_id=bundle.meta.snapshot_id,
                progress={"phase": "done", "symbols_total": 20, "symbols_completed": 20})
    return jobs.get(job["job_id"])


def test_finished_job_elapsed_time_does_not_keep_increasing():
    job = {"status": "partial", "created_at": "2025-03-03T09:00:00+08:00", "updated_at": "2025-03-03T12:00:00+08:00"}
    assert elapsed_seconds(job, datetime(2025, 3, 4, 20, tzinfo=ZONE)) == 3 * 3600
    job["progress"] = {"finished_at": job["updated_at"]}
    job["updated_at"] = "2025-03-04T10:00:00+08:00"  # A later report-link update is not runtime.
    assert elapsed_seconds(job, datetime(2025, 3, 4, 20, tzinfo=ZONE)) == 3 * 3600
    job["status"] = "running"
    assert elapsed_seconds(job, datetime(2025, 3, 3, 13, tzinfo=ZONE)) == 4 * 3600


def test_future_state_needs_new_incremental_job_without_backdating(bundle, tmp_path):
    service = FinSight(tmp_path)
    job = finalized_before_cutoff(service, bundle)
    original = service.store.load_snapshot(bundle.meta.snapshot_id)["universe"].copy()
    report = service.collection_report(job["job_id"], export=True)
    assert report["processing_complete"] and report["symbols_processed"] == 20
    assert report["publication"]["status"] == "blocked" and report["publication"]["coverage"] == 0
    assert report["publication"]["missing_reason_counts"]["unknown_state"] == 20
    assert report["recovery"]["next_session"] == "2025-03-03"
    assert report["recovery"]["earliest_capture_at"] == "2025-03-03T18:00:00+08:00"
    with pytest.raises(ValueError, match="不能回填"):
        Jobs(service.store).resume(job["job_id"])
    assert Jobs(service.store).get(job["job_id"])["status"] == "partial"
    new = Jobs(service.store).create(job["config"])
    assert new["job_id"] != job["job_id"]
    pd.testing.assert_frame_equal(original, service.store.load_snapshot(bundle.meta.snapshot_id)["universe"])
    folder = Path(report["exports"])
    assert all((folder / file).exists() for file in ("collection_report.json", "collection_report.md", "requests.csv"))


def test_finalized_source_failure_remains_resumable(bundle, tmp_path):
    class Offline:
        def echo(self):
            raise ValueError("fixture unavailable")

    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    jobs = Jobs(service.store)
    job = jobs.create({"case": "partial-source"})
    broker = RequestBroker(service.store, job["job_id"], api=Offline(), retries=0, interval=0)
    with pytest.raises(RequestFailure):
        broker.call("echo")
    jobs.update(job["job_id"], status="partial", snapshot_id=bundle.meta.snapshot_id)
    assert recovery_status(service.store, jobs.get(job["job_id"]))["can_resume"]
    assert jobs.resume(job["job_id"])["status"] == "queued"


def test_returned_rows_force_refresh_and_cache_preserve_original_observation(store):
    class API:
        calls = 0
        def echo(self):
            self.calls += 1
            return pd.DataFrame({"value": list(range(self.calls))})

    api = API()
    instant = [datetime(2025, 6, 2, 9, tzinfo=ZONE)]
    job = Jobs(store).create({"case": "row-counts"})
    broker = RequestBroker(store, job["job_id"], api=api, interval=0, clock=lambda: instant[0])
    first = broker.call("echo")
    with store.connect() as db:
        first_path = db.execute("SELECT path FROM job_requests").fetchone()[0]
    instant[0] += timedelta(hours=1)
    cached = broker.call("echo")
    assert cached.attrs["fetched_at"] == first.attrs["fetched_at"] and api.calls == 1
    assert request_profile(store, job["job_id"])[0]["response_rows"] == 1
    instant[0] += timedelta(hours=8)
    refreshed = broker.call("echo", force_refresh=True)
    assert api.calls == 2 and len(refreshed) == 2 and refreshed.attrs["fetched_at"] != first.attrs["fetched_at"]
    assert len(pd.read_parquet(store.root / first_path)) == 1
    row = request_profile(store, job["job_id"])[0]
    assert row["response_rows"] == 2 and row["row_count_measurements"] == 1 and row["network_attempts"] == 2


def test_old_metrics_migrate_with_unknown_row_count(tmp_path):
    with sqlite3.connect(tmp_path / "finsight.sqlite") as db:
        db.execute("CREATE TABLE job_requests (job_id TEXT,request_id TEXT,method TEXT,kwargs TEXT,status TEXT,path TEXT,"
                   "fetched_at TEXT,attempts INTEGER DEFAULT 0,elapsed REAL DEFAULT 0,bytes INTEGER DEFAULT 0,"
                   "cache_hit INTEGER DEFAULT 0,error_kind TEXT,error TEXT,PRIMARY KEY(job_id,request_id))")
        db.execute("INSERT INTO job_requests(job_id,request_id,method,kwargs,status) VALUES ('legacy','one','old','{}','done')")
    store = Store(tmp_path)
    row = request_profile(store, "legacy")[0]
    assert row["response_rows"] is None and row["row_count_measurements"] == 0 and row["completed"] == 1
    with store.connect() as db:
        assert db.execute("SELECT version FROM schema_migrations WHERE version=5").fetchone()


def test_morning_bootstrap_reused_after_close_with_fresh_valuation(tmp_path):
    class API(IncrementalAPI):
        valuation_calls = 0
        available_day = "2025-05-30"
        def stock_value_em(self, symbol):
            self.valuation_calls += 1
            return pd.DataFrame({"数据日期": pd.bdate_range("2024-01-02", self.available_day), "PE(TTM)": 12., "市净率": 1.5})

    service = FinSight(tmp_path)
    api = API()
    morning = datetime(2025, 6, 2, 9, tzinfo=ZONE)
    first = service.sync("akshare", date(2025, 5, 30), date(2024, 1, 2), symbols=["600036"],
                         api=api, clock=lambda: morning, interval=0, retries=0)
    assert not first["publishable"]
    before = len(Jobs(service.store).list())
    with pytest.raises(ValueError, match="不能回填"):
        service.sync("akshare", date(2025, 5, 30), symbols=["600036"],
                     api=api, clock=lambda: morning, interval=0, retries=0)
    assert len(Jobs(service.store).list()) == before and api.valuation_calls == 1
    api.price_ranges.clear()
    api.available_day = "2025-06-02"
    evening = morning.replace(hour=18, minute=15)
    second = service.sync("akshare", evening.date(), date(2024, 1, 2), symbols=["600036"],
                          api=api, clock=lambda: evening, interval=0, retries=0)
    assert second["publishable"] and api.valuation_calls == 2
    assert api.price_ranges and all(lower > "20240102" for _, lower, _ in api.price_ranges)
    latest = service.store.load_snapshot(second["snapshot_id"])
    assert latest["prices"]["trade_date"].max() == "2025-06-02"
    assert set(latest["financials"]["available_on"]) == {"2025-06-02"}
    assert set(latest["universe"]["known_on"]) == {"2025-06-02"}
    assert not latest.meta.research_eligible
    assert Path(second["collection_report_exports"], "collection_report.json").exists()
    report = service.store.run(second["collection_report_run_id"])["payload"]
    assert sum(row["observed_network_requests"] for row in report["methods"]) == 4
    assert all(row["response_rows"] is not None for row in report["methods"])


def test_cli_collection_profile_exports_without_network_or_model(tmp_path, capsys):
    service = FinSight(tmp_path)
    job = Jobs(service.store).create({"case": "profile"})
    assert main(["--data-dir", str(tmp_path), "jobs", "profile", job["job_id"]]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == job["job_id"] and payload["methods"] == []
    assert Path(payload["exports"], "collection_report.md").exists()


def test_dashboard_distinguishes_completed_capture_from_blocked_publication(bundle, tmp_path, monkeypatch):
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(tmp_path))
    service = FinSight(tmp_path)
    finalized_before_cutoff(service, bundle)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    assert not app.exception
    assert any("全主板股票处理已完成" in item.value for item in app.success)
    assert any("不能回填" in item.value for item in app.info)
    assert not any(button.label == "恢复该任务" for button in app.button)
    assert any(button.label == "新建增量采集" for button in app.button)
    assert app.metric[3].value == "阻止发布"
    assert not service.store.runs("collection_report")  # Page refresh stays read-only.
