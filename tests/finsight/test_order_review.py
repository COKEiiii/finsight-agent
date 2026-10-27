import csv
import io
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from finsight.journal_review import adverse_price_impact, execution_review, summarize_orders
from finsight.models import TradeJournalEntry
from finsight.portfolio import import_journal, journal_entries, reconcile
from finsight.service import FinSight


def ready(tmp_path, bundle, signal=date(2025, 2, 26)):
    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    result = service.screen(bundle.meta.snapshot_id, signal)
    return service, result


def event(result, event_id, status, quantity, *, day="2025-02-27", order="42", **kwargs):
    return TradeJournalEntry(event_id=event_id, status=status, quantity=quantity,
        trade_date=date.fromisoformat(day), order_id=order, run_id=result.run_id,
        strategy_version=result.strategy_version, **{"symbol": "600000.SH", "action": "buy", **kwargs})


def csv_events(entries):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(TradeJournalEntry.model_fields))
    writer.writeheader()
    writer.writerows(entry.model_dump(mode="json") for entry in entries)
    return stream.getvalue()


def test_partial_and_final_incremental_fills_close_one_order(bundle, tmp_path):
    service, result = ready(tmp_path, bundle)
    entries = [event(result, "plan", "planned", 300, planned_price=10),
               event(result, "submit", "submitted", 300, planned_price=10),
               event(result, "fill-a", "partial", 100, planned_price=10, executed_price=10.1, transaction_cost=5),
               event(result, "fill-b", "filled", 200, planned_price=10, executed_price=10.2, transaction_cost=5)]
    assert import_journal(service.store, csv_events(entries))["inserted"] == 4
    assert import_journal(service.store, csv_events(entries))["duplicates"] == 4
    report = reconcile(service.store, bundle, 100000)
    order = report["execution_review"]["orders"][0]
    assert order["status"] == "filled" and order["requested_quantity"] == order["filled_quantity"] == 300
    assert order["fill_events"] == 2 and order["unfilled_quantity"] == 0
    assert order["transaction_cost"] == 10 and order["average_executed_price"] == pytest.approx(3050 / 300)
    assert order["known_adverse_price_impact"] == pytest.approx(50)
    assert report["execution_review"]["open_order_count"] == 0
    assert report["cash"] == pytest.approx(96940) and report["holdings"] == {"600000.SH": 300}


def test_partial_then_cancel_retains_fills_but_not_an_open_order(bundle, tmp_path):
    service, result = ready(tmp_path, bundle)
    entries = [event(result, "submit", "submitted", 300),
               event(result, "fill", "partial", 200, executed_price=10, transaction_cost=5),
               event(result, "cancel", "cancelled", 100)]
    import_journal(service.store, csv_events(entries))
    report = reconcile(service.store, bundle, 100000)
    order = report["execution_review"]["orders"][0]
    assert order["filled_quantity"] == 200 and order["unfilled_quantity"] == 100
    assert order["status"] == "cancelled" and not order["is_open"]
    assert report["execution_review"]["open_order_count"] == 0
    assert report["execution_review"]["cancelled_order_count"] == 1
    assert report["cash"] == 97995 and report["holdings"] == {"600000.SH": 200}


def test_buy_and_sell_adverse_price_impact_have_same_cost_sign(bundle, tmp_path):
    _, result = ready(tmp_path, bundle)
    buy = event(result, "buy", "filled", 100, executed_price=10.1, planned_price=10)
    sell = event(result, "sell", "filled", 100, action="sell", executed_price=9.9, planned_price=10)
    assert adverse_price_impact(buy) == pytest.approx(10)
    assert adverse_price_impact(sell) == pytest.approx(10)
    favorable = sell.model_copy(update={"executed_price": 10.1})
    assert adverse_price_impact(favorable) == pytest.approx(-10)
    missing = buy.model_copy(update={"event_id": "no-ref", "planned_price": None, "order_id": "other"})
    review = execution_review([buy, missing], bundle.meta.as_of, bundle["calendar"])
    assert review["unreferenced_fill_events"] == 1 and review["known_adverse_price_impact"] == pytest.approx(10)


@pytest.mark.parametrize("changed", [{"symbol": "600036.SH"}, {"action": "sell"}])
def test_order_identity_conflicts_roll_back_the_entire_batch(bundle, tmp_path, changed):
    service, result = ready(tmp_path, bundle)
    submit = event(result, "submit", "submitted", 100)
    bad = event(result, "bad", "filled", 100, executed_price=10, **changed)
    with pytest.raises(ValueError, match="same symbol, action, signal"):
        import_journal(service.store, csv_events([submit, bad]))
    assert journal_entries(service.store) == []
    import_journal(service.store, csv_events([submit]))
    good = event(result, "unrelated", "submitted", 100, order="new")
    with pytest.raises(ValueError, match="same symbol, action, signal"):
        import_journal(service.store, csv_events([good, bad]))
    assert [entry.event_id for entry in journal_entries(service.store)] == ["submit"]


def test_broker_number_may_repeat_across_accounts_and_days(bundle, tmp_path):
    service, result = ready(tmp_path, bundle)
    entries = [event(result, "one", "submitted", 100),
               event(result, "two", "submitted", 100, day="2025-02-28", symbol="600036.SH"),
               event(result, "shadow", "submitted", 100, account_id="shadow", symbol="000001.SZ")]
    import_journal(service.store, csv_events(entries))
    assert len(summarize_orders(journal_entries(service.store), bundle.meta.as_of)) == 2
    assert len(summarize_orders(journal_entries(service.store, "shadow"), bundle.meta.as_of)) == 1


