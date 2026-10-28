"""Human review of frozen responses; no inference and no baseline rewriting."""
from __future__ import annotations

import csv
import io
import json
import math
from datetime import datetime, timezone
from uuid import uuid4

from finsight.data.store import Store
from finsight.evaluation_jobs import _atomic, _load, _summary
from finsight.models import ResearchCard, StrategyConfig, fingerprint
from finsight.research import render_card

VERSION = "human_review_v1"
IDENTITY = ("suite_id", "checkpoint_fingerprint", "engine_id", "model", "case_id", "response_fingerprint",
            "synthetic_modification")
RATINGS = ("reviewer", "serious_factual_errors", "evidence_complete", "manual_revision_count", "review_seconds", "notes")
FIELDS = IDENTITY + RATINGS


def _context(store, suite_id):
    folder, state, cases = _load(store, suite_id)
    if state["status"] != "completed" or not state["models"]:
        raise ValueError("human review requires a completed evaluation suite with model responses")
    case_map = {case["case_id"]: case for case in cases[:state["limit"]]}
    responses, identities = {}, {}
    checkpoint = fingerprint(state)
    for model in state["models"]:
        rows = state["results"][model]
        if len(rows) != len(case_map) or {row["case_id"] for row in rows} != set(case_map):
            raise ValueError("evaluation responses are duplicated or incomplete")
        for row in rows:
            case = case_map[row["case_id"]]
            card = row.get("card")
            if card and (card.get("run_id") != state["source_run_id"] or card.get("model") != model or
                         card.get("symbol") != case["symbol"]):
                raise ValueError("response is bound to a different screening run, model or symbol")
            key = (model, row["case_id"])
            responses[key] = row
            identities[key] = dict(zip(IDENTITY, (suite_id, checkpoint, state["engine_id"], model, row["case_id"],
                fingerprint(row), str(case["synthetic_modification"]).lower())))
    return state, responses, identities


def _csv(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return "\ufeff" + stream.getvalue()


def review_packet(store: Store, suite_id: str) -> dict:
    state, responses, identities = _context(store, suite_id)
    lines = ["# FinSight 研究卡人工审阅包", f"套件：{suite_id}",
        f"提示词：{state['prompt_version']} · 引擎：{state['engine_id']}",
        "原生成响应与原分数保持不变；变造案例只用于评估。请对照每张卡的字段事实和证据审阅。",
        "CSV 中身份列保持原样。逐行填写审阅人、严重事实错误数量、证据完整 true/false、修订次数和审阅秒数。",
        "严重事实错误包括数字、指标、单位或日期错配，以及缺乏证据的确定性断言；核对缺失财务字段、已发生的负增长和陈旧年报风险是否明确披露。",
        "有错误或证据不全时必须写明问题；未审阅行保持空白。未成功生成的案例也需注明原因，不能标记证据完整。",
        "修订次数记录实际修改量，不改变原卡；审阅耗时是人工耗时，与生成耗时分开统计。", ""]
    for key, row in responses.items():
        modified = identities[key]["synthetic_modification"] == "true"
        lines += [f"## {key[0]} · {key[1]}", f"变造输入：{'是，仅供评估' if modified else '否，真实候选'} · 原状态：{row['status']}"]
        if row.get("card"):
            lines.append(render_card(ResearchCard.model_validate(row["card"])))
        else:
            lines.append("无可审阅的研究卡；请记录评估中断或生成缺失。")
        lines += ["", "---", ""]
    return {"suite_id": suite_id, "checkpoint_fingerprint": fingerprint(state),
        "template_csv": _csv(identities.values()), "cards_markdown": "\n".join(lines), "model_calls": 0}


def export_packet(store: Store, suite_id: str) -> dict:
    packet = review_packet(store, suite_id)
    folder = store.artifact_dir("review-packet-" + uuid4().hex)
    _atomic(folder / "review.csv", packet["template_csv"])
    _atomic(folder / "cards.md", packet["cards_markdown"])
    return {key: packet[key] for key in ("suite_id", "checkpoint_fingerprint", "model_calls")} | {
        "exports": str(folder), "review_csv": str(folder / "review.csv"), "cards_markdown": str(folder / "cards.md")}


def _integer(value, field):
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"{field} must be a nonnegative integer")
    return int(value)


def _parse(text, identities, responses):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
    if reader.fieldnames is None or len(reader.fieldnames) != len(FIELDS) or set(reader.fieldnames) != set(FIELDS):
        raise ValueError("use the bound review CSV exported by evaluate --review-template; columns do not match")
    try:
        imported = list(reader)
    except csv.Error as exc:
        raise ValueError("malformed review CSV") from exc
    reviews, seen = [], set()
    for line, raw in enumerate(imported, 2):
        if None in raw or any(value is None for value in raw.values()):
            raise ValueError(f"review line {line}: malformed CSV row")
        row = {key: value.strip() for key, value in raw.items()}
        key = (row["model"], row["case_id"])
        if key not in identities or key in seen:
            raise ValueError(f"review line {line}: unknown or duplicate model/case")
        seen.add(key)
        if any(row[field] != identities[key][field] for field in IDENTITY):
            raise ValueError(f"review line {line}: suite/checkpoint/response binding changed")
        if not any(row[field] for field in RATINGS):
            continue
        if not all(row[field] for field in RATINGS[:-1]):
            raise ValueError(f"review line {line}: partially filled rating; complete this row or leave it blank")
        if row["evidence_complete"].lower() not in {"true", "false"}:
            raise ValueError(f"review line {line}: evidence_complete must be true or false")
        errors = _integer(row["serious_factual_errors"], "serious_factual_errors")
        revisions = _integer(row["manual_revision_count"], "manual_revision_count")
        try:
            seconds = float(row["review_seconds"])
        except ValueError as exc:
            raise ValueError(f"review line {line}: review_seconds must be positive and finite") from exc
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f"review line {line}: review_seconds must be positive and finite")
        complete = row["evidence_complete"].lower() == "true"
        response = responses[key]
        if complete and (response["status"] != "completed" or not (response.get("card") or {}).get("content")):
            raise ValueError(f"review line {line}: a missing/failed response cannot have complete evidence")
        if (errors or not complete) and not row["notes"]:
            raise ValueError(f"review line {line}: explain factual errors or incomplete evidence in notes")
        reviews.append({**identities[key], "reviewer": row["reviewer"], "serious_factual_errors": errors,
            "evidence_complete": complete, "manual_revision_count": revisions, "review_seconds": seconds, "notes": row["notes"]})
    if not reviews:
        raise ValueError("no completed human ratings to import")
    return reviews


