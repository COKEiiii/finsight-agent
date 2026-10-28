"""Read-only, source-bound research history across partial generation batches."""
from __future__ import annotations

import json

from finsight.data.store import Store
from finsight.models import ResearchCard, ScreeningResult, StrategyConfig, fingerprint
from finsight.research import evidence_for


def report_group(provider: str, model: str, prompt_version: str) -> str:
    return fingerprint({"provider": provider, "model": model, "prompt_version": prompt_version})


def saved_research(store: Store, run_id: str) -> dict:
    source = store.run(run_id)
    if source["kind"] != "screen":
        raise ValueError("saved research requires a screening run")
    result = ScreeningResult.model_validate(source["payload"])
    config = StrategyConfig.model_validate(source["config"])
    if (result.run_id != run_id or result.snapshot_id != source["snapshot_id"] or
            result.config_id != config.config_id or result.strategy_version != config.version):
        raise ValueError("screening/configuration binding mismatch")
    candidates = {candidate["symbol"]: candidate for candidate in result.candidates}
    expected = {symbol: evidence_for(result, candidate) for symbol, candidate in candidates.items()}
    typed = result.schema_version >= 2
    groups, rejected = {}, []
    for record in store.runs("research"):  # rowid descending: same-second batches remain deterministic.
        try:
            payload = json.loads(record["payload"])
            if not isinstance(payload, dict):
                raise ValueError("invalid research batch structure")
            if payload.get("screen_run_id") != run_id:
                continue
            if (record["snapshot_id"] != result.snapshot_id or
                    StrategyConfig.model_validate_json(record["config"]).config_id != result.config_id or
                    payload.get("config_id", result.config_id) != result.config_id or
                    payload.get("strategy_version", result.strategy_version) != result.strategy_version):
                raise ValueError("research batch snapshot/strategy binding mismatch")
            provider = payload["provider"]
            if provider not in {"demo", "ollama", "deepseek"}:
                raise ValueError("unknown research provider")
            cards = [ResearchCard.model_validate(card) for card in payload["cards"]]
            if len({card.symbol for card in cards}) != len(cards):
                raise ValueError("duplicate stock in research batch")
            if len({card.model for card in cards}) > 1 or any(
                    payload.get("model", card.model) != card.model for card in cards):
                raise ValueError("research batch model binding mismatch")
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({"research_run_id": record["run_id"], "reason": str(exc)[:300]})
            continue
        for card in cards:
            evidence = expected.get(card.symbol)
            if (card.run_id != run_id or evidence is None or card.evidence != evidence or
                    card.facts != (evidence if typed else {}) or
                    (card.status in {"completed", "demo"} and card.content is None) or
                    (provider == "demo" and (card.status != "demo" or card.model != "offline-template")) or
                    (provider != "demo" and card.status == "demo")):
                rejected.append({"research_run_id": record["run_id"], "symbol": card.symbol,
                    "reason": "research card stock/signal/field evidence binding mismatch"})
                continue
            group_id = report_group(provider, card.model, card.prompt_version)
            group = groups.setdefault(group_id, {"group_id": group_id, "provider": provider, "model": card.model,
                "prompt_version": card.prompt_version, "cards": {}, "screen_run_id": run_id,
                "snapshot_id": result.snapshot_id, "as_of": str(result.as_of), "timestamp_timezone": "UTC",
                "strategy_version": result.strategy_version, "config_id": result.config_id})
            entry = {"card": card.model_dump(mode="json"), "research_run_id": record["run_id"],
                "batch_id": payload.get("batch_id"), "created_at": record["created_at"], "previous_completed": None}
            latest = group["cards"].get(card.symbol)
            if latest is None:
                group["cards"][card.symbol] = entry
            elif (latest["card"]["status"] == "failed" and latest["previous_completed"] is None and
                    card.status in {"completed", "demo"}):
                latest["previous_completed"] = entry
    for group in groups.values():
        group["cards"] = sorted(group["cards"].values(), key=lambda row: (
            candidates[row["card"]["symbol"]]["rank"], row["card"]["symbol"]))
        group["saved_symbols"] = len(group["cards"])
        group["candidate_count"] = len(result.candidates)
    return {"screen_run_id": run_id, "snapshot_id": result.snapshot_id, "strategy_version": result.strategy_version,
        "config_id": result.config_id, "groups": list(groups.values()), "rejected": rejected, "model_calls": 0}
