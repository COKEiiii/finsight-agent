from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from finsight.data.store import SnapshotBundle
from finsight.models import StrategyConfig
from finsight.portfolio import select_targets
from finsight.screening import screen


@dataclass
class Lot:
    quantity: int
    acquired_on: str


class Ledger:
    def __init__(self, cash: float):
        if not math.isfinite(cash) or cash <= 0:
            raise ValueError("initial capital must be positive and finite")
        self.cash = float(cash)
        self.lots: dict[str, list[Lot]] = {}
        self.pending_cash: list[tuple[str, float]] = []
        self.applied_actions: set[tuple[str, str]] = set()

    @property
    def symbols(self) -> set[str]:
        return {s for s in self.lots if self.quantity(s) > 0}

    @property
    def receivables(self) -> float:
        return sum(amount for _, amount in self.pending_cash)

    def quantity(self, symbol: str) -> int:
        return sum(lot.quantity for lot in self.lots.get(symbol, []))

    def sellable(self, symbol: str, day: str) -> int:
        return sum(lot.quantity for lot in self.lots.get(symbol, []) if lot.acquired_on < day)

    def nav(self, marks: dict[str, float]) -> float:
        return self.cash + self.receivables + sum(self.quantity(s) * float(marks[s]) for s in self.symbols)

    def record_fill(self, symbol: str, action: str, quantity: int, price: float, cost: float, day: str):
        if quantity <= 0 or not math.isfinite(price) or price <= 0 or not math.isfinite(cost) or cost < 0:
            raise ValueError("invalid execution quantity, price or fee")
        if action == "buy":
            if quantity % 100:
                raise ValueError("buy quantity must be a multiple of 100")
            debit = quantity * price + cost
            if debit > self.cash + 1e-7:
                raise ValueError("insufficient cash")
            self.cash -= debit
            self.lots.setdefault(symbol, []).append(Lot(quantity, day))
        elif action == "sell":
            if quantity > self.sellable(symbol, day):
                raise ValueError("T+1 violation or insufficient holding")
            if quantity % 100 and quantity != self.quantity(symbol):
                raise ValueError("odd-lot sale must liquidate the holding")
            remaining = quantity
            for lot in self.lots.get(symbol, []):
                if lot.acquired_on < day:
                    sold = min(remaining, lot.quantity)
                    lot.quantity -= sold
                    remaining -= sold
            self.lots[symbol] = [lot for lot in self.lots.get(symbol, []) if lot.quantity]
            self.cash += quantity * price - cost
        else:
            raise ValueError("unknown execution action")
        if self.cash < -1e-7:
            raise ValueError("negative ledger cash")

    def apply_actions(self, actions: pd.DataFrame, day: str):
        for action in actions[actions["ex_date"] == day].to_dict("records"):
            key = (action["symbol"], day)
            if key in self.applied_actions:
                continue
            ratio, dividend = float(action["split_ratio"]), float(action["cash_per_share"])
            if ratio <= 0 or dividend < 0 or action["pay_date"] < day:
                raise ValueError("invalid corporate action")
            symbol = action["symbol"]
            entitled = self.quantity(symbol) * dividend
            if entitled:
                self.pending_cash.append((action["pay_date"], entitled))
            for lot in self.lots.get(symbol, []):
                adjusted = lot.quantity * ratio
                if abs(adjusted - round(adjusted)) > 1e-8:
                    raise ValueError("fractional corporate action requires explicit settlement data")
                lot.quantity = round(adjusted)
            self.applied_actions.add(key)
        payable = sum(amount for pay_day, amount in self.pending_cash if pay_day <= day)
        self.cash += payable
        self.pending_cash = [(d, a) for d, a in self.pending_cash if d > day]


def trading_cost(day: str, action: str, gross: float) -> float:
    # Research assumptions. Commission includes brokerage/exchange handling charges.
    commission = max(5.0, gross * .0003)
    transfer = gross * (.00002 if day < "2022-04-29" else .00001)
    stamp = gross * (.001 if day < "2023-08-28" else .0005) if action == "sell" else 0
    return round(commission + transfer + stamp, 2)


