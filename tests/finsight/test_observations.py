"""Temporary fixtures exercise recording integrity; they are never market evidence."""
import copy
import fcntl
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from finsight.cli import check_daily, main
from finsight.data.universe import membership_id, universe_id
from finsight.jobs import Jobs
from finsight.models import ScreeningResult, StrategyConfig
from finsight.observations import CHINA, observation_report, seal_signal, timestamp
from finsight.service import FinSight


@pytest.fixture
def signal(store, bundle, config):
    # Test-only synthetic values deliberately exercise the real-source admission path.
    # No fixture is persisted to the user's data directory or used for performance claims.
    bundle.meta.source, bundle.meta.synthetic = "akshare", False
    bundle.meta.captured_at = datetime(2025, 2, 28, 19, 59, 55, tzinfo=CHINA)
    bundle.meta.universe_id, bundle.meta.universe_complete = universe_id(True), True
    bundle.meta.membership_symbols = sorted(bundle["universe"]["symbol"])
    bundle.meta.membership_id = membership_id(bundle.meta.membership_symbols)
    store.save_snapshot(bundle)
    candidates = [{"symbol": f"{600000+i:06d}.SH", "rank": i+1, "score": 90-i,
                   "industry": f"industry-{i%4}", "close": 20} for i in range(10)]
    result = ScreeningResult(run_id=uuid4().hex, snapshot_id=bundle.meta.snapshot_id,
        as_of=bundle.meta.as_of, strategy_version=config.version, config_id=config.config_id,
        code_version="unit-test", candidates=candidates, exclusions=[], eligible_count=20,
        universe_count=20, synthetic=False, research_eligible=False, universe_id=bundle.meta.universe_id,
        membership_id=bundle.meta.membership_id, coverage={"coverage": 1.})
    store.save_run("screen", result.snapshot_id, config, result.model_dump(mode="json"), result.run_id)
    set_time(store, result.run_id, "2025-02-28T20:00:00+08:00")
    return result


def set_time(store, run_id, value):
    # Only temporary test databases permit controlled past timestamps.
    utc = timestamp(value).astimezone(timezone.utc).replace(tzinfo=None).isoformat(" ")
    with store.connect() as db:
        db.execute("UPDATE runs SET created_at=? WHERE run_id=?", (utc, run_id))


def captured(store, signal, at="2025-03-03T09:00:00+08:00"):
    view = seal_signal(store, signal.run_id)
    set_time(store, view["seal_run_id"], at)
    return seal_signal(store, signal.run_id)


def revision(store, signal, config, *, day=None, score_delta=1, mutate=None):
    result = signal.model_copy(deep=True)
    result.run_id = uuid4().hex
    if day:
        bundle = store.load_snapshot(signal.snapshot_id)
        bundle.meta.snapshot_id = "test-" + uuid4().hex
        bundle.meta.as_of = date.fromisoformat(day)
        store.save_snapshot(bundle)
        result.snapshot_id, result.as_of = bundle.meta.snapshot_id, bundle.meta.as_of
    result.candidates[0]["score"] += score_delta
    if mutate:
        mutate(result)
    store.save_run("screen", result.snapshot_id, config, result.model_dump(mode="json"), result.run_id)
    set_time(store, result.run_id, f"{result.as_of}T20:00:00+08:00")
    return result


def test_same_signal_reuses_original_timestamp_and_payload(store, signal, config):
    original = captured(store, signal)
    saved = store.run(original["seal_run_id"])
    repeat = revision(store, signal, config, score_delta=0)
    again = seal_signal(store, repeat.run_id)
    assert again["seal_run_id"] == original["seal_run_id"]
    assert again["recorded_at"] == "2025-03-03T09:00:00+08:00"
    assert again["screen_run_id"] == signal.run_id
    assert store.run(original["seal_run_id"]) == saved
    assert len(store.runs("signal_seal")) == 1


@pytest.mark.parametrize("at,status", [
    ("2025-03-03T09:14:59+08:00", "on_time"),
    ("2025-03-03T09:15:00+08:00", "late_seal"),
])
def test_conservative_auction_boundary_and_weekend(store, signal, at, status):
    view = captured(store, signal, at)
    assert view["next_session"] == "2025-03-03"
    assert view["timing_status"] == status
    assert view["prospective_eligible"] is (status == "on_time")
    assert view["rebalance_due"] is True
    assert not view["historical_validation_passed"]
    assert timestamp("2025-03-03 01:14:59") == timestamp("2025-03-03T09:14:59+08:00")


