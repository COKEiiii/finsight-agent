import json
from datetime import date

from finsight.cli import main
from finsight.service import FinSight


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
