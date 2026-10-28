"""Versioned offline revalidation; never rewrite the original measured benchmark."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

from finsight.data.store import Store, code_version
from finsight.evaluation_jobs import _engine_id, _load
from finsight.models import ResearchCard, ScreeningResult, StrategyConfig, fingerprint
from finsight.research import finalize_content
from finsight.research_facts import facts_for, hypothetical

RUBRIC_VERSION = "offline_review_v0.3_risk_domains"


def scenario_check(scenario, content, facts):
    gaps_and_risks = content.information_gaps + [claim.text for claim in content.risks]
    if scenario == "missing_roe":
        return any(re.search(r"ROE|净资产收益率", text, re.I) and
            re.search(r"未提供|缺少|缺失|缺乏", text) and "无法判断" in text for text in gaps_and_risks)
    if scenario == "stale_report":
        return any(re.search(r"年报|财报|报告期", text) and re.search(r"陈旧|过期|过时|时效.{0,8}(?:不足|风险|无法判断)", text) and
            not re.search(r"不陈旧|未过期|不再过时|没有过时", text) for text in gaps_and_risks)
    if scenario == "negative_growth":
        pattern = re.compile(r"负增长|(?:营收|营业收入|收入).{0,8}(?:下滑|下降|负值)")
        return any("financials.revenue_growth" in claim.evidence_ids and any(
            not hypothetical(claim.text, match) for match in pattern.finditer(claim.text)) for claim in content.risks)
    if scenario == "unit_conversion":
        fact = facts.get("market.amount_20", {})
        return fact.get("unit") == "元/日" and fact.get("value") is not None
    return scenario == "actual_candidate"


def audit_suite(store: Store, suite_id: str) -> dict:
    folder, state, cases = _load(store, suite_id)
    if state["status"] != "completed":
        raise ValueError("offline audit requires a completed evaluation suite")
    source = store.run(state["source_run_id"])
    result = ScreeningResult.model_validate(source["payload"])
    result = result.model_copy(update={"schema_version": max(2, result.schema_version)})
    case_map = {case["case_id"]: case for case in cases[:state["limit"]]}
    rows, reports = [], {}
    for model in state["models"]:
        originals = state["results"][model]
        ids = [row["case_id"] for row in originals]
        if len(set(ids)) != len(ids) or set(ids) != set(case_map):
            raise ValueError("evaluation cases are duplicated or incomplete")
        model_rows = []
        for original in originals:
            case = case_map[original["case_id"]]
            facts = facts_for(result, case["candidate"])
            row = {"model": model, "case_id": case["case_id"], "scenario": case["scenario"],
                "synthetic_modification": case["synthetic_modification"], "original_status": original["status"],
                "original_scenario_passed": original["passed"], "saved_response_valid_now": False,
                "scenario_passed_now": False, "recoverable_saved_response": False, "mode_if_valid": None,
                "issue": None, "human_review": "pending"}
            try:
                if original["status"] not in {"completed", "failed"} or not original.get("card"):
                    raise ValueError("no comparable completed/failed response; usage or model identity may be uncertain")
                card = ResearchCard.model_validate(original["card"])
                if card.run_id != result.run_id or card.model != model or card.symbol != case["symbol"]:
                    raise ValueError("saved card is bound to a different signal, model or symbol")
                for table in (card.facts, card.evidence):
                    if set(table) != set(facts):
                        raise ValueError("saved field inventory differs from frozen case")
                    for field, expected in facts.items():
                        observed = table[field]
                        if any(observed.get(key) != value for key, value in expected.items()):
                            raise ValueError(f"saved fact does not match frozen case: {field}")
                if card.content_template is None:
                    raise ValueError("original model template unavailable; do not invent a replacement")
                rendered = finalize_content(card.content_template.model_dump(), facts, result.as_of)
                assisted = "模板辅助修正" in rendered.summary
                row.update(saved_response_valid_now=True, scenario_passed_now=bool(scenario_check(case["scenario"], rendered, facts)),
                    recoverable_saved_response=original["status"] == "failed", mode_if_valid="assisted" if assisted else "freeform")
                if not row["scenario_passed_now"]:
                    row["issue"] = {"missing_roe": "missing_metric_not_explicitly_disclosed",
                        "stale_report": "stale_report_warning_missing", "negative_growth": "observed_decline_missing_from_risks",
                        "unit_conversion": "field_or_unit_mismatch"}.get(case["scenario"], "scenario_not_satisfied")
            except (ValueError, TypeError, KeyError) as exc:
                row["issue"] = f"validation: {str(exc)[:500]}"
            model_rows.append(row)
        total = len(model_rows)
        reports[model] = {"evaluated": total, "original_completed": sum(row["original_status"] == "completed" for row in model_rows),
            "original_scenario_passed": sum(row["original_scenario_passed"] for row in model_rows),
            "saved_responses_valid_now": sum(row["saved_response_valid_now"] for row in model_rows),
            "recoverable_saved_responses": sum(row["recoverable_saved_response"] for row in model_rows),
            "scenario_passed_now": sum(row["scenario_passed_now"] for row in model_rows),
            "remaining_issues": [row["case_id"] for row in model_rows if not row["scenario_passed_now"]],
            "human_quality_passed": False}
        rows.extend(model_rows)
    payload = {"suite_id": suite_id, "source_run_id": result.run_id, "source_snapshot_id": result.snapshot_id,
        "rubric_version": RUBRIC_VERSION, "rubric_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_checkpoint_fingerprint": fingerprint(state), "original_engine_id": state["engine_id"],
        "current_engine_id": _engine_id(), "code_version": code_version(),
        "audited_at": datetime.now(timezone.utc).isoformat(), "models": reports, "results": rows,
        "model_calls": 0, "quality_gate_passed": False,
        "limitations": "离线复核原响应，不重计原生成成功率，不代表新模型实验或人工事实验收。原评估及人工表保持不变。"}
    config = StrategyConfig.model_validate(source["config"])
    run_id = "audit-" + uuid4().hex
    store.save_run("evaluation_audit", result.snapshot_id, config, payload, run_id)
    out = store.artifact_dir(run_id)
    payload.update(run_id=run_id, exports=str(out))
    (out / "audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows).to_csv(out / "case_review.csv", index=False, encoding="utf-8-sig")
    lines = ["# FinSight 已保存响应离线复核", f"原套件：{suite_id} · 复核规则：{RUBRIC_VERSION}", payload["limitations"],
        "", "| 模型 | 原生成完成 | 当前规则可验证 | 其中原失败响应可验证 | 原场景通过 | 复核场景通过 |",
        "|---|---:|---:|---:|---:|---:|"]
    for model, report in reports.items():
        n = report["evaluated"]
        lines.append(f"| {model} | {report['original_completed']}/{n} | {report['saved_responses_valid_now']}/{n} | "
            f"{report['recoverable_saved_responses']} | {report['original_scenario_passed']}/{n} | {report['scenario_passed_now']}/{n} |")
    lines += ["", "## 待改进案例"]
    for row in rows:
        if row["issue"]:
            lines.append(f"- {row['model']} · {row['case_id']} · {row['issue']}")
    lines += ["", "原文件与原分数未覆盖；本次零模型调用。仍需人工核实事实、证据完整性及阅读效率。"]
    (out / "audit.md").write_text("\n".join(lines), encoding="utf-8")
    return {key: value for key, value in payload.items() if key != "results"}
