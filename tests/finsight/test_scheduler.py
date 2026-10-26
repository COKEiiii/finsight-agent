from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from finsight.cli import check_daily
from finsight.models import MarketSnapshot
from finsight.scheduler import job_definition


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


def test_job_only_collects_market_data(tmp_path):
    job = job_definition(tmp_path, python="/test/venv/bin/python")
    assert job["StartCalendarInterval"] == {"Hour": 18, "Minute": 5}
    assert job["RunAtLoad"]
    assert job["ProgramArguments"][-2:] == ["watch", "--once"]
    assert "research" not in job["ProgramArguments"]
    assert set(job["EnvironmentVariables"]) == {"PYTHONPATH"}
    assert job["ProgramArguments"][0] == "/test/venv/bin/python"