def test_late_publication_is_not_relabelled_as_timely_capture(store, signal):
    set_time(store, signal.run_id, "2025-03-03T09:16:00+08:00")
    view = captured(store, signal, "2025-03-03T09:17:00+08:00")
    assert view["timing_status"] == "late_publication" and not view["prospective_eligible"]


@pytest.mark.parametrize("changed", ["synthetic", "preview", "config", "date", "members", "kind"])
def test_reject_invalid_source_without_recording(store, signal, changed):
    row = store.run(signal.run_id)
    value = row["payload"]
    if changed == "synthetic":
        value["synthetic"] = True
    elif changed == "preview":
        value["publication_status"] = "preview"
    elif changed == "config":
        value["config_id"] = "different"
    elif changed == "date":
        value["as_of"] = "2025-02-27"
    elif changed == "members":
        value["membership_id"] = "different"
    with store.connect() as db:
        db.execute("UPDATE runs SET payload=?,kind=? WHERE run_id=?", (
            json.dumps(value), "research" if changed == "kind" else "screen", signal.run_id))
    with pytest.raises(ValueError):
        seal_signal(store, signal.run_id)
    assert store.runs("signal_seal") == []


@pytest.mark.parametrize("source_at", ["2025-02-28T17:59:59+08:00", "2099-01-01T20:00:00+08:00"])
def test_impossible_source_timestamps_cannot_be_observed(store, signal, source_at):
    set_time(store, signal.run_id, source_at)
    with pytest.raises(ValueError, match="timestamp"):
        seal_signal(store, signal.run_id)
    assert not store.runs("signal_seal")


def test_signal_must_not_predate_its_own_snapshot(store, signal):
    set_time(store, signal.run_id, "2025-02-28T19:59:50+08:00")
    with pytest.raises(ValueError, match="snapshot capture"):
        seal_signal(store, signal.run_id)


def test_calendar_has_no_invented_next_session(store, signal, config):
    bundle = store.load_snapshot(signal.snapshot_id)
    bundle.meta.snapshot_id = "no-future-calendar"
    bundle.tables["calendar"] = bundle["calendar"].loc[bundle["calendar"]["trade_date"] <= str(signal.as_of)]
    store.save_snapshot(bundle)
    signal.snapshot_id = bundle.meta.snapshot_id
    store.save_run("screen", signal.snapshot_id, config, signal.model_copy(update={"run_id": "no-next"}).model_dump(mode="json"), "no-next")
    set_time(store, "no-next", "2025-02-28T20:00:00+08:00")
    view = seal_signal(store, "no-next")
    assert view["timing_status"] == "unknown_calendar"
    assert view["next_session"] is view["rebalance_due"] is None
    assert not view["prospective_eligible"]
    report = observation_report(store, config, end=date(2025, 3, 4))
    assert report["calendar_range_incomplete"]
    assert report["missing_signal_sessions"] == []


@pytest.mark.parametrize("change", [
    {"synthetic": True}, {"source": "demo"}, {"universe_complete": False},
    {"universe_id": "different-same-count"}, {"publishable": False}, {"publication_status": "preview"},
])
def test_snapshot_admission_is_independent_of_screen_label(store, signal, change):
    path = store.root / "snapshots" / signal.snapshot_id / "metadata.json"
    metadata = json.loads(path.read_text())
    path.write_text(json.dumps({**metadata, **change}))
    with pytest.raises(ValueError, match="mainboard"):
        seal_signal(store, signal.run_id)
    assert not store.runs("signal_seal")


def test_next_session_uses_stored_holiday_calendar(store, signal, config):
    bundle = store.load_snapshot(signal.snapshot_id)
    bundle.meta.snapshot_id = "holiday-fixture"
    bundle.tables["calendar"] = bundle["calendar"].loc[bundle["calendar"]["trade_date"] != "2025-03-03"]
    store.save_snapshot(bundle)
    result = signal.model_copy(update={"run_id": "holiday-screen", "snapshot_id": bundle.meta.snapshot_id})
    store.save_run("screen", result.snapshot_id, config, result.model_dump(mode="json"), result.run_id)
    set_time(store, result.run_id, "2025-02-28T20:00:00+08:00")
    view = captured(store, result, "2025-03-04T09:00:00+08:00")
    assert view["next_session"] == "2025-03-04" and view["prospective_eligible"]


