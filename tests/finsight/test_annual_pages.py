from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from finsight.data.collector import Collector
from finsight.data.eastmoney import annual_page, annual_pages
from finsight.data.requests import RequestBroker, RequestFailure
from finsight.jobs import Jobs
from finsight.service import FinSight


def page_frame(page=1, period="2024-12-31"):
    codes = ["000001", "000002"] if page == 1 else ["600036"]
    return pd.DataFrame([{"SECURITY_CODE": code, "SECURITY_TYPE_CODE": "058001001", "REPORTDATE": period,
        "WEIGHTAVG_ROE": None if code == "000002" else 12., "YSTZ": -8., "NOTICE_DATE": "2025-04-15",
        "UPDATE_DATE": "2025-04-15", "_response_page": page, "_response_pages": 2,
        "_response_count": 3, "_query_scope": "ashare_v1"} for code in codes])


class PageAPI:
    def __init__(self):
        self.pages = []
        self.fail = False

    def eastmoney_annual_page(self, report_date, page, page_size, scope):
        assert page_size == 500 and scope == "ashare_v1"
        self.pages.append(page)
        if page == 2 and self.fail:
            raise RequestFailure("timeout", "eastmoney_annual_page")
        return page_frame(page, report_date)


def make_collector(store, api, clock, *, base=None):
    if base is not None:
        base.meta.captured_at = datetime(2025, 2, 28, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
    job = Jobs(store).create({"case": "annual-pages", "mainboard": True})
    broker = RequestBroker(store, job["job_id"], api=api, interval=0, retries=0, clock=clock)
    return Collector(broker, {"mainboard": True, "financial_source": "paged", "financial_page_size": 500}, base=base), broker, job["job_id"]


def calendar():
    return pd.DataFrame({"trade_date": pd.bdate_range("2024-01-01", "2027-01-01").strftime("%Y-%m-%d")})


def test_resume_reuses_completed_page_and_keeps_original_visibility(store):
    instant = [datetime(2025, 5, 30, 18, tzinfo=ZoneInfo("Asia/Shanghai"))]
    api = PageAPI()
    api.fail = True
    collector, broker, job = make_collector(store, api, lambda: instant[0])
    with pytest.raises(RequestFailure, match="timeout"):
        collector._annual(2024, calendar())
    assert api.pages == [1, 2]
    instant[0] += timedelta(days=1)
    api.fail = False
    frame = collector._annual(2024, calendar())
    assert api.pages == [1, 2, 2]
    assert len(frame) == 3 and frame["revenue_growth"].eq(-8).all()
    assert frame.loc[frame["symbol"] == "000002.SZ", "roe"].isna().all()
    assert frame.loc[frame["symbol"] == "000001.SZ", "fetched_at"].iloc[0].startswith("2025-05-30")
    assert frame.loc[frame["symbol"] == "600036.SH", "available_on"].iloc[0] == "2025-05-31"
    assert frame["source"].eq("eastmoney:RPT_LICO_FN_CPD:WEIGHTAVG_ROE").all()
    assert frame["roe_basis"].eq("weighted_annual").all()
    progress = Jobs(store).get(job)["progress"]
    assert progress["financial_page"] == progress["financial_pages"] == 2
    requests = Jobs(store).get(job)["requests"]
    assert requests["cache_hits"] == 1 and requests["network_attempts"] == 3
    assert broker.timeout == 30


def test_update_date_and_first_observation_do_not_backdate_a_revision(store):
    api = PageAPI()
    collector, _, _ = make_collector(store, api,
        lambda: datetime(2025, 5, 30, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
    original = api.eastmoney_annual_page

    def updated(**kwargs):
        frame = original(**kwargs)
        frame["UPDATE_DATE"] = "2025-05-30"
        return frame

    api.eastmoney_annual_page = updated
    frame = collector._annual(2024, calendar())
    assert frame["announcement_date"].eq("2025-05-30").all()
    assert frame["available_on"].eq("2025-06-02").all()


@pytest.mark.parametrize("problem", ["changed_count", "changed_pages", "duplicate", "wrong_page", "wrong_scope",
    "wrong_period", "missing_weighted_roe", "missing_row", "unsorted", "boolean_pages", "missing_date"])
def test_incomplete_or_mixed_batches_never_return_partial_financials(problem):
    def call(method, **kwargs):
        page = kwargs["page"]
        frame = page_frame(page)
        if page == 2:
            if problem == "changed_count":
                frame["_response_count"] = 4
            if problem == "changed_pages":
                frame["_response_pages"] = 3
            if problem == "duplicate":
                frame["SECURITY_CODE"] = "000001"
            if problem == "wrong_page":
                frame["_response_page"] = 1
            if problem == "wrong_scope":
                frame["_query_scope"] = "another_universe"
            if problem == "wrong_period":
                frame["REPORTDATE"] = "2024-09-30"
            if problem == "missing_weighted_roe":
                frame = frame.drop(columns="WEIGHTAVG_ROE")
            if problem == "missing_row":
                frame = frame.iloc[:0]
            if problem == "unsorted":
                frame["SECURITY_CODE"] = "000000"
            if problem == "boolean_pages":
                frame["_response_pages"] = True
            if problem == "missing_date":
                frame = frame.drop(columns="NOTICE_DATE")
        frame.attrs["fetched_at"] = "2025-05-30T18:00:00+08:00"
        return frame

    with pytest.raises(ValueError):
        annual_pages(call, "2024-12-31")


def test_new_bulk_path_can_recover_independently_of_market_failover(store, bundle):
    collector, _, job = make_collector(store, PageAPI(),
        lambda: datetime(2026, 10, 29, 18, tzinfo=ZoneInfo("Asia/Shanghai")), base=bundle)
    collector.fallback_market = True
    years = []
    collector._annual = lambda year, dates: years.append(year) or pd.DataFrame({"report_period": [f"{year}-12-31"]})
    result = collector._annual_reports(date(2025, 1, 1), "2026-10-29", calendar())
    assert years == [2025]  # Older reports cannot pass the 550-day selection limit.
    assert len(result) == 1
    assert Jobs(store).get(job)["progress"]["financial_bulk_status"] == "complete"


def test_latest_batch_failure_discards_older_batch_and_requires_single_stock_recheck(store, bundle):
    collector, _, job = make_collector(store, PageAPI(),
        lambda: datetime(2026, 5, 29, 18, tzinfo=ZoneInfo("Asia/Shanghai")), base=bundle)
    years = []

    def one_year(year, dates):
        years.append(year)
        if year == 2025:
            raise ValueError("latest batch unavailable")
        return pd.DataFrame({"report_period": ["2024-12-31"]})

    collector._annual = one_year
    result = collector._annual_reports(date(2025, 1, 1), "2026-05-29", calendar())
    assert years == [2024, 2025] and result.empty and collector.fallback_market
    assert Jobs(store).get(job)["progress"]["financial_bulk_status"] == "per_stock_fallback"
    assert Jobs(store).get(job)["progress"]["financial_bulk_failed_at"] == "2026-05-29T18:00:00+08:00"


def test_schema_failure_cooldown_uses_failure_time_instead_of_job_completion(store):
    jobs = Jobs(store)
    prior = jobs.create({"case": "schema-failure", "financial_source": "paged"})
    failed_at = datetime(2026, 10, 29, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    jobs.update(prior["job_id"], status="completed", progress={"financial_bulk_status": "per_stock_fallback",
        "financial_bulk_failed_at": failed_at.isoformat()})
    # Finishing or resuming after midnight must not move the source-failure day.
    with store.connect() as db:
        db.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", ("2026-10-30T03:00:00+08:00", prior["job_id"]))
    for now, expected_skip in [(failed_at, True), (failed_at + timedelta(minutes=2), False)]:
        current = jobs.create({"case": f"schema-check-{now.isoformat()}"})
        broker = RequestBroker(store, current["job_id"], api=object(), clock=lambda: now)
        broker.api = None
        collector = Collector(broker, {"mainboard": True, "financial_source": "paged"})
        assert collector.skip_bulk_financials is expected_skip


def test_legacy_batch_failure_does_not_disable_opt_in_paged_source(store):
    now = datetime(2026, 10, 29, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
    prior = Jobs(store).create({"case": "legacy-failure", "financial_source": "auto"})
    Jobs(store).update(prior["job_id"], progress={"financial_bulk_status": "per_stock_fallback",
        "financial_bulk_failed_at": now.isoformat()})
    current = Jobs(store).create({"case": "paged-after-legacy"})
    broker = RequestBroker(store, current["job_id"], api=object(), clock=lambda: now)
    broker.api = None
    assert not Collector(broker, {"mainboard": True, "financial_source": "paged"}).skip_bulk_financials


def test_same_day_failed_source_skips_new_job_but_not_explicit_resume_or_next_day(store):
    now = datetime(2026, 10, 29, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
    jobs = Jobs(store)
    prior = jobs.create({"case": "prior"})
    old_broker = RequestBroker(store, prior["job_id"], api=object())
    old_broker._record("failed-page", "eastmoney_annual_page", {}, "failed", error_kind="timeout", loaded_at=now.isoformat())
    for same_job, next_day, expected_skip in [(False, False, True), (True, False, False), (False, True, False)]:
        job = prior if same_job else jobs.create({"case": f"new-{next_day}"})
        broker = RequestBroker(store, job["job_id"], api=object(), clock=lambda: now + timedelta(days=int(next_day)))
        broker.api = None  # Read the real circuit-breaker tables without starting any workers.
        collector = Collector(broker, {"mainboard": True, "financial_source": "paged"})
        assert collector.skip_bulk_financials is expected_skip


def test_http_page_uses_named_fields_stable_sort_and_ashare_scope(monkeypatch):
    frame = page_frame().drop(columns=["_response_page", "_response_pages", "_response_count", "_query_scope"])
    seen = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"pages": 2, "count": 3, "data": frame.to_dict("records")}}

    def get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr("finsight.data.eastmoney.requests.get", get)
    result = annual_page("2024-12-31", 1)
    assert len(result) == 2 and result["_response_pages"].eq(2).all()
    assert seen["timeout"] == 25 and seen["params"]["sortColumns"] == "SECURITY_CODE"
    assert 'SECURITY_TYPE_CODE="058001001"' in seen["params"]["filter"]
    assert result["WEIGHTAVG_ROE"].iloc[0] == 12.


@pytest.mark.parametrize("kwargs", [{"report_date": "2024-09-30"}, {"page": True}, {"page": 0},
    {"page_size": 501}, {"scope": "all"}])
def test_invalid_page_parameters_do_not_issue_requests(monkeypatch, kwargs):
    monkeypatch.setattr("finsight.data.eastmoney.requests.get", lambda *a, **k: pytest.fail("unexpected network request"))
    with pytest.raises(ValueError):
        annual_page(**({"report_date": "2024-12-31", "page": 1} | kwargs))


@pytest.mark.parametrize("period", ["2024-09-30", "20241231", "2024-12-31junk"])
def test_invalid_aggregate_period_is_rejected_before_using_broker(period):
    with pytest.raises(ValueError):
        annual_pages(lambda *a, **k: pytest.fail("unexpected broker request"), period)


@pytest.mark.parametrize("count,pages", [(1, 2), (1001, 2), (True, 2)])
def test_impossible_metadata_does_not_trigger_more_pages(count, pages):
    calls = []

    def call(method, **kwargs):
        calls.append(kwargs["page"])
        frame = page_frame()
        frame["_response_count"], frame["_response_pages"] = count, pages
        frame.attrs["fetched_at"] = "2025-05-30T18:00:00+08:00"
        return frame

    with pytest.raises(ValueError, match="metadata"):
        annual_pages(call, "2024-12-31")
    assert calls == [1]


@pytest.mark.parametrize("mode,fallback", [("auto", True), ("single", False)])
def test_existing_auto_fallback_and_explicit_single_do_not_probe_new_source(store, mode, fallback):
    collector, _, _ = make_collector(store, PageAPI(),
        lambda: datetime(2026, 10, 29, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
    collector.financial_source = mode
    collector.fallback_market = fallback
    collector._annual = lambda *_: pytest.fail("unexpected annual request")
    assert collector._annual_reports(date(2025, 1, 1), "2026-10-29", calendar()).empty


def test_job_persists_opt_in_and_resume_cannot_change_financial_source(tmp_path, monkeypatch):
    service = FinSight(tmp_path)

    def queued(job_id, **kwargs):
        return Jobs(service.store).get(job_id)

    monkeypatch.setattr(service, "run_job", queued)
    job = service.sync("akshare", mainboard=True, limit=100, financial_source="paged", financial_page_size=250)
    assert job["config"]["financial_source"] == "paged"
    assert job["config"]["limit"] == 100
    assert job["config"]["financial_page_size"] == 250
    assert job["config"]["strategy"]["version"] == "strategy_v0.1"
    same = service.sync("akshare", resume=job["job_id"])
    assert same["job_id"] == job["job_id"] and same["config"] == job["config"]
    with pytest.raises(ValueError, match="fixed for a resumed job"):
        service.sync("akshare", resume=job["job_id"], financial_source="single")
    with pytest.raises(ValueError, match="page size is fixed"):
        service.sync("akshare", resume=job["job_id"], financial_page_size=100)


@pytest.mark.parametrize("provider,mainboard,mode", [("demo", True, "paged"), ("akshare", False, "paged"),
    ("akshare", True, "invalid")])
def test_invalid_financial_source_profiles_do_not_start_jobs(tmp_path, provider, mainboard, mode):
    service = FinSight(tmp_path)
    with pytest.raises(ValueError):
        service.sync(provider, mainboard=mainboard, financial_source=mode)
    assert Jobs(service.store).list() == []


def test_page_size_cache_keys_are_isolated(store):
    class SizedAPI:
        sizes = []

        def eastmoney_annual_page(self, report_date, page, page_size, scope):
            self.sizes.append(page_size)
            frame = page_frame()
            frame["_response_pages"], frame["_response_count"] = 1, 2
            return frame

    api = SizedAPI()
    _, broker, job = make_collector(store, api,
        lambda: datetime(2025, 5, 30, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
    for size in (100, 500, 100):
        assert len(annual_pages(broker.call, "2024-12-31", page_size=size)) == 2
    assert api.sizes == [100, 500]
    assert Jobs(store).get(job)["requests"]["cache_hits"] == 1


@pytest.mark.parametrize("mode,size", [("paged", True), ("paged", 0), ("paged", 501), ("paged", 123), (None, 100)])
def test_invalid_page_size_does_not_start_collection(tmp_path, mode, size):
    service = FinSight(tmp_path)
    with pytest.raises(ValueError):
        service.sync("akshare", mainboard=True, financial_source=mode, financial_page_size=size)
    assert Jobs(service.store).list() == []


def test_cli_forwards_opt_in_page_settings_without_network(tmp_path, monkeypatch, capsys):
    from finsight.cli import main
    observed = {}

    def sync(self, *args, **kwargs):
        observed.update(kwargs)
        return {"status": "queued", "publishable": False}

    monkeypatch.setattr(FinSight, "sync", sync)
    assert main(["--data-dir", str(tmp_path), "sync", "--provider", "akshare", "--universe", "mainboard",
        "--limit", "100", "--background", "--financial-source", "paged", "--financial-page-size", "250"]) == 0
    assert observed["financial_source"] == "paged" and observed["financial_page_size"] == 250
    assert "queued" in capsys.readouterr().out
