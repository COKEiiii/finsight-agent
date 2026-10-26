"""Deterministic, explicitly synthetic fixtures. No generated data is market evidence."""
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from finsight.data.store import SnapshotBundle
from finsight.models import MarketSnapshot

SAMPLE_CODES = (
    "600000", "600036", "600519", "600887", "601318", "601398", "600900", "601088",
    "600309", "601012", "000001", "000333", "000651", "000858", "000725", "002415",
    "002594", "002475", "002714", "002352",
)


def make_demo(as_of: date = date(2025, 12, 31), start: date = date(2022, 1, 3)) -> SnapshotBundle:
    if start >= as_of:
        raise ValueError("demo start must precede as_of")
    # This is deliberately a WEEKDAY fixture, not an exchange calendar.
    dates = pd.bdate_range(start, pd.Timestamp(as_of) + pd.Timedelta(days=45))
    visible = dates[dates.date <= as_of]
    if not len(visible):
        raise ValueError("no demo sessions in requested interval")
    as_of = visible[-1].date()
    rng = np.random.default_rng(20261026)
    prices, valuations, universe, financials = [], [], [], []
    for i, code in enumerate(SAMPLE_CODES):
        symbol = code + (".SH" if code.startswith("6") else ".SZ")
        returns = rng.normal(.00015 + (i % 5) * .00006, .009 + (i % 3) * .001, len(visible))
        closes = np.round((12 + i * 2) * np.exp(np.cumsum(returns)), 2)
        previous = np.r_[closes[0], closes[:-1]]
        opens = np.round(previous * (1 + rng.normal(0, .002, len(visible))), 2)
        for j, day in enumerate(visible):
            prices.append(dict(symbol=symbol, trade_date=str(day.date()), open=float(opens[j]),
                               high=float(max(opens[j], closes[j]) * 1.003),
                               low=float(min(opens[j], closes[j]) * .997), close=float(closes[j]),
                               adj_close=float(closes[j]), volume=8_000_000,
                               amount=float(closes[j] * 8_000_000), prev_close=float(previous[j]),
                               limit_up=round(float(previous[j]) * 1.1 + 1e-8, 2),
                               limit_down=round(float(previous[j]) * .9 + 1e-8, 2),
                               suspended=False, source="synthetic", fetched_at="2026-10-26T00:00:00+08:00"))
            valuations.append(dict(symbol=symbol, trade_date=str(day.date()), pe_ttm=8 + i * 1.1,
                                   pb=.7 + i * .13, source="synthetic", fetched_at="2026-10-26T00:00:00+08:00"))
        universe.append(dict(symbol=symbol, name=f"演示公司{i + 1:02d}（虚构）", industry=f"演示行业{i % 7 + 1}",
                             listed_on="2000-01-01", board="mainboard", is_st=False, is_delisting=False,
                             known_on="2000-01-01", source="synthetic"))
        for year in range(start.year - 1, as_of.year + 1):
            financials.append(dict(symbol=symbol, report_period=f"{year}-12-31",
                                   announcement_date=f"{year + 1}-04-15", available_on=f"{year + 1}-04-16",
                                   roe=5 + i * .8, revenue_growth=-5 + i * 1.5, source="synthetic",
                                   version=f"{year}-original", fetched_at="2026-10-26T00:00:00+08:00"))
    bm = 4000 * np.exp(np.cumsum(rng.normal(.0002, .008, len(visible))))
    tables = {
        "universe": pd.DataFrame(universe), "prices": pd.DataFrame(prices),
        "financials": pd.DataFrame(financials), "valuations": pd.DataFrame(valuations),
        "calendar": pd.DataFrame({"trade_date": dates.strftime("%Y-%m-%d")}),
        "benchmark": pd.DataFrame({"trade_date": visible.strftime("%Y-%m-%d"), "close": bm}),
        "actions": pd.DataFrame(columns=["symbol", "ex_date", "pay_date", "split_ratio", "cash_per_share"]),
    }
    return SnapshotBundle(MarketSnapshot(
        snapshot_id=f"demo-{as_of}-{uuid4().hex[:10]}", as_of=as_of,
        captured_at=datetime.now(ZoneInfo("Asia/Shanghai")), source="synthetic",
        universe_scope="20 synthetic companies; weekday calendar; not real stocks or trading results",
        synthetic=True, quality_issues=["合成演示数据与工作日日历；不可用于投资或策略验证"],
    ), tables)
