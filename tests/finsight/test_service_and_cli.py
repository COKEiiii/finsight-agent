import json
from datetime import date

import pytest

from finsight.cli import main
from finsight.models import StrategyConfig
from finsight.service import FinSight


@pytest.mark.parametrize("changed_config", [False, True])
def test_historical_screen_audits_full_snapshot_without_mutating_cutoff(tmp_path, bundle, changed_config):
    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    if changed_config:
        service.config = StrategyConfig(version="historical-audit-regression", min_amount_20=60_000_000)
    result = service.screen(bundle.meta.snapshot_id, date(2025, 2, 26))
    assert result.publication_status == "published" and len(result.candidates) == 10
    assert result.as_of == date(2025, 2, 26) and not result.research_eligible
    saved = service.store.load_snapshot(bundle.meta.snapshot_id)
    assert saved.meta.as_of == date(2025, 2, 28) and saved.meta.publishable
    assert saved["prices"]["trade_date"].max() == "2025-02-28"


def test_historical_screen_cannot_backdate_current_stock_states(tmp_path, bundle):
    bundle.meta.schema_version = 2
    bundle.meta.membership_symbols = sorted(bundle["universe"]["symbol"])
    bundle.tables["universe"]["known_on"] = "2025-02-28"
    service = FinSight(tmp_path)
    service._save_bundle(bundle)
    with pytest.raises(ValueError, match="coverage below"):
        service.screen(bundle.meta.snapshot_id, date(2025, 2, 26))
    preview = service.screen(bundle.meta.snapshot_id, date(2025, 2, 26), preview=True)
    assert preview.publication_status == "preview" and preview.candidates == []
    assert not preview.research_eligible


def test_end_to_end_offline_workflow(tmp_path):
    service = FinSight(tmp_path)
    quality = service.sync("demo", date(2025, 2, 28), date(2024, 1, 2))
    result = service.screen(quality["snapshot_id"])
    cards = service.research(result.run_id, "demo")
    report = service.backtest(quality["snapshot_id"], date(2025, 1, 1), date(2025, 2, 28))
    assert len(result.candidates) == len(cards) == 10
    assert all(card.status == "demo" for card in cards)
    assert not report["research_eligible"]
    folder = service.store.artifact_dir(result.run_id)
    assert all((folder / file).exists() for file in ["screening.json", "candidates.csv", "portfolio_plan.json", "journal_template.csv"])
    bt_folder = service.store.artifact_dir(report["run_id"])
    assert (bt_folder / "equity_curve.csv").exists()
    assert (bt_folder / "backtest.md").exists()


def test_cli_returns_parseable_json_and_useful_errors(tmp_path, capsys, monkeypatch):
    root = ["--data-dir", str(tmp_path)]
    assert main(root + ["sync", "--provider", "demo", "--as-of", "2025-02-28", "--start", "2024-01-02"]) == 0
    quality = json.loads(capsys.readouterr().out)
    assert quality["synthetic"]
    assert main(root + ["screen"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["candidates"]) == 10
    assert main(root + ["research", "--run", result["run_id"], "--provider", "demo", "--limit", "1"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    assert main(root + ["screen", "--as-of", "2026-01-01"]) == 2
    assert "cutoff" in capsys.readouterr().err
    from finsight.data.requests import SourceUnavailable

    def failed_sync(*args, **kwargs):
        raise SourceUnavailable("source unavailable; checkpoints preserved")

    monkeypatch.setattr(FinSight, "sync", failed_sync)
    assert main(root + ["sync", "--provider", "akshare"]) == 2
    assert "checkpoints preserved" in capsys.readouterr().err


def test_usage_counts_failures_but_not_cached_generation_twice(tmp_path, capsys):
    from finsight.research import demo_content, evidence_for

    service = FinSight(tmp_path)
    snapshot = service.sync("demo", date(2025, 2, 28), date(2024, 1, 2))["snapshot_id"]
    result = service.screen(snapshot)
    value = demo_content(result.candidates[0], evidence_for(result, result.candidates[0])).model_dump()
    class Adapter:
        provider = "deepseek"
        base_url = "http://fixture"
        model = "deepseek-fixture"
        calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return {"message": {"content": json.dumps(value)}, "input_tokens": 13, "output_tokens": 7}
    adapter = Adapter()
    service.research(result.run_id, "deepseek", limit=1, adapter=adapter)
    cached = service.research(result.run_id, "deepseek", limit=1, adapter=adapter)
    assert adapter.calls == 1 and cached[0].cache_hit
    value["reasons"][0] = {"text": "估值合理。", "evidence_ids": ["valuation.pe_ttm"]}
    adapter.model = "deepseek-failing"
    failed = service.research(result.run_id, "deepseek", limit=1, adapter=adapter)
    assert failed[0].status == "failed"
    usage = service.research_usage()
    assert usage["reported_tokens"] == 80 and usage["llm_calls"] == 4 and usage["cache_hits"] == 1
    assert sum(row["failed_cards"] for row in usage["by_model"]) == 1
    assert main(["--data-dir", str(tmp_path), "usage", "--model", "deepseek-fixture"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["reported_tokens"] == 20 and output["cache_hits"] == 1
