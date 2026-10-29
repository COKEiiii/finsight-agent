import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from finsight.backtest import Commission, Ledger, execute_order, run_backtest, simulate, trading_cost
from finsight.cli import main
from finsight.cost_sensitivity import compare_costs, render_costs
from finsight.models import fingerprint
from finsight.service import FinSight


def test_commission_and_minimum_preserve_effective_date_tax_rules():
    assert trading_cost("2025-01-02", "buy", 10_000) == 5.1
    assert trading_cost("2025-01-02", "buy", 10_000, commission=Commission(.001, 5)) == 10.1
    assert trading_cost("2025-01-02", "buy", 10_000, commission=Commission(0, 0)) == .1
    assert trading_cost("2025-01-02", "sell", 10_000, commission=Commission(0, 0)) == 5.1
    assert trading_cost("2023-08-27", "sell", 10_000) - trading_cost("2023-08-28", "sell", 10_000) == 5
    with pytest.raises(ValueError, match="before 2022"):
        trading_cost("2021-12-31", "buy", 100)


@pytest.mark.parametrize("rate,minimum", [(float("nan"), 5), (float("inf"), 5), (-.1, 5), (1, 5), (.0003, -1), (.0003, float("inf")), (True, 5)])
def test_invalid_commissions_rejected(rate, minimum):
    with pytest.raises(ValueError, match="commission"):
        Commission(rate, minimum)


@pytest.mark.parametrize("action,gross", [("other", 100), ("buy", -1), ("sell", float("nan"))])
def test_invalid_cost_calculation_rejected(action, gross):
    with pytest.raises(ValueError, match="trading cost"):
        trading_cost("2025-01-02", action, gross)


def test_custom_commission_reaches_fill_and_cash_ledger():
    ledger = Ledger(10_000)
    event = execute_order(ledger, "600000.SH", "buy", 100,
        {"open": 10, "volume": 1_000, "suspended": False, "limit_up": 11}, "2025-01-02", 20,
        commission=Commission(.01, 5))
    assert event["status"] == "filled" and event["executed_price"] == 10.02
    assert event["transaction_cost"] == 10.03
    assert ledger.cash == pytest.approx(8987.97)


def test_execution_caps_use_fee_adjusted_nav(bundle, config, monkeypatch):
    symbols = list(bundle["universe"]["symbol"].iloc[:8])
    candidates = [{"symbol": symbol, "industry": f"industry-{i%4}", "close": 10} for i, symbol in enumerate(symbols)]
    monkeypatch.setattr("finsight.backtest.screen", lambda *a, **kw: SimpleNamespace(
        candidates=candidates, eligible_count=8, publication_status="published"))
    bundle.tables["prices"] = pd.DataFrame([{
        "symbol": symbol, "trade_date": day, "open": 10., "close": 10., "volume": 100_000_000,
        "suspended": False, "limit_up": 11., "limit_down": 9.,
    } for day in ("2025-01-31", "2025-02-03", "2025-02-04") for symbol in symbols])
    result = simulate(bundle, config, date(2025, 2, 3), date(2025, 2, 4), capital=10_000_000,
                      slippage_bps=0, commission=Commission(.001, 5))
    assert result["trades"]
    assert len([fill for fill in result["trades"] if fill["status"] == "filled"]) == 8
    assert max(point["equity_weight"] for point in result["curve"]) <= .8 + 1e-12
    assert all(point["cash"] >= 0 for point in result["curve"])


@pytest.mark.parametrize("industry_limited", [False, True])
def test_failed_sale_cannot_expand_holdings_or_industry_exposure(bundle, config, monkeypatch, industry_limited):
    symbols = list(bundle["universe"]["symbol"].iloc[:9])
    count = 7 if industry_limited else 8
    old, new = symbols[0], symbols[8]
    industries = {s:("same" if industry_limited and i in {0,1,2,8} else f"industry-{i}") for i,s in enumerate(symbols)}
    for symbol, industry in industries.items():
        bundle.tables["universe"].loc[bundle["universe"]["symbol"] == symbol, "industry"] = industry
    def selection(bundle, config, day, **kwargs):
        chosen = symbols[:count] if str(day) == "2025-01-31" else [new, *symbols[1:count]]
        return SimpleNamespace(candidates=[{"symbol": s, "close": 10., "industry": industries[s]} for s in chosen],
            eligible_count=count, publication_status="published")
    monkeypatch.setattr("finsight.backtest.screen", selection)
    bundle.meta.as_of = date(2025, 3, 3)
    bundle.tables["prices"] = pd.DataFrame([{
        "symbol": symbol, "trade_date": day, "open": 10., "close": 10., "volume": 0 if symbol == old and day == "2025-03-03" else 100_000_000,
        "suspended": symbol == old and day == "2025-03-03", "limit_up": 11., "limit_down": 9.,
    } for day in ("2025-01-31", "2025-02-03", "2025-02-28", "2025-03-03") for symbol in symbols])
    result = simulate(bundle, config, date(2025, 2, 3), date(2025, 3, 3), slippage_bps=0)
    final = result["curve"][-1]["holdings"]
    assert old in final and new not in final and len(final) == count
    assert any(t["symbol"] == old and t["action"] == "sell" and t["status"] == "cancelled" for t in result["trades"])
    assert any(t["symbol"] == new and t["action"] == "buy" and t["status"] == "cancelled" for t in result["trades"])
    assert any(t["symbol"] == new and t["reason"] == "holding_or_industry_cap" for t in result["trades"])


