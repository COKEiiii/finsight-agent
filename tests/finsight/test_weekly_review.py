"""All trades, account declarations and past timestamps below are temporary fixtures."""

import copy
import csv
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from finsight.account_inputs import actual_holdings, holdings_csv, validate_actual_cash
from finsight.cli import main
from finsight.models import StrategyConfig, TradeJournalEntry, fingerprint
from finsight.observations import CHINA
from finsight.portfolio import import_journal, journal_entries
from finsight.service import FinSight
from finsight.weekly_review import latest_report


@pytest.fixture
def prepared(tmp_path, bundle):
    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    signal = service.screen(bundle.meta.snapshot_id, date(2025, 2, 26))
    return service, signal


def entry(signal, event_id="fill", *, status="filled", day="2025-02-27", **kwargs):
    return TradeJournalEntry(
        event_id=event_id,
        order_id=event_id,
        trade_date=date.fromisoformat(day),
        run_id=signal.run_id,
        strategy_version=signal.strategy_version,
        **{
            "symbol": "600000.SH",
            "action": "buy",
            "quantity": 100,
            "status": status,
            "executed_price": 10 if status in {"filled", "partial"} else None,
            **kwargs,
        },
    )


def record(service, *entries):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(TradeJournalEntry.model_fields))
    writer.writeheader()
    writer.writerows(row.model_dump(mode="json") for row in entries)
    return import_journal(service.store, stream.getvalue())


def codes(report):
    return {row["code"] for row in report["weekly_review"]["findings"]}


def test_missing_account_data_and_explicit_zero_holdings_are_different():
    assert actual_holdings(None) is None and actual_holdings({}) == {}
    assert actual_holdings({"600000": 100, "000001.sz": 0, "830001.BJ": 200}) == {
        "000001.SZ": 0,
        "600000.SH": 100,
        "830001.BJ": 200,
    }
    assert holdings_csv("\ufeffsymbol,quantity\n600000,100\n000001.SZ,0\n") == {
        "000001.SZ": 0,
        "600000.SH": 100,
    }


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"600000.SH": -1},
        {"600000.SH": 1.0},
        {"600000.SH": True},
        {"600000.SH": "100"},
        {"bad": 100},
        {"０００００１.SZ": 100},
        {600000: 100},
        {"600000": 1, "600000.SH": 2},
    ],
)
def test_invalid_holdings_are_not_silently_coerced(value):
    with pytest.raises(ValueError):
        actual_holdings(value)


@pytest.mark.parametrize(
    "text",
    [
        "symbol,quantity\n",
        "symbol,quantity\n600000,1.5\n",
        "symbol,quantity\n600000,-1\n",
        "symbol,quantity\n600000,100\n600000.SH,200\n",
        "symbol,quantity\n600000,100,extra\n",
        "symbol,quantity\n600000\n",
        'symbol,quantity\n"600000,100\n',
        "symbol,symbol\n600000,100\n",
    ],
)
def test_invalid_holdings_csv_requires_correction(text):
    with pytest.raises(ValueError):
        holdings_csv(text)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "100"])
def test_invalid_actual_cash_is_rejected(value):
    with pytest.raises(ValueError, match="actual cash"):
        validate_actual_cash(value)


def test_empty_journal_stays_unexecuted_and_unreconciled(prepared):
    service, signal = prepared
    report = service.portfolio_report(signal.snapshot_id)
    assert [row["label"] for row in report["weekly_review"]["groups"]] == ["策略", "数据", "执行", "人工操作"]
    assert {
        "no_execution_history",
        "no_week_fills",
        "missing_actual_cash",
        "missing_actual_holdings",
    } <= codes(report)
    assert report["weekly_review"]["status"] == "awaiting_first_execution"
    assert report["weekly_review"]["manual_review"]["status"] == "pending"
    assert report["performance"]["metrics"] is None
    assert report["account_comparison"]["holdings"] is report["actual_holdings_difference"] is None
    assert not journal_entries(service.store)


def test_explicit_empty_account_match_does_not_invent_trade_acceptance(prepared):
    service, signal = prepared
    report = service.portfolio_report(
        signal.snapshot_id, actual_cash=100_000, actual_holdings={}, actual_as_of=date(2025, 2, 28)
    )
    assert "inputs_match" in codes(report)
    assert not {"missing_actual_cash", "missing_actual_holdings", "unknown_account_date"} & codes(report)
    assert report["actual_cash_difference"] == 0 and report["actual_holdings_difference"] == {}
    assert report["weekly_review"]["status"] == "awaiting_first_execution" and not report["research_eligible"]


