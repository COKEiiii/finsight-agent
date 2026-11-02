from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import baostock as bs
import pandas as pd
import pytest
from baostock.data.resultset import ResultData

from finsight.data.collector import Collector
from finsight.data.daily import validate_daily
from finsight.data.request_worker import alternate_query
from finsight.data.requests import RequestBroker, RequestFailure
from finsight.jobs import Jobs
from finsight.service import FinSight


class DailyAPI:
    def __init__(self):
        self.calls = []
        self.bad_date = None
        self.missing_code = None
        self.status = "1"

    def rows(self, code, start, end):
        dates = pd.bdate_range(start, end)
        prices = 20 + (dates - pd.Timestamp("2024-01-01")).days * .01
        return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "code": code, "open": prices,
            "high": prices + 1, "low": prices - 1, "close": prices + .1, "preclose": prices - .1,
            "volume": "1000000", "amount": "100000000", "tradestatus": self.status,
            "peTTM": "12", "pbMRQ": "1.5", "isST": "0", "adjustflag": "3"})

    def baostock_daily_kline(self, date):
        self.calls.append(("daily", date))
        if self.bad_date == date:
            raise RequestFailure("timeout", "baostock_daily_kline")
        frames = [self.rows(code, date, date) for code in ("sh.600036", "sz.000001") if code != self.missing_code]
        return pd.concat(frames, ignore_index=True)

    def baostock_kline(self, code, start_date, end_date, adjustflag):
        self.calls.append(("stock", code, adjustflag, start_date, end_date))
        frame = self.rows(code, start_date, end_date)
        if adjustflag == "1":
            frame = frame[["date", "close"]].copy()
            frame["close"] *= 2  # Deliberately cannot be reconstructed by assuming an adjustment factor of one.
        return frame

    def tool_trade_date_hist_sina(self):
        return pd.DataFrame({"trade_date": pd.bdate_range("2024-01-01", "2025-12-31")})

    def stock_info_sh_name_code(self, symbol):
        return pd.DataFrame({"证券代码": ["600036"], "证券简称": ["招商银行"], "上市日期": ["2002-04-09"]})

    def stock_info_sz_name_code(self, symbol):
        return pd.DataFrame({"A股代码": ["000001"], "A股简称": ["平安银行"], "A股上市日期": ["1991-04-03"], "板块": ["主板"]})

    def baostock_industry(self):
        return pd.DataFrame({"code": ["sh.600036", "sz.000001"], "industry": ["金融", "金融"]})

    def baostock_index(self, start_date, end_date):
        return pd.DataFrame({"date": pd.bdate_range(start_date, end_date).strftime("%Y-%m-%d"), "close": 4000.})

    def stock_financial_analysis_indicator_em(self, symbol, indicator):
        return pd.DataFrame({"REPORT_DATE": ["2023-12-31"], "NOTICE_DATE": ["2024-03-20"],
            "ROEJQ": [12.], "TOTALOPERATEREVETZ": [-5.]})


