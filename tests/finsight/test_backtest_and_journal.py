import csv
import io
from datetime import date

import pandas as pd
import pytest

from finsight.backtest import Ledger, execute_order, run_backtest, simulate, trading_cost
from finsight.models import TradeJournalEntry
from finsight.portfolio import import_journal, journal_entries, portfolio_plan, reconcile, select_targets
from finsight.screening import screen


def test_t1_cash_fees_and_share_conservation():
    ledger = Ledger(10000)
    ledger.record_fill("600000.SH", "buy", 100, 10, 5, "2025-01-02")
    assert ledger.cash == 8995 and ledger.quantity("600000.SH") == 100
    with pytest.raises(ValueError, match=r"T\+1"):
        ledger.record_fill("600000.SH", "sell", 100, 11, 5, "2025-01-02")
    ledger.record_fill("600000.SH", "sell", 100, 11, 5, "2025-01-03")
    assert ledger.cash == 10090 and not ledger.symbols


def test_insufficient_cash_and_lot_rules_leave_ledger_unchanged():
    ledger = Ledger(1000)
    for qty in [99, 1000]:
        with pytest.raises(ValueError):
            ledger.record_fill("600000.SH", "buy", qty, 10, 5, "2025-01-02")
        assert ledger.cash == 1000 and not ledger.symbols


def test_dividend_receivable_split_and_payment_conserve_nav():
    ledger = Ledger(10000)
    ledger.record_fill("600000.SH", "buy", 100, 10, 0, "2025-01-02")
    actions = pd.DataFrame([{"symbol": "600000.SH", "ex_date": "2025-01-03", "pay_date": "2025-01-06",
                             "split_ratio": 2, "cash_per_share": 1}])
    ledger.apply_actions(actions, "2025-01-03")
    assert ledger.quantity("600000.SH") == 200
    assert ledger.nav({"600000.SH": 4.5}) == 10000
    assert ledger.cash == 9000 and ledger.receivables == 100
    ledger.apply_actions(actions, "2025-01-03")
    assert ledger.quantity("600000.SH") == 200  # no double application
    ledger.apply_actions(actions, "2025-01-06")
    assert ledger.cash == 9100 and ledger.receivables == 0


@pytest.mark.parametrize("bar,reason", [
    (None, "suspended"),
    ({"open": 11, "volume": 1000, "suspended": False, "limit_up": 11}, "price_limit"),
    ({"open": 10, "volume": 1000, "suspended": False}, "unverified"),
])
def test_untradeable_orders_cancelled_without_cash_effect(bar, reason):
    ledger = Ledger(10000)
    event = execute_order(ledger, "600000.SH", "buy", 100, bar, "2025-01-02")
    assert event["status"] == "cancelled" and reason in event["reason"]
    assert ledger.cash == 10000 and not ledger.symbols


def test_historical_tax_effective_date():
    assert trading_cost("2023-08-27", "sell", 10000) - trading_cost("2023-08-28", "sell", 10000) == 5
    assert trading_cost("2025-01-02", "buy", 10000) == 5.1


def test_rebalance_only_next_session_and_money_consistent(bundle, config):
    result = simulate(bundle, config, date(2025, 1, 1), date(2025, 2, 28))
    assert result["trades"]
    signal_dates = {s["signal_date"] for s in result["signals"]}
    assert all(t["trade_date"] not in signal_dates for t in result["trades"])
    assert all(p["cash"] >= 0 for p in result["curve"])
    assert result["metrics"]["transaction_costs"] == pytest.approx(sum(t["transaction_cost"] for t in result["trades"]))
    assert result["metrics"]["transaction_costs"] > 0
    assert max(p["equity_weight"] for p in result["curve"]) < .85  # market drift after the allocation


def test_backtest_demo_never_passes_research_gate(bundle, config):
    report = run_backtest(bundle, config, date(2025, 1, 1), date(2025, 2, 28))
    assert report["research_eligible"] is False
    assert "研究演示" in report["status"]
    assert len(report["benchmark"]) == len(report["strategy"]["curve"])


def test_industry_and_position_targets(bundle, config):
    result = screen(bundle)
    rows = [{**r, "industry": "one"} for r in result.candidates]
    assert len(select_targets(rows, config)) == 3
    plan = portfolio_plan(result, config, 100000, bundle["calendar"])
    assert plan["rebalance_due"] and plan["execution_date"] == "2025-03-03"
    assert sum(r["target_weight"] for r in plan["targets"]) <= .8


def csv_text(entries):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(entries[0]))
    writer.writeheader()
    writer.writerows(entries)
    return stream.getvalue()


def test_journal_idempotence_linkage_and_atomic_import(bundle, store, config):
    store.save_snapshot(bundle)
    result = screen(bundle)
    store.save_run("screen", result.snapshot_id, config, result.model_dump(mode="json"), result.run_id)
    entry = TradeJournalEntry(event_id="fill-1", trade_date=date(2025, 3, 3), symbol="600000.SH", action="buy",
                              status="filled", quantity=100, executed_price=10, transaction_cost=5,
                              run_id=result.run_id, strategy_version=config.version).model_dump(mode="json")
    text = csv_text([entry])
    assert import_journal(store, text)["inserted"] == 1
    assert import_journal(store, text)["duplicates"] == 1
    conflict = {**entry, "quantity": 200}
    with pytest.raises(ValueError, match="different contents"):
        import_journal(store, csv_text([{**entry, "event_id": "fill-2"}, conflict]))
    assert len(journal_entries(store)) == 1
    with pytest.raises(ValueError, match="existing screening"):
        import_journal(store, csv_text([{**entry, "event_id": "fill-3", "run_id": "unknown"}]))


def test_reconcile_uses_actual_fees_not_research_cost(bundle, store, config):
    result = screen(bundle, as_of=date(2025, 2, 26))
    store.save_run("screen", result.snapshot_id, config, result.model_dump(mode="json"), result.run_id)
    entry = TradeJournalEntry(event_id="actual", trade_date=date(2025, 2, 27), symbol="600000.SH", action="buy",
                              status="filled", quantity=100, executed_price=10, transaction_cost=12,
                              run_id=result.run_id, strategy_version=config.version)
    import_journal(store, csv_text([entry.model_dump(mode="json")]))
    report = reconcile(store, bundle, 100000, actual_cash=98988, actual_holdings={"600000.SH": 100})
    assert report["cash"] == 98988 and report["actual_cash_difference"] == 0
    assert report["actual_holdings_difference"] == {}
