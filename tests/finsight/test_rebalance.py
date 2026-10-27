import copy
import csv
import io
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from finsight.cli import main
from finsight.data.quality import audit
from finsight.models import StrategyConfig, TradeJournalEntry
from finsight.portfolio import import_journal, journal_entries
from finsight.rebalance import rebalance_plan
from finsight.screening import screen
from finsight.service import FinSight


def inputs(bundle, config, count=1, holdings=None, cash=100000, receivables=0):
    value = copy.deepcopy(bundle)
    for field in ("open", "close", "high", "low", "adj_close"):
        value.tables["prices"].loc[:, field] = 10.
    symbols = list(value["universe"]["symbol"])
    base = screen(value, config)
    candidates = [{**base.candidates[0], "symbol": symbol, "rank": i + 1, "close": 10.,
                   "industry": value["universe"].set_index("symbol").loc[symbol, "industry"]}
                  for i, symbol in enumerate(symbols[:count])]
    result = base.model_copy(update={"candidates": candidates})
    account = {"as_of": str(value.meta.as_of), "account_id": "synthetic", "initial_cash": 100000,
               "holdings": holdings or {}, "cash": cash, "receivables": receivables,
               "nav": cash + sum((holdings or {}).values()) * 10 + receivables,
               "execution_review": {"open_order_count": 0}, "future_events": 0}
    return value, result, account


def test_delta_uses_current_nav_instead_of_starting_capital(bundle, config):
    value, result, account = inputs(bundle, config, holdings={"600000.SH": 1000}, cash=110000)
    plan = rebalance_plan(result, config, value, account)
    assert plan["status"] == "ready" and plan["account_nav"] == 120000
    assert plan["targets"][0]["reference_quantity"] == 1200
    assert plan["orders"][0]["action"] == "buy" and plan["orders"][0]["quantity"] == 200
    assert plan["execution_date"] == "2025-03-03" and plan["synthetic"]
    assert plan["estimated_holdings"] == {"600000.SH": 1200}


def test_sell_first_cash_fees_and_post_cost_exposure_conserve_value(bundle, config):
    value, result, account = inputs(bundle, config, count=8, holdings={"600309.SH": 10000}, cash=0)
    plan = rebalance_plan(result, config, value, account)
    orders = plan["orders"]
    assert orders[0]["action"] == "sell" and orders[0]["symbol"] == "600309.SH"
    assert all(row["action"] == "buy" and row["requires_sale_proceeds"] for row in orders[1:])
    assert all(row["quantity"] % 100 == 0 and row["estimated_cash_after"] >= 0 for row in orders)
    assert 0 < plan["estimated_equity_weight"] <= .8
    assert plan["estimated_fees"] == pytest.approx(sum(row["estimated_fee"] for row in orders))
    equity = sum(plan["estimated_holdings"].values()) * 10
    assert plan["estimated_nav_after"] == pytest.approx(plan["estimated_cash_after"] + equity)
    assert plan["estimated_nav_after"] == pytest.approx(account["nav"] - plan["estimated_fees"] - plan["estimated_adverse_price_impact"])
    assert len(plan["estimated_holdings"]) <= 8
    assert len(plan["skipped"]) > 0  # Costs prevent the eighth full lot-sized target at exactly 80%.


def test_receivables_are_net_value_but_never_spendable_cash(bundle, config):
    value, result, account = inputs(bundle, config, cash=0, receivables=100000)
    plan = rebalance_plan(result, config, value, account)
    assert plan["targets"][0]["reference_quantity"] == 1000
    assert plan["orders"] == [] and plan["estimated_cash_after"] == 0
    assert plan["skipped"][0]["quantity"] == 1000


@pytest.mark.parametrize("holdings,cash,expected", [({"600000.SH": 225}, 7750, {"600000.SH": 125}),
    ({"600036.SH": 125}, 8750, {"600000.SH": 100})])