def make_collector(store, api, mode="daily", clock=None, seed=True, case=None):
    clock = clock or (lambda: datetime(2025, 2, 28, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
    job = Jobs(store).create({"case": case or f"daily-{mode}", "price_source": mode})
    broker = RequestBroker(store, job["job_id"], api=api, interval=0, retries=0, clock=clock)
    collector = Collector(broker, {"mainboard": True, "price_source": mode})
    if seed:
        for code in ("sh.600036", "sz.000001"):
            symbol = code[3:] + "." + code[:2].upper()
            collector.base_groups["prices"][symbol] = api.rows(code, "2024-11-01", "2025-02-27").rename(columns={"date": "trade_date"})
    return collector, broker, job["job_id"]


def stocks():
    return pd.DataFrame({"code": ["600036", "000001"], "symbol": ["600036.SH", "000001.SZ"],
        "name": ["招商银行", "平安银行"], "listed_on": ["2002-04-09", "1991-04-03"]})


def calendar():
    return pd.DataFrame({"trade_date": pd.bdate_range("2024-01-01", "2025-12-31").strftime("%Y-%m-%d")})


def prepare(collector):
    collector._prepare_daily_prices(stocks(), date(2024, 11, 1), "2025-02-28", calendar())
    return collector._price_lower("600036.SH", date(2024, 11, 1), calendar())


@pytest.mark.parametrize("problem", ["date", "adjustment", "duplicate", "code", "missing_field", "empty"])
def test_invalid_daily_batches_are_rejected_before_use(problem):
    frame = DailyAPI().baostock_daily_kline("2025-02-28")
    frame.attrs["fetched_at"] = "2025-02-28T18:00:00+08:00"
    if problem == "date":
        frame["date"] = "2025-02-27"
    elif problem == "adjustment":
        frame["adjustflag"] = "1"
    elif problem == "duplicate":
        frame.loc[1, "code"] = frame.loc[0, "code"]
    elif problem == "code":
        frame.loc[0, "code"] = "unverified.600036"
    elif problem == "missing_field":
        frame = frame.drop(columns="peTTM")
    else:
        frame = frame.iloc[:0]
    with pytest.raises(ValueError):
        validate_daily(frame, "2025-02-28")


def test_daily_raw_and_single_adjusted_quotes_match_single_stock_values(store):
    api = DailyAPI()
    collector, _, job = make_collector(store, api)
    lower = prepare(collector)
    daily_calls = len(api.calls)
    prices, vals, _ = collector._bars_baostock("600036", lower, "2025-02-28")
    assert api.calls[daily_calls:] == [("stock", "sh.600036", "1", lower, "2025-02-28")]
    assert prices.adj_close.eq(prices.close * 2).all()
    assert prices.volume.eq(1000000).all() and vals.pe_ttm.eq(12).all()
    collector.daily_prices.clear()
    reference_prices, reference_vals, _ = collector._bars_baostock("600036", lower, "2025-02-28")
    pd.testing.assert_frame_equal(prices.drop(columns="source"), reference_prices.drop(columns="source"))
    pd.testing.assert_frame_equal(vals.drop(columns="source"), reference_vals.drop(columns="source"))
    progress = Jobs(store).get(job)["progress"]
    assert progress["daily_raw_status"] == "complete" and not any(progress["daily_raw_missing_members"].values())


@pytest.mark.parametrize("status,expected", [("", None), ("0", True)])
def test_daily_quotes_do_not_invent_status_or_turnover(store, status, expected):
    api = DailyAPI()
    api.status = status
    collector, _, _ = make_collector(store, api)
    lower = prepare(collector)
    for frame in collector.daily_prices.values():
        frame["amount"], frame["volume"] = "", ""
    prices, _, _ = collector._bars_baostock("600036", lower, "2025-02-28")
    assert prices[["amount", "volume"]].isna().all().all()
    assert prices.suspended.isna().all() if expected is None else prices.suspended.eq(expected).all()


def test_missing_member_uses_verified_per_stock_fallback_and_records_batch_gap(store):
    api = DailyAPI()
    api.missing_code = "sh.600036"
    collector, _, job = make_collector(store, api)
    lower = prepare(collector)
    before = len(api.calls)
    prices, _, _ = collector._bars_baostock("600036", lower, "2025-02-28")
    assert len(prices) == len(collector.daily_sessions)
    assert [call[2] for call in api.calls[before:]] == ["3", "1"]
    assert set(Jobs(store).get(job)["progress"]["daily_raw_missing_members"].values()) == {1}
    assert Jobs(store).get(job)["progress"]["daily_raw_status"] == "partial_members"


def test_incomplete_date_window_is_not_silently_shortened(store):
    api = DailyAPI()
    collector, _, job = make_collector(store, api)
    api.bad_date = "2025-02-26"
    lower = prepare(collector)
    assert Jobs(store).get(job)["progress"]["daily_raw_status"] == "per_stock_fallback"
    assert ("daily", "2025-02-28") not in api.calls
    before = len(api.calls)
    prices, _, _ = collector._bars_baostock("600036", lower, "2025-02-28")
    assert prices.trade_date.max() == "2025-02-28"
    assert [call[2] for call in api.calls[before:]] == ["3", "1"]


def test_revised_61_day_range_uses_old_path_when_not_in_daily_window(store):
    api = DailyAPI()
    collector, _, _ = make_collector(store, api)
    prepare(collector)
    before = len(api.calls)
    prices, _, _ = collector._bars_baostock("600036", "2024-11-01", "2025-02-28")
    assert len(prices) > 61 and [call[2] for call in api.calls[before:]] == ["3", "1"]


def test_bootstrap_does_not_download_430_daily_market_tables(store):
    api = DailyAPI()
    collector, _, job = make_collector(store, api, seed=False)
    prepare(collector)
    assert api.calls == []
    assert Jobs(store).get(job)["progress"]["daily_raw_status"] == "single_stock_bootstrap"


def test_default_auto_does_not_request_daily_market_tables(store):
    api = DailyAPI()
    collector, _, _ = make_collector(store, api, mode="auto")
    prepare(collector)
    assert api.calls == []


def test_resume_reuses_date_checkpoints_and_new_day_rechecks_recent_dates(store):
    instant = [datetime(2025, 2, 28, 18, tzinfo=ZoneInfo("Asia/Shanghai"))]
    api = DailyAPI()
    collector, _, _ = make_collector(store, api, clock=lambda: instant[0])
    prepare(collector)
    first_count = len(api.calls)
    original_seen = collector.daily_prices["sh.600036"]["_raw_fetched_at"].iloc[0]
    instant[0] += timedelta(days=1)
    prepare(collector)
    assert len(api.calls) == first_count
    assert collector.daily_prices["sh.600036"]["_raw_fetched_at"].iloc[0] == original_seen
    new, _, _ = make_collector(store, api, clock=lambda: instant[0], case="new-day-job")
    prepare(new)
    assert len(api.calls) == 2 * first_count


def test_service_freezes_price_mode_on_resume(tmp_path, monkeypatch):
    service = FinSight(tmp_path)
    monkeypatch.setattr(service, "run_job", lambda job_id, **kwargs: Jobs(service.store).get(job_id))
    job = service.sync("akshare", mainboard=True, limit=100, price_source="daily")
    assert job["config"]["price_source"] == "daily"
    assert service.sync(resume=job["job_id"])["config"] == job["config"]
    with pytest.raises(ValueError, match="fixed for a resumed job"):
        service.sync(resume=job["job_id"], price_source="auto")


@pytest.mark.parametrize("provider,mainboard,mode", [("demo", True, "daily"), ("akshare", False, "daily"),
    ("akshare", True, "unknown")])
def test_invalid_daily_profiles_do_not_create_jobs(tmp_path, provider, mainboard, mode):
    service = FinSight(tmp_path)
    with pytest.raises(ValueError):
        service.sync(provider, mainboard=mainboard, price_source=mode)
    assert Jobs(service.store).list() == []


def test_worker_daily_route_preserves_actual_sdk_fields(monkeypatch):
    monkeypatch.setattr(alternate_query, "logged_in", True, raising=False)
    response = ResultData()
    response.error_code = "0"
    expected = DailyAPI().baostock_daily_kline("2025-02-28")
    response.fields, response.data = list(expected.columns), expected.values.tolist()
    seen = []
    monkeypatch.setattr(bs, "query_daily_history_k_AStock", lambda date: seen.append(date) or response)
    actual = alternate_query("baostock_daily_kline", {"date": "2025-02-28"})
    assert seen == ["2025-02-28"]
    pd.testing.assert_frame_equal(actual, expected)


def test_cli_passes_price_mode_without_starting_a_collector(tmp_path, monkeypatch, capsys):
    from finsight.cli import main
    observed = {}
    monkeypatch.setattr(FinSight, "sync", lambda self, *a, **k: observed.update(k) or {"status": "queued"})
    assert main(["--data-dir", str(tmp_path), "sync", "--provider", "akshare", "--universe", "mainboard",
        "--price-source", "daily", "--background"]) == 0
    assert observed["price_source"] == "daily"
    assert "queued" in capsys.readouterr().out


def test_incremental_daily_flow_keeps_ranking_and_hfq_identical_to_single_stock(tmp_path, monkeypatch):
    services = [FinSight(tmp_path / name) for name in ("single", "daily")]
    apis = [DailyAPI(), DailyAPI()]
    common = {"provider": "akshare", "mainboard": True, "symbols": ["600036", "000001"],
        "financial_source": "single", "price_source": "daily", "retries": 0, "interval": 0}
    for service, api in zip(services, apis):
        first = service.sync(**common, as_of=date(2025, 2, 27), start=date(2024, 11, 1), api=api,
            clock=lambda: datetime(2025, 2, 27, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
        assert first["publishable"]
        api.calls.clear()
    prepare_daily = Collector._prepare_daily_prices
    outcomes = []
    for index, (service, api) in enumerate(zip(services, apis)):
        monkeypatch.setattr(Collector, "_prepare_daily_prices", (lambda *a: None) if index == 0 else prepare_daily)
        result = service.sync(**common, as_of=date(2025, 2, 28), api=api,
            clock=lambda: datetime(2025, 2, 28, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
        assert result["publishable"]
        screening = service.store.run(result["screen_run_id"])
        outcomes.append(screening["payload"])
    provenance = {"market_source", "valuation_source"}
    assert len(outcomes[0]["candidates"]) == len(outcomes[1]["candidates"]) == 2
    for single, daily in zip(outcomes[0]["candidates"], outcomes[1]["candidates"], strict=True):
        assert {k: v for k, v in single.items() if k not in provenance} == {k: v for k, v in daily.items() if k not in provenance}
        assert single["market_source"] != daily["market_source"]
        assert single["valuation_source"] != daily["valuation_source"]
    assert outcomes[0]["exclusions"] == outcomes[1]["exclusions"]
    assert sum(call[0] == "stock" and call[2] == "3" for call in apis[0].calls) == 2
    assert sum(call[0] == "stock" and call[2] == "3" for call in apis[1].calls) == 0
    assert sum(call[0] == "stock" and call[2] == "1" for call in apis[1].calls) == 2


def test_stale_seed_uses_individual_history_without_unused_daily_downloads(store):
    api = DailyAPI()
    collector, _, job = make_collector(store, api)
    for symbol in collector.base_groups["prices"]:
        collector.base_groups["prices"][symbol] = pd.DataFrame({"trade_date": ["2024-01-02"]})
    prepare(collector)
    assert api.calls == [] and Jobs(store).get(job)["progress"]["daily_raw_status"] == "single_stock_bootstrap"


@pytest.mark.parametrize("adjustment", ["1", "3"])
def test_incomplete_cached_quote_window_is_refetched_once_when_data_arrives(store, adjustment):
    class Lagging(DailyAPI):
        lag = True

        def baostock_kline(self, **kwargs):
            frame = super().baostock_kline(**kwargs)
            if self.lag and kwargs["adjustflag"] == adjustment:
                frame = frame[frame["date"] != kwargs["end_date"]]
            return frame

    api = Lagging()
    instant = [datetime(2025, 2, 28, 18, tzinfo=ZoneInfo("Asia/Shanghai"))]
    collector, _, _ = make_collector(store, api, clock=lambda: instant[0])
    first, _, _ = collector._bars_baostock("600036", "2025-02-24", "2025-02-28")
    assert len(api.calls) == 2  # A fresh partial response does not trigger a request loop.
    assert (first.trade_date.max() == "2025-02-27" if adjustment == "3" else first.adj_close.isna().any())
    api.lag = False
    instant[0] += timedelta(minutes=1)
    second, _, _ = collector._bars_baostock("600036", "2025-02-24", "2025-02-28")
    assert second.trade_date.max() == "2025-02-28" and second.adj_close.notna().all()
    assert len(api.calls) == 3 and api.calls[-1][2] == adjustment


@pytest.mark.parametrize("problem", ["wrong_date", "missing_valuation"])
def test_worker_rejects_bad_daily_identity_before_returning_success(monkeypatch, problem):
    monkeypatch.setattr(alternate_query, "logged_in", True, raising=False)
    response = ResultData()
    expected = DailyAPI().baostock_daily_kline("2025-02-28")
    if problem == "wrong_date":
        expected["date"] = "2025-02-27"
    else:
        expected = expected.drop(columns="peTTM")
    response.error_code, response.fields, response.data = "0", list(expected.columns), expected.values.tolist()
    monkeypatch.setattr(bs, "query_daily_history_k_AStock", lambda **kwargs: response)
    with pytest.raises(ValueError):
        alternate_query("baostock_daily_kline", {"date": "2025-02-28"})


def test_bulk_timeout_does_not_extend_single_stock_deadline(store):
    collector, broker, _ = make_collector(store, DailyAPI())
    calls = []

    class Pool:
        def execute(self, method, kwargs, timeout, **extra):
            calls.append((method, timeout))
            return {"frame": pd.DataFrame({"value": [1]})}

    broker.api, broker.pool = None, Pool()
    broker.call("baostock_daily_kline", date="2025-02-28")
    broker.call("baostock_kline", code="sh.600036", adjustflag="1")
    assert calls == [("baostock_daily_kline", 120), ("baostock_kline", 30)]