def test_default_costs_match_explicit_parameters(bundle, config):
    start, end = date(2025, 1, 1), date(2025, 2, 28)
    default = run_backtest(bundle, config, start, end)
    explicit = run_backtest(bundle, config, start, end, commission=Commission(.0003, 5), slippage_bps=5)
    assert default == explicit


@pytest.fixture(scope="module")
def compared():
    from finsight.data.demo import make_demo
    from finsight.data.quality import audit
    from finsight.models import StrategyConfig
    bundle = make_demo(date(2025, 2, 28), date(2024, 1, 2))
    config = StrategyConfig()
    audit(bundle, config)
    report = compare_costs(bundle, config, date(2025, 1, 1), date(2025, 2, 28))
    return bundle, config, report


def test_six_fixed_scenarios_keep_strategy_and_baseline_comparable(compared):
    bundle, config, report = compared
    assert report["scenario_count"] == 6 and len(report["summary"]) == 12
    assert report["config_id"] == config.config_id and report["strategy_config"] == config.model_dump()
    assert report["informative"] and not report["historical_validation_passed"] and report["synthetic"]
    assert "研究演示" in report["status"]
    assert report["model_calls"] == report["market_requests"] == 0
    bases = {row["portfolio"]:row for row in report["summary"] if row["scenario"] == "base"}
    for scenario in report["scenarios"]:
        profile, backtest = scenario["profile"], scenario["backtest"]
        assert backtest["cost_model"]["commission_rate"] == profile["commission_rate"]
        for key in ("strategy", "momentum_baseline"):
            simulation = backtest[key]
            assert simulation["commission"]["rate"] == profile["commission_rate"]
            assert simulation["slippage_bps"] == profile["slippage_bps"]
            expected_signals = report["scenarios"][0]["backtest"][key]["signals"]
            assert [{field:signal[field] for field in ("signal_date", "candidate_symbols", "targets")} for signal in simulation["signals"]] == [
                {field:signal[field] for field in ("signal_date", "candidate_symbols", "targets")} for signal in expected_signals]
            assert simulation["metrics"]["transaction_costs"] == pytest.approx(sum(fill["transaction_cost"] for fill in simulation["trades"]))
            assert all(point["cash"] >= 0 for point in simulation["curve"])
            for fill in simulation["trades"]:
                if fill["status"] == "filled":
                    assert fill["transaction_cost"] == trading_cost(fill["trade_date"], fill["action"], fill["notional"],
                        commission=Commission(profile["commission_rate"], profile["minimum_commission"]))
    for row in report["summary"]:
        base = bases[row["portfolio"]]
        assert row["return_difference_vs_base"] == row["cumulative_return"] - base["cumulative_return"]
        assert row["nav_difference_vs_base"] == row["ending_nav"] - base["ending_nav"]
    default = run_backtest(bundle, config, date(2025, 1, 1), date(2025, 2, 28))
    assert report["scenarios"][0]["backtest"] == default


def test_minimum_fee_can_make_lower_commission_effect_identical(compared):
    _, _, report = compared
    by_key = {(row["scenario"],row["portfolio"]):row for row in report["summary"]}
    for portfolio in ("strategy", "momentum_baseline"):
        assert by_key[("base",portfolio)]["transaction_costs"] == by_key[("commission_low",portfolio)]["transaction_costs"]
        assert by_key[("base",portfolio)]["ending_nav"] == by_key[("commission_low",portfolio)]["ending_nav"]


def test_no_fills_are_explicitly_inconclusive(bundle, config):
    report = compare_costs(bundle, config, date(2025, 2, 26), date(2025, 2, 28))
    assert not report["informative"]
    assert all(not row["informative"] and row["fill_count"] == 0 for row in report["summary"])
    assert "无法据此判断成本影响" in render_costs(report)


def test_missing_price_sessions_remain_visible_in_json_and_csv_rows(bundle, config):
    bundle.tables["prices"] = bundle["prices"].loc[bundle["prices"]["trade_date"] != "2025-01-10"]
    report = compare_costs(bundle, config, date(2025, 1, 1), date(2025, 2, 28))
    assert report["input_quality"]["price_sessions_missing"] == ["2025-01-10"]
    assert not report["informative"] and not report["historical_validation_passed"]
    assert all(row["price_sessions_missing"] == 1 and not row["informative"] for row in report["summary"])
    assert all(row["snapshot_id"] == bundle.meta.snapshot_id and row["config_id"] == config.config_id for row in report["summary"])