def test_odd_lot_reduction_requires_whole_lots_unless_liquidating(bundle, config, holdings, cash, expected):
    value, result, account = inputs(bundle, config, holdings=holdings, cash=cash)
    plan = rebalance_plan(result, config, value, account)
    assert plan["estimated_holdings"] == expected
    if "600000.SH" in holdings:
        assert plan["orders"][0]["quantity"] == 100 and plan["target_quantity_differences"] == {"600000.SH": 25}
    else:
        assert plan["orders"][0]["quantity"] == 125 and plan["orders"][0]["action"] == "sell"


def test_existing_position_below_next_lot_is_not_topped_up_by_an_odd_buy(bundle, config):
    value, result, account = inputs(bundle, config, holdings={"600000.SH": 50}, cash=9500)
    plan = rebalance_plan(result, config, value, account)
    assert plan["orders"] == [] and plan["target_quantity_differences"] == {"600000.SH": -50}


@pytest.mark.parametrize("condition", ["preview", "open", "future", "stale", "industry", "state", "calendar"])
def test_unresolved_account_or_data_conditions_block_orders(bundle, config, condition):
    value, result, account = inputs(bundle, config, holdings={"600036.SH": 100}, cash=99000)
    if condition == "preview":
        result.publication_status = "preview"
    elif condition == "open":
        account["execution_review"]["open_order_count"] = 1
    elif condition == "future":
        account["future_events"] = 1
    elif condition == "stale":
        value.tables["prices"] = value["prices"].loc[~((value["prices"]["symbol"] == "600036.SH") & (value["prices"]["trade_date"] == "2025-02-28"))]
    elif condition == "industry":
        value.tables["universe"].loc[value["universe"]["symbol"] == "600036.SH", "known_on"] = "2025-03-03"
    elif condition == "state":
        value.tables["prices"]["suspended"] = value["prices"]["suspended"].astype(object)
        value.tables["prices"].loc[value["prices"]["symbol"] == "600036.SH", "suspended"] = None
    else:
        value.tables["calendar"] = value["calendar"].loc[value["calendar"]["trade_date"] <= "2025-02-28"]
    plan = rebalance_plan(result, config, value, account)
    assert plan["status"] == "blocked" and plan["orders"] == [] and plan["blockers"]


def test_non_month_end_produces_reference_without_orders(bundle, config):
    value, result, account = inputs(bundle, config)
    value.meta.as_of = result.as_of = date(2025, 2, 26)
    account["as_of"] = "2025-02-26"
    plan = rebalance_plan(result, config, value, account)
    assert plan["status"] == "reference" and plan["orders"] == [] and plan["execution_date"] is None


def test_frozen_snapshot_and_strategy_binding_are_enforced(bundle, config):
    value, result, account = inputs(bundle, config)
    bad_config = config.model_copy(update={"version": "shadow"})
    with pytest.raises(ValueError, match="frozen"):
        rebalance_plan(result, bad_config, value, account)
    result.as_of = date(2025, 2, 26)
    with pytest.raises(ValueError, match="original cutoff"):
        rebalance_plan(result, config, value, account)


def test_suspension_is_an_execution_warning_not_an_assumed_fill(bundle, config):
    value, result, account = inputs(bundle, config, holdings={"600036.SH": 100}, cash=99000)
    value.tables["prices"].loc[value["prices"]["symbol"] == "600036.SH", "suspended"] = True
    plan = rebalance_plan(result, config, value, account)
    assert any("suspended" in item for item in plan["warnings"])
    assert all("executed_price" not in row and "status" not in row for row in plan["orders"])


def test_sale_fees_can_exhaust_a_tiny_account_without_dividing_by_zero(bundle, config):
    value, result, account = inputs(bundle, config, holdings={"600036.SH": 1}, cash=4.99)
    value.tables["prices"].loc[value["prices"]["symbol"] == "600036.SH", "close"] = .02
    account["nav"] = 5.01
    plan = rebalance_plan(result, config, value, account)
    assert plan["status"] == "requires_review" and plan["estimated_equity_weight"] is None
    assert plan["estimated_cash_after"] == pytest.approx(0, abs=1e-8)
    assert plan["orders"][0]["action"] == "sell" and plan["orders"][0]["quantity"] == 1


