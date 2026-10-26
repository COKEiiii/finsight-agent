from __future__ import annotations

import copy
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from reference_screening import compute_factors_reference
from test_live_provider import FakeAKShare

from finsight.data.collector import merge_observations
from finsight.data.quality import audit
from finsight.data.requests import RequestBroker, RequestFailure, WorkerPool
from finsight.data.store import canonical
from finsight.data.universe import current_members, membership_id, universe_id
from finsight.jobs import Jobs
from finsight.models import FACTORS
from finsight.scheduler import rollout_status
from finsight.screening import compute_factors, screen
from finsight.service import FinSight


def version2(bundle):
    bundle.meta.schema_version = 2
    bundle.meta.membership_symbols = current_members(bundle["universe"])
    bundle.meta.membership_id = membership_id(bundle.meta.membership_symbols)
    bundle.meta.universe_id = universe_id(False, bundle.meta.membership_symbols)
    return bundle


def expand(bundle, count=1000):
    value = copy.deepcopy(bundle)
    symbols = list(bundle["universe"]["symbol"])
    mapping = []
    for i in range(count):
        if i < 1000:
            symbol = f"{600000+i:06d}.SH"
        elif i < 2000:
            symbol = f"{601000+i-1000:06d}.SH"
        else:
            symbol = f"{i-2000+1:06d}.SZ"
        mapping.append({"_old": symbols[i % len(symbols)], "symbol": symbol})
    mapping = pd.DataFrame(mapping)
    for name in ("universe", "prices", "financials", "valuations"):
        source = bundle[name].rename(columns={"symbol": "_old"})
        if name == "prices":
            source = source.groupby("_old").tail(61)
        value.tables[name] = mapping.merge(source, on="_old").drop(columns="_old")
    return version2(value)


def test_vectorized_factors_match_original_oracle(bundle, config):
    expected, _ = compute_factors_reference(bundle, bundle.meta.as_of, config)
    actual, _ = compute_factors(bundle, bundle.meta.as_of, config)
    fields = ["symbol", *FACTORS, "amount_20", "close"]
    pd.testing.assert_frame_equal(actual[fields].sort_values("symbol").reset_index(drop=True),
                                 expected[fields].sort_values("symbol").reset_index(drop=True), check_exact=False, rtol=1e-12)


def test_average_roe_cannot_silently_replace_frozen_weighted_roe(bundle, config):
    value = version2(bundle)
    symbol = value.meta.membership_symbols[0]
    mask = value["financials"]["symbol"] == symbol
    value.tables["financials"].loc[mask, "source"] = "baostock:query_profit_data"
    value.tables["financials"].loc[mask, "roe_basis"] = "average_annual"
    frame, exclusions = compute_factors(value, value.meta.as_of, config)
    assert symbol not in set(frame["symbol"])
    row = next(item for item in exclusions if item["symbol"] == symbol)
    assert row["category"] == "data" and "financial_definition_mismatch" in row["codes"]


@pytest.mark.parametrize("missing,can_publish", [(20, True), (21, False)])
def test_98_percent_boundary_is_exact(bundle, missing, can_publish):
    value = expand(bundle, 1000)
    unavailable = set(value.meta.membership_symbols[:missing])
    value.tables["valuations"] = value["valuations"][~value["valuations"]["symbol"].isin(unavailable)]
    report = audit(value)
    assert report["publishable"] is can_publish
    assert report["coverage"]["coverage"] == (1000-missing)/1000
    assert report["coverage"]["missing_count"] == missing
    if not can_publish:
        with pytest.raises(ValueError, match="formal ranking"):
            screen(value)
    preview = screen(value, preview=True)
    assert preview.publication_status == ("published" if can_publish else "preview")


def test_unknown_status_is_missing_not_a_rule_exclusion(bundle):
    value = version2(bundle)
    value.tables["universe"]["is_st"] = value["universe"]["is_st"].astype(object)
    value.tables["universe"].loc[0, "is_st"] = None
    report = audit(value)
    assert report["coverage"]["required"] == 20
    assert report["coverage"]["missing_count"] == 1
    assert report["coverage"]["confirmed_exclusions"] == 0


def test_confirmed_st_is_separately_accounted(bundle):
    value = version2(bundle)
    value.tables["universe"].loc[0, "is_st"] = True
    report = audit(value)
    assert report["coverage"]["required"] == 19
    assert report["coverage"]["confirmed_exclusions"] == 1
    assert report["coverage"]["coverage"] == 1