def test_legacy_undated_inputs_are_computed_but_not_called_confirmed(prepared):
    service, signal = prepared
    report = service.portfolio_report(signal.snapshot_id, actual_cash=100_000, actual_holdings={})
    assert report["actual_cash_difference"] == 0 and report["actual_holdings_difference"] == {}
    assert "unknown_account_date" in codes(report) and "inputs_match" not in codes(report)


@pytest.mark.parametrize(
    "arguments",
    [
        {"actual_as_of": date(2025, 3, 3)},
        {"actual_cash": -1},
        {"actual_holdings": {"600000.SH": 1.5}},
        {"review_notes": {"data": "已核对"}},
        {"review_notes": {"other": "no"}, "reviewer": "tester"},
        {"review_notes": {"data": 1}, "reviewer": "tester"},
        {"review_notes": {"data": "x" * 4001}, "reviewer": "tester"},
    ],
)
def test_invalid_comparison_or_notes_save_no_report(prepared, arguments):
    service, signal = prepared
    with pytest.raises(ValueError):
        service.portfolio_report(signal.snapshot_id, **arguments)
    assert not service.store.runs("portfolio_report")


def test_actual_fills_and_adverse_price_have_evidence_without_causal_claims(prepared):
    service, signal = prepared
    record(service, entry(signal, planned_price=10, executed_price=10.1, transaction_cost=5))
    report = service.portfolio_report(
        signal.snapshot_id, actual_cash=98985, actual_holdings={"600000": 100}, actual_as_of=date(2025, 2, 28)
    )
    assert {"recorded_costs", "adverse_price", "inputs_match", "non_month_end_signal"} <= codes(report)
    assert report["actual_cash_difference"] == 0 and report["actual_holdings_difference"] == {}
    evidence = next(row for row in report["weekly_review"]["findings"] if row["code"] == "adverse_price")
    assert evidence["category"] == "execution" and evidence["evidence"]["amount"] == pytest.approx(10)
    assert "不自动判断" in evidence["next_step"]
    assert report["weekly_review"]["source_screens"][0]["source_fingerprint"] == fingerprint(
        service.store.run(signal.run_id)["payload"]
    )


def test_differences_and_missing_plan_price_are_separate_findings(prepared):
    service, signal = prepared
    record(service, entry(signal, transaction_cost=5))
    report = service.portfolio_report(
        signal.snapshot_id, actual_cash=98000, actual_holdings={}, actual_as_of=date(2025, 2, 28)
    )
    assert {"cash_difference", "holdings_difference", "missing_plan_price"} <= codes(report)
    assert report["execution_review"]["known_adverse_price_impact"] is None
    assert report["actual_holdings_difference"] == {"600000.SH": -100}


def test_future_events_do_not_create_current_execution_evidence(prepared):
    service, signal = prepared
    record(service, entry(signal, day="2025-03-03"))
    report = service.portfolio_report(signal.snapshot_id)
    assert {"later_events", "no_execution_history", "no_week_fills"} <= codes(report)
    assert report["visible_event_ids"] == [] and report["journal_event_ids"] == ["fill"]
    assert report["weekly_review"]["source_screens"] == []


def test_old_executions_are_traceable_without_becoming_this_weeks_costs(tmp_path, bundle):
    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    signal = service.screen(bundle.meta.snapshot_id, date(2025, 2, 20))
    record(service, entry(signal, day="2025-02-21", transaction_cost=7))
    report = service.portfolio_report(signal.snapshot_id)
    assert (
        report["execution_review"]["fill_events"] == 0 and report["execution_review"]["transaction_cost"] == 0
    )
    assert "no_week_fills" in codes(report) and "no_execution_history" not in codes(report)
    assert report["weekly_review"]["source_screens"][0]["run_id"] == signal.run_id
    assert "non_month_end_signal" not in codes(report)