def execute_order(ledger: Ledger, symbol: str, action: str, quantity: int, bar: dict | None,
                  day: str, slippage_bps: float = 5) -> dict:
    event = {"trade_date": day, "symbol": symbol, "action": action, "quantity": quantity,
             "status": "cancelled", "reason": "", "executed_price": None, "transaction_cost": 0.0}
    if bar is None or bool(bar.get("suspended", True)) or not (float(bar.get("volume", 0)) > 0):
        event["reason"] = "suspended_or_no_bar"
        return event
    limit_key = "limit_up" if action == "buy" else "limit_down"
    limit = float(bar.get(limit_key, float("nan")))
    opening = float(bar["open"])
    if not math.isfinite(limit):
        event["reason"] = "unverified_price_limit"
        return event
    if (action == "buy" and opening >= limit - 1e-8) or (action == "sell" and opening <= limit + 1e-8):
        event["reason"] = "opening_at_price_limit"
        return event
    multiplier = 1 + slippage_bps / 10_000 * (1 if action == "buy" else -1)
    price = (math.ceil(opening * multiplier * 100 - 1e-8) if action == "buy" else
             math.floor(opening * multiplier * 100 + 1e-8)) / 100
    if price <= 0 or (action == "buy" and price > limit) or (action == "sell" and price < limit):
        event["reason"] = "slippage_crosses_price_limit"
        return event
    cost = trading_cost(day, action, quantity * price)
    try:
        ledger.record_fill(symbol, action, quantity, price, cost, day)
    except ValueError as exc:
        event["reason"] = str(exc)
        return event
    return {**event, "status": "filled", "reason": "", "executed_price": price,
            "transaction_cost": cost, "notional": quantity * price}


