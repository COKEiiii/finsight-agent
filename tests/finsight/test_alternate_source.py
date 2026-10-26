from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from finsight.data.collector import Collector
from finsight.data.requests import RequestBroker
from finsight.data.store import Store
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