def test_stale_holding_price_and_future_industry_are_data_issues(prepared, bundle):
    service, signal = prepared
    record(service, entry(signal))
    changed = copy.deepcopy(bundle)
    changed.meta.snapshot_id += "-stale"
    changed.tables["prices"] = changed["prices"].loc[
        ~((changed["prices"]["symbol"] == "600000.SH") & (changed["prices"]["trade_date"] == "2025-02-28"))
    ]
    changed.tables["universe"].loc[changed["universe"]["symbol"] == "600000.SH", "known_on"] = "2025-03-03"
    service._save_bundle(changed)
    report = service.portfolio_report(changed.meta.snapshot_id)
    assert {"stale_quote", "nav_price_flags", "unknown_holding_industry"} <= codes(report)
    assert report["holding_quote_dates"]["600000.SH"] == "2025-02-27"
    assert report["holding_industries"]["600000.SH"] == "unknown"


def test_weight_breach_is_a_monitor_fact_not_a_loss_explanation(prepared):
    service, signal = prepared
    record(service, entry(signal, quantity=1000))
    report = service.portfolio_report(signal.snapshot_id, capital=20000)
    assert "single_weight" in codes(report)
    finding = next(row for row in report["weekly_review"]["findings"] if row["code"] == "single_weight")
    assert finding["evidence"]["weight"] > finding["evidence"]["limit"] == 0.15
    assert finding["category"] == "strategy"


def test_mixed_source_configs_are_not_reported_as_one_strategy(prepared):
    service, signal = prepared
    other = FinSight(service.store.root, StrategyConfig(version="shadow-test"))
    shadow = other.screen(signal.snapshot_id, date(2025, 2, 26))
    record(service, entry(signal, "base"), entry(shadow, "shadow", symbol="600036.SH"))
    report = service.portfolio_report(signal.snapshot_id)
    assert "mixed_strategy_sources" in codes(report)
    assert {row["config_id"] for row in report["weekly_review"]["source_screens"]} == {
        service.config.config_id,
        other.config.config_id,
    }


@pytest.mark.parametrize("corruption", ["config", "kind", "date"])
def test_changed_signal_binding_cannot_produce_a_verified_weekly_report(prepared, corruption):
    service, signal = prepared
    record(service, entry(signal))
    row = service.store.run(signal.run_id)
    payload = row["payload"]
    if corruption == "config":
        payload["config_id"] = "changed"
    if corruption == "date":
        payload["as_of"] = "2025-03-03"
    with service.store.connect() as db:
        db.execute(
            "UPDATE runs SET payload=?,kind=? WHERE run_id=?",
            (json.dumps(payload), "research" if corruption == "kind" else "screen", signal.run_id),
        )
    with pytest.raises(ValueError, match="weekly review"):
        service.portfolio_report(signal.snapshot_id)
    assert not service.store.runs("portfolio_report")


def test_human_notes_are_bound_separately_and_do_not_clear_findings(prepared, monkeypatch):
    from finsight.research import ChatAdapter

    monkeypatch.setattr(
        ChatAdapter, "complete", lambda *a, **kw: pytest.fail("weekly review must not invoke models")
    )
    service, signal = prepared
    before = fingerprint(service.store.run(signal.run_id))
    report = service.portfolio_report(
        signal.snapshot_id,
        review_notes={"data": "需要核对分红送转", "manual": "尚未录入账户数据"},
        reviewer="unit tester",
    )
    human = report["weekly_review"]["manual_review"]
    assert human["status"] == "notes_recorded" and human["noted_categories"] == ["data", "manual"]
    assert "missing_actual_holdings" in codes(report) and "corporate_actions" in codes(report)
    assert not report["research_eligible"] and fingerprint(service.store.run(signal.run_id)) == before
    loaded = latest_report(service.store, signal.snapshot_id, "paper", service.config)
    assert loaded["weekly_review"]["manual_review"] == human
    folder = service.store.artifact_dir(report["run_id"])
    text = (folder / "weekly_review.md").read_text()
    assert "## 四类复盘检查" in text and "需要核对分红送转" in text and "unit tester" in text
    rows = list(csv.DictReader(io.StringIO((folder / "weekly_findings.csv").read_text(encoding="utf-8-sig"))))
    assert all(
        row["snapshot_id"] == signal.snapshot_id
        and row["journal_fingerprint"] == report["journal_fingerprint"]
        for row in rows
    )
    assert all(isinstance(json.loads(row["evidence"]), dict) for row in rows)