def metrics(values: list[float], initial_cash: float, costs: float, turnover: float) -> dict:
    series = pd.Series([initial_cash, *values], dtype=float)
    returns = series.pct_change().dropna()
    vol = float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 else None
    sharpe = float(returns.mean() / returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 and returns.std(ddof=1) > 0 else None
    return {"cumulative_return": float(series.iloc[-1] / initial_cash - 1),
            "max_drawdown": float((series / series.cummax() - 1).min()),
            "annualized_volatility": vol, "sharpe_zero_rf": sharpe,
            "turnover": float(turnover), "transaction_costs": costs, "sessions": len(values),
            "short_sample_warning": len(values) < 252}


def simulate(bundle: SnapshotBundle, config: StrategyConfig, start: date, end: date,
             capital: float = 100_000, baseline: bool = False, slippage_bps: float = 5) -> dict:
    if start > end or end > bundle.meta.as_of:
        raise ValueError("invalid backtest interval or end exceeds snapshot cutoff")
    if not math.isfinite(slippage_bps) or slippage_bps < 0:
        raise ValueError("slippage must be nonnegative and finite")
    if not bundle.meta.publishable:
        raise ValueError("snapshot failed batch validation")
    all_days = sorted(bundle["calendar"]["trade_date"].astype(str))
    days = [d for d in all_days if str(start) <= d <= str(end)]
    if not days:
        raise ValueError("no sessions in backtest interval")
    positions = {d: i for i, d in enumerate(all_days)}
    bars = {d: frame.set_index("symbol").to_dict("index") for d, frame in bundle["prices"].groupby("trade_date")}
    ledger, marks, curve, trades, signals, issues = Ledger(capital), {}, [], [], [], []
    pending: list[dict] = []
    total_cost, turnover = 0.0, 0.0

    def build_orders(signal_day: str, nav: float):
        result = screen(bundle, config, date.fromisoformat(signal_day), baseline=baseline)
        targets = select_targets(result.candidates, config)
        weights = {row["symbol"]: row for row in targets}
        orders = []
        for symbol in sorted(ledger.symbols | set(weights)):
            row = weights.get(symbol)
            target = math.floor(nav * row["target_weight"] / row["close"] / 100) * 100 if row else 0
            delta = target - ledger.quantity(symbol)
            if delta:
                orders.append({"symbol": symbol, "action": "buy" if delta > 0 else "sell", "quantity": abs(delta)})
        signals.append({"signal_date": signal_day, "candidate_symbols": [r["symbol"] for r in result.candidates],
                        "targets": {s: r["target_weight"] for s, r in weights.items()},
                        "eligible_count": result.eligible_count, "orders": orders})
        return orders

    first_index = positions[days[0]]
    if first_index:
        previous_day = all_days[first_index - 1]
        for symbol, bar in bars.get(previous_day, {}).items():
            marks[symbol] = float(bar["close"])
        if previous_day[:7] != days[0][:7]:
            pending = build_orders(previous_day, capital)
    for day in days:
        ledger.apply_actions(bundle["actions"], day)
        daily = bars.get(day, {})
        # Opening marks, not closing marks, are used for execution-time exposure checks.
        opening_marks = {**marks, **{s: float(b["open"]) for s, b in daily.items()}}
        for order in sorted(pending, key=lambda o: (o["action"] != "sell", o["symbol"])):
            symbol = order["symbol"]
            event = None
            if order["action"] == "buy" and symbol in daily:
                opening = float(daily[symbol]["open"])
                nav = ledger.nav(opening_marks)
                single_room = nav * config.max_stock_weight - ledger.quantity(symbol) * opening
                equity = sum(ledger.quantity(s) * opening_marks[s] for s in ledger.symbols)
                total_room = nav * config.max_equity_weight - equity
                cap = max(0, math.floor(min(single_room, total_room) / (opening * (1 + slippage_bps / 10000)) / 100) * 100)
                order = {**order, "quantity": min(order["quantity"], cap)}
                while order["quantity"] > 0:
                    price = math.ceil(opening * (1 + slippage_bps / 10000) * 100 - 1e-8) / 100
                    if order["quantity"] * price + trading_cost(day, "buy", order["quantity"] * price) <= ledger.cash:
                        break
                    order["quantity"] -= 100
                if order["quantity"] == 0:
                    event = {**order, "trade_date": day, "status": "cancelled", "reason": "cash_or_exposure_cap",
                             "executed_price": None, "transaction_cost": 0.0}
            if event is None:
                event = execute_order(ledger, **order, bar=daily.get(symbol), day=day, slippage_bps=slippage_bps)
            trades.append(event)
            total_cost += event["transaction_cost"]
            if event["status"] == "filled":
                turnover += event["notional"] / max(ledger.nav(opening_marks), 1)
        pending = []
        for symbol, bar in daily.items():
            marks[symbol] = float(bar["close"])
        for symbol in ledger.symbols:
            if symbol not in daily:
                issues.append(f"{day}:{symbol}: stale close carried for NAV; unresolved trading status")
        nav = ledger.nav(marks)
        industry = bundle["universe"]
        industry = industry[industry["known_on"] <= day].sort_values("known_on").drop_duplicates("symbol", keep="last").set_index("symbol")["industry"].to_dict()
        exposure = {}
        for symbol in ledger.symbols:
            key = industry.get(symbol, "unknown")
            exposure[key] = exposure.get(key, 0) + ledger.quantity(symbol) * marks[symbol] / nav
        curve.append({"trade_date": day, "nav": nav, "cash": ledger.cash,
                      "receivables": ledger.receivables, "equity_weight": 1 - (ledger.cash + ledger.receivables) / nav,
                      "industry_exposure": exposure, "holdings": {s: ledger.quantity(s) for s in sorted(ledger.symbols)}})
        i = positions[day]
        if i + 1 < len(all_days) and all_days[i + 1][:7] != day[:7]:
            pending = build_orders(day, nav)
    stats = metrics([r["nav"] for r in curve], capital, total_cost, turnover)
    return {"curve": curve, "trades": trades, "signals": signals, "metrics": stats,
            "issues": sorted(set(issues)), "initial_capital": capital,
            "slippage_bps": slippage_bps, "name": "momentum_baseline" if baseline else config.version}


def run_backtest(bundle: SnapshotBundle, config: StrategyConfig, start: date, end: date,
                 capital: float = 100_000) -> dict:
    strategy = simulate(bundle, config, start, end, capital)
    baseline = simulate(bundle, config, start, end, capital, baseline=True)
    benchmark = bundle["benchmark"].set_index("trade_date")["close"].sort_index()
    first = strategy["curve"][0]["trade_date"]
    prior = benchmark[benchmark.index < first]
    reference = float(prior.iloc[-1]) if not prior.empty else None
    comparison = []
    for point in strategy["curve"]:
        day = point["trade_date"]
        value = float(benchmark.loc[day]) / reference if reference and day in benchmark.index else None
        comparison.append({"trade_date": day, "benchmark_nav": capital * value if value else None,
                           "benchmark_80_20_nav": capital * (.2 + .8 * value) if value else None})
    final_bm = comparison[-1]["benchmark_nav"]
    strategy["metrics"]["excess_return_vs_hs300_price"] = (
        strategy["metrics"]["cumulative_return"] - (final_bm / capital - 1) if final_bm else None)
    passed = bundle.meta.research_eligible and not strategy["issues"] and not baseline["issues"] and all(
        p["benchmark_nav"] is not None for p in comparison)
    return {"snapshot_id": bundle.meta.snapshot_id, "start": str(start), "end": str(end),
            "research_eligible": passed, "synthetic": bundle.meta.synthetic,
            "status": "historical_checks_passed" if passed else "研究演示：历史验证未通过",
            "strategy": strategy, "momentum_baseline": baseline, "benchmark": comparison,
            "assumptions": ["日频开盘成交近似，不代表真实订单簿成交；未知涨跌停价不成交。",
                "分红按税前现金及给定支付日记账；不模拟个人红利税。配股等未知公司行动阻断正式验证。",
                "沪深300为价格指数，不含分红；同时展示80%指数与20%现金的持有对照。",
                "动量基线使用同一准入股票池、组合约束、执行与费用。",
                "佣金双边万三最低5元，过户费与卖出印花税按生效日期配置；滑点双边5bp。",
                "2023–2024开发；2025独立验证。短样本年化指标不作为策略有效性的证明。"]}
