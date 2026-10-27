from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from finsight.cli import check_daily
from finsight.data.universe import universe_id
from finsight.jobs import Jobs
from finsight.models import MarketSnapshot
from finsight.scheduler import job_definition
from finsight.service import FinSight


def test_scheduler_skips_completed_snapshot_and_never_calls_model(monkeypatch):
    class Provider:
        def __init__(self, root):
            pass
        def calendar(self):
            return pd.DataFrame({"trade_date": ["2025-05-30", "2025-06-02", "2025-06-03"]})
    class Store:
        root = "unused"
        def snapshots(self):
            return [MarketSnapshot(snapshot_id="test", as_of=date(2025, 5, 30),
                captured_at=datetime(2025, 5, 30, 18, tzinfo=ZoneInfo("Asia/Shanghai")), source="akshare",
                universe_scope="sample:20 current mainboard stocks")]
    class Service:
        store = Store()
        def sync(self, *args, **kwargs):
            pytest.fail("already captured session must not be recollected")
    monkeypatch.setattr("finsight.data.akshare_provider.AKShareProvider", Provider)
    now = datetime(2025, 6, 2, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert check_daily(Service(), "sample", now)["status"] == "up_to_date"


def test_stale_exchange_calendar_is_an_error(monkeypatch):
    class Provider:
        def __init__(self, root):
            pass
        def calendar(self):
            return pd.DataFrame({"trade_date": ["2024-12-31"]})
    class Store:
        root = "unused"
    class Service:
        store = Store()
    monkeypatch.setattr("finsight.data.akshare_provider.AKShareProvider", Provider)
    with pytest.raises(ValueError, match="calendar"):
        check_daily(Service(), "sample", datetime(2025, 6, 2, 18, tzinfo=ZoneInfo("Asia/Shanghai")))


def test_scheduler_waits_for_new_session_after_temporally_blocked_bootstrap(monkeypatch):
    class Provider:
        def __init__(self, root):
            pass
        def calendar(self):
            return pd.DataFrame({"trade_date": ["2025-05-30", "2025-06-02", "2025-06-03"]})
    class Store:
        root = "unused"
        def snapshots(self):
            return [MarketSnapshot(snapshot_id="blocked", as_of=date(2025, 5, 30),
                captured_at=datetime(2025, 6, 2, 9, tzinfo=ZoneInfo("Asia/Shanghai")), source="akshare",
                universe_id=universe_id(True), universe_scope="current mainboard", publishable=False,
                quality_issues=["no stock state known at cutoff; first capture cannot be backdated to the prior session"])]
    class Service:
        store = Store()
        def sync(self, *args, **kwargs):
            pytest.fail("waiting for the close must not restart initialization")
    monkeypatch.setattr("finsight.data.akshare_provider.AKShareProvider", Provider)
    result = check_daily(Service(), "mainboard", datetime(2025, 6, 2, 14, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert result["status"] == "waiting_new_session" and result["next_session"] == "2025-06-02"


def test_job_only_collects_market_data(tmp_path):
    job = job_definition(tmp_path, python="/test/venv/bin/python")
    assert job["StartCalendarInterval"] == {"Hour": 18, "Minute": 5}
    assert job["RunAtLoad"]
    assert job["ProgramArguments"][-2:] == ["watch", "--once"]
    assert "research" not in job["ProgramArguments"]
    assert set(job["EnvironmentVariables"]) == {"PYTHONPATH"}
    assert job["ProgramArguments"][0] == "/test/venv/bin/python"


def test_failed_task_without_snapshot_has_an_automatic_retry_cooldown(tmp_path, monkeypatch):
    class Provider:
        def __init__(self, root):
            pass

        def calendar(self):
            return pd.DataFrame({"trade_date": ["2025-05-30", "2025-06-02", "2025-06-03"]})

    service = FinSight(tmp_path)
    job = Jobs(service.store).create({"universe_id": universe_id(True), "as_of": "2025-06-02"})
    Jobs(service.store).update(job["job_id"], status="failed", error="provider unavailable")
    with service.store.connect() as db:
        db.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", ("2025-06-02T18:05:00+08:00", job["job_id"]))
    monkeypatch.setattr("finsight.data.akshare_provider.AKShareProvider", Provider)
    monkeypatch.setattr(service, "sync", lambda *a, **kw: pytest.fail("cooldown must not restart the source"))
    result = check_daily(service, "mainboard", datetime(2025, 6, 2, 18, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert result["status"] == "waiting_retry" and result["job_id"] == job["job_id"]