@pytest.mark.parametrize("corruption", ["note", "snapshot", "account", "version"])
def test_saved_report_binding_or_note_changes_remain_errors(prepared, corruption):
    service, signal = prepared
    report = service.portfolio_report(signal.snapshot_id)
    payload = service.store.run(report["run_id"])["payload"]
    if corruption == "note":
        payload["weekly_review"]["manual_review"]["notes"]["data"] = "changed"
    if corruption == "snapshot":
        payload["snapshot_id"] = "different"
    if corruption == "account":
        payload["weekly_review"]["account_id"] = "different"
    if corruption == "version":
        payload["portfolio_review_version"] = "unknown"
    with service.store.connect() as db:
        db.execute("UPDATE runs SET payload=? WHERE run_id=?", (json.dumps(payload), report["run_id"]))
    with pytest.raises(ValueError, match="saved|binding|integrity"):
        latest_report(service.store, signal.snapshot_id, "paper", service.config)


def test_cli_notes_and_comparison_date_use_same_service(prepared, tmp_path, capsys):
    service, signal = prepared
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"strategy": "等待实际执行", "execution": "本周未成交"}))
    assert (
        main(
            [
                "--data-dir",
                str(service.store.root),
                "journal",
                "report",
                "--snapshot",
                signal.snapshot_id,
                "--capital",
                "100000",
                "--actual-cash",
                "100000",
                "--actual-as-of",
                "2025-02-28",
                "--review-notes",
                str(path),
                "--reviewer",
                "unit tester",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["weekly_review"]["manual_review"]["reviewer"] == "unit tester"
    assert "missing_actual_holdings" in codes(report) and "unknown_account_date" not in codes(report)


def test_dashboard_form_and_refresh_keep_saved_inputs_and_notes(prepared, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from finsight.research import ChatAdapter

    monkeypatch.setattr(
        ChatAdapter, "complete", lambda *a, **kw: pytest.fail("weekly UI must not call models")
    )
    service, signal = prepared
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(service.store.root))
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"), default_timeout=60
    ).run()
    next(item for item in app.number_input if item.label == "实际现金核对（元，可选）").set_value(100000.0)
    next(item for item in app.checkbox if item.label == "确认账户无股票持仓").check()
    next(item for item in app.text_input if item.label.startswith("复盘记录人")).set_value("unit tester")
    next(item for item in app.text_area if item.label == "策略复盘笔记（可选）").set_value(
        "等待首轮真实人工执行"
    )
    next(item for item in app.button if item.label == "生成持仓与周度检查报告").click().run()
    assert not app.exception and len(service.store.runs("portfolio_report")) == 1
    saved = service.store.runs("portfolio_report")[0]
    assert json.loads(saved["payload"])["account_comparison"]["holdings"] == {}
    app.run()
    assert not app.exception and len(service.store.runs("portfolio_report")) == 1
    assert any("等待首轮真实人工执行" in item.value for item in app.text)
    record(service, entry(signal, "submitted", status="submitted"))
    app.run()
    assert not app.exception and any("日志已有更新" in item.value for item in app.warning)
    assert service.store.runs("portfolio_report")[0]["payload"] == saved["payload"]


@pytest.mark.parametrize("sealed", [False, True])
def test_real_signal_proof_is_read_only_and_semantically_bound(tmp_path, sealed):
    from finsight.data.demo import make_demo
    from finsight.data.universe import membership_id, universe_id

    service = FinSight(tmp_path)

    def fixture(day):
        # Artificial values/acceptance labels are confined to the temporary test store.
        bundle = make_demo(day, date(2024, 1, 2))
        bundle.meta.synthetic, bundle.meta.source = False, "akshare"
        bundle.meta.universe_id, bundle.meta.universe_complete = universe_id(True), True
        bundle.meta.membership_symbols = sorted(bundle["universe"]["symbol"])
        bundle.meta.membership_id = membership_id(bundle.meta.membership_symbols)
        bundle.meta.captured_at = datetime.combine(day, datetime.min.time(), CHINA).replace(
            hour=19, minute=59, second=55
        )
        service._save_bundle(bundle)
        return bundle

    def set_time(run_id, value):
        utc = datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None).isoformat(" ")
        with service.store.connect() as db:
            db.execute("UPDATE runs SET created_at=? WHERE run_id=?", (utc, run_id))

    original_bundle = fixture(date(2025, 2, 28))
    original = service.screen(original_bundle.meta.snapshot_id)
    set_time(original.run_id, "2025-02-28T20:00:00+08:00")
    seal = None
    if sealed:
        seal = service.observe_signal(original.run_id)
        set_time(seal["seal_run_id"], "2025-03-03T09:00:00+08:00")
    repeat = service.screen(original_bundle.meta.snapshot_id)
    assert repeat.candidates == original.candidates
    current = fixture(date(2025, 3, 7))
    record(service, entry(repeat, day="2025-03-03", symbol=repeat.candidates[0]["symbol"]))
    before = {rid: fingerprint(service.store.run(rid)) for rid in (original.run_id, repeat.run_id)}
    report = service.portfolio_report(current.meta.snapshot_id)
    assert len(service.store.runs("signal_seal")) == int(sealed)
    assert all(fingerprint(service.store.run(rid)) == value for rid, value in before.items())
    if sealed:
        source = report["weekly_review"]["source_screens"][0]
        assert source["prospective_seal_id"] == seal["seal_run_id"]
        assert source["prospective_source_run_id"] == original.run_id and source["prospective_eligible"]
        assert not {"signal_timing_unverified", "late_source"} & codes(report)
        assert service.store.run(seal["seal_run_id"])["created_at"] == "2025-03-03 01:00:00"
        assert (
            latest_report(service.store, current.meta.snapshot_id, "paper", service.config)["run_id"]
            == report["run_id"]
        )
        set_time(seal["seal_run_id"], "2025-03-03T09:01:00+08:00")
        with pytest.raises(ValueError, match="prospective seal changed"):
            latest_report(service.store, current.meta.snapshot_id, "paper", service.config)
    else:
        assert {"signal_timing_unverified", "late_source"} <= codes(report)


def test_month_end_execution_date_deviation_is_not_mistaken_for_non_month_end(tmp_path):
    from finsight.data.demo import make_demo

    bundle = make_demo(date(2025, 3, 7), date(2024, 1, 2))
    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    signal = service.screen(bundle.meta.snapshot_id, date(2025, 2, 28))
    record(service, entry(signal, day="2025-03-04", symbol=signal.candidates[0]["symbol"]))
    report = service.portfolio_report(bundle.meta.snapshot_id)
    assert "execution_date_deviation" in codes(report) and "non_month_end_signal" not in codes(report)
    finding = next(
        row for row in report["weekly_review"]["findings"] if row["code"] == "execution_date_deviation"
    )
    assert finding["evidence"]["expected"] == "2025-03-03"


def test_legacy_weekly_report_is_readable_without_automatic_upgrade(prepared):
    service, signal = prepared
    report = service.portfolio_report(signal.snapshot_id)
    payload = service.store.run(report["run_id"])["payload"]
    for field in (
        "weekly_review",
        "portfolio_review_version",
        "weekly_review_fingerprint",
        "monitor_config_id",
        "strategy_config",
    ):
        payload.pop(field)
    legacy_id = service.store.save_run("portfolio_report", signal.snapshot_id, service.config, payload)
    before = len(service.store.runs("portfolio_report"))
    loaded = latest_report(service.store, signal.snapshot_id, "paper", service.config)
    assert loaded["run_id"] == legacy_id and "weekly_review" not in loaded
    assert len(service.store.runs("portfolio_report")) == before


def test_changed_source_after_saving_is_not_silently_displayed_as_verified(prepared):
    service, signal = prepared
    record(service, entry(signal, status="submitted"))
    report = service.portfolio_report(signal.snapshot_id)
    original_report = service.store.run(report["run_id"])
    source = service.store.run(signal.run_id)
    source["payload"]["candidates"][0]["score"] += 1
    with service.store.connect() as db:
        db.execute("UPDATE runs SET payload=? WHERE run_id=?", (json.dumps(source["payload"]), signal.run_id))
    with pytest.raises(ValueError, match="screening source changed"):
        latest_report(service.store, signal.snapshot_id, "paper", service.config)
    assert service.store.run(report["run_id"]) == original_report
    assert len(service.store.runs("portfolio_report")) == 1