def test_universe_identity_is_not_just_member_count():
    assert universe_id(False, ["600000.SH", "000001.SZ"]) != universe_id(False, ["600036.SH", "000333.SZ"])
    assert universe_id(False, ["600000.SH", "000001.SZ"]) == universe_id(False, ["000001.SZ", "600000.SH"])
    assert universe_id(True, limit=100) != universe_id(True)


def test_partition_manifest_deduplicates_and_reads_only_needed_window(bundle, store):
    value = version2(bundle)
    store.save_snapshot(value)
    loaded = store.load_snapshot(value.meta.snapshot_id)
    pd.testing.assert_frame_equal(loaded["prices"], canonical(value["prices"]))
    before = set((store.root / "objects").glob("*.parquet"))
    duplicate = copy.deepcopy(value)
    duplicate.meta.snapshot_id += "-second"
    store.save_snapshot(duplicate)
    assert set((store.root / "objects").glob("*.parquet")) == before
    small = store.load_snapshot(value.meta.snapshot_id, ("2025-02-01", "2025-02-28"))
    assert len(small["prices"]) < len(loaded["prices"])
    assert small["prices"]["trade_date"].min() >= "2025-02-01"
    manifest = json.loads((store.root / "snapshots" / value.meta.snapshot_id / "manifest.json").read_text())
    digest = manifest["tables"]["prices"][0]["sha256"]
    (store.root / "objects" / f"{digest}.parquet").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        store.load_snapshot(value.meta.snapshot_id)


def test_factor_cache_invalidates_and_scores_always_rerank(bundle, store, monkeypatch):
    import finsight.screening as screening_module
    calls = []
    original = screening_module.compute_factors

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(screening_module, "compute_factors", counted)
    value = version2(bundle)
    first = screen(value, store=store)
    again = screen(value, store=store)
    assert again.factor_cache_hits == 20 and first.candidates == again.candidates
    assert len(calls) == 1
    symbol = value["universe"].iloc[0]["symbol"]
    value.tables["financials"].loc[value["financials"]["symbol"] == symbol, "roe"] = 80
    revised = screen(value, store=store)
    assert revised.factor_cache_hits == 19
    assert len(calls) == 2


def test_3000_stock_vectorized_ranking_under_one_minute(bundle, store):
    value = expand(bundle, 3000)
    started = time.perf_counter()
    first = screen(value, store=store)
    assert time.perf_counter() - started < 60
    assert first.eligible_count == 3000 and len(first.candidates) == 10
    assert first.candidates == screen(value, store=store).candidates


class EchoAPI:
    calls = 0
    def echo(self):
        self.calls += 1
        return pd.DataFrame({"value": [self.calls]})


def test_checkpoint_original_time_cache_corruption_and_cross_day_resume(store):
    jobs = Jobs(store)
    job = jobs.create({"case": "first"})
    api = EchoAPI()
    instant = [datetime(2025, 5, 30, 18, tzinfo=ZoneInfo("Asia/Shanghai"))]
    broker = RequestBroker(store, job["job_id"], api=api, interval=0, clock=lambda: instant[0])
    first = broker.call("echo")
    instant[0] += timedelta(days=1)
    resumed = broker.call("echo")
    assert resumed.attrs["fetched_at"] == first.attrs["fetched_at"]
    assert resumed.attrs["loaded_at"] != first.attrs["fetched_at"]
    assert api.calls == 1
    job2 = jobs.create({"case": "second"})
    broker2 = RequestBroker(store, job2["job_id"], api=api, interval=0, clock=lambda: instant[0])
    broker2.call("echo")
    assert api.calls == 2
    with store.connect() as db:
        path = db.execute("SELECT path FROM request_cache").fetchone()[0]
    (store.root / path).write_bytes(b"bad")
    job3 = jobs.create({"case": "third"})
    RequestBroker(store, job3["job_id"], api=api, interval=0, clock=lambda: instant[0]).call("echo")
    assert api.calls == 3


def test_global_budget_and_retry_backoff(store):
    class Flaky:
        calls = 0
        def echo(self):
            self.calls += 1
            if self.calls < 3:
                raise RequestFailure("timeout", "echo")
            return pd.DataFrame({"value": [1]})
    sleeps = []
    job = Jobs(store).create({"case": "retry"})
    broker = RequestBroker(store, job["job_id"], api=Flaky(), interval=0, sleeper=sleeps.append)
    broker.call("echo")
    assert sleeps == [5, 30] and broker.limit == 1
    assert Jobs(store).get(job["job_id"])["requests"]["network_attempts"] == 3
    assert store.reserve_request(.1) >= 0
    assert store.reserve_request(.1) > 0


