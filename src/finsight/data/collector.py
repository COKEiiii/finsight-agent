"""Incremental collection, with small per-stock tasks and reproducible captured versions."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from uuid import uuid4

import numpy as np
import pandas as pd

from finsight.data.akshare_provider import AKShareProvider, date_column, normalize_symbol
from finsight.data.demo import SAMPLE_CODES
from finsight.data.quality import REQUIRED
from finsight.data.requests import RequestBroker
from finsight.data.store import SnapshotBundle, canonical
from finsight.data.universe import MAINBOARD_PREFIXES, membership_id, universe_id
from finsight.jobs import Jobs
from finsight.models import MarketSnapshot, fingerprint


def merge_observations(old: pd.DataFrame, new: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if old.empty:
        return canonical(new)
    if new.empty:
        return canonical(old)
    # Unchanged observations keep their original first-fetch timestamp. Revisions get a new version.
    columns = sorted((set(old.columns) | set(new.columns)) - {"fetched_at", "loaded_at"})
    # Parquet/nullable columns may switch int/float dtypes without changing their values.
    # Hash both observations in a common dtype space so that this does not create a revision.
    comparable = pd.concat([old.reindex(columns=columns), new.reindex(columns=columns)], ignore_index=True)
    hashes = pd.util.hash_pandas_object(comparable, index=False)
    old_hash, new_hash = hashes.iloc[:len(old)], hashes.iloc[len(old):]
    fingerprints = dict(zip(pd.MultiIndex.from_frame(old[keys]), old_hash))
    fetched = dict(zip(pd.MultiIndex.from_frame(old[keys]), old.get("fetched_at", pd.Series(index=old.index, dtype=str))))
    new = new.copy()
    if "fetched_at" in new:
        for index, key, digest in zip(new.index, pd.MultiIndex.from_frame(new[keys]), new_hash):
            if fingerprints.get(key) == digest:
                new.at[index, "fetched_at"] = fetched.get(key, new.at[index, "fetched_at"])
    return canonical(pd.concat([old, new], ignore_index=True).drop_duplicates(keys, keep="last"))


class Collector(AKShareProvider):
    def __init__(self, broker: RequestBroker, config: dict, base: SnapshotBundle | None = None,
                 seeds: list[SnapshotBundle] | None = None):
        super().__init__(broker.store.root, timeout=broker.timeout, retries=broker.retries)
        self.broker, self.config, self.base = broker, config, base
        self.jobs, self.job_id = Jobs(broker.store), broker.job_id
        self.observations, self.observation_lock = {}, threading.Lock()
        self.mainboard = config.get("mainboard", False)
        self.sample_mode = not self.mainboard or bool(config.get("symbols"))
        self.fallback_market = False
        if broker.api is None:
            with broker.store.connect() as db:
                row = db.execute("SELECT 1 FROM job_requests r JOIN jobs j ON j.job_id=r.job_id "
                    "WHERE r.method='stock_yjbb_em' AND r.error_kind IN ('timeout','rate_limit') "
                    "AND substr(j.created_at,1,10)>=? LIMIT 1", ((broker.clock().date() - timedelta(days=7)).isoformat(),)).fetchone()
            self.fallback_market = row is not None
            if self.fallback_market:
                self.issues.append("近七日东方财富批量财报曾超时；行情使用 BaoStock，财务仍取单股年报加权 ROE。")
        self.base_groups = {key: {s: f for s, f in base[key].groupby("symbol", sort=False)} if base is not None and "symbol" in base[key] else {}
                            for key in ("prices", "financials", "valuations")}
        if base is not None and base.meta.captured_at > broker.clock():
            raise ValueError("base observations were captured after the current job time")
        self.seeds = sorted(seeds or [], key=lambda item: item.meta.captured_at)
        protected = {key: set(groups) for key, groups in self.base_groups.items()}
        for seed in self.seeds:
            if seed.meta.synthetic or seed.meta.source != "akshare" or seed.meta.captured_at > broker.clock():
                raise ValueError("seed observations must be real and already captured")
            for key in self.base_groups:
                for symbol, frame in seed[key].groupby("symbol", sort=False):
                    if symbol in protected[key]:
                        continue
                    old = self.base_groups[key].get(symbol)
                    if old is None:
                        self.base_groups[key][symbol] = frame
                    elif key != "financials":
                        self.base_groups[key][symbol] = merge_observations(old, frame, ["symbol", "trade_date"])
                    else:
                        combined = pd.concat([old, frame], ignore_index=True)
                        identity = [column for column in ("symbol", "report_period", "announcement_date", "roe", "revenue_growth", "source", "roe_basis") if column in combined]
                        self.base_groups[key][symbol] = canonical(combined.sort_values("available_on", kind="stable").drop_duplicates(identity, keep="first"))

    def call(self, method: str, *, force_refresh: bool = False, **kwargs) -> pd.DataFrame:
        ttl = 86400 if self.sample_mode and method == "stock_individual_info_em" else None
        frame = self.broker.call(method, cache_ttl=ttl, force_refresh=force_refresh, **kwargs)
        with self.observation_lock:
            self.observations[fingerprint({"method": method, "kwargs": kwargs})] = frame.attrs["fetched_at"]
        return frame

    def observed(self, method: str, **kwargs) -> str:
        return self.observations.get(fingerprint({"method": method, "kwargs": kwargs}), self.captured_at.isoformat())

    def stock_list(self):
        pieces = []
        missing = []
        for market, method, kwargs, mapping in [
            ("SH", "stock_info_sh_name_code", {"symbol": "主板A股"}, {"证券代码": "code", "证券简称": "name", "上市日期": "listed_on"}),
            ("SZ", "stock_info_sz_name_code", {"symbol": "A股列表"}, {"A股代码": "code", "A股简称": "name", "A股上市日期": "listed_on"}),
        ]:
            try:
                frame = self.call(method, **kwargs)
                observed = frame.attrs["fetched_at"]
                if market == "SZ":
                    frame = frame[frame["板块"] == "主板"]
                frame = frame.rename(columns=mapping)[["code", "name", "listed_on"]].copy()
                frame["observed_at"] = observed
                pieces.append(frame)
            except (ValueError, KeyError):
                missing.append(market)
        if missing:
            try:
                quote = self.call("stock_zh_a_spot_em")
                observed = quote.attrs["fetched_at"]
                quote = quote.rename(columns={"代码": "code", "名称": "name"})
                quote["code"] = quote["code"].astype(str).str.zfill(6)
                quote["listed_on"] = None
                note = "当前全市场行情列表；上市日期逐股核实"
            except ValueError:
                quote = self.call("baostock_basic")
                observed = quote.attrs["fetched_at"]
                quote = quote.rename(columns={"code_name": "name", "ipoDate": "listed_on"})
                quote["code"] = quote["code"].str[3:]
                quote = quote[quote["type"] == "1"].copy()
                note = "BaoStock 基本资料"
            quote = quote[quote["code"].str.startswith(MAINBOARD_PREFIXES)].copy()
            quote["_market"] = np.where(quote["code"].str.startswith("6"), "SH", "SZ")
            quote = quote[quote["_market"].isin(missing)]
            quote["observed_at"] = observed
            pieces.append(quote[["code", "name", "listed_on", "observed_at"]])
            self.issues.append(f"部分交易所列表不可用，缺失市场采用{note}。")
        frame = pd.concat(pieces, ignore_index=True)
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame["symbol"] = frame["code"].map(normalize_symbol)
        frame["listed_on"] = date_column(frame["listed_on"])
        if not any(frame["symbol"].str.endswith(".SH")) or not any(frame["symbol"].str.endswith(".SZ")):
            raise ValueError("mainboard stock list does not cover both exchanges")
        return frame.drop_duplicates("symbol")

    def _annual(self, year: int, calendar: pd.DataFrame) -> pd.DataFrame:
        frame = self.call("stock_yjbb_em", date=f"{year}1231")
        fetched = frame.attrs["fetched_at"]
        frame = frame.rename(columns={"股票代码": "code", "净资产收益率": "roe",
            "营业总收入-同比增长": "revenue_growth", "最新公告日期": "announcement_date"})
        frame["symbol"] = frame["code"].astype(str).map(normalize_symbol)
        frame["announcement_date"] = date_column(frame["announcement_date"])
        frame["report_period"] = f"{year}-12-31"
        frame["available_on"] = frame["announcement_date"].map(
            lambda d: max(next((s for s in calendar["trade_date"] if s > str(d)), "9999-12-31"), fetched[:10]))
        frame["version"] = "observed-" + fetched
        frame["source"], frame["fetched_at"] = "akshare:stock_yjbb_em", fetched
        frame["roe_basis"] = "weighted_annual"
        return frame[["symbol", "report_period", "announcement_date", "available_on", "roe", "revenue_growth", "version", "source", "fetched_at", "roe_basis"]]

    def financial_history(self, symbol: str, start: date, day: date, calendar: pd.DataFrame):
        frame = self.call("stock_financial_analysis_indicator_em", symbol=symbol, indicator="按报告期")
        fetched = frame.attrs["fetched_at"]
        frame = frame.rename(columns={"REPORT_DATE": "report_period", "NOTICE_DATE": "announcement_date",
            "ROEJQ": "roe", "TOTALOPERATEREVETZ": "revenue_growth"})
        frame["report_period"], frame["announcement_date"] = date_column(frame["report_period"]), date_column(frame["announcement_date"])
        frame = frame[frame["report_period"].str.endswith("12-31") & (frame["announcement_date"] <= str(day)) &
                      (frame["report_period"] >= f"{start.year - 2}-01-01")].copy()
        frame["symbol"] = symbol
        frame["available_on"] = frame["announcement_date"].map(
            lambda d: max(next((s for s in calendar["trade_date"] if s > str(d)), "9999-12-31"), fetched[:10]))
        frame["version"], frame["source"], frame["fetched_at"] = "observed-" + fetched, "akshare:stock_financial_analysis_indicator_em", fetched
        frame["roe_basis"] = "weighted_annual"
        return frame[["symbol", "report_period", "announcement_date", "available_on", "roe", "revenue_growth", "version", "source", "fetched_at", "roe_basis"]]

    def _bars(self, code: str, lower: str, day: str):
        kwargs = dict(symbol=code, period="daily", start_date=lower.replace("-", ""), end_date=day.replace("-", ""))
        raw = self.call("stock_zh_a_hist", **kwargs, adjust="")
        raw_fetched = raw.attrs["fetched_at"]
        raw = raw.rename(columns={"日期": "trade_date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交额": "amount", "成交量": "volume"})
        adj = self.call("stock_zh_a_hist", **kwargs, adjust="hfq")
        fetched = max(raw_fetched, adj.attrs["fetched_at"])
        adj = adj.rename(columns={"日期": "trade_date", "收盘": "adj_close"})
        raw["trade_date"], adj["trade_date"] = date_column(raw["trade_date"]), date_column(adj["trade_date"])
        raw = raw.merge(adj[["trade_date", "adj_close"]], on="trade_date", how="left", validate="one_to_one")
        raw = raw[raw["trade_date"].between(lower, day)].copy()
        raw["symbol"] = normalize_symbol(code)
        raw["volume"] = pd.to_numeric(raw["volume"], errors="coerce") * 100
        # Zero volume alone does not certify a suspension; retain unknown states in missing coverage.
        raw["suspended"] = pd.Series(False, index=raw.index, dtype="boolean").mask(raw["volume"].fillna(0).eq(0), pd.NA)
        raw["prev_close"] = raw["close"].shift(1)
        raw["limit_up"], raw["limit_down"] = float("nan"), float("nan")
        raw["source"], raw["fetched_at"] = "akshare:stock_zh_a_hist", fetched
        return raw[["symbol", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "amount", "suspended", "prev_close", "limit_up", "limit_down", "source", "fetched_at"]]

    def _bars_baostock(self, code: str, lower: str, day: str):
        qualified = ("sh." if code.startswith("6") else "sz.") + code
        kwargs = {"code": qualified, "start_date": lower, "end_date": day}
        raw = self.call("baostock_kline", **kwargs, adjustflag="3")
        raw_fetched = raw.attrs["fetched_at"]
        adjusted = self.call("baostock_kline", **kwargs, adjustflag="1")
        fetched = max(raw_fetched, adjusted.attrs["fetched_at"])
        raw = raw.rename(columns={"date": "trade_date", "preclose": "prev_close"})
        adjusted = adjusted.rename(columns={"date": "trade_date", "close": "adj_close"})
        raw = raw.merge(adjusted[["trade_date", "adj_close"]], on="trade_date", how="left", validate="one_to_one")
        raw = raw[raw["trade_date"].between(lower, day)].copy()
        for field in ("open", "high", "low", "close", "prev_close", "volume", "amount", "adj_close", "peTTM", "pbMRQ"):
            raw[field] = pd.to_numeric(raw[field], errors="coerce")
        raw["symbol"] = normalize_symbol(code)
        # Only explicit 0/1 codes certify a state. Unknown values must
        # remain missing coverage rather than silently becoming normal trading.
        status = raw.get("tradestatus", pd.Series(pd.NA, index=raw.index, dtype="string"))
        raw["suspended"] = status.astype("string").str.strip().map({"0": True, "1": False}).astype("boolean")
        raw["limit_up"], raw["limit_down"] = np.nan, np.nan
        raw["source"], raw["fetched_at"] = "baostock:query_history_k_data_plus", fetched
        prices = raw[["symbol", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "amount",
                      "suspended", "prev_close", "limit_up", "limit_down", "source", "fetched_at"]]
        vals = raw[["symbol", "trade_date", "peTTM", "pbMRQ", "source", "fetched_at"]].rename(
            columns={"peTTM": "pe_ttm", "pbMRQ": "pb"})
        is_st = bool(raw.loc[raw["trade_date"] == day, "isST"].eq("1").any())
        return prices, vals, is_st

    def financial_history_fallback(self, symbol: str, start: date, day: date, calendar: pd.DataFrame):
        code = symbol[:6]
        income = self.call("stock_financial_report_sina", stock=code, symbol="利润表")
        income_fetched = income.attrs["fetched_at"]
        income = income.copy()
        income["report_period"] = date_column(income["报告日"])
        revenue_field = "营业总收入" if "营业总收入" in income else "营业收入"
        if revenue_field not in income:
            raise ValueError("Sina annual total revenue is unavailable")
        income["revenue"] = pd.to_numeric(income[revenue_field], errors="coerce")
        income["announcement_date"] = date_column(income["公告日期"])
        by_period = income.drop_duplicates("report_period").set_index("report_period")
        records = []
        for year in range(start.year - 1, day.year):
            current, previous = f"{year}-12-31", f"{year - 1}-12-31"
            if current not in by_period.index or previous not in by_period.index:
                continue
            revenue, prior = by_period.loc[current, "revenue"], by_period.loc[previous, "revenue"]
            if pd.isna(revenue) or pd.isna(prior) or prior <= 0:
                continue
            try:
                profit = self.call("baostock_profit", code=("sh." if code.startswith("6") else "sz.") + code, year=year)
                profit_fetched = profit.attrs["fetched_at"]
                row = profit.loc[profit["statDate"] == current].iloc[0]
                roe = pd.to_numeric(row["roeAvg"], errors="coerce")
                if pd.isna(roe):
                    continue
                announcement = max(str(by_period.loc[current, "announcement_date"]), str(row["pubDate"]))
                if announcement > str(day):
                    continue
                fetched = max(income_fetched, profit_fetched)
                available = max(next((s for s in calendar["trade_date"] if s > announcement), "9999-12-31"), fetched[:10])
                records.append({"symbol": symbol, "report_period": current, "announcement_date": announcement,
                    "available_on": available, "roe": float(roe) * 100,
                    "revenue_growth": (float(revenue) / float(prior) - 1) * 100,
                    "roe_basis": "average_annual",
                    "version": "observed-" + fetched, "source": "baostock:query_profit_data+sina:stock_financial_report_sina",
                    "fetched_at": fetched})
            except (ValueError, KeyError, IndexError):
                continue
        return pd.DataFrame(records, columns=sorted(REQUIRED["financials"] | {"version", "source", "fetched_at", "roe_basis"}))

    def _symbol(self, stock, annual, start, day, calendar):
        self.jobs.check_cancelled(self.job_id)
        symbol, code, issues = stock["symbol"], stock["code"], []
        observed = stock.get("observed_at") or self.observed("stock_individual_info_em", symbol=code, timeout=10)
        observed = max(observed, stock.get("industry_observed_at") or observed)
        industry = stock.get("industry")
        industry_source = stock.get("industry_source") or "stock_individual_info_em"
        try:
            info = self.call("stock_individual_info_em", symbol=code, timeout=10) if not self.fallback_market else pd.DataFrame({"item": [], "value": []})
            industry_observed = info.attrs.get("fetched_at", observed)
            values = dict(zip(info["item"], info["value"]))
            industry = values.get("行业", industry)
            if pd.isna(stock.get("listed_on")):
                value = pd.to_datetime(str(values.get("上市时间")).split(".")[0], format="%Y%m%d", errors="coerce")
                stock["listed_on"] = None if pd.isna(value) else value.strftime("%Y-%m-%d")
            observed = max(observed, industry_observed)
        except (ValueError, KeyError) as exc:
            issues.append(f"{symbol}: metadata: {exc}")
        state = dict(symbol=symbol, name=stock["name"], industry=industry, listed_on=stock.get("listed_on"),
            board="mainboard", is_st="ST" in stock["name"].upper(), is_delisting="退" in stock["name"],
            known_on=observed[:10], is_member=True, source="akshare:observed-current-list", industry_source=industry_source)
        old_prices = self.base_groups["prices"].get(symbol, pd.DataFrame(columns=sorted(REQUIRED["prices"])))
        old_vals = self.base_groups["valuations"].get(symbol, pd.DataFrame(columns=sorted(REQUIRED["valuations"])))
        old_fin = self.base_groups["financials"].get(symbol, pd.DataFrame(columns=sorted(REQUIRED["financials"])))
        listing = pd.to_datetime(state["listed_on"], errors="coerce")
        structural = state["is_st"] or state["is_delisting"] or (pd.notna(listing) and listing + pd.DateOffset(years=2) > pd.Timestamp(day))
        if structural:
            return state, old_prices, old_vals, old_fin, issues
        lower = str(start)
        if not old_prices.empty:
            prior = calendar[calendar["trade_date"] <= old_prices["trade_date"].max()]["trade_date"].tolist()
            if prior:
                lower = max(str(start), prior[max(0, len(prior) - 10)])
        backup_vals = None
        try:
            if self.fallback_market:
                new_prices, backup_vals, confirmed_st = self._bars_baostock(code, lower, day)
                state["is_st"] = state["is_st"] or confirmed_st
            else:
                new_prices = self._bars(code, lower, day)
            if not old_prices.empty:
                overlap = old_prices.merge(new_prices, on=["symbol", "trade_date"], suffixes=("_old", "_new"))
                changed = any(not np.allclose(overlap[k + "_old"], overlap[k + "_new"], equal_nan=True, rtol=1e-10, atol=1e-10)
                              for k in ("open", "high", "low", "close", "adj_close", "amount", "volume"))
                if changed:
                    sessions = calendar[calendar["trade_date"] <= day]["trade_date"].tolist()
                    revised_lower = min(lower, sessions[max(0, len(sessions) - 61)])
                    if revised_lower < lower:
                        if self.fallback_market:
                            new_prices, backup_vals, _ = self._bars_baostock(code, revised_lower, day)
                        else:
                            new_prices = self._bars(code, revised_lower, day)
                    issues.append(f"{symbol}: price/adjustment revision detected; factor window refreshed")
                # The first bar of a newly requested range has no predecessor in that response.
                # Reuse its known prior close so an unchanged overlap does not look like a revision.
                previous = old_prices.set_index("trade_date")["prev_close"]
                first = new_prices["trade_date"] == new_prices["trade_date"].min()
                new_prices.loc[first, "prev_close"] = new_prices.loc[first, "trade_date"].map(previous).fillna(
                    new_prices.loc[first, "prev_close"])
            prices = merge_observations(old_prices, new_prices, ["symbol", "trade_date"])
        except (ValueError, KeyError) as exc:
            prices = old_prices
            backup_vals = old_vals
            issues.append(f"{symbol}: prices unavailable: {exc}")
        try:
            if backup_vals is not None:
                vals = backup_vals
            else:
                vals = self.call("stock_value_em", symbol=code)
                if (day not in set(date_column(vals["数据日期"])) and
                        vals.attrs.get("loaded_at") != vals.attrs.get("fetched_at")):
                    # This endpoint has no date parameter. A morning cache can
                    # otherwise hide the completed session on an evening run.
                    vals = self.call("stock_value_em", symbol=code, force_refresh=True)
                fetched = vals.attrs["fetched_at"]
                vals = vals.rename(columns={"数据日期": "trade_date", "PE(TTM)": "pe_ttm", "市净率": "pb"})
                vals["trade_date"] = date_column(vals["trade_date"])
                vals["symbol"], vals["source"], vals["fetched_at"] = symbol, "akshare:stock_value_em", fetched
            vals = vals[vals["trade_date"].between(str(start), day)].copy()
            vals = merge_observations(old_vals, vals[["symbol", "trade_date", "pe_ttm", "pb", "source", "fetched_at"]], ["symbol", "trade_date"])
        except (ValueError, KeyError) as exc:
            vals = old_vals
            issues.append(f"{symbol}: valuation unavailable: {exc}")
        fin = annual[annual["symbol"] == symbol] if not annual.empty else annual
        if fin.empty:
            try:
                recent_cached = False
                if self.fallback_market and not old_fin.empty and "fetched_at" in old_fin:
                    latest = old_fin.sort_values("report_period").iloc[-1]
                    fetched = pd.to_datetime(latest["fetched_at"], errors="coerce")
                    recent_cached = (pd.notna(fetched) and
                        fetched.date() == self.broker.clock().date() and
                        latest.get("roe_basis") == "weighted_annual" and
                        (date.fromisoformat(day) - date.fromisoformat(latest["report_period"])).days <= 550)
                fin = old_fin if recent_cached else self.financial_history(symbol, start, date.fromisoformat(day), calendar)
            except (ValueError, KeyError) as exc:
                issues.append(f"{symbol}: financials unavailable: {exc}")
        if not fin.empty or not old_fin.empty:
            fin = pd.concat([f for f in (old_fin, fin) if not f.empty], ignore_index=True)
            fin = fin.sort_values("available_on", kind="stable").drop_duplicates(
                ["symbol", "report_period", "announcement_date", "roe", "revenue_growth"], keep="first")
        return state, prices, vals, fin, issues

    def fetch(self):
        self.jobs.update(self.job_id, progress={"phase": "calendar_and_membership"})
        calendar = self.calendar()
        now = self.broker.clock()
        cutoff = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
        requested = date.fromisoformat(self.config["as_of"]) if self.config.get("as_of") else cutoff
        if requested > cutoff or calendar["trade_date"].max() < str(requested):
            raise ValueError("requested session is not complete or exchange calendar is stale")
        day = calendar.loc[calendar["trade_date"] <= str(requested), "trade_date"].max()
        if any(str(parent.meta.as_of) > day for parent in ([self.base] if self.base else []) + self.seeds):
            raise ValueError("local observation seed exceeds the requested snapshot date")
        start = date.fromisoformat(self.config["start"]) if self.config.get("start") else date.fromisoformat(day) - timedelta(days=430)
        if str(start) >= day:
            raise ValueError("start must precede snapshot date")
        self.config["as_of"], self.config["start"] = day, str(start)
        self.jobs.update(self.job_id, config=self.config)
        symbols = self.config.get("symbols")
        if not self.sample_mode:
            stocks = self.stock_list()
        elif self.fallback_market:
            requested_symbols = {normalize_symbol(symbol) for symbol in symbols or SAMPLE_CODES}
            stocks = self.stock_list()
            absent = requested_symbols - set(stocks["symbol"])
            if absent:
                raise ValueError("requested stocks absent from the current exchange list: " + ", ".join(sorted(absent)))
            stocks = stocks[stocks["symbol"].isin(requested_symbols)].copy()
        else:
            stocks = self.sample_list(symbols or list(SAMPLE_CODES))
        limit = self.config.get("limit")
        if limit and len(stocks) > limit:
            stocks = stocks.sort_values("symbol").iloc[np.linspace(0, len(stocks) - 1, limit, dtype=int)].copy()
        if stocks.empty:
            raise ValueError("empty stock universe")
        members = sorted(set(stocks["symbol"]))
        uid = universe_id(self.mainboard, [normalize_symbol(s) for s in symbols] if symbols else
                          (None if self.mainboard else [normalize_symbol(s) for s in SAMPLE_CODES]), limit)
        if self.base and self.base.meta.universe_id != uid:
            raise ValueError("base snapshot belongs to a different universe definition")
        self.jobs.update(self.job_id, progress={"phase": "financial_bulk", "symbols_total": len(stocks),
            "universe_id": uid, "mode": "incremental" if self.base else "bootstrap_with_local_data" if self.seeds else "bootstrap",
            "seeded_price_symbols": len(self.base_groups["prices"]),
            "seed_snapshot_ids": [item.meta.snapshot_id for item in self.seeds]})
        annual = []
        if self.mainboard and not self.fallback_market:
            lower_year = date.fromisoformat(day).year - 2 if self.base else start.year - 2
            for year in range(lower_year, date.fromisoformat(day).year):
                try:
                    annual.append(self._annual(year, calendar))
                except (ValueError, KeyError) as exc:
                    self.issues.append(f"annual {year}: {exc}")
                    self.fallback_market = True
                    self.issues.append("东方财富年报批量接口失败；行情改用 BaoStock，财务仍取单股年报加权 ROE，来源逐行记录。")
                    break
        if self.fallback_market:
            try:
                industries = self.call("baostock_industry")
                industry_observed = industries.attrs["fetched_at"]
                industries["symbol"] = industries["code"].str[3:].map(normalize_symbol)
                industries = industries.drop_duplicates("symbol").set_index("symbol")
                stocks["industry"] = stocks["symbol"].map(industries["industry"])
                stocks["industry_source"] = "baostock:query_stock_industry"
                stocks["industry_observed_at"] = industry_observed
            except (ValueError, KeyError):
                self.issues.append("备用行业分类不可用；未知行业计入缺失。")
        annual = pd.concat(annual, ignore_index=True) if annual else pd.DataFrame(columns=sorted(REQUIRED["financials"]))
        results = []
        with ThreadPoolExecutor(max_workers=self.broker.workers) as executor:
            futures = [executor.submit(self._symbol, row, annual, start, day, calendar) for row in stocks.to_dict("records")]
            for future in as_completed(futures):
                self.jobs.check_cancelled(self.job_id)
                results.append(future.result())
                self.jobs.update(self.job_id, progress={"phase": "per_stock", "symbols_completed": len(results),
                                                       "symbols_with_errors": sum(bool(item[4]) for item in results),
                                                       "effective_workers": self.broker.limit})
        results.sort(key=lambda r: r[0]["symbol"])
        states = pd.DataFrame([r[0] for r in results])
        if self.base:
            old_states = self.base["universe"]
            # Keep historical states and append explicit membership removals, never infer a delisting.
            previous = old_states.sort_values("known_on", kind="stable").drop_duplicates("symbol", keep="last")
            removed = previous[~previous["symbol"].isin(members)].copy()
            removed["is_member"], removed["known_on"] = False, max(stocks.get("observed_at", pd.Series([now.isoformat()])).dropna())[:10]
            states = pd.concat([old_states, states, removed], ignore_index=True).drop_duplicates(["symbol", "known_on"], keep="last")
        tables = {"universe": states, "calendar": calendar}
        for key, pos in (("prices", 1), ("valuations", 2), ("financials", 3)):
            parts = [r[pos] for r in results if not r[pos].empty]
            tables[key] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=sorted(REQUIRED[key]))
        try:
            benchmark = self.call("stock_zh_index_daily_em", symbol="sh000300", start_date=start.strftime("%Y%m%d"), end_date=day.replace("-", "")) if not self.fallback_market else self.call("baostock_index", start_date=str(start), end_date=day)
        except ValueError:
            benchmark = self.call("baostock_index", start_date=str(start), end_date=day)
        benchmark = benchmark.rename(columns={"date": "trade_date"})
        benchmark["close"] = pd.to_numeric(benchmark["close"], errors="coerce")
        benchmark["trade_date"] = date_column(benchmark["trade_date"])
        tables["benchmark"] = benchmark[benchmark["trade_date"].between(str(start), day)][["trade_date", "close"]]
        tables["actions"] = self.base["actions"] if self.base else pd.DataFrame(columns=sorted(REQUIRED["actions"]))
        issues = [*self.issues, *[issue for r in results for issue in r[4]],
            "历史股票池、财务修订、涨跌停价及公司行动尚未核验；当前覆盖率不等于历史研究通过。"]
        full = self.mainboard and not symbols and not limit
        return SnapshotBundle(MarketSnapshot(snapshot_id=f"akshare-{day}-{uuid4().hex[:10]}", as_of=date.fromisoformat(day),
            captured_at=self.broker.clock(), source="akshare", schema_version=2, universe_id=uid,
            membership_id=membership_id(members), membership_symbols=members, universe_complete=full, job_id=self.job_id,
            data_parent_snapshot_ids=([self.base.meta.snapshot_id] if self.base else []) + [item.meta.snapshot_id for item in self.seeds],
            universe_scope="current mainboard" if full else f"{'mainboard validation' if self.mainboard else 'sample'}:{len(stocks)}",
            quality_issues=issues), tables)