def test_missing_whole_session_blocks_research_even_when_the_account_is_in_cash(bundle, config):
    # Controlled acceptance flags are restricted to this temporary test fixture.
    bundle.meta.synthetic = False
    bundle.meta.research_checks = dict.fromkeys(bundle.meta.research_checks, True)
    bundle.tables["prices"] = bundle["prices"].loc[bundle["prices"]["trade_date"] != "2025-02-27"]
    report = run_backtest(bundle, config, date(2025, 2, 26), date(2025, 2, 28))
    assert not report["strategy"]["trades"]
    assert report["input_quality"]["missing_price_sessions"] == ["2025-02-27"]
    assert not report["research_eligible"]


@pytest.mark.parametrize("slippage", [-1, float("nan"), 10_000])
def test_invalid_slippage_cannot_change_the_ledger(slippage):
    ledger = Ledger(10_000)
    with pytest.raises(ValueError, match="slippage"):
        execute_order(ledger, "600000.SH", "buy", 100, None, "2025-01-02", slippage)
    assert ledger.cash == 10_000 and not ledger.symbols


def test_sensitivity_service_exports_and_cli_preserve_existing_records(tmp_path, bundle, capsys, monkeypatch):
    from finsight.research import ChatAdapter
    monkeypatch.setattr(ChatAdapter, "complete", lambda *a, **kw: pytest.fail("cost comparison must not call a model"))
    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    screen = service.screen(bundle.meta.snapshot_id)
    before = fingerprint(service.store.run(screen.run_id))
    assert main(["--data-dir", str(tmp_path), "backtest", "--snapshot", bundle.meta.snapshot_id,
        "--start", "2025-01-01", "--end", "2025-02-28", "--cost-sensitivity"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scenario_count"] == 6 and len(result["summary"]) == 12
    folder = Path(result["exports"])
    saved = json.loads((folder / "cost_sensitivity.json").read_text())
    assert saved["config_id"] == service.config.config_id
    assert saved["snapshot_table_hashes"] == service.store.load_snapshot(screen.snapshot_id, table_names=()).meta.table_hashes
    assert "对基准收益差（百分点）" in (folder / "cost_sensitivity.md").read_text()
    assert (folder / "cost_sensitivity.csv").read_text().startswith("\ufeffscenario,")
    assert len(service.store.runs("cost_sensitivity")) == 1
    expected = run_backtest(bundle, service.config, date(2025, 1, 1), date(2025, 2, 28))
    assert saved["scenarios"][0]["backtest"] == expected
    assert fingerprint(service.store.run(screen.run_id)) == before
    assert not service.store.runs("research") and not service.store.runs("signal_seal")
    assert service.store.load_snapshot(screen.snapshot_id)["prices"].equals(bundle["prices"])


def test_report_rendering_uses_percentage_points_for_return_differences(compared):
    report = copy.deepcopy(compared[2])
    report["summary"][0]["return_difference_vs_base"] = .005
    assert "+0.5000" in render_costs(report)


@pytest.mark.parametrize("start,end", [(date(2025, 3, 1),date(2025, 2, 28)), (date(2025, 1, 1),date(2025, 3, 3))])
def test_invalid_interval_creates_no_saved_experiment(tmp_path, bundle, start, end):
    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    with pytest.raises(ValueError, match="interval|cutoff"):
        service.cost_sensitivity(bundle.meta.snapshot_id, start, end)
    assert not service.store.runs("cost_sensitivity")


def test_dashboard_only_computes_costs_on_click_and_labels_units(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from finsight.research import ChatAdapter
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ChatAdapter, "complete", lambda *a, **kw: pytest.fail("cost UI must not invoke models"))
    service = FinSight(tmp_path)
    service.sync("demo", date(2025, 2, 28), date(2024, 1, 2))
    original = FinSight.cost_sensitivity
    calls = []
    def compare(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FinSight, "cost_sensitivity", compare)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=60).run()
    assert not app.exception and not calls
    next(item for item in app.date_input if item.label == "回测开始").set_value(date(2025, 1, 1))
    next(item for item in app.date_input if item.label == "回测结束").set_value(date(2025, 2, 28))
    next(item for item in app.button if item.label == "运行成本敏感性对比").click().run()
    assert not app.exception and len(calls) == 1
    table = next(item.value for item in app.dataframe if "单边佣金 bp" in item.value.columns)
    assert len(table) == 12
    assert table.iloc[0]["单边佣金 bp"] == 3
    assert "对基准收益差（百分点）" in table.columns
    assert any("研究演示" in item.value for item in app.warning)
    app.run()
    assert not app.exception and len(calls) == 1
    assert len(service.store.runs("cost_sensitivity")) == 1
    assert not service.store.runs("backtest")
