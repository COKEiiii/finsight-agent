"""Durable, sequential local evaluation. No trading or paid provider calls."""
from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from finsight.data.store import Store, code_version, safe_id
from finsight.evaluation import CASE_VERSION, _scenario_pass, build_cases
from finsight.models import ScreeningResult, fingerprint
from finsight.research import FIELD_PROMPT_VERSION, ChatAdapter, research_candidate

MODELS = {"qwen3.5:4b", "qwen3:8b"}
PROTOCOL = "local_evaluation_v1"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _engine_id():
    root = Path(__file__).parent
    names = ("evaluation.py", "evaluation_jobs.py", "research.py", "research_facts.py", "models.py")
    return hashlib.sha256(b"".join(name.encode() + (root / name).read_bytes() for name in names)).hexdigest()


def _atomic(path: Path, text: str):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path, value):
    _atomic(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _validate_models(models):
    if len(set(models)) != len(models) or any(model not in MODELS for model in models):
        raise ValueError("evaluation models must be distinct qwen3.5:4b / qwen3:8b names")


def _model_identities(models):
    endpoint = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get(endpoint + "/api/tags")
            response.raise_for_status()
            installed = {row.get("name", row.get("model")): row for row in response.json()["models"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("cannot inspect installed local Ollama models; no evaluation requests started") from exc
    identities = {}
    for model in models:
        digest = installed.get(model, {}).get("digest")
        if not isinstance(digest, str) or not digest:
            raise ValueError(f"local evaluation model not installed: {model}")
        identities[model] = {"endpoint": endpoint, "digest": digest}
    return identities


def _summary(state):
    reports = {}
    for model in state["models"]:
        rows = state["results"].get(model, [])
        completed = [row for row in rows if row["status"] == "completed"]
        fresh = [row for row in rows if not row.get("cache_hit") and row["status"] != "interrupted"]
        assisted = sum(row.get("mode") == "assisted" for row in completed)
        passed = sum(row["passed"] for row in rows)
        reports[model] = {"evaluated": len(rows), "planned": state["limit"], "completed": len(completed),
            "passed": passed, "pass_rate": passed / len(rows) if rows else None,
            "structured_pass_rate": len(completed) / len(rows) if rows else None,
            "freeform_completed": len(completed) - assisted, "assisted_completed": assisted,
            "fresh_completed": sum(row["status"] == "completed" for row in fresh),
            "fresh_cases": len(fresh),
            "fresh_freeform_completed": sum(row.get("mode") == "freeform" for row in fresh),
            "fresh_assisted_completed": sum(row.get("mode") == "assisted" for row in fresh),
            "cache_hits": sum(row.get("cache_hit", False) for row in rows),
            "interrupted_cases": sum(row["status"] == "interrupted" for row in rows),
            "elapsed_seconds": sum(row.get("elapsed_seconds") or 0 for row in fresh),
            "llm_calls": sum(row.get("llm_calls") or 0 for row in rows),
            "input_tokens": sum(row.get("input_tokens") or 0 for row in rows),
            "output_tokens": sum(row.get("output_tokens") or 0 for row in rows),
            "serious_factual_errors": "pending human review", "manual_revision_count": "pending human review"}
        reports[model]["automated_gate_passed"] = (len(rows) == 30 and not reports[model]["interrupted_cases"] and
            reports[model]["structured_pass_rate"] >= .95 and reports[model]["pass_rate"] >= .95)
    return {key: state[key] for key in ("suite_id", "source_run_id", "case_count", "case_version", "actual_candidates",
        "synthetic_scenarios", "status", "pid", "created_at", "updated_at", "in_flight", "error", "engine_id", "prompt_version")} | {
        "models": reports, "model_identities": state["model_identities"], "exports": state["exports"],
        "quality_gate_passed": False, "human_review": "pending; review_template.csv requires factual and effort review",
        "usage_scope": "Reported completed/failed case usage only. Interrupted in-flight usage is unknown. Cache generation usage is excluded.",
        "timing_scope": "Fresh-card time only; cached cards are excluded. Freeform/assisted are completion modes, cache is a separate dimension."}


def _save(folder, state):
    state["updated_at"] = _now()
    # The authoritative checkpoint is committed before derived exports.
    _json(folder / "state.json", state)
    summary = _summary(state)
    _json(folder / "summary.json", summary)
    for model, rows in state["results"].items():
        _json(folder / f"{model.replace(':', '_')}.json", {**summary["models"][model], "results": rows})
    review = folder / "review_template.csv"
    if state["models"] and not review.exists():
        stream = io.StringIO()
        fields = ("model", "case_id", "synthetic_modification", "reviewer", "serious_factual_errors",
                  "evidence_complete", "manual_revision_count", "review_seconds", "notes")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        cases = json.loads((folder / "cases.json").read_text())
        writer.writerows({"model": model, "case_id": case["case_id"], "synthetic_modification": case["synthetic_modification"]}
                         for model in state["models"] for case in cases[:state["limit"]])
        _atomic(review, "\ufeff" + stream.getvalue())
    lines = ["# FinSight 本地模型评估", f"评估：{state['suite_id']} · 状态：{state['status']}",
        "10 个真实候选，20 个明确标注的变造案例；不代表历史策略验证。",
        "", "| 模型 | 已评估 | 结构化完成 | 场景通过 | 自由生成完成 | 模板辅助完成 | 缓存 | 中断未测 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for model, report in summary["models"].items():
        lines.append(f"| {model} | {report['evaluated']}/{report['planned']} | {report['completed']} | {report['passed']} | "
            f"{report['freeform_completed']} | {report['assisted_completed']} | {report['cache_hits']} | {report['interrupted_cases']} |")
    lines += ["", "自由生成／模板辅助区分内容模式，缓存另列；耗时仅统计新生成。",
        "人工审阅尚未完成，不宣称质量达标。review_template.csv 记录事实错误、证据完整性、修订量和阅读用时。",
        "中断时已经开始但未落盘的单案例不自动重试，以免超出其请求预算；其用量未知，其余案例可恢复。"]
    _atomic(folder / "evaluation.md", "\n".join(lines))


def _load(store, suite_id):
    folder = store.root / "exports" / safe_id(suite_id)
    try:
        state = json.loads((folder / "state.json").read_text())
        cases = json.loads((folder / "cases.json").read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("evaluation checkpoint missing/corrupt; legacy prepared cases require a new suite") from exc
    if state.get("protocol") != PROTOCOL or state.get("suite_id") != suite_id or fingerprint(cases) != state.get("cases_fingerprint"):
        raise ValueError("evaluation cases/checkpoint identity mismatch")
    if fingerprint(store.run(state["source_run_id"])["payload"]) != state["source_fingerprint"]:
        raise ValueError("evaluation source screening changed")
    return folder, state, cases


def prepare_suite(store: Store, run_id: str, *, models=(), limit=30, allow_demo=False):
    _validate_models(models)
    if not 1 <= limit <= 30:
        raise ValueError("evaluation limit must be 1..30")
    record = store.run(run_id)
    if record["kind"] != "screen":
        raise ValueError("evaluation requires a screening run")
    result = ScreeningResult.model_validate(record["payload"])
    cases = build_cases(result, allow_demo=allow_demo)
    suite_id = "evaluation-" + uuid4().hex
    folder = store.artifact_dir(suite_id)
    _json(folder / "cases.json", cases)
    state = {"protocol": PROTOCOL, "suite_id": suite_id, "source_run_id": run_id,
        "source_fingerprint": fingerprint(record["payload"]), "cases_fingerprint": fingerprint(cases),
        "case_count": 30, "case_version": CASE_VERSION, "actual_candidates": 10, "synthetic_scenarios": 20,
        "models": list(models), "limit": limit, "results": {model: [] for model in models},
        "model_identities": {}, "engine_id": _engine_id(), "prompt_version": FIELD_PROMPT_VERSION,
        "created_at": _now(), "updated_at": _now(), "status": "prepared", "pid": None,
        "in_flight": None, "error": None, "exports": str(folder), "attempts": [], "allow_demo": allow_demo}
    _save(folder, state)
    return _summary(state)


def suite_status(store: Store, suite_id: str):
    folder, state, _ = _load(store, suite_id)
    if state["status"] == "running":
        alive = False
        try:
            os.kill(state["pid"], 0)
            with (folder / "run.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock, fcntl.LOCK_UN)
                except BlockingIOError:
                    alive = True
        except (OSError, TypeError):
            pass
        if not alive:
            state["status"] = "interrupted"
            state["error"] = "no live evaluator owns the recorded process and lock; resume this suite"
    return _summary(state)


def run_suite(store: Store, suite_id: str, *, models=()):
    _validate_models(models)
    folder, _, _ = _load(store, suite_id)
    with (folder / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("evaluation already running; inspect the existing suite") from exc
        folder, state, cases = _load(store, suite_id)
        if models and state["models"] and state["models"] != list(models):
            raise ValueError("resume must use the suite's frozen model order")
        if state["status"] == "completed":
            return _summary(state)
        if state["engine_id"] != _engine_id() or state["prompt_version"] != FIELD_PROMPT_VERSION:
            raise ValueError("evaluation engine/prompt changed; start a new suite to avoid mixing versions")
        if models:
            state["models"] = list(models)
            state["results"] = {model: state["results"].get(model, []) for model in models}
        if not state["models"]:
            raise ValueError("choose local models when resuming a prepared suite")
        identities = _model_identities(state["models"])
        if state["model_identities"] and identities != state["model_identities"]:
            raise ValueError("local model digest or endpoint changed; start a new evaluation suite")
        state["model_identities"] = identities
        # A killed process may have spent up to three requests on this case.
        # Preserve the uncertainty instead of silently resetting its budget.
        if state["in_flight"]:
            pending = state["in_flight"]
            state["results"][pending["model"]].append({**pending, "status": "interrupted", "passed": False,
                "error": "in-flight case interrupted; request usage unknown; not automatically retried"})
            state["in_flight"] = None
        state.update(status="running", pid=os.getpid(), error=None)
        state["attempts"].append({"started_at": _now(), "pid": os.getpid(), "code_version": code_version()})
        _save(folder, state)
        result = ScreeningResult.model_validate(store.run(state["source_run_id"])["payload"])
        result = result.model_copy(update={"schema_version": max(2, result.schema_version)})
        try:
            for model in state["models"]:
                adapter = ChatAdapter("ollama", model, base_url=identities[model]["endpoint"])
                consecutive_provider_failures = 0
                try:
                    done = {row["case_id"] for row in state["results"][model]}
                    for case in cases[:state["limit"]]:
                        if case["case_id"] in done:
                            continue
                        if _engine_id() != state["engine_id"]:
                            raise ValueError("evaluation implementation changed during execution")
                        if _model_identities(state["models"]) != identities:
                            raise ValueError("local model identity changed during evaluation")
                        state["in_flight"] = {"model": model, "case_id": case["case_id"], "scenario": case["scenario"],
                            "synthetic_modification": case["synthetic_modification"], "started_at": _now()}
                        _save(folder, state)
                        scope = fingerprint({"suite": suite_id, "engine": state["engine_id"], "model": identities[model]})
                        card = research_candidate(result, case["candidate"], store, adapter, cache_scope="evaluation:" + scope)
                        identity_changed = _model_identities(state["models"]) != identities
                        for table in (card.facts, card.evidence):
                            for fact in table.values():
                                fact["evaluation_only"] = True
                                fact["synthetic_modification"] = case["synthetic_modification"]
                        mode = ("assisted" if "模板辅助修正" in card.content.summary else "freeform") if card.content else "failed"
                        passed = _scenario_pass(case["scenario"], card)
                        if case["scenario"] == "unit_conversion":
                            fact = card.facts.get("market.amount_20", {})
                            passed = passed and fact.get("value") == case["candidate"]["amount_20"] and fact.get("unit") == "元/日"
                        row = {"case_id": case["case_id"], "scenario": case["scenario"], "symbol": case["symbol"],
                            "synthetic_modification": case["synthetic_modification"], "passed": passed,
                            "status": card.status, "mode": mode, "cache_hit": card.cache_hit,
                            "error": card.error, "elapsed_seconds": card.elapsed_seconds, "llm_calls": card.llm_calls,
                            "tool_calls": card.tool_calls, "input_tokens": card.input_tokens, "output_tokens": card.output_tokens,
                            "manual_review": "pending", "card": card.model_dump(mode="json")}
                        if identity_changed:
                            row.update(status="identity_changed", passed=False, mode="invalid", error="model identity changed during this case")
                        state["results"][model].append(row)
                        state["in_flight"] = None
                        _save(folder, state)
                        if identity_changed:
                            raise ValueError("local model identity changed during evaluation")
                        provider_failed = card.status == "failed" and (card.error or "").startswith("ollama:")
                        consecutive_provider_failures = consecutive_provider_failures + 1 if provider_failed else 0
                        if consecutive_provider_failures >= 3:
                            raise ValueError("persistent local provider failures; completed cases retained, repair Ollama before resuming")
                finally:
                    adapter.close()
            state.update(status="completed", pid=None)
            _save(folder, state)
        except BaseException as exc:
            detail = str(exc)[:400] if isinstance(exc, ValueError) else type(exc).__name__
            state.update(status="interrupted", pid=None, error=f"evaluation interrupted: {detail}")
            _save(folder, state)
            raise
        return _summary(state)
