from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from finsight.data.akshare_provider import AKShareProvider
from finsight.data.quality import audit
from finsight.screening import screen


class FakeAKShare:
    def tool_trade_date_hist_sina(self):
        return pd.DataFrame({"trade_date": pd.bdate_range("2024-01-02", "2025-06-30")})

    def stock_individual_info_em(self, symbol, timeout):
        return pd.DataFrame({"item": ["股票简称", "上市时间", "行业"],
                             "value": ["测试" + symbol, 20000101, "行业" + str(int(symbol[-1]) % 7)]})

    def stock_financial_analysis_indicator_em(self, symbol, indicator):
        return pd.DataFrame({"REPORT_DATE": ["2024-12-31", "2025-03-31"],
                             "NOTICE_DATE": ["2025-04-15", "2025-04-30"],
                             "ROEJQ": [12.0, 3.0], "TOTALOPERATEREVETZ": [8.0, 4.0]})

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust):
        days = pd.bdate_range(start_date, end_date)
        close = np.linspace(20, 30, len(days))
        return pd.DataFrame({"日期": days, "开盘": close, "收盘": close * (2 if adjust else 1),
                             "最高": close * 1.01, "最低": close * .99,
                             "成交额": 100_000_000, "成交量": 100_000})

    def stock_value_em(self, symbol):
        return pd.DataFrame({"数据日期": pd.bdate_range("2024-01-02", "2025-05-30"),
                             "PE(TTM)": 12.0, "市净率": 1.5})

    def stock_zh_index_daily_em(self, symbol, start_date, end_date):
        days = pd.bdate_range(start_date, end_date)
        return pd.DataFrame({"date": days, "close": np.linspace(3000, 4000, len(days))})


def test_twenty_stock_live_adapter_and_mixed_metadata_cache(tmp_path):
    provider = AKShareProvider(tmp_path, api=FakeAKShare())
    provider.captured_at = datetime(2025, 5, 30, 18, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    bundle = provider.fetch()
    assert len(bundle["universe"]) == 20
    assert len(bundle["financials"]) == 20  # annual reports only
    assert set(bundle["financials"]["report_period"]) == {"2024-12-31"}
    assert set(bundle["financials"]["available_on"]) == {"2025-05-30"}
    assert bundle["prices"]["volume"].iloc[0] == 10_000_000  # lots -> shares
    assert audit(bundle)["publishable"]
    assert not bundle.meta.research_eligible
    assert screen(bundle).eligible_count == 20
    # The formerly mixed string/integer value column can be read back from Parquet.
    metadata = provider.call("stock_individual_info_em", symbol="600036", timeout=10)
    assert metadata["value"].iloc[1] == "20000101"


def test_whole_financial_failure_is_not_publishable(tmp_path):
    class NoFinancials(FakeAKShare):
        def stock_financial_analysis_indicator_em(self, **kwargs):
            return pd.DataFrame()
    provider = AKShareProvider(tmp_path, api=NoFinancials(), retries=0)
    provider.captured_at = datetime(2025, 5, 30, 18, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    bundle = provider.fetch(symbols=["600036"])
    assert not audit(bundle)["publishable"]
    with pytest.raises(ValueError, match="formal ranking"):
        screen(bundle)


def test_before_close_cannot_request_current_session(tmp_path):
    provider = AKShareProvider(tmp_path, api=FakeAKShare())
    provider.captured_at = datetime(2025, 5, 30, 17, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(ValueError, match="18:00"):
        provider.fetch(provider.captured_at.date())


def test_mainboard_list_fallback_does_not_invent_listing_dates(tmp_path):
    class BrokenListing:
        def stock_info_sh_name_code(self, **kwargs):
            raise ValueError("upstream unavailable")
        def stock_zh_a_spot_em(self):
            return pd.DataFrame({"代码": ["600036", "300001", "000333", "688001"], "名称": ["A", "B", "C", "D"]})
    provider = AKShareProvider(tmp_path, api=BrokenListing(), retries=0)
    result = provider.stock_list()
    assert set(result["code"]) == {"600036", "000333"}
    assert result["listed_on"].isna().all()
    assert provider.issues
