from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from finsight.service import FinSight

APP = Path(__file__).resolve().parents[2] / "src/finsight/dashboard.py"


def test_four_pages_and_interactive_offline_research(tmp_path, monkeypatch):
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(tmp_path))
    service = FinSight(tmp_path)
    service.sync("demo", date(2025, 2, 28), date(2024, 1, 2))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert [t.label for t in app.tabs] == ["数据状态", "候选排行榜", "研究卡", "组合与绩效"]
    next(b for b in app.button if b.label == "运行选股").click().run()
    assert not app.exception and len(service.store.runs("screen")) == 1
    next(s for s in app.selectbox if s.label == "研究模型来源").select("demo").run()
    next(b for b in app.button if b.label == "生成研究卡").click().run()
    assert not app.exception and len(service.store.runs("research")) == 1
    next(b for b in app.button if b.label == "生成持仓与周度检查报告").click().run()
    assert not app.exception and len(service.store.runs("portfolio_report")) == 1


def test_empty_workspace_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert any("先创建" in info.value for info in app.info)


def test_create_demo_switches_to_new_snapshot_even_when_older_date(tmp_path, monkeypatch):
    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(tmp_path))
    FinSight(tmp_path).sync("demo", date(2026, 2, 27), date(2025, 1, 2))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    next(b for b in app.button if b.label == "创建离线演示数据").click().run()
    assert not app.exception
    assert app.metric[0].value == "2025-12-31"


def test_legacy_quality_report_is_rechecked_before_displaying_publishable(tmp_path, monkeypatch):
    import json

    from finsight.data.demo import make_demo
    from finsight.data.quality import audit

    monkeypatch.setenv("FINSIGHT_DATA_DIR", str(tmp_path))
    service = FinSight(tmp_path)
    bundle = make_demo(date(2025, 2, 28), date(2024, 1, 2))
    previous_quality = audit(bundle)
    bundle.meta.synthetic = False
    bundle.tables["universe"]["known_on"] = "2025-03-03"
    service.store.save_snapshot(bundle)
    quality_file = tmp_path / "snapshots" / bundle.meta.snapshot_id / "quality.json"
    quality_file.write_text(json.dumps(previous_quality), encoding="utf-8")
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert app.metric[3].value == "阻止发布"
    assert next(button for button in app.button if button.label == "运行选股").disabled
