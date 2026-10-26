"""Fixed, reproducible 30-case local research evaluation; human review stays explicit."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from finsight.data.store import Store
from finsight.models import ScreeningResult
from finsight.research import ChatAdapter, research_candidate

SCENARIOS = ("actual_candidate", "missing_roe", "stale_report", "negative_growth", "unit_conversion")


def build_cases(result: ScreeningResult, *, allow_demo: bool = False) -> list[dict]:
    if result.synthetic and not allow_demo:
        raise ValueError("30-case model evaluation requires real candidates; use allow_demo only for tests")
    if len(result.candidates) < 10:
        raise ValueError("evaluation needs ten screened candidates")
    cases = []
    for index, original in enumerate(result.candidates[:10]):
        for scenario in SCENARIOS:
            if scenario != "actual_candidate" and index >= 5:
                continue
            candidate = deepcopy(original)
            if scenario == "missing_roe":
                candidate.pop("roe", None)
            elif scenario == "stale_report":
                candidate["report_period"] = str(result.as_of - timedelta(days=730))
            elif scenario == "negative_growth":
                candidate["revenue_growth"] = -abs(float(candidate["revenue_growth"])) - 1
            elif scenario == "unit_conversion":
                candidate["amount_20"] = float(candidate["amount_20"]) * 10000
            cases.append({"case_id": f"{scenario}-{index + 1:02d}", "scenario": scenario,
                          "symbol": candidate["symbol"], "candidate": candidate,
                          "source_run_id": result.run_id, "source_snapshot_id": result.snapshot_id,
                          "synthetic_modification": scenario != "actual_candidate"})
    if len(cases) != 30:
        raise AssertionError("evaluation suite must contain exactly 30 cases")
    return cases


def _scenario_pass(scenario: str, card) -> bool:
    if card.status != "completed" or card.content is None:
        return False
    if scenario == "missing_roe":
        text = " ".join(card.content.information_gaps + [item.text for item in card.content.risks])
        return "ROE" in text.upper() and "无法判断" in text
    if scenario == "stale_report":
        text = " ".join(card.content.information_gaps + [item.text for item in card.content.risks])
        return any(word in text for word in ("陈旧", "过期", "时效", "核对", "核实"))
    if scenario == "negative_growth":
        text = " ".join(item.text for item in card.content.risks)
        return any(word in text for word in ("营收", "收入")) and any(word in text for word in ("下降", "负增长", "下滑"))
    return True


def evaluate_run(store: Store, run_id: str, *, models: tuple[str, ...] = (),
                 limit: int = 30, allow_demo: bool = False) -> dict:
    record = store.run(run_id)
    if record["kind"] != "screen":
        raise ValueError("evaluation requires a screening run")
    result = ScreeningResult.model_validate(record["payload"])
    if result.schema_version < 2:
        result = result.model_copy(update={"schema_version": 2})
    cases = build_cases(result, allow_demo=allow_demo)
    if not 1 <= limit <= 30:
        raise ValueError("evaluation limit must be 1..30")
    suite_id = "evaluation-" + uuid4().hex
    folder = store.artifact_dir(suite_id)
    (folder / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    output = {"suite_id": suite_id, "source_run_id": run_id, "case_count": 30,
              "actual_candidates": sum(c["scenario"] == "actual_candidate" for c in cases),
              "synthetic_scenarios": 20, "models": {}, "exports": str(folder),
              "human_review": "pending; serious factual errors and manual revision effort require user audit"}
    for model in models:
        if model not in {"qwen3.5:4b", "qwen3:8b"}:
            raise ValueError("evaluation model must be qwen3.5:4b or qwen3:8b")
        adapter = ChatAdapter("ollama", model)
        rows = []
        try:
            for case in cases[:limit]:
                card = research_candidate(result, case["candidate"], store, adapter)
                rows.append({"case_id": case["case_id"], "scenario": case["scenario"],
                    "symbol": case["symbol"], "passed": _scenario_pass(case["scenario"], card),
                    "status": card.status, "error": card.error, "elapsed_seconds": card.elapsed_seconds,
                    "llm_calls": card.llm_calls, "tool_calls": card.tool_calls,
                    "input_tokens": card.input_tokens, "output_tokens": card.output_tokens,
                    "manual_review": "pending", "card": card.model_dump(mode="json")})
        finally:
            adapter.close()
        output["models"][model] = {"evaluated": len(rows), "passed": sum(row["passed"] for row in rows),
            "pass_rate": sum(row["passed"] for row in rows) / len(rows),
            "elapsed_seconds": sum(row["elapsed_seconds"] for row in rows),
            "llm_calls": sum(row["llm_calls"] for row in rows),
            "serious_factual_errors": "pending human review",
            "manual_revision_count": "pending human review", "results": rows}
        (folder / f"{model.replace(':', '_')}.json").write_text(json.dumps(output["models"][model], ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "summary.json").write_text(json.dumps({**output, "models": {
        name: {key: value for key, value in report.items() if key != "results"} for name, report in output["models"].items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return {**output, "models": {name: {key: value for key, value in report.items() if key != "results"}
                                 for name, report in output["models"].items()}}