@pytest.mark.parametrize("corruption", ["copy", "calendar", "nonobject", "source", "future_seal"])
def test_corruption_is_reported_without_fabricating_valid_observations(store, signal, config, corruption):
    view = captured(store, signal)
    row = store.run(view["seal_run_id"])
    value = row["payload"]
    if corruption == "copy":
        value["screening_result"]["candidates"][0]["score"] += 10
    elif corruption == "calendar":
        value["calendar_hash"] = "bad"
    elif corruption == "nonobject":
        value = []
    elif corruption == "source":
        payload = store.run(signal.run_id)["payload"]
        payload["candidates"][0]["score"] += 10
        with store.connect() as db:
            db.execute("UPDATE runs SET payload=? WHERE run_id=?", (json.dumps(payload), signal.run_id))
    else:
        set_time(store, view["seal_run_id"], "2099-01-01T20:00:00+08:00")
    with store.connect() as db:
        db.execute("UPDATE runs SET payload=? WHERE run_id=?", (json.dumps(value), view["seal_run_id"]))
    report = observation_report(store, config)
    assert report["observed_sessions"] == report["prospective_sessions"] == 0
    assert len(report["rejected"]) == 1
    if corruption != "source":
        new = seal_signal(store, signal.run_id)
        assert new["seal_run_id"] != view["seal_run_id"]
        assert len(new["rejected_existing"]) == 1
        assert not new["prospective_eligible"]  # A new capture cannot inherit the old timestamp.


def test_legacy_seal_without_explicit_calendar_hash_retains_time(store, signal):
    view = captured(store, signal)
    row = store.run(view["seal_run_id"])
    row["payload"].pop("calendar_hash")
    with store.connect() as db:
        db.execute("UPDATE runs SET payload=? WHERE run_id=?", (json.dumps(row["payload"]), view["seal_run_id"]))
    assert seal_signal(store, signal.run_id)["recorded_at"] == view["recorded_at"]


def test_same_day_revisions_preserve_latest_eligible_not_late_version(store, signal, config):
    first = captured(store, signal)
    newer = revision(store, signal, config)
    chosen = captured(store, newer, "2025-03-03T09:10:00+08:00")
    late = revision(store, signal, config, score_delta=2)
    last = captured(store, late, "2025-03-03T09:16:00+08:00")
    report = observation_report(store, config)
    assert report["observed_sessions"] == report["prospective_sessions"] == 1
    row = report["timeline"][0]
    assert row["seal_run_id"] == chosen["seal_run_id"]
    assert row["revisions"] == 2
    assert row["version_seal_ids"] == [first["seal_run_id"], chosen["seal_run_id"], last["seal_run_id"]]


def test_same_second_revisions_follow_insert_order_not_random_ids(store, signal, config):
    captured(store, signal)
    latest = captured(store, revision(store, signal, config))
    report = observation_report(store, config)
    assert report["timeline"][0]["seal_run_id"] == latest["seal_run_id"]


def test_differences_do_not_invent_trades_or_returns_and_gaps_use_calendar(store, signal, config):
    captured(store, signal)
    changed = revision(store, signal, config, day="2025-03-04", mutate=lambda r: r.candidates[-1].update(symbol="601999.SH"))
    captured(store, changed, "2025-03-05T09:00:00+08:00")
    report = observation_report(store, config, start=date(2025, 2, 28), end=date(2025, 3, 4))
    assert report["missing_signal_sessions"] == ["2025-03-03"]
    row = report["timeline"][1]
    assert row["entered"] == ["601999.SH"] and row["exited"] == [signal.candidates[-1]["symbol"]]
    assert row["target_turnover_reference"] == 0
    assert row["comparison_is_prospective"] and row["intervening_sessions"] == ["2025-03-03"]
    assert report["actual_executions"] is report["portfolio_returns"] is None


def test_target_turnover_counts_cash_without_assuming_executed_trades(store, signal, config):
    captured(store, signal)
    changed = revision(store, signal, config, day="2025-03-03", mutate=lambda r: r.candidates.pop(0))
    captured(store, changed, "2025-03-04T09:00:00+08:00")
    row = observation_report(store, config)["timeline"][1]
    assert row["target_turnover_reference"] == .1


def test_strategy_versions_are_not_merged_and_late_previous_is_flagged(store, signal, config):
    captured(store, signal, "2025-03-03T09:16:00+08:00")
    changed = revision(store, signal, config, day="2025-03-03")
    captured(store, changed, "2025-03-04T09:00:00+08:00")
    config2 = StrategyConfig(version="independent-version")
    independent = revision(store, signal, config2, mutate=lambda r: (
        setattr(r, "config_id", config2.config_id), setattr(r, "strategy_version", config2.version)))
    captured(store, independent)
    report = observation_report(store, config)
    assert report["observed_sessions"] == 2 and report["prospective_sessions"] == 1
    assert not report["timeline"][1]["comparison_is_prospective"]
    assert observation_report(store, config2)["observed_sessions"] == 1