def _report(state, responses, reviews, *, source_synthetic=True):
    automated = _summary(state)["models"]
    reports = {}
    for model in state["models"]:
        rows = [row for row in reviews if row["model"] == model]
        count = sum(key[0] == model for key in responses)
        errors = sum(row["serious_factual_errors"] for row in rows)
        incomplete = sum(not row["evidence_complete"] and responses[(model, row["case_id"])]["status"] == "completed" for row in rows)
        reviewed_unavailable = sum(responses[(model, row["case_id"])]["status"] != "completed" for row in rows)
        unavailable = sum(row["status"] != "completed" for key, row in responses.items() if key[0] == model)
        human_pass = len(rows) == count and errors == 0 and incomplete == 0
        reports[model] = {"reviewed": len(rows), "planned": count, "review_complete": len(rows) == count,
            "serious_factual_errors": errors if rows else None, "incomplete_completed_cards": incomplete,
            "unavailable_cards": unavailable, "reviewed_unavailable_cards": reviewed_unavailable,
            "manual_revision_count": sum(row["manual_revision_count"] for row in rows) if rows else None,
            "review_seconds": sum(row["review_seconds"] for row in rows) if rows else None,
            "mean_review_seconds": sum(row["review_seconds"] for row in rows) / len(rows) if rows else None,
            "reviewers": sorted({row["reviewer"] for row in rows}), "human_review_passed": human_pass,
            "automated_gate_passed": automated[model]["automated_gate_passed"],
            "generation_seconds": automated[model]["elapsed_seconds"], "model_requests": automated[model]["llm_calls"],
            "structured_pass_rate": automated[model]["structured_pass_rate"], "scenario_pass_rate": automated[model]["pass_rate"],
            "freeform_completed": automated[model]["freeform_completed"], "assisted_completed": automated[model]["assisted_completed"],
            "quality_gate_passed": human_pass and automated[model]["automated_gate_passed"] and not source_synthetic}
    return {"models": reports, "review_complete": all(row["review_complete"] for row in reports.values()),
        "quality_gate_passed": all(row["quality_gate_passed"] for row in reports.values()),
        "limitations": "人工填写结果，仅验收本套件原版本的研究质量；不替代当前版本评估或历史策略验证。缺失/失败卡单列，自动门槛仍按原30案例计分。"}


def import_review(store: Store, suite_id: str, text: str) -> dict:
    state, responses, identities = _context(store, suite_id)
    reviews = _parse(text, identities, responses)
    source = store.run(state["source_run_id"])
    run_id = "review-" + uuid4().hex
    payload = {"run_id": run_id, "suite_id": suite_id, "review_version": VERSION,
        "checkpoint_fingerprint": fingerprint(state), "engine_id": state["engine_id"], "prompt_version": state["prompt_version"],
        "reviewed_at": datetime.now(timezone.utc).isoformat(), "reviews": reviews, "model_calls": 0,
        **_report(state, responses, reviews, source_synthetic=source["payload"].get("synthetic", True))}
    # Ratings are a separate immutable run; the evaluation checkpoint and user's original CSV stay untouched.
    store.save_run("evaluation_review", source["snapshot_id"], StrategyConfig.model_validate(source["config"]), payload, run_id)
    folder = store.artifact_dir(run_id)
    _atomic(folder / "review.json", json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    by_case = {(row["model"], row["case_id"]): row for row in reviews}
    _atomic(folder / "review.csv", _csv(
        {**identity, **({**by_case[key], "evidence_complete": str(by_case[key]["evidence_complete"]).lower()}
                       if key in by_case else {})} for key, identity in identities.items()))
    return payload | {"exports": str(folder)}


def review_status(store: Store, suite_id: str) -> dict:
    state, responses, _ = _context(store, suite_id)
    for record in store.runs("evaluation_review"):
        saved = json.loads(record["payload"])
        if saved["suite_id"] == suite_id:
            if saved["checkpoint_fingerprint"] != fingerprint(state):
                raise ValueError("reviewed evaluation checkpoint changed; do not reuse these ratings")
            return saved | {"exports": str(store.root / "exports" / record["run_id"])}
    return {"suite_id": suite_id, "human_review": "pending", "model_calls": 0, **_report(state, responses, [])}


def completed_suites(store: Store) -> list[str]:
    suites = []
    for path in sorted((store.root / "exports").glob("evaluation-*/state.json"), reverse=True):
        try:
            state = json.loads(path.read_text())
            if state.get("status") == "completed" and state.get("models"):
                suites.append((state.get("created_at", ""), path.parent.name))
        except (OSError, ValueError):
            continue
    return [suite_id for _, suite_id in sorted(suites, reverse=True)]