def test_cli_non_month_end_plan_exports_a_valid_empty_order_table(bundle, tmp_path, capsys):
    service = FinSight(tmp_path)
    bundle.meta.as_of = date(2025, 2, 26)
    for name in ("prices", "valuations", "benchmark"):
        bundle.tables[name] = bundle[name].loc[bundle[name]["trade_date"] <= "2025-02-26"]
    service._save_bundle(bundle)
    result = service.screen(bundle.meta.snapshot_id)
    assert main(["--data-dir", str(tmp_path), "journal", "plan", "--run", result.run_id, "--capital", "100000"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "reference" and plan["orders"] == []
    frame = pd.read_csv(service.store.artifact_dir(plan["run_id"]) / "rebalance_orders.csv")
    assert frame.empty and "quantity" in frame.columns


def test_plan_freezes_one_journal_view_for_nav_performance_and_traceability(bundle, tmp_path, monkeypatch):
    import finsight.portfolio as portfolio

    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    result = service.screen(bundle.meta.snapshot_id)
    calls = []
    def read_once(*args, **kwargs):
        calls.append(True)
        assert len(calls) == 1, "a later journal read could mix concurrent imports into the same plan"
        return []
    monkeypatch.setattr(portfolio, "journal_entries", read_once)
    plan = service.rebalance(result.run_id)
    assert len(calls) == 1 and plan["journal_event_ids"] == []
    assert plan["account_nav"] == plan["cash_before"] == 100000


def test_service_uses_saved_signal_config_even_when_current_config_differs(bundle, tmp_path):
    service = FinSight(tmp_path, StrategyConfig(version="shadow-rebalance", target_weight=.08))
    service._save_bundle(bundle)
    result = service.screen(bundle.meta.snapshot_id)
    plan = FinSight(tmp_path).rebalance(result.run_id)
    assert plan["config_id"] == result.config_id and plan["strategy_version"] == "shadow-rebalance"
    assert all(row["target_weight"] == .08 for row in plan["targets"])


def test_service_cli_and_ui_share_account_plan_without_recording_trades(bundle, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(tmp_path))
    service = FinSight(tmp_path)
    audit(bundle)
    service._save_bundle(bundle)
    before = service.screen(bundle.meta.snapshot_id, date(2025, 2, 26))
    entry = TradeJournalEntry(event_id="realistic-synthetic-fill", trade_date=date(2025, 2, 27), symbol="600000.SH", action="buy",
        status="filled", quantity=100, executed_price=10, transaction_cost=5, run_id=before.run_id, strategy_version=before.strategy_version)
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(TradeJournalEntry.model_fields))
    writer.writeheader()
    writer.writerow(entry.model_dump(mode="json"))
    import_journal(service.store, stream.getvalue())
    result = service.screen(bundle.meta.snapshot_id)
    plan = service.rebalance(result.run_id)
    repeat = service.rebalance(result.run_id)
    assert plan["orders"] == repeat["orders"] and plan["journal_fingerprint"] == repeat["journal_fingerprint"]
    assert plan["cash_before"] == 98995 and len(journal_entries(service.store)) == 1
    assert plan["journal_event_ids"] == [entry.event_id] and plan["signal_run_id"] == result.run_id
    folder = service.store.artifact_dir(plan["run_id"])
    assert json.loads((folder / "rebalance_plan.json").read_text())["holdings_before"] == {"600000.SH": 100}
    assert not pd.read_csv(folder / "rebalance_orders.csv").empty
    assert main(["--data-dir",str(tmp_path),"journal","plan","--run",result.run_id,"--capital","100000"]) == 0
    assert json.loads(capsys.readouterr().out)["orders"] == plan["orders"]
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=30).run()
    next(button for button in app.button if button.label == "生成账户调仓计划").click().run()
    assert not app.exception and len(journal_entries(service.store)) == 1