def test_lock_prevents_duplicate_capture_without_recollecting(store, signal):
    with (store.root / ".observations.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="without recollecting"):
            seal_signal(store, signal.run_id)
    assert not store.runs("signal_seal")


def test_cli_exports_use_saved_time_and_do_not_call_models(store, signal, capsys, monkeypatch):
    from finsight.research import ChatAdapter
    monkeypatch.setattr(ChatAdapter, "complete", lambda *a, **kw: pytest.fail("observations must not invoke a model"))
    args = ["--data-dir", str(store.root), "observations"]
    assert main(args + ["capture", "--run", signal.run_id]) == 0
    view = json.loads(capsys.readouterr().out)
    assert main(args + ["report"]) == 0
    report = json.loads(capsys.readouterr().out)
    folder = Path(report["exports"])
    assert "timeline" not in report
    exported = json.loads((folder / "observations.json").read_text())
    assert exported["timeline"][0]["recorded_at"] == view["recorded_at"]
    assert (folder / "observations.csv").read_text().startswith("\ufeffsignal_date")
    assert "不是实际换手或收益" in (folder / "observations.md").read_text()
    assert main(args + ["report", "--start", "2025-03-04", "--end", "2025-03-03"]) == 2
    assert "start" in capsys.readouterr().err


@pytest.mark.parametrize("capture_fails", [False, True])
def test_real_full_job_captures_after_publish_and_observation_failure_is_isolated(store, signal, config, monkeypatch, capture_fails):
    from finsight.data.collector import Collector
    bundle = store.load_snapshot(signal.snapshot_id)
    bundle.meta.snapshot_id = "collected-test-" + uuid4().hex
    bundle.meta.schema_version = 2
    service = FinSight(store.root, config)
    monkeypatch.setattr(Collector, "fetch", lambda self: copy.deepcopy(bundle))
    if capture_fails:
        monkeypatch.setattr(service, "observe_signal", lambda *a: (_ for _ in ()).throw(OSError("disk unavailable")))
    job = Jobs(store).create({"strategy": config.model_dump(), "workers": 1, "interval": 1., "timeout": 30., "retries": 0,
        "universe_id": universe_id(True)})
    result = service.run_job(job["job_id"], api=object())
    assert Jobs(store).get(job["job_id"])["status"] == "completed"
    assert result["signal_observation"]["status"] == ("failed" if capture_fails else "recorded")
    with store.connect() as db:
        assert db.execute("SELECT screen_run_id FROM publications").fetchone()[0] == result["screen_run_id"]
    assert len(store.runs("signal_seal")) == (0 if capture_fails else 1)


def test_daily_retry_uses_existing_publication_without_downloading(store, signal, monkeypatch):
    class Provider:
        def __init__(self, *a):
            pass

        def calendar(self):
            return pd.DataFrame({"trade_date": ["2025-02-28", "2025-03-03"]})

    monkeypatch.setattr("finsight.data.akshare_provider.AKShareProvider", Provider)
    service = FinSight(store.root)
    monkeypatch.setattr(service, "sync", lambda *a, **kw: pytest.fail("must reuse the published snapshot"))
    with store.connect() as db:
        db.execute("INSERT INTO publications VALUES (?,?,?,?,?)", (universe_id(True), "2025-02-28", signal.snapshot_id, signal.run_id, 1.))
    at = datetime(2025, 3, 3, 8, tzinfo=CHINA)
    first = check_daily(service, "mainboard", at)
    assert first["status"] == "up_to_date" and first["signal_observation"]["status"] == "recorded"
    second = check_daily(service, "mainboard", at + timedelta(minutes=1))
    assert second["signal_observation"]["seal_run_id"] == first["signal_observation"]["seal_run_id"]
    assert len(store.runs("signal_seal")) == 1


def test_dashboard_refresh_and_capture_preserve_existing_observation(store, signal, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from finsight.research import ChatAdapter
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(store.root))
    monkeypatch.setattr(ChatAdapter, "complete", lambda *a, **kw: pytest.fail("view/capture must not call a model"))
    # Render an actual full screening shape rather than the minimal observation fixtures.
    service = FinSight(store.root)
    result = service.screen(signal.snapshot_id)
    first = service.observe_signal(result.run_id)
    before = store.run(first["seal_run_id"])
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    assert not app.exception
    assert any(item.label == "前瞻信号观察" for item in app.expander)
    assert len(store.runs("signal_seal")) == 1
    next(button for button in app.button if button.label == "固定当前榜单信号").click().run()
    assert not app.exception
    app.run()
    assert not app.exception and len(store.runs("signal_seal")) == 1
    assert store.run(first["seal_run_id"]) == before
