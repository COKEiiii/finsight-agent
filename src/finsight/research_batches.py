"""Explicit research plans and per-card durable results; no automatic model resume."""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from finsight.data.akshare_provider import normalize_symbol
from finsight.data.store import Store, code_version
from finsight.models import ScreeningResult, StrategyConfig, fingerprint
from finsight.research import (
    FIELD_PROMPT_VERSION,
    PROMPT_VERSION,
    ChatAdapter,
    render_card,
    research_candidate,
    research_model,
)
from finsight.research_history import report_group, saved_research


def research_plan(store: Store, run_id: str, provider="ollama", model=None, limit=10, *, symbols=None, missing_only=False):
    if not 1 <= limit <= 100:
        raise ValueError("research limit must be in 1..100")
    resolved = research_model(provider, model)
    record = store.run(run_id)
    if record["kind"] != "screen":
        raise ValueError("research requires a screening run")
    result = ScreeningResult.model_validate(record["payload"])
    config = StrategyConfig.model_validate(record["config"])
    if (result.run_id != run_id or result.snapshot_id != record["snapshot_id"] or
            result.config_id != config.config_id or result.strategy_version != config.version):
        raise ValueError("screening/configuration binding mismatch")
    selected = result.candidates
    if symbols is not None:
        requested = []
        for symbol in symbols:
            text = str(symbol).strip().upper()
            normalized = normalize_symbol(text)
            if "." in text and text != normalized:
                raise ValueError("research symbol exchange suffix does not match its code")
            requested.append(normalized)
        if not requested or len(set(requested)) != len(requested):
            raise ValueError("research symbols must be nonempty and distinct")
        unknown = set(requested) - {candidate["symbol"] for candidate in selected}
        if unknown:
            raise ValueError("research symbols are not candidates in this screening: " + ", ".join(sorted(unknown)))
        selected = [candidate for candidate in selected if candidate["symbol"] in requested]
    version = FIELD_PROMPT_VERSION if result.schema_version >= 2 else PROMPT_VERSION
    ready = set()
    if missing_only:
        wanted = report_group(provider, resolved, version)
        for group in saved_research(store, run_id)["groups"]:
            if group["group_id"] == wanted:
                ready = {entry["card"]["symbol"] for entry in group["cards"] if entry["card"]["status"] in {"completed", "demo"}}
    skipped = [candidate["symbol"] for candidate in selected if candidate["symbol"] in ready]
    pending = [candidate["symbol"] for candidate in selected if candidate["symbol"] not in ready]
    planned = pending[:limit]
    return {"screen_run_id": run_id, "snapshot_id": result.snapshot_id, "config_id": config.config_id,
        "strategy_version": config.version, "provider": provider, "model": resolved, "prompt_version": version,
        "missing_only": missing_only, "symbols": planned, "skipped_successful_symbols": skipped,
        "remaining_symbols": pending[limit:], "maximum_model_requests": 0 if provider == "demo" else 3 * len(planned),
        "model_calls": 0, "note": "只读计划；同名模型和提示词版本的已保存成功卡可跳过。不会认证其质量或改写历史来源。"}


def _write(path: Path, value: dict):
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_research(store: Store, run_id: str, provider="ollama", model=None, limit=10, adapter=None, *, symbols=None, missing_only=False):
    if adapter is not None and provider != "demo":
        if adapter.provider != provider or model is not None and adapter.model != model:
            raise ValueError("research adapter/provider/model mismatch")
        model = adapter.model
    plan = research_plan(store, run_id, provider, model, limit, symbols=symbols, missing_only=missing_only)
    if not plan["symbols"]:
        return []
    with (store.root / ".research.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("a research batch is already running; saved reports remain readable") from exc
        # Recheck under the lock: another completed batch may have filled the missing reports.
        plan = research_plan(store, run_id, provider, model, limit, symbols=symbols, missing_only=missing_only)
        if not plan["symbols"]:
            return []
        record = store.run(run_id)
        result = ScreeningResult.model_validate(record["payload"])
        config = StrategyConfig.model_validate(record["config"])
        batch_id = "research-batch-" + uuid4().hex
        folder = store.artifact_dir(batch_id)
        version = code_version()
        manifest = {**plan, "batch_id": batch_id, "code_version": version, "status": "running", "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(), "source_fingerprint": fingerprint(record["payload"]),
            "active_symbol": None, "card_run_ids": [], "completed_symbols": [], "failed_symbols": [],
            "active_card_run_id": None, "input_tokens": 0, "output_tokens": 0,
            "in_flight_usage_unknown": False,
            "notes": ["逐只保存完整结果；不会自动恢复或重试整批。",
                "若进程意外退出，active_symbol 可能已有未记录的请求或用量；不要据缺少结果推断零费用。"]}
        _write(folder / "batch.json", manifest)
        own_adapter = provider != "demo" and adapter is None
        cards = []
        try:
            store.save_run("research_batch", result.snapshot_id, config, {"batch_id": batch_id, "screen_run_id": run_id,
                "provider": provider, "model": plan["model"], "manifest": str(folder / "batch.json"), "code_version": version}, batch_id)
            if own_adapter:
                adapter = ChatAdapter(provider, plan["model"])
            for candidate in result.candidates:
                if candidate["symbol"] not in plan["symbols"]:
                    continue
                manifest["active_symbol"] = candidate["symbol"]
                card_id = uuid4().hex
                manifest["active_card_run_id"] = card_id
                manifest["in_flight_usage_unknown"] = provider != "demo"
                _write(folder / "batch.json", manifest)
                card = research_candidate(result, candidate, store, adapter, demo=provider == "demo")
                payload = {"screen_run_id": run_id, "batch_id": batch_id, "provider": provider, "model": card.model,
                    "strategy_version": result.strategy_version, "config_id": result.config_id,
                    "cards": [card.model_dump(mode="json")], "code_version": version}
                # Preserve the returned response before SQLite publication. If publication fails,
                # active_card_run_id identifies the local response; do not infer that it cost zero.
                out = store.artifact_dir(card_id)
                _write(out / "research.json", payload)
                (out / "research.md").write_text(render_card(card), encoding="utf-8")
                manifest["model_calls"] += card.llm_calls
                manifest["input_tokens"] += card.input_tokens
                manifest["output_tokens"] += card.output_tokens
                manifest["in_flight_usage_unknown"] = False
                _write(folder / "batch.json", manifest)
                store.save_run("research", result.snapshot_id, config, payload, card_id)
                cards.append(card)
                manifest["card_run_ids"].append(card_id)
                manifest["completed_symbols" if card.status != "failed" else "failed_symbols"].append(card.symbol)
                manifest.update(active_symbol=None, active_card_run_id=None, in_flight_usage_unknown=False)
                # SQLite is authoritative for completed cards; exports can be rebuilt from these records.
                _write(folder / "batch.json", manifest)
            manifest.update(status="completed", pid=None, finished_at=datetime.now(timezone.utc).isoformat())
        except BaseException as exc:
            manifest.update(status="interrupted", pid=None, error=type(exc).__name__,
                finished_at=datetime.now(timezone.utc).isoformat())
            raise
        finally:
            try:
                _write(folder / "batch.json", manifest)
                _write(folder / "research.json", {"batch_id": batch_id, "screen_run_id": run_id,
                    "cards": [card.model_dump(mode="json") for card in cards], "card_run_ids": manifest["card_run_ids"]})
                (folder / "research.md").write_text("# FinSight 研究卡\n\n" + "\n\n---\n\n".join(render_card(card) for card in cards), encoding="utf-8")
            finally:
                if own_adapter and adapter is not None:
                    adapter.close()
        return cards
