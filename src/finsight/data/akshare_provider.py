from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from finsight.data.demo import SAMPLE_CODES
from finsight.data.store import SnapshotBundle
from finsight.models import MarketSnapshot, fingerprint


def normalize_symbol(code: str) -> str:
    code = str(code).split(".")[0].zfill(6)
    if not code.isdigit() or len(code) != 6:
        raise ValueError("invalid mainland stock code")
    return code + (".SH" if code.startswith("6") else ".SZ")


def date_column(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d")


class AKShareProvider:
    """A bounded, cached upstream adapter. Latest records never certify historical PIT integrity."""

    def __init__(self, root: Path, timeout: float = 30, retries: int = 1, api=None):
        self.root = Path(root)
        self.timeout, self.retries, self.api = timeout, retries, api
        self.captured_at = datetime.now(ZoneInfo("Asia/Shanghai"))
        self.issues: list[str] = []

    def call(self, method: str, **kwargs) -> pd.DataFrame:
        # Daily keys preserve updates; previous raw captures are never overwritten.
        key = fingerprint({"method": method, "kwargs": kwargs, "capture_date": str(self.captured_at.date())})
        folder = self.root / "raw" / key
        if (folder / "data.parquet").exists():
            return pd.read_parquet(folder / "data.parquet")
        for attempt in range(self.retries + 1):
            try:
                if self.api is not None:
                    frame = getattr(self.api, method)(**kwargs)
                else:
                    result = subprocess.run(
                        [sys.executable, "-m", "finsight.data.akshare_worker"],
                        input=json.dumps({"method": method, "kwargs": kwargs}),
                        text=True, capture_output=True, timeout=self.timeout, check=True,
                    )
                    frame = pd.read_json(io.StringIO(result.stdout), orient="table")
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    raise ValueError(f"{method}: empty response")
                # Key/value endpoints contain strings and numbers in a single object column.
                # Parquet has no implicit mixed scalar union, so preserve those values as strings.
                for column in frame.select_dtypes(include=["object"]).columns:
                    types = {type(v) for v in frame[column].dropna()}
                    if str in types and len(types) > 1:
                        frame[column] = frame[column].astype("string")
                folder.mkdir(parents=True, exist_ok=True)
                # Only successful calls enter the cache.
                frame.to_parquet(folder / "data.parquet", index=False)
                (folder / "request.json").write_text(json.dumps({
                    "method": method, "kwargs": kwargs, "fetched_at": self.captured_at.isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                return frame
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                if attempt == self.retries:
                    # Keep provider exceptions bounded; never log environment credentials.
                    raise ValueError(f"{method}: failed after {attempt + 1} attempt(s): {type(exc).__name__}") from exc
                time.sleep(.25)
        raise AssertionError("unreachable")

    def calendar(self) -> pd.DataFrame:
        frame = self.call("tool_trade_date_hist_sina")
        return pd.DataFrame({"trade_date": date_column(frame["trade_date"])}).dropna().drop_duplicates().sort_values("trade_date")

    def stock_list(self) -> pd.DataFrame:
        try:
            sh = self.call("stock_info_sh_name_code", symbol="主板A股").rename(columns={
                "证券代码": "code", "证券简称": "name", "上市日期": "listed_on",
            })
            sz = self.call("stock_info_sz_name_code", symbol="A股列表").rename(columns={
                "A股代码": "code", "A股简称": "name", "A股上市日期": "listed_on",
            })
            sz = sz[sz["板块"] == "主板"]
            frame = pd.concat([sh[["code", "name", "listed_on"]], sz[["code", "name", "listed_on"]]], ignore_index=True)
        except (ValueError, KeyError):
            # A current quote list can supply identities, never historical membership/listing dates.
            frame = self.call("stock_zh_a_spot_em").rename(columns={"代码": "code", "名称": "name"})
            frame["code"] = frame["code"].astype(str).str.zfill(6)
            frame = frame[frame["code"].str.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))].copy()
            frame["listed_on"] = None
            frame = frame[["code", "name", "listed_on"]]
            self.issues.append("交易所列表接口不可用，使用当前行情列表确认股票代码；上市日期逐股核实，不回填历史股票池。")
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame["symbol"] = frame["code"].map(normalize_symbol)
        frame["listed_on"] = date_column(frame["listed_on"])
        return frame.drop_duplicates("symbol")

    def sample_list(self, codes: list[str]) -> pd.DataFrame:
        rows = []
        for symbol in sorted({normalize_symbol(c) for c in codes}):
            code = symbol[:6]
            try:
                frame = self.call("stock_individual_info_em", symbol=code, timeout=10)
                values = dict(zip(frame["item"], frame["value"]))
                listed = pd.to_datetime(str(values["上市时间"]).split(".")[0], format="%Y%m%d", errors="coerce")
                rows.append({"code": code, "symbol": symbol, "name": str(values["股票简称"]),
                             "listed_on": None if pd.isna(listed) else listed.strftime("%Y-%m-%d"),
                             "industry": values.get("行业")})
            except (ValueError, KeyError) as exc:
                self.issues.append(f"{symbol}: metadata unavailable: {exc}")
                rows.append({"code": code, "symbol": symbol, "name": code,
                             "listed_on": None, "industry": None})
        return pd.DataFrame(rows)

    def financial_history(self, symbol: str, start: date, day: date, calendar: pd.DataFrame) -> pd.DataFrame:
        frame = self.call("stock_financial_analysis_indicator_em", symbol=symbol, indicator="按报告期").rename(columns={
            "REPORT_DATE": "report_period", "NOTICE_DATE": "announcement_date",
            "ROEJQ": "roe", "TOTALOPERATEREVETZ": "revenue_growth",
        })
        frame["report_period"] = date_column(frame["report_period"])
        frame["announcement_date"] = date_column(frame["announcement_date"])
        frame = frame[frame["report_period"].str.endswith("12-31") &
                      (frame["report_period"] >= f"{start.year - 2}-01-01") &
                      (frame["announcement_date"] <= str(day))].copy()
        frame["available_on"] = frame["announcement_date"].map(
            lambda d: max(next((s for s in calendar["trade_date"] if s > str(d)), "9999-12-31"),
                          str(self.captured_at.date())))
        frame["symbol"] = symbol
        frame["version"] = "latest-unverified-" + str(self.captured_at.date())
        frame["source"], frame["fetched_at"] = "akshare:stock_financial_analysis_indicator_em", self.captured_at.isoformat()
        return frame[["symbol", "report_period", "announcement_date", "available_on", "roe", "revenue_growth", "version", "source", "fetched_at"]]

    def fetch(self, as_of: date | None = None, start: date | None = None,
              symbols: list[str] | None = None, mainboard: bool = False) -> SnapshotBundle:
        calendar = self.calendar()
        now = self.captured_at
        latest_allowed = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
        requested = as_of or latest_allowed
        if requested > latest_allowed:
            raise ValueError("daily data is available only after 18:00 Asia/Shanghai; requested cutoff is too early")
        available = calendar[calendar["trade_date"] <= str(requested)]
        if available.empty or calendar.iloc[-1]["trade_date"] < str(requested):
            raise ValueError("trading calendar does not cover requested date; no weekday fallback is allowed")
        day = date.fromisoformat(available.iloc[-1]["trade_date"])
        start = start or day - timedelta(days=430)
        if start >= day:
            raise ValueError("start must precede snapshot date")
        # Sample mode does not depend on a single all-exchange listing endpoint.
        stocks = self.stock_list() if mainboard and not symbols else self.sample_list(list(symbols or SAMPLE_CODES))
        wanted = {normalize_symbol(s) for s in symbols} if symbols else (
            set(stocks["symbol"]) if mainboard else {normalize_symbol(s) for s in SAMPLE_CODES}
        )
        stocks = stocks[stocks["symbol"].isin(wanted)]
        if stocks.empty:
            raise ValueError("no selected mainboard stocks")
        if wanted - set(stocks["symbol"]):
            self.issues.append("Some requested symbols are not present in the current mainboard stock list")
        reports = []
        for year in (range(start.year - 2, day.year) if mainboard else []):
            try:
                report = self.call("stock_yjbb_em", date=f"{year}1231").rename(columns={
                    "股票代码": "code", "净资产收益率": "roe", "营业总收入-同比增长": "revenue_growth",
                    "最新公告日期": "announcement_date", "所处行业": "industry",
                })
                report["symbol"] = report["code"].astype(str).str.zfill(6).map(normalize_symbol)
                report = report[report["symbol"].isin(wanted)].copy()
                report["announcement_date"] = date_column(report["announcement_date"])
                report["report_period"] = f"{year}-12-31"
                # First observation guards revised snapshots: do not backdate a freshly fetched value.
                report["available_on"] = report["announcement_date"].map(
                    lambda d: next((s for s in calendar["trade_date"] if s > str(d)), "9999-12-31")
                ).map(lambda d: max(d, str(now.date())))
                report["version"] = "latest-unverified-" + str(now.date())
                report["source"], report["fetched_at"] = "akshare:stock_yjbb_em", now.isoformat()
                reports.append(report)
            except (ValueError, KeyError) as exc:
                self.issues.append(f"annual report {year}: {exc}")
        financials = pd.concat(reports, ignore_index=True) if reports else pd.DataFrame(columns=[
            "symbol", "roe", "revenue_growth", "announcement_date", "report_period", "available_on", "version", "source", "fetched_at",
        ])
        bars, valuations, states = [], [], []
        for stock in stocks.to_dict("records"):
            code, symbol = stock["code"], stock["symbol"]
            if financials[financials["symbol"] == symbol].empty:
                try:
                    individual = self.financial_history(symbol, start, day, calendar)
                    financials = pd.concat([financials, individual], ignore_index=True) if not financials.empty else individual
                except (ValueError, KeyError) as exc:
                    self.issues.append(f"{symbol}: financials unavailable: {exc}")
            industry = stock.get("industry")
            matching = financials[financials["symbol"] == symbol]
            if not matching.empty and "industry" in matching:
                industries = matching.sort_values("report_period")["industry"].dropna()
                industry = industries.iloc[-1] if not industries.empty else None
            if (industry is None or pd.isna(industry) or pd.isna(stock["listed_on"])) and mainboard:
                try:
                    info = self.call("stock_individual_info_em", symbol=code, timeout=10)
                    info = dict(zip(info["item"], info["value"]))
                    industry = info.get("行业", industry)
                    if pd.isna(stock["listed_on"]):
                        listed = pd.to_datetime(str(info["上市时间"]).split(".")[0], format="%Y%m%d", errors="coerce")
                        stock["listed_on"] = None if pd.isna(listed) else listed.strftime("%Y-%m-%d")
                except (ValueError, KeyError) as exc:
                    self.issues.append(f"{symbol}: listing/industry verification unavailable: {exc}")
            states.append(dict(symbol=symbol, name=stock["name"], industry=industry,
                               listed_on=stock["listed_on"], board="mainboard",
                               is_st="ST" in stock["name"].upper(), is_delisting="退" in stock["name"],
                               known_on=str(now.date()), source="akshare:current-list"))
            try:
                kwargs = dict(symbol=code, period="daily", start_date=start.strftime("%Y%m%d"), end_date=day.strftime("%Y%m%d"))
                raw = self.call("stock_zh_a_hist", **kwargs, adjust="").rename(columns={
                    "日期": "trade_date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
                    "成交额": "amount", "成交量": "volume",
                })
                adj = self.call("stock_zh_a_hist", **kwargs, adjust="hfq").rename(columns={"日期": "trade_date", "收盘": "adj_close"})
                raw["trade_date"], adj["trade_date"] = date_column(raw["trade_date"]), date_column(adj["trade_date"])
                raw = raw.merge(adj[["trade_date", "adj_close"]], on="trade_date", how="left", validate="one_to_one")
                raw["symbol"] = symbol
                raw["volume"] = pd.to_numeric(raw["volume"], errors="coerce") * 100  # upstream unit: lots
                raw["suspended"] = raw["volume"].fillna(0) == 0
                raw["prev_close"] = raw["close"].shift(1)
                # No guessed limit price on ex-dates: backtest refuses to fill without verified limits.
                raw["limit_up"], raw["limit_down"] = float("nan"), float("nan")
                raw["source"], raw["fetched_at"] = "akshare:stock_zh_a_hist", now.isoformat()
                bars.append(raw[["symbol", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "amount",
                                 "prev_close", "limit_up", "limit_down", "suspended", "source", "fetched_at"]])
                val = self.call("stock_value_em", symbol=code).rename(columns={
                    "数据日期": "trade_date", "PE(TTM)": "pe_ttm", "市净率": "pb",
                })
                val["trade_date"] = date_column(val["trade_date"])
                val = val[(val["trade_date"] >= str(start)) & (val["trade_date"] <= str(day))].copy()
                val["symbol"], val["source"], val["fetched_at"] = symbol, "akshare:stock_value_em", now.isoformat()
                valuations.append(val[["symbol", "trade_date", "pe_ttm", "pb", "source", "fetched_at"]])
            except (ValueError, KeyError) as exc:
                self.issues.append(f"{symbol}: {exc}")
        benchmark = self.call("stock_zh_index_daily_em", symbol="sh000300", start_date=start.strftime("%Y%m%d"), end_date=day.strftime("%Y%m%d"))
        benchmark = benchmark.rename(columns={"date": "trade_date"})
        benchmark["trade_date"] = date_column(benchmark["trade_date"])
        prices = pd.concat(bars, ignore_index=True) if bars else pd.DataFrame(columns=[
            "symbol", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "amount", "suspended",
        ])
        vals = pd.concat(valuations, ignore_index=True) if valuations else pd.DataFrame(columns=["symbol", "trade_date", "pe_ttm", "pb"])
        return SnapshotBundle(MarketSnapshot(
            snapshot_id=f"akshare-{day}-{uuid4().hex[:10]}", as_of=day, captured_at=now,
            source="akshare", universe_scope="current mainboard" if mainboard else f"sample:{len(stocks)} current mainboard stocks",
            quality_issues=self.issues + [
                "当前股票列表与最新财报未证明历史版本完整；历史验证未通过",
                "未提供经核验的历史涨跌停价和公司行动；回测不可将假设成交作为真实成交",
            ],
        ), {"universe": pd.DataFrame(states), "prices": prices, "financials": financials,
            "valuations": vals, "calendar": calendar,
            "benchmark": benchmark[["trade_date", "close"]],
            "actions": pd.DataFrame(columns=["symbol", "ex_date", "pay_date", "split_ratio", "cash_per_share"])})
