from __future__ import annotations

from datetime import date
from uuid import uuid4

import numpy as np
import pandas as pd

from finsight.data.store import SnapshotBundle, code_version
from finsight.models import FACTORS, ScreeningResult, StrategyConfig


def compute_factors(bundle: SnapshotBundle, as_of: date, config: StrategyConfig) -> tuple[pd.DataFrame, list[dict]]:
    day = str(as_of)
    if as_of > bundle.meta.as_of:
        raise ValueError("selection date exceeds snapshot cutoff")
    universe = bundle["universe"]
    universe = universe[universe["known_on"] <= day].sort_values("known_on").drop_duplicates("symbol", keep="last")
    prices = bundle["prices"]
    prices = prices[prices["trade_date"] <= day].sort_values(["symbol", "trade_date"])
    financials = bundle["financials"]
    financials = financials[(financials["available_on"] <= day) & (financials["announcement_date"] < day) &
                            financials["report_period"].str.endswith("12-31")]
    financials = financials.sort_values(["report_period", "available_on"]).drop_duplicates("symbol", keep="last").set_index("symbol")
    vals = bundle["valuations"]
    vals = vals[vals["trade_date"] == day].set_index("symbol")
    groups = {s: frame for s, frame in prices.groupby("symbol", sort=False)}
    rows, exclusions = [], []
    known = set(universe["symbol"])
    for symbol in sorted(set(bundle["universe"]["symbol"]) - known):
        exclusions.append({"symbol": symbol, "reasons": ["stock state not known at selection time"]})
    calendar = list(bundle["calendar"].query("trade_date <= @day")["trade_date"])
    for stock in universe.to_dict("records"):
        symbol, reasons = stock["symbol"], []
        if stock["board"] != "mainboard" or not symbol.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
            reasons.append("outside Shanghai/Shenzhen mainboard")
        if pd.isna(stock["listed_on"]) or pd.Timestamp(stock["listed_on"]) + pd.DateOffset(years=config.min_listing_years) > pd.Timestamp(day):
            reasons.append("listed for less than two years or listing date missing")
        if pd.isna(stock["is_st"]) or bool(stock["is_st"]):
            reasons.append("risk warning or unknown status")
        if pd.isna(stock["is_delisting"]) or bool(stock["is_delisting"]):
            reasons.append("delisting or unknown status")
        if pd.isna(stock["industry"]) or not str(stock["industry"]).strip():
            reasons.append("industry missing")
        hist = groups.get(symbol, pd.DataFrame())
        if hist.empty or hist.iloc[-1]["trade_date"] != day:
            reasons.append("no current bar / suspended")
        elif pd.isna(hist.iloc[-1]["suspended"]) or bool(hist.iloc[-1]["suspended"]):
            reasons.append("suspended")
        if len(hist) < 61 or len(calendar) < 61:
            reasons.append("fewer than 61 sessions for momentum")
        elif list(hist.tail(61)["trade_date"]) != calendar[-61:]:
            reasons.append("missing sessions in factor window")
        if symbol not in financials.index:
            reasons.append("no annual report available by selection time")
        if symbol not in vals.index:
            reasons.append("same-day valuation missing")
        if reasons:
            exclusions.append({"symbol": symbol, "reasons": reasons})
            continue
        fin, val = financials.loc[symbol], vals.loc[symbol]
        report_age = (as_of - date.fromisoformat(fin["report_period"])).days
        close = hist["adj_close"].tail(61).astype(float)
        amount_20 = hist["amount"].tail(20).astype(float).mean()
        factors = dict(roe=fin["roe"], revenue_growth=fin["revenue_growth"],
                       momentum_60=close.iloc[-1] / close.iloc[0] - 1,
                       ma_ratio=close.tail(20).mean() / close.tail(60).mean() - 1,
                       pe_ttm=val["pe_ttm"], pb=val["pb"])
        if not np.isfinite(list(factors.values())).all() or not np.isfinite(close).all() or (close <= 0).any():
            reasons.append("nonfinite or missing factor input")
        if not np.isfinite(amount_20) or hist["amount"].tail(20).isna().any() or amount_20 < config.min_amount_20:
            reasons.append("20-day average turnover below threshold or missing")
        if report_age > config.max_report_age_days or report_age < 0:
            reasons.append("annual report stale or future dated")
        if val["pe_ttm"] <= 0 or val["pb"] <= 0:
            reasons.append("nonpositive PE-TTM/PB")
        if reasons:
            exclusions.append({"symbol": symbol, "reasons": reasons})
            continue
        rows.append({"symbol": symbol, "name": stock["name"], "industry": stock["industry"],
                     **{k: float(v) for k, v in factors.items()}, "amount_20": float(amount_20),
                     "close": float(hist.iloc[-1]["close"]), "trade_date": day,
                     "report_period": fin["report_period"], "announcement_date": fin["announcement_date"],
                     "available_on": fin["available_on"], "source": bundle.meta.source,
                     "financial_source": fin.get("source", bundle.meta.source),
                     "financial_version": fin.get("version", "unverified"),
                     "valuation_source": val.get("source", bundle.meta.source),
                     "fetched_at": fin.get("fetched_at", bundle.meta.captured_at.isoformat())})
    return pd.DataFrame(rows), exclusions


def screen(bundle: SnapshotBundle, config: StrategyConfig | None = None, as_of: date | None = None,
           baseline: bool = False, run_id: str | None = None) -> ScreeningResult:
    if not bundle.meta.publishable:
        raise ValueError("snapshot failed batch validation; a formal ranking cannot be published")
    config, as_of = config or StrategyConfig(), as_of or bundle.meta.as_of
    frame, exclusions = compute_factors(bundle, as_of, config)
    if not frame.empty:
        frame["score"] = 0.0
        for factor in FACTORS:
            frame[f"{factor}_score"] = frame[factor].rank(method="average", pct=True, ascending=factor not in {"pe_ttm", "pb"}) * 100
            frame["score"] += config.weights[factor] * frame[f"{factor}_score"]
        if baseline:
            frame["score"] = frame["momentum_60_score"]
        frame = frame.sort_values(["score", "symbol"], ascending=[False, True]).head(config.top_n)
        frame["rank"] = range(1, len(frame) + 1)
    return ScreeningResult(
        run_id=run_id or uuid4().hex, snapshot_id=bundle.meta.snapshot_id, as_of=as_of,
        strategy_version=config.version, config_id=config.config_id, code_version=code_version(),
        candidates=frame.to_dict("records"), exclusions=exclusions,
        eligible_count=len(set(bundle["universe"]["symbol"])) - len(exclusions),
        universe_count=len(set(bundle["universe"]["symbol"])), synthetic=bundle.meta.synthetic,
        research_eligible=bundle.meta.research_eligible,
    )
