from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from finsight.data.collector import Collector, merge_observations
from finsight.data.quality import audit
from finsight.data.requests import RequestBroker
from finsight.data.store import Store
from finsight.factors import coverage_report, evaluate
from finsight.jobs import Jobs


class AlternateAPI:
    def baostock_kline(self, code, start_date, end_date, adjustflag):
        dates = pd.bdate_range("2026-07-01", "2026-10-26").strftime("%Y-%m-%d")
        rows = []
        for index, day in enumerate(dates):
            if not start_date <= day <= end_date:
                continue
            row = {"date": day, "close": str(10 + index / 100)}
            if adjustflag == "3":
                row.update(code=code, open="10", high="11", low="9", preclose="10", volume="1000000",
                           amount="100000000", tradestatus="1", peTTM="8", pbMRQ="1.2", isST="0")
            rows.append(row)
        return pd.DataFrame(rows)

    def stock_financial_report_sina(self, stock, symbol):
        assert symbol == "利润表"
        return pd.DataFrame({"报告日": ["20251231", "20241231"], "营业总收入": [110., 100.],
                             "公告日期": ["20260315", "20250315"]})

    def baostock_profit(self, code, year):
        return pd.DataFrame({"statDate": [f"{year}-12-31"], "pubDate": ["2026-03-20"], "roeAvg": ["0.10"]})

    def stock_financial_analysis_indicator_em(self, symbol, indicator):
        return pd.DataFrame({"REPORT_DATE": ["2025-12-31"], "NOTICE_DATE": ["2026-03-20"],
                             "ROEJQ": [10.2], "TOTALOPERATEREVETZ": [10.]})


