from __future__ import annotations

import csv
import io
import math
from collections import Counter, defaultdict

import pandas as pd

from finsight.data.store import SnapshotBundle, Store
from finsight.models import ScreeningResult, StrategyConfig, TradeJournalEntry


def select_targets(candidates: list[dict], config: StrategyConfig) -> list[dict]:
    counts, targets = Counter(), []
    for row in candidates:
        industry = row["industry"]
        if not industry or counts[industry] >= config.max_industry_names:
            continue
        targets.append({**row, "target_weight": config.target_weight})
        counts[industry] += 1
        if len(targets) >= config.max_holdings:
            break
    return targets


def portfolio_plan(result: ScreeningResult, config: StrategyConfig, capital: float,
                   calendar: pd.DataFrame) -> dict:
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("capital must be positive and finite")
    sessions = sorted(calendar["trade_date"].astype(str))
    next_day = next((d for d in sessions if d > str(result.as_of)), None)
    due = next_day is not None and next_day[:7] != str(result.as_of)[:7]
    targets = select_targets(result.candidates, config)
    for item in targets:
        item["reference_quantity"] = math.floor(capital * item["target_weight"] / item["close"] / 100) * 100
    return {"run_id": result.run_id, "signal_date": str(result.as_of), "execution_date": next_day if due else None,
            "rebalance_due": due, "capital": capital, "targets": targets,
            "target_cash_weight": 1 - sum(x["target_weight"] for x in targets),
            "note": "数量依据收盘价估算；人工核对持仓、费用和实际成交。非月末仅供研究参考。"}


def import_journal(store: Store, text: str) -> dict:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ValueError("journal CSV requires a header")
    entries = []
    for row in reader:
        values = {key: val for key, val in row.items() if val not in (None, "")}
        entries.append(TradeJournalEntry.model_validate(values))
    seen, inserted = {}, 0
    with store.connect() as db:
        # Every row is validated before insertion, and all writes are one transaction.
        for entry in entries:
            payload = entry.model_dump_json()
            if entry.event_id in seen and seen[entry.event_id] != payload:
                raise ValueError("conflicting duplicate event_id within CSV")
            seen[entry.event_id] = payload
            existing = db.execute("SELECT payload FROM journal WHERE event_id=?", (entry.event_id,)).fetchone()
            if existing:
                if existing["payload"] != payload:
                    raise ValueError("event_id already exists with different contents")
                continue
            run = db.execute("SELECT kind, payload FROM runs WHERE run_id=?", (entry.run_id,)).fetchone()
            if run is None or run["kind"] != "screen":
                raise ValueError("journal entries must reference an existing screening run")
            screening = ScreeningResult.model_validate_json(run["payload"])
            if entry.strategy_version != screening.strategy_version or entry.trade_date <= screening.as_of:
                raise ValueError("journal strategy/date conflicts with the linked signal")
            db.execute("INSERT INTO journal VALUES (?,?,?,?)", (entry.event_id, entry.account_id,
                       str(entry.trade_date), payload))
            inserted += 1
    return {"rows": len(entries), "inserted": inserted, "duplicates": len(entries) - inserted}


def journal_entries(store: Store, account_id: str = "paper") -> list[TradeJournalEntry]:
    with store.connect() as db:
        rows = db.execute("SELECT payload FROM journal WHERE account_id=? ORDER BY trade_date, rowid", (account_id,)).fetchall()
    return [TradeJournalEntry.model_validate_json(row["payload"]) for row in rows]


def reconcile(store: Store, bundle: SnapshotBundle, initial_cash: float, account_id: str = "paper",
              actual_cash: float | None = None, actual_holdings: dict[str, int] | None = None) -> dict:
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("initial cash must be positive and finite")
    from finsight.backtest import Ledger
    ledger = Ledger(initial_cash)
    events = journal_entries(store, account_id)
    flags, execution_rows = [], []
    dates = sorted(set(str(e.trade_date) for e in events if e.trade_date <= bundle.meta.as_of) |
                   set(bundle["actions"]["ex_date"]) | set(bundle["actions"]["pay_date"]))
    for day in dates:
        if day > str(bundle.meta.as_of):
            continue
        ledger.apply_actions(bundle["actions"], day)
        daily = [e for e in events if str(e.trade_date) == day and e.status in {"filled", "partial"}]
        for entry in daily:
            assert entry.executed_price is not None
            try:
                ledger.record_fill(entry.symbol, entry.action, entry.quantity, entry.executed_price,
                                   entry.transaction_cost, day)
                execution_rows.append({**entry.model_dump(mode="json"), "slippage": (
                    entry.executed_price / entry.planned_price - 1 if entry.planned_price else None)})
            except ValueError as exc:
                # Do not silently omit an impossible fill and then present an apparently reconciled account.
                raise ValueError(f"journal event {entry.event_id}: {exc}") from exc
    prices = bundle["prices"]
    marks = prices[prices["trade_date"] <= str(bundle.meta.as_of)].sort_values("trade_date").groupby("symbol").tail(1)
    mark_map = dict(zip(marks["symbol"], marks["close"]))
    for symbol in ledger.symbols:
        if symbol not in mark_map:
            raise ValueError(f"no mark for held symbol {symbol}")
        if marks.loc[marks["symbol"] == symbol, "trade_date"].iloc[0] != str(bundle.meta.as_of):
            flags.append(f"{symbol}: stale holding price")
    nav = ledger.nav(mark_map)
    holdings = {s: ledger.quantity(s) for s in sorted(ledger.symbols)}
    weights = {s: holdings[s] * float(mark_map[s]) / nav for s in holdings}
    for s, weight in weights.items():
        if weight > .15:
            flags.append(f"{s}: single stock weight above 15%")
    if sum(weights.values()) > .8:
        flags.append("equity weight above 80%")
    industry_map = bundle["universe"].sort_values("known_on").drop_duplicates("symbol", keep="last").set_index("symbol")["industry"].to_dict()
    industry_weights = defaultdict(float)
    for symbol, weight in weights.items():
        industry_weights[industry_map.get(symbol, "unknown")] += weight
    difference = None if actual_holdings is None else {s: actual_holdings.get(s, 0) - holdings.get(s, 0)
        for s in set(actual_holdings) | set(holdings) if actual_holdings.get(s, 0) != holdings.get(s, 0)}
    return {"account_id": account_id, "as_of": str(bundle.meta.as_of), "cash": ledger.cash,
            "receivables": ledger.receivables, "nav": nav, "holdings": holdings, "weights": weights,
            "industry_weights": dict(industry_weights), "return": nav / initial_cash - 1,
            "actual_cash_difference": None if actual_cash is None else actual_cash - ledger.cash,
            "actual_holdings_difference": difference, "flags": flags,
            "unexecuted_events": sum(e.status not in {"filled", "partial"} for e in events),
            "future_events": sum(e.trade_date > bundle.meta.as_of for e in events),
            "executions": execution_rows, "corporate_actions_verified": bundle.meta.research_checks.get("corporate_actions", False)}


def journal_template(run: ScreeningResult) -> str:
    columns = list(TradeJournalEntry.model_fields)
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    return stream.getvalue()