def test_legacy_events_are_not_guessed_or_collided_with_explicit_tickets(bundle, tmp_path):
    service, result = ready(tmp_path, bundle)
    entries = [event(result, "fill", "filled", 100, order=None, executed_price=10),
               event(result, "second", "filled", 100, order="event:fill", executed_price=10)]
    import_journal(service.store, csv_events(entries))
    review = reconcile(service.store, bundle, 100000)["execution_review"]
    assert len(review["orders"]) == 2 and review["unlinked_event_count"] == 1
    assert all(order["requested_quantity"] is None for order in review["orders"])
    assert review["known_adverse_price_impact"] is None


def test_weekly_window_excludes_old_and_future_fills(bundle, tmp_path):
    service, result = ready(tmp_path, bundle, signal=date(2025, 2, 20))
    entries = [event(result, "old", "filled", 100, day="2025-02-21", order="old", executed_price=10, transaction_cost=5),
               event(result, "this-week", "filled", 100, executed_price=10, transaction_cost=7),
               event(result, "future", "filled", 100, day="2025-03-03", order="future", executed_price=10, transaction_cost=9)]
    import_journal(service.store, csv_events(entries))
    report = reconcile(service.store, bundle, 100000)
    review = report["execution_review"]
    assert review["period_start"] == "2025-02-24" and review["period_end"] == "2025-02-28"
    assert review["fill_events"] == 1 and review["transaction_cost"] == 7
    assert report["future_events"] == 1 and len(review["orders"]) == 2
    assert report["holdings"] == {"600000.SH": 200}


def test_short_filled_report_is_flagged_without_fabricating_missing_fills(bundle, tmp_path):
    service, result = ready(tmp_path, bundle)
    import_journal(service.store, csv_events([event(result, "submit", "submitted", 300),
        event(result, "fill", "filled", 100, executed_price=10)]))
    report = reconcile(service.store, bundle, 100000)
    assert report["holdings"] == {"600000.SH": 100}
    assert "reported_filled_but_quantity_short" in report["execution_review"]["orders"][0]["flags"]


@pytest.mark.parametrize("status,quantity,kwargs,match", [
    ("filled", 99, {"executed_price": 10}, "multiple of 100"),
    ("submitted", 100, {"transaction_cost": 5}, "non-execution"),
    ("planned", 0, {}, "must be positive"),
])
def test_new_execution_data_is_validated_before_insertion(bundle, tmp_path, status, quantity, kwargs, match):
    service, result = ready(tmp_path, bundle)
    with pytest.raises(ValueError, match=match):
        import_journal(service.store, csv_events([event(result, "bad", status, quantity, **kwargs)]))
    assert journal_entries(service.store) == []


def test_future_industry_is_not_used_and_invalid_holding_mark_blocks_nav(bundle, tmp_path):
    service, result = ready(tmp_path, bundle)
    import_journal(service.store, csv_events([event(result, "fill", "filled", 100, executed_price=10)]))
    bundle.tables["universe"].loc[bundle["universe"]["symbol"] == "600000.SH", "known_on"] = "2025-03-03"
    report = reconcile(service.store, bundle, 100000)
    assert "unknown" in report["industry_weights"]
    assert any("industry unavailable at cutoff" in flag for flag in report["flags"])
    bundle.tables["prices"].loc[(bundle["prices"]["symbol"] == "600000.SH") &
                               (bundle["prices"]["trade_date"] == str(bundle.meta.as_of)), "close"] = float("nan")
    with pytest.raises(ValueError, match="invalid mark for held symbol"):
        reconcile(service.store, bundle, 100000)


def test_exported_weekly_review_and_order_files_are_traceable(bundle, tmp_path):
    service, result = ready(tmp_path, bundle)
    import_journal(service.store, csv_events([event(result, "fill", "filled", 100, executed_price=10, transaction_cost=5)]))
    report = service.portfolio_report(bundle.meta.snapshot_id, 100000, actual_cash=98995, actual_holdings={"600000.SH": 100})
    folder = service.store.artifact_dir(report["run_id"])
    assert all((folder / name).exists() for name in ("orders.csv", "executions.csv", "portfolio_nav.csv", "weekly_review.md"))
    text = (folder / "weekly_review.md").read_text()
    assert "合成演示" in text and bundle.meta.snapshot_id in text and "## 人工核实" in text
    assert report["actual_cash_difference"] == 0 and report["actual_holdings_difference"] == {}


def test_manual_form_reuses_event_id_until_user_starts_a_new_event(tmp_path, monkeypatch):
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(tmp_path))
    service = FinSight(tmp_path)
    snapshot = service.sync("demo", date(2025, 2, 28), date(2024, 1, 2))["snapshot_id"]
    service.screen(snapshot)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    original = next(item for item in app.text_input if item.label.startswith("事件编号")).value
    next(item for item in app.text_input if item.label.startswith("委托编号")).set_value("42")
    next(button for button in app.button if button.label == "保存日志事件").click().run()
    assert not app.exception and len(journal_entries(service.store)) == 1
    next(button for button in app.button if button.label == "保存日志事件").click().run()
    assert len(journal_entries(service.store)) == 1
    next(button for button in app.button if button.label == "使用新的事件编号").click().run()
    assert not app.exception
    assert next(item for item in app.text_input if item.label.startswith("事件编号")).value != original