def local_worker(connection):
    while True:
        request = connection.recv()
        if request is None:
            return
        if request["method"] == "slow":
            time.sleep(1)
        connection.send({"ok": True, "frame": pd.DataFrame({"value": [1]})})


def test_timed_out_worker_is_terminated_and_replaced():
    pool = WorkerPool(1, target=local_worker)
    try:
        with pytest.raises(RequestFailure, match="timeout"):
            pool.execute("slow", {}, .02)
        assert pool.execute("fast", {}, 10)["frame"]["value"].iloc[0] == 1
    finally:
        pool.close()


class IncrementalAPI(FakeAKShare):
    def __init__(self):
        self.price_ranges = []

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust):
        self.price_ranges.append((symbol, start_date, end_date))
        days = pd.bdate_range(start_date, end_date)
        close = 20 + (days - pd.Timestamp("2024-01-02")).days.to_numpy() * .01
        return pd.DataFrame({"日期": days, "开盘": close, "收盘": close * (2 if adjust else 1),
            "最高": close * 1.01, "最低": close * .99, "成交额": 100_000_000, "成交量": 100_000})

    def stock_value_em(self, symbol):
        return pd.DataFrame({"数据日期": pd.bdate_range("2024-01-02", "2025-06-30"), "PE(TTM)": 12., "市净率": 1.5})


def test_incremental_collection_matches_full_and_preserves_first_financials(tmp_path):
    service = FinSight(tmp_path / "incremental")
    api = IncrementalAPI()
    t0 = datetime(2025, 5, 30, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
    first = service.sync("akshare", date(2025, 5, 30), date(2024, 1, 2), symbols=["600036"],
                         retries=0, interval=0, api=api, clock=lambda: t0)
    assert first["publishable"]
    api.price_ranges.clear()
    t1 = t0 + timedelta(days=3)
    second = service.sync("akshare", date(2025, 6, 2), date(2024, 1, 2), symbols=["600036"],
                          retries=0, interval=0, api=api, clock=lambda: t1)
    assert second["publishable"]
    assert all(start > "20240102" for _, start, _ in api.price_ranges)
    incremental = service.store.load_snapshot(second["snapshot_id"])
    assert "2025-05-30" in set(incremental["financials"]["available_on"])
    fresh = FinSight(tmp_path / "full")
    complete = fresh.sync("akshare", date(2025, 6, 2), date(2024, 1, 2), symbols=["600036"],
                          retries=0, interval=0, api=IncrementalAPI(), clock=lambda: t1)
    full = fresh.store.load_snapshot(complete["snapshot_id"])
    for key in ("open", "close", "adj_close", "volume", "amount"):
        np.testing.assert_allclose(incremental["prices"][key], full["prices"][key])
    assert service.screen(second["snapshot_id"]).candidates[0]["score"] == fresh.screen(complete["snapshot_id"]).candidates[0]["score"]


def test_duplicate_job_and_cancellation_are_durable(store):
    jobs = Jobs(store)
    first = jobs.create({"case": "same"})
    assert jobs.create({"case": "same"})["job_id"] == first["job_id"]
    jobs.cancel(first["job_id"])
    assert jobs.get(first["job_id"])["status"] == "cancelled"
    assert jobs.resume(first["job_id"])["status"] == "queued"


def test_five_day_rollout_counts_distinct_real_sessions_only(bundle, store):
    value = version2(bundle)
    value.meta.synthetic = False
    value.meta.universe_id = universe_id(True)
    value.meta.universe_complete = True
    store.save_snapshot(value)
    days = sorted(value["calendar"].loc[value["calendar"]["trade_date"] <= str(value.meta.as_of), "trade_date"])[-5:]
    with store.connect() as db:
        for day in days[:-1]:
            db.execute("INSERT INTO publications VALUES (?,?,?,?,?)", (value.meta.universe_id, day, value.meta.snapshot_id, "run", 1.))
    assert not rollout_status(store)["promotion_eligible"]
    with store.connect() as db:
        db.execute("INSERT INTO publications VALUES (?,?,?,?,?)", (value.meta.universe_id, days[-1], value.meta.snapshot_id, "run", .98))
    assert rollout_status(store)["consecutive_days"] == 5


def test_unchanged_observation_keeps_original_fetch_time():
    old = pd.DataFrame([{"symbol": "600000.SH", "trade_date": "2025-01-02", "close": 10., "fetched_at": "old"}])
    new = old.assign(fetched_at="new")
    assert merge_observations(old, new, ["symbol", "trade_date"])["fetched_at"].iloc[0] == "old"
    new["close"] = 11.
    assert merge_observations(old, new, ["symbol", "trade_date"])["fetched_at"].iloc[0] == "new"