def status_collector(tmp_path, status, *, omit_status=False, empty_turnover=False):
    class StatusAPI(AlternateAPI):
        def baostock_kline(self, **kwargs):
            frame = super().baostock_kline(**kwargs)
            if kwargs["adjustflag"] == "3":
                if omit_status:
                    frame = frame.drop(columns="tradestatus")
                else:
                    frame["tradestatus"] = status
                if empty_turnover:
                    frame["amount"] = ""
                    frame["volume"] = ""
            return frame

    store = Store(tmp_path)
    job = Jobs(store).create({"case": "trade-status"})
    broker = RequestBroker(store, job["job_id"], api=StatusAPI(), interval=0, retries=0,
        clock=lambda: datetime(2026, 10, 26, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
    return Collector(broker, {"mainboard": True})


@pytest.mark.parametrize("status,expected", [
    ("0", True), ("1", False), (" 0 ", True), (" 1 ", False), (0, True), (1, False),
    ("", None), (" ", None), (None, None), ("2", None), ("unknown", None),
    (True, None), (False, None), (0.0, None), (1.0, None),
])
def test_baostock_unknown_status_stays_nullable(tmp_path, status, expected):
    collector = status_collector(tmp_path, status)
    prices, _, _ = collector._bars_baostock("600036", "2026-07-01", "2026-10-26")
    assert str(prices["suspended"].dtype) == "boolean"
    if expected is None:
        assert prices["suspended"].isna().all()
    else:
        assert prices["suspended"].eq(expected).all()


def test_baostock_missing_status_column_preserves_prices_as_unknown(tmp_path):
    collector = status_collector(tmp_path, None, omit_status=True)
    prices, valuations, _ = collector._bars_baostock("600036", "2026-07-01", "2026-10-26")
    assert len(prices) == len(valuations) >= 61
    assert prices["suspended"].isna().all()


def test_baostock_suspension_does_not_invent_turnover(tmp_path):
    collector = status_collector(tmp_path, "0", empty_turnover=True)
    prices, _, _ = collector._bars_baostock("600036", "2026-07-01", "2026-10-26")
    assert prices["suspended"].all()
    assert prices[["amount", "volume"]].isna().all().all()


def test_status_dtype_change_keeps_first_visibility_but_unknown_is_a_revision(tmp_path):
    collector = status_collector(tmp_path, "1")
    new, _, _ = collector._bars_baostock("600036", "2026-07-01", "2026-10-26")
    old = new.copy()
    old["suspended"] = old["suspended"].astype(bool)
    old["fetched_at"] = "2026-10-25T18:00:00+08:00"
    merged = merge_observations(old, new, ["symbol", "trade_date"])
    assert merged["fetched_at"].eq("2026-10-25T18:00:00+08:00").all()
    new.loc[new["trade_date"] == "2026-10-26", "suspended"] = pd.NA
    revised = merge_observations(old, new, ["symbol", "trade_date"])
    current = revised.loc[revised["trade_date"] == "2026-10-26"].iloc[0]
    assert pd.isna(current["suspended"])
    assert current["fetched_at"] == "2026-10-26T18:00:00+08:00"


@pytest.mark.parametrize("status,category,code", [
    ("0", "structural_rule", "suspended"),
    ("1", "strategy_rule", "nonpositive_valuation"),
    ("", "data", "unknown_suspension"),
    (None, "data", "unknown_suspension"),
])
def test_parsed_status_keeps_unknown_in_coverage_even_with_negative_valuation(
        tmp_path, bundle, config, status, category, code):
    collector = status_collector(tmp_path, status)
    prices, _, _ = collector._bars_baostock("600036", "2026-07-01", "2026-10-26")
    symbol = bundle["universe"].iloc[0]["symbol"]
    bundle.tables["prices"]["suspended"] = bundle["prices"]["suspended"].astype("boolean")
    current = (bundle["prices"]["symbol"] == symbol) & (bundle["prices"]["trade_date"] == str(bundle.meta.as_of))
    bundle.tables["prices"].loc[current, "suspended"] = prices["suspended"].iloc[-1]
    bundle.tables["valuations"].loc[bundle["valuations"]["symbol"] == symbol, "pe_ttm"] = -5.
    frame, exclusions = evaluate(bundle, bundle.meta.as_of, config)
    row = next(item for item in exclusions if item["symbol"] == symbol)
    assert row["category"] == category and code in row["codes"]
    coverage = coverage_report(bundle, frame, exclusions)
    assert coverage["missing_count"] == int(category == "data")
    assert coverage["confirmed_exclusions"] == int(category == "structural_rule")


def test_free_alternate_source_preserves_factor_units_and_first_visibility(tmp_path):
    store = Store(tmp_path)
    job = Jobs(store).create({"source": "fixture"})
    broker = RequestBroker(store, job["job_id"], api=AlternateAPI(), workers=1, interval=0, retries=0,
                           clock=lambda: datetime(2026, 10, 26, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    collector = Collector(broker, {"mainboard": True})
    prices, valuations, is_st = collector._bars_baostock("600036", "2026-07-01", "2026-10-26")
    assert len(prices) >= 61 and len(valuations) == len(prices) and not is_st
    assert prices["volume"].iloc[0] == 1_000_000  # BaoStock volume is already shares.
    assert valuations["pe_ttm"].iloc[0] == 8 and valuations["pb"].iloc[0] == 1.2
    calendar = pd.DataFrame({"trade_date": pd.bdate_range("2025-01-01", "2026-12-31").strftime("%Y-%m-%d")})
    financials = collector.financial_history_fallback("600036.SH", date(2025, 1, 1), date(2026, 10, 26), calendar)
    assert len(financials) == 1
    row = financials.iloc[0]
    assert row["roe"] == 10.0
    assert abs(row["revenue_growth"] - 10.0) < 1e-9
    assert row["announcement_date"] == "2026-03-20"
    assert row["available_on"] == "2026-10-26"
    assert "baostock" in row["source"] and "sina" in row["source"]
    collector.fallback_market = True
    state, _, _, finance, issues = collector._symbol({"symbol": "600036.SH", "code": "600036", "name": "招商银行",
        "listed_on": "2002-04-09", "observed_at": "2026-10-26T18:00:00+08:00",
        "industry": "J66货币金融服务", "industry_source": "baostock:query_stock_industry"},
        pd.DataFrame(), date(2025, 1, 1), "2026-10-26", calendar)
    assert state["industry"] == "J66货币金融服务"
    assert state["industry_source"] == "baostock:query_stock_industry" and not issues
    assert finance.iloc[-1]["roe"] == 10.2 and finance.iloc[-1]["roe_basis"] == "weighted_annual"


def test_sample_collection_uses_exchange_identities_when_metadata_endpoint_is_down(tmp_path):
    class SampleAPI(AlternateAPI):
        def tool_trade_date_hist_sina(self):
            return pd.DataFrame({"trade_date": pd.bdate_range("2025-01-01", "2026-12-31")})

        def stock_info_sh_name_code(self, symbol):
            return pd.DataFrame({"证券代码": ["600036", "600519"], "证券简称": ["招商银行", "贵州茅台"],
                                 "上市日期": ["2002-04-09", "2001-08-27"]})

        def stock_info_sz_name_code(self, symbol):
            return pd.DataFrame({"A股代码": ["000001"], "A股简称": ["平安银行"],
                                 "A股上市日期": ["1991-04-03"], "板块": ["主板"]})

        def stock_individual_info_em(self, **kwargs):
            pytest.fail("unavailable metadata endpoint must not be retried for every sample stock")

        def baostock_industry(self):
            return pd.DataFrame({"code": ["sh.600036", "sz.000001", "sh.600519"], "industry": ["金融", "金融", "食品"]})

        def baostock_index(self, start_date, end_date):
            return pd.DataFrame({"date": pd.bdate_range(start_date, end_date).strftime("%Y-%m-%d"), "close": 4000.})

    store = Store(tmp_path)
    job = Jobs(store).create({"case": "sample-alternate"})
    broker = RequestBroker(store, job["job_id"], api=SampleAPI(), interval=0, retries=0,
        clock=lambda: datetime(2026, 10, 26, 18, tzinfo=ZoneInfo("Asia/Shanghai")))
    collector = Collector(broker, {"mainboard": False, "symbols": ["600036", "000001"],
        "as_of": "2026-10-26", "start": "2026-07-01"})
    collector.fallback_market = True
    result = collector.fetch()
    assert set(result["universe"]["symbol"]) == {"600036.SH", "000001.SZ"}
    assert result["universe"]["industry"].notna().all()
    assert audit(result)["publishable"]
