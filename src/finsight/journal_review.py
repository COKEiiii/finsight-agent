"""Order lifecycle and execution review derived only from recorded simulation events."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

import pandas as pd

from finsight.models import TradeJournalEntry

EXECUTIONS = {"filled", "partial"}
INTENTS = {"planned", "submitted"}


def order_identity(entry: TradeJournalEntry | dict) -> tuple:
    values = entry.model_dump(mode="json") if isinstance(entry, TradeJournalEntry) else entry
    return tuple(values[field] for field in ("symbol", "action", "run_id", "strategy_version"))


def order_key(entry: TradeJournalEntry) -> tuple[str, str, str, str]:
    # Broker ticket numbers may restart each trading day. Legacy events without
    # a ticket stay separate rather than guessing which submission they filled.
    return entry.account_id, str(entry.trade_date), "order" if entry.order_id else "event", entry.order_id or entry.event_id


def adverse_price_impact(entry: TradeJournalEntry) -> float | None:
    if entry.status not in EXECUTIONS or entry.planned_price is None:
        return None
    sign = 1 if entry.action == "buy" else -1
    return entry.quantity * (entry.executed_price - entry.planned_price) * sign


def summarize_orders(entries: list[TradeJournalEntry], as_of: date) -> list[dict]:
    groups = defaultdict(list)
    for _, entry in sorted(enumerate(entries), key=lambda item: (item[1].trade_date, item[0])):
        if entry.trade_date <= as_of:
            groups[order_key(entry)].append(entry)
    orders = []
    for key, events in sorted(groups.items()):
        if len({order_identity(entry) for entry in events}) != 1:
            raise ValueError(f"order has conflicting symbol/action/signal: {key}")
        first, last = events[0], events[-1]
        intents = [entry for entry in events if entry.status in INTENTS]
        fills = [entry for entry in events if entry.status in EXECUTIONS]
        requested = intents[-1].quantity if intents else None
        filled = sum(entry.quantity for entry in fills)
        notional = sum(entry.quantity * entry.executed_price for entry in fills)
        references = [entry for entry in fills if entry.planned_price is not None]
        reference_notional = sum(entry.quantity * entry.planned_price for entry in references)
        impact = sum(adverse_price_impact(entry) for entry in references) if references else None
        flags = []
        status = last.status
        if requested is not None and filled > requested:
            flags.append("filled_quantity_exceeds_requested")
        if requested is not None and last.status == "filled" and filled < requested:
            flags.append("reported_filled_but_quantity_short")
        if last.status in INTENTS | {"partial"} and requested is not None and filled >= requested > 0:
            status = "filled"
        if any(entry.transaction_cost for entry in events if entry.status not in EXECUTIONS):
            flags.append("non_execution_fee_not_in_cash_ledger")
        if not first.order_id:
            flags.append("unlinked_event")
        if requested == 0:
            flags.append("zero_order_quantity")
        orders.append({"account_id": first.account_id, "trade_date": str(first.trade_date),
            "order_id": first.order_id or None, "standalone_event_id": first.event_id if not first.order_id else None,
            "symbol": first.symbol, "action": first.action, "run_id": first.run_id,
            "strategy_version": first.strategy_version, "status": status, "reported_status": last.status,
            "requested_quantity": requested, "filled_quantity": filled,
            "unfilled_quantity": max(0, requested - filled) if requested is not None else None,
            "is_open": status in INTENTS | {"partial"},
            "fill_events": len(fills), "event_ids": [entry.event_id for entry in events],
            "average_executed_price": notional / filled if filled else None,
            "executed_notional": notional, "transaction_cost": sum(entry.transaction_cost for entry in fills),
            "referenced_fill_quantity": sum(entry.quantity for entry in references),
            "price_reference_complete": bool(fills) and len(references) == len(fills),
            "known_adverse_price_impact": impact,
            "adverse_slippage_bps": impact / reference_notional * 10_000 if reference_notional else None,
            "flags": flags})
    return orders


def execution_review(entries: list[TradeJournalEntry], as_of: date, calendar: pd.DataFrame) -> dict:
    sessions = sorted(set(day for day in calendar["trade_date"].astype(str) if day <= str(as_of)))[-5:]
    visible = [entry for entry in entries if entry.trade_date <= as_of]
    fills = [entry for entry in visible if entry.status in EXECUTIONS and str(entry.trade_date) in sessions]
    references = [entry for entry in fills if entry.planned_price is not None]
    orders = summarize_orders(entries, as_of)
    return {"period_start": sessions[0] if sessions else None, "period_end": sessions[-1] if sessions else None,
        "trading_sessions": len(sessions), "fill_events": len(fills),
        "transaction_cost": sum(entry.transaction_cost for entry in fills),
        "buy_notional": sum(entry.quantity * entry.executed_price for entry in fills if entry.action == "buy"),
        "sell_notional": sum(entry.quantity * entry.executed_price for entry in fills if entry.action == "sell"),
        "known_adverse_price_impact": sum(adverse_price_impact(entry) for entry in references) if references else None,
        "referenced_fill_events": len(references), "unreferenced_fill_events": len(fills) - len(references),
        "open_order_count": sum(order["is_open"] for order in orders),
        "cancelled_order_count": sum(order["status"] == "cancelled" for order in orders),
        "rejected_order_count": sum(order["status"] == "rejected" for order in orders),
        "unlinked_event_count": sum(not entry.order_id for entry in visible),
        "orders": orders,
        "notes": ["委托按账户、交易日和委托编号关联，部分成交与最终成交的数量均为增量。",
                  "未填委托编号的旧事件保持独立，不能自动推断其关联。",
                  "不利价差为正表示相对各笔计划价更差；买入价高或卖出价低均为正。费用单独计算。",
                  "未提供计划价的成交不计入价差，不能以零价差替代。待处理委托为截至快照的状态，成交指标使用最近五个交易日。"]}


def render_weekly_review(report: dict) -> str:
    execution = report["execution_review"]
    def number(value):
        return "无法计算" if value is None else f"{value:,.2f}"
    lines = ["# FinSight 模拟组合复盘", f"账户：{report['account_id']} · 截止：{report['as_of']}",
        f"数据类型：{'合成演示' if report.get('synthetic') else '真实行情快照'} · 历史研究核验：{'通过' if report.get('research_eligible') else '未通过'}",
        f"快照：{report['snapshot_id']}", f"初始资金：{number(report['initial_cash'])}",
        f"期末净值：{number(report['nav'])} · 现金：{number(report['cash'])} · 应收：{number(report['receivables'])}",
        f"最近五个交易日净值收益：{number(report['performance'].get('weekly_return') * 100) + '%' if report['performance'].get('weekly_return') is not None else '无法计算（净值观测不足）'}",
        "", "## 订单与成交", f"统计区间：{execution['period_start']} 至 {execution['period_end']}",
        f"成交事件：{execution['fill_events']} · 实际费用：{number(execution['transaction_cost'])}",
        f"已知不利价差金额：{number(execution['known_adverse_price_impact'])} · 缺少计划价成交：{execution['unreferenced_fill_events']}",
        f"截至快照待处理委托：{execution['open_order_count']} · 已撤单：{execution['cancelled_order_count']} · 已拒单：{execution['rejected_order_count']}",
        "", "| 日期 | 委托编号 | 股票 | 方向 | 委托量 | 增量成交合计 | 状态 | 实收费用 |", "|---|---|---|---|---:|---:|---|---:|"]
    for order in execution["orders"]:
        lines.append(f"| {order['trade_date']} | {order['order_id'] or '未关联'} | {order['symbol']} | {order['action']} | "
                     f"{order['requested_quantity'] if order['requested_quantity'] is not None else '未知'} | {order['filled_quantity']} | {order['status']} | {number(order['transaction_cost'])} |")
    lines += ["", "## 规则监控"] + ([f"- {flag}" for flag in report["flags"]] or ["- 未触发现有持仓规则阈值；这不代表策略已验证有效。"])
    lines += ["", "## 数据完整性", f"- 公司行动核验：{'已通过' if report['corporate_actions_verified'] else '未通过，分红送转仍需人工核实。'}",
              f"- 未关联委托编号的历史事件：{execution['unlinked_event_count']}"]
    for flag in report["performance"].get("flags", []):
        lines.append(f"- {flag}")
    lines += ["", "## 人工核实", f"- 现金对账差异：{number(report['actual_cash_difference']) if report['actual_cash_difference'] is not None else '未提供实际现金，尚未对账。'}",
        f"- 持仓对账差异：{report['actual_holdings_difference'] if report['actual_holdings_difference'] is not None else '未提供实际持仓，尚未对账。'}",
        "- 逐项区分策略、数据、执行和人工操作问题；上述监控不自动认定亏损原因。"]
    for order in execution["orders"]:
        if order["flags"]:
            lines.append(f"- {order['trade_date']} {order['symbol']} {order['order_id'] or order['standalone_event_id']}: {', '.join(order['flags'])}")
    lines += ["", *[f"- {note}" for note in execution["notes"]]]
    return "\n".join(lines)
