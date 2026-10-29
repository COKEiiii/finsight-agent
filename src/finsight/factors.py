"""Vectorized raw factors and explicit rule/data classifications shared by coverage and ranking."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from finsight.data.store import SnapshotBundle
from finsight.data.universe import MAINBOARD_PREFIXES, current_members
from finsight.models import StrategyConfig, fingerprint

STRUCTURAL_RULES = {"outside_board", "listing_age", "risk_warning", "delisting", "suspended", "not_member"}
PRICE_FIELDS = ("adj_close", "amount", "close", "open", "high", "low", "volume")


def invalid_price_mask(window: pd.DataFrame) -> pd.Series:
    """The same validity boundary for selection and row-level gap diagnostics."""
    numeric = window[list(PRICE_FIELDS)].apply(pd.to_numeric, errors="coerce")
    return ~np.isfinite(numeric).all(axis=1) | (numeric["close"] <= 0) | (numeric["open"] <= 0) | (numeric["amount"] < 0) | (numeric["volume"] < 0) | (
        numeric["high"] < numeric[["open", "close"]].max(axis=1)) | (numeric["low"] > numeric[["open", "close"]].min(axis=1))


def evaluate(bundle: SnapshotBundle, as_of: date, config: StrategyConfig) -> tuple[pd.DataFrame, list[dict]]:
    if as_of > bundle.meta.as_of:
        raise ValueError("selection date exceeds snapshot cutoff")
    day = str(as_of)
    all_states = bundle["universe"]
    # The manifest's requested members remain in the denominator even if their metadata is missing.
    intended = set(bundle.meta.membership_symbols or current_members(all_states))
    states = all_states[all_states["known_on"] <= day].sort_values("known_on", kind="stable").drop_duplicates("symbol", keep="last").set_index("symbol")
    prices = bundle["prices"]
    prices = prices[prices["trade_date"] <= day].sort_values(["symbol", "trade_date"], kind="stable")
    calendar = sorted(bundle["calendar"].loc[bundle["calendar"]["trade_date"] <= day, "trade_date"].astype(str))
    expected = calendar[-61:]
    window = prices.groupby("symbol", sort=False).tail(61).copy()
    if not window.empty:
        for name in PRICE_FIELDS:
            window[name] = pd.to_numeric(window[name], errors="coerce")
        window["_invalid"] = invalid_price_mask(window)
        grouped = window.groupby("symbol", sort=False)
        price_metrics = grouped.agg(count=("trade_date", "count"), first_date=("trade_date", "first"),
            last_date=("trade_date", "last"), last_close=("close", "last"), first_adj=("adj_close", "first"),
            last_adj=("adj_close", "last"), valid_adj=("adj_close", "count"), min_adj=("adj_close", "min"), invalid_count=("_invalid", "sum"))
        price_metrics["mean_20"] = window.groupby("symbol", sort=False).tail(20).groupby("symbol")["adj_close"].mean()
        price_metrics["mean_60"] = window.groupby("symbol", sort=False).tail(60).groupby("symbol")["adj_close"].mean()
        tail20 = window.groupby("symbol", sort=False).tail(20).groupby("symbol")["amount"]
        price_metrics["amount_20"] = tail20.mean()
        price_metrics["amount_count"] = tail20.count()
        # Equal count and the exact ordered session sequence prevent suspended/missing rows being skipped.
        sequences = grouped["trade_date"].agg(tuple)
        price_metrics["continuous"] = sequences.map(lambda values: list(values) == expected and len(expected) == 61)
        last_rows = window.groupby("symbol", sort=False).tail(1).set_index("symbol")
        price_metrics["suspended"] = last_rows["suspended"]
        if "source" in last_rows:
            price_metrics["source"] = last_rows["source"]
        if "fetched_at" in last_rows:
            price_metrics["fetched_at"] = last_rows["fetched_at"]
        price_metrics["momentum_60"] = price_metrics["last_adj"] / price_metrics["first_adj"] - 1
        price_metrics["ma_ratio"] = price_metrics["mean_20"] / price_metrics["mean_60"] - 1
        window["_input_hash"] = pd.util.hash_pandas_object(window[["trade_date", "adj_close", "amount", "close"]], index=False)
        price_metrics["input_id"] = window.groupby("symbol", sort=False)["_input_hash"].agg(tuple).map(lambda v: fingerprint([int(n) for n in v]))
        price_map = price_metrics.to_dict("index")
    else:
        price_map = {}
    financials = bundle["financials"]
    financials = financials[(financials["available_on"] <= day) & (financials["announcement_date"] < day) &
                            financials["report_period"].str.endswith("12-31")].copy()
    basis = financials.get("roe_basis", pd.Series(index=financials.index, dtype="string"))
    source = financials.get("source", pd.Series("", index=financials.index)).fillna("").astype(str)
    financials["_roe_compatible"] = (basis.isna() | basis.eq("weighted_annual")) & ~source.str.contains("baostock", case=False)
    financials = financials.sort_values(["report_period", "_roe_compatible", "available_on"], kind="stable").drop_duplicates("symbol", keep="last").set_index("symbol").to_dict("index")
    vals = bundle["valuations"]
    vals = vals[vals["trade_date"] == day].drop_duplicates("symbol", keep="last").set_index("symbol").to_dict("index")
    state_map = states.to_dict("index")
    rows, exclusions = [], []
    for symbol in sorted(intended):
        reasons, codes, kinds = [], [], []

        def reject(code, reason, kind="data"):
            codes.append(code)
            reasons.append(reason)
            kinds.append(kind)

        stock = state_map.get(symbol)
        industry = stock.get("industry") if stock else None
        if stock is None:
            reject("unknown_state", "stock state not known at selection time")
        else:
            if "is_member" in stock and not stock["is_member"]:
                reject("not_member", "not a member at selection time", "rule")
            if stock["board"] != "mainboard" or not symbol.startswith(MAINBOARD_PREFIXES):
                reject("outside_board", "outside Shanghai/Shenzhen mainboard", "rule")
            if pd.isna(stock["listed_on"]):
                reject("listing_missing", "listing date missing")
            elif pd.Timestamp(stock["listed_on"]) + pd.DateOffset(years=config.min_listing_years) > pd.Timestamp(day):
                reject("listing_age", "listed for less than two years or listing date missing", "rule")
            for field, code, label in (("is_st", "risk_warning", "risk warning"), ("is_delisting", "delisting", "delisting")):
                if pd.isna(stock[field]):
                    reject("unknown_" + field, label + " status unknown")
                elif bool(stock[field]):
                    reject(code, label, "rule")
        metric = price_map.get(symbol)
        if metric and metric["last_date"] == day and not pd.isna(metric["suspended"]) and bool(metric["suspended"]):
            reject("suspended", "suspended", "rule")
        if set(codes) & STRUCTURAL_RULES:
            # A verified structural exclusion needs no financial/valuation download.
            indices = [i for i, code in enumerate(codes) if code in STRUCTURAL_RULES]
            exclusions.append({"symbol": symbol, "industry": industry if pd.notna(industry) else "unknown", "reasons": [reasons[i] for i in indices],
                               "codes": [codes[i] for i in indices], "category": "structural_rule"})
            continue
        if stock is not None and (pd.isna(industry) or not str(industry).strip()):
            reject("industry_missing", "industry missing")
        if not metric or metric["last_date"] != day:
            reject("current_bar_missing", "no current bar / unknown trading state")
        elif pd.isna(metric["suspended"]):
            reject("unknown_suspension", "suspension status unknown")
        if not metric or metric["count"] < 61 or len(expected) < 61:
            reject("short_history", "fewer than 61 sessions for momentum")
        elif not metric["continuous"]:
            reject("missing_sessions", "missing sessions in factor window")
        fin, val = financials.get(symbol), vals.get(symbol)
        if fin is None:
            reject("financial_missing", "no annual report available by selection time")
        if val is None:
            reject("valuation_missing", "same-day valuation missing")
        if metric:
            if metric["invalid_count"]:
                reject("invalid_price", "invalid raw price/volume in factor window")
            if metric["amount_count"] < 20 or not np.isfinite(metric["amount_20"]):
                reject("amount_missing", "20-day turnover missing")
            elif metric["amount_20"] < config.min_amount_20:
                reject("liquidity", "20-day average turnover below threshold or missing", "rule")
            if metric["valid_adj"] < metric["count"] or metric["min_adj"] <= 0:
                reject("factor_input_missing", "nonfinite or missing factor input")
        if fin:
            if not fin["_roe_compatible"]:
                reject("financial_definition_mismatch", "ROE definition differs from the frozen weighted annual metric")
            if not all(pd.notna(fin.get(k)) and np.isfinite(float(fin[k])) for k in ("roe", "revenue_growth")):
                reject("financial_value_missing", "nonfinite or missing factor input")
            age = (as_of - date.fromisoformat(fin["report_period"])).days
            if age < 0 or age > config.max_report_age_days:
                # An old record does not prove the latest report is absent; keep this in data coverage failures.
                reject("stale_financial", "annual report stale or future dated")
        if val:
            if not all(pd.notna(val.get(k)) and np.isfinite(float(val[k])) for k in ("pe_ttm", "pb")):
                reject("valuation_value_missing", "nonfinite or missing factor input")
            elif val["pe_ttm"] <= 0 or val["pb"] <= 0:
                reject("nonpositive_valuation", "nonpositive PE-TTM/PB", "rule")
        if reasons:
            exclusions.append({"symbol": symbol, "industry": industry if pd.notna(industry) else "unknown",
                               "reasons": reasons, "codes": codes,
                               "category": "data" if "data" in kinds else "strategy_rule"})
            continue
        values = {"roe": float(fin["roe"]), "revenue_growth": float(fin["revenue_growth"]),
                  "momentum_60": float(metric["momentum_60"]), "ma_ratio": float(metric["ma_ratio"]),
                  "pe_ttm": float(val["pe_ttm"]), "pb": float(val["pb"])}
        if not np.isfinite(list(values.values())).all():
            exclusions.append({"symbol": symbol, "industry": industry, "reasons": ["nonfinite or missing factor input"],
                               "codes": ["factor_input_missing"], "category": "data"})
            continue
        rows.append({"symbol": symbol, "name": stock["name"], "industry": industry, **values,
            "amount_20": float(metric["amount_20"]), "close": float(metric["last_close"]), "trade_date": day,
            "report_period": fin["report_period"], "announcement_date": fin["announcement_date"],
            "available_on": fin["available_on"], "source": bundle.meta.source,
            "financial_source": fin.get("source", bundle.meta.source), "financial_version": fin.get("version", "unverified"),
            "market_source": metric.get("source", bundle.meta.source),
            "market_version": metric.get("fetched_at", bundle.meta.captured_at.isoformat()),
            "valuation_source": val.get("source", bundle.meta.source),
            "fetched_at": fin.get("fetched_at", bundle.meta.captured_at.isoformat()),
            "factor_input_id": metric["input_id"]})
    return pd.DataFrame(rows), exclusions


def coverage_report(bundle: SnapshotBundle, frame: pd.DataFrame, exclusions: list[dict]) -> dict:
    structural = [e for e in exclusions if e["category"] == "structural_rule"]
    missing = [e for e in exclusions if e["category"] == "data"]
    evaluated = len(frame) + sum(e["category"] == "strategy_rule" for e in exclusions)
    required = evaluated + len(missing)
    coverage = evaluated / required if required else 1.0
    industries = {}
    for row in [*frame.to_dict("records"), *[e for e in exclusions if e["category"] != "structural_rule"]]:
        name = row.get("industry") or "unknown"
        group = industries.setdefault(name, {"required": 0, "evaluated": 0, "missing": 0})
        group["required"] += 1
        group["missing"] += int(row.get("category") == "data")
        group["evaluated"] += int(row.get("category") != "data")
    for group in industries.values():
        group["coverage"] = group["evaluated"] / group["required"]
    return {"threshold": .98, "coverage": coverage, "required": required, "evaluated": evaluated,
            "eligible": len(frame), "confirmed_exclusions": len(structural), "missing_count": len(missing),
            "missing": missing, "industries": industries, "meets_threshold": evaluated * 100 >= required * 98,
            "universe_id": bundle.meta.universe_id, "membership_id": bundle.meta.membership_id}
