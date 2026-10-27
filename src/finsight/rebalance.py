"""Manual month-end order proposals from a frozen screen and recorded account.

Prices are estimates at the signal close, never simulated/actual fills. Sale
proceeds are conditional; a failed sale requires a new cash check before buying.
"""
from __future__ import annotations

import math

import pandas as pd

from finsight.backtest import trading_cost
from finsight.data.store import SnapshotBundle
from finsight.models import ScreeningResult, StrategyConfig
from finsight.portfolio import portfolio_plan

ORDER_COLUMNS = ("sequence", "symbol", "action", "quantity", "current_quantity", "target_quantity",
                 "reference_price", "estimated_price", "estimated_notional", "estimated_fee",
                 "estimated_cash_after", "requires_sale_proceeds", "signal_run_id", "strategy_version")


def rebalance_plan(result: ScreeningResult, config: StrategyConfig, bundle: SnapshotBundle,
                   account: dict, slippage_bps: float = 5) -> dict:
    if result.snapshot_id != bundle.meta.snapshot_id or result.as_of != bundle.meta.as_of:
        raise ValueError("account rebalance requires the screening snapshot at its original cutoff")
    if result.config_id != config.config_id or result.strategy_version != config.version:
        raise ValueError("rebalance configuration must match the frozen screening configuration")
    if account["as_of"] != str(result.as_of):
        raise ValueError("account cutoff must match screening date")
    if not math.isfinite(slippage_bps) or not 0 <= slippage_bps < 10_000:
        raise ValueError("invalid slippage assumption")
    nav, cash = account["nav"], account["cash"]
    if not math.isfinite(nav) or nav <= 0 or not math.isfinite(cash) or cash < 0:
        raise ValueError("account net value and cash must be valid")
    reference = portfolio_plan(result, config, nav, bundle["calendar"])
    targets = {row["symbol"]: row for row in reference["targets"]}
    holdings = dict(account["holdings"])
    prices = bundle["prices"].loc[bundle["prices"]["trade_date"] == str(result.as_of)].set_index("symbol")
    states = bundle["universe"].loc[bundle["universe"]["known_on"] <= str(result.as_of)]
    industries = states.sort_values("known_on").drop_duplicates("symbol", keep="last").set_index("symbol")["industry"].to_dict()
    blockers, warnings, marks = [], [], {}
    for symbol in sorted(set(holdings) | set(targets)):
        if symbol not in prices.index or not math.isfinite(float(prices.loc[symbol, "close"])) or prices.loc[symbol, "close"] <= 0:
            blockers.append(f"{symbol}: no valid current-session reference price")
            continue
        marks[symbol] = float(prices.loc[symbol, "close"])
        industry = industries.get(symbol)
        if not isinstance(industry, str) or industry.strip().lower() in {"", "unknown", "未知"}:
            blockers.append(f"{symbol}: industry unknown at cutoff")
        else:
            industries[symbol] = industry.strip()
        suspended = prices.loc[symbol, "suspended"]
        if pd.isna(suspended) or suspended not in (True, False):
            blockers.append(f"{symbol}: trading state unknown at cutoff")
        elif bool(suspended):
            warnings.append(f"{symbol}: suspended at signal close; do not assume next-session execution")
    if result.publication_status != "published" or not bundle.meta.publishable:
        blockers.append("unpublished screening cannot authorize a rebalance")
    if account["execution_review"]["open_order_count"]:
        blockers.append("pending orders must be resolved before proposing another rebalance")
    if account["future_events"]:
        blockers.append("journal contains later events; reconcile at the current account cutoff first")
    if reference["execution_date"] is None and not any(d > str(result.as_of) for d in bundle["calendar"]["trade_date"]):
        blockers.append("saved calendar has no next trading session")
    plan = {"signal_run_id": result.run_id, "snapshot_id": result.snapshot_id,
        "strategy_version": result.strategy_version, "config_id": result.config_id,
        "account_id": account["account_id"], "signal_date": str(result.as_of),
        "execution_date": reference["execution_date"], "rebalance_due": reference["rebalance_due"],
        "synthetic": bundle.meta.synthetic, "research_eligible": result.research_eligible,
        "basis": "recorded journal and signal-close marks", "initial_cash": account["initial_cash"],
        "account_nav": nav, "cash_before": cash, "holdings_before": holdings, "receivables": account["receivables"],
        "target_cash_weight": reference["target_cash_weight"], "targets": reference["targets"],
        "orders": [], "skipped": [], "blockers": blockers, "warnings": warnings,
        "status": "blocked" if blockers else "ready" if reference["rebalance_due"] else "reference",
        "assumptions": {"slippage_bps": slippage_bps, "costs": "backtest effective-date research fee table"},
        "notes": ["这是人工模拟调仓提案，不代表委托或成交，不自动写入交易日志。",
                  "目标数量按当前净值计算，并扣除已有持仓；应收分红参与净值但不可用于买入。",
                  "估计成交价使用信号收盘价加减滑点；次日开盘需重新核对价格、T+1、停牌、涨跌停、公司行动及资金。",
                  "买入不得预支未成交卖单的资金；卖出失败、部分成交或价格变化时必须重新计算。",
                  "费用为研究估算，实际费用以模拟账户成交记录为准。"]}
    if blockers or not reference["rebalance_due"]:
        return plan

    positions, available, fees, impact = dict(holdings), cash, 0.0, 0.0
    # Track the cash that was already available, separate from conditional sales.
    original_cash_remaining = cash

    def quote(symbol, action, quantity):
        close = marks[symbol]
        raw = close * (1 + slippage_bps / 10_000 * (1 if action == "buy" else -1))
        price = (math.ceil(raw * 100 - 1e-8) if action == "buy" else math.floor(raw * 100 + 1e-8)) / 100
        if price <= 0:
            raise ValueError("estimated price must be positive")
        notional = quantity * price
        fee = trading_cost(reference["execution_date"], action, notional)
        adverse = quantity * (price - close) * (1 if action == "buy" else -1)
        return price, notional, fee, adverse

    def append_order(symbol, action, quantity, target):
        nonlocal available, fees, impact, original_cash_remaining
        price, notional, fee, adverse = quote(symbol, action, quantity)
        debit = notional + fee if action == "buy" else -notional + fee
        depends = action == "buy" and debit > original_cash_remaining + 1e-8
        if action == "buy" or debit > 0:
            original_cash_remaining = max(0, original_cash_remaining - debit)
        available -= debit
        fees += fee
        impact += adverse
        positions[symbol] = positions.get(symbol, 0) + quantity * (1 if action == "buy" else -1)
        plan["orders"].append({"sequence": len(plan["orders"]) + 1, "symbol": symbol, "action": action,
            "quantity": quantity, "current_quantity": holdings.get(symbol, 0), "target_quantity": target,
            "reference_price": marks[symbol], "estimated_price": price, "estimated_notional": notional,
            "estimated_fee": fee, "estimated_cash_after": available, "requires_sale_proceeds": depends,
            "signal_run_id": result.run_id, "strategy_version": result.strategy_version})

    for symbol in sorted(holdings):
        target = targets[symbol]["reference_quantity"] if symbol in targets else 0
        delta = holdings[symbol] - target
        quantity = delta if target == 0 else max(0, delta // 100 * 100)
        if quantity <= 0:
            continue
        _, notional, fee, _ = quote(symbol, "sell", quantity)
        if available + notional < fee:
            plan["skipped"].append({"symbol": symbol, "action": "sell", "quantity": quantity, "reason": "insufficient cash for sale fees"})
            continue
        append_order(symbol, "sell", quantity, target)

    for symbol, target in targets.items():
        requested = max(0, (target["reference_quantity"] - positions.get(symbol, 0)) // 100 * 100)
        if not requested:
            continue
        equity = sum(quantity * marks[s] for s, quantity in positions.items())

        def affordable(quantity):
            _, notional, fee, adverse = quote(symbol, "buy", quantity)
            after_nav = nav - fees - impact - fee - adverse
            names = {s for s, q in positions.items() if q > 0} | {symbol}
            return (notional + fee <= available + 1e-8 and after_nav > 0 and
                equity + quantity * marks[symbol] <= config.max_equity_weight * after_nav + 1e-8 and
                (positions.get(symbol, 0) + quantity) * marks[symbol] <= config.max_stock_weight * after_nav + 1e-8 and
                len(names) <= config.max_holdings and
                sum(industries[s] == industries[symbol] for s in names) <= config.max_industry_names)

        lower, upper = 0, requested // 100
        while lower < upper:
            middle = (lower + upper + 1) // 2
            if affordable(middle * 100):
                lower = middle
            else:
                upper = middle - 1
        quantity = lower * 100
        if quantity:
            append_order(symbol, "buy", quantity, target["reference_quantity"])
        if quantity < requested:
            plan["skipped"].append({"symbol": symbol, "action": "buy", "quantity": requested - quantity,
                                    "reason": "cash, costs or position limits"})
    equity = sum(quantity * marks[s] for s, quantity in positions.items())
    after_nav = nav - fees - impact
    plan.update(estimated_cash_after=available, estimated_nav_after=after_nav,
        estimated_fees=fees, estimated_adverse_price_impact=impact,
        estimated_equity_weight=equity / after_nav if after_nav > 1e-8 else None,
        target_quantity_differences={s: positions.get(s, 0) - target["reference_quantity"] for s, target in targets.items()
                                     if positions.get(s, 0) != target["reference_quantity"]},
        estimated_holdings={s: q for s, q in sorted(positions.items()) if q > 0})
    if after_nav <= 1e-8:
        plan["warnings"].append("estimated costs exhaust account net value; manual review required")
        plan["status"] = "requires_review"
    elif equity > config.max_equity_weight * after_nav + 1e-8 or any(
            quantity * marks[s] > config.max_stock_weight * after_nav + 1e-8 for s, quantity in positions.items()):
        plan["warnings"].append("projected holdings still exceed weight limits; manual review required")
        plan["status"] = "requires_review"
    return plan
