"""Immutable signal observations, distinct from executions and portfolio returns."""
from __future__ import annotations

import csv
import fcntl
import io
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from finsight.data.store import Store, code_version
from finsight.data.universe import universe_id
from finsight.models import ScreeningResult, StrategyConfig, fingerprint
from finsight.portfolio import select_targets

PROTOCOL = "signal_seal_v1"
POLICY = "before_next_opening_auction_v1"
CHINA = ZoneInfo("Asia/Shanghai")
TIMING_LABELS = {"on_time": "截点前固定", "late_publication": "榜单发布较晚", "late_seal": "信号记录较晚",
    "unknown_calendar": "下一交易日未知"}


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(CHINA)


def _source(store, run_id):
    record = store.run(run_id)
    if record["kind"] != "screen":
        raise ValueError("signal observation requires a screening run")
    result = ScreeningResult.model_validate(record["payload"])
    config = StrategyConfig.model_validate(record["config"])
    if (result.run_id != run_id or result.snapshot_id != record["snapshot_id"] or result.config_id != config.config_id or
            result.strategy_version != config.version):
        raise ValueError("signal screening/configuration binding mismatch")
    if result.synthetic or result.publication_status != "published":
        raise ValueError("only real published signals may enter prospective observations")
    meta = store.load_snapshot(result.snapshot_id, table_names=()).meta
    if (meta.source != "akshare" or meta.synthetic or not meta.publishable or meta.publication_status != "published" or
            not meta.universe_complete or meta.universe_id != universe_id(True)):
        raise ValueError("prospective observations require a published complete mainboard snapshot")
    if (result.as_of != meta.as_of or result.universe_id != meta.universe_id or result.membership_id != meta.membership_id):
        raise ValueError("signal date/universe/membership does not match its snapshot")
    if timestamp(record["created_at"]) < datetime.combine(result.as_of, time(18), CHINA):
        raise ValueError("signal timestamp precedes the completed-session research cutoff")
    # SQLite timestamps have second precision; do not reject a subsecond capture
    # in that same second, but never admit a signal predating its own snapshot.
    if timestamp(record["created_at"]) + timedelta(seconds=1) <= timestamp(meta.captured_at):
        raise ValueError("signal timestamp precedes its data snapshot capture")
    return record, result, config, meta


def _identity(result):
    return fingerprint({"snapshot_id": result.snapshot_id, "as_of": str(result.as_of), "config_id": result.config_id,
        "universe_id": result.universe_id, "membership_id": result.membership_id,
        "candidates": result.candidates, "exclusions": result.exclusions})


def signal_identity(result):
    """Semantic identity shared by read-only review and idempotent capture."""
    return _identity(result)


def _sealed(store, row):
    value = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    if not isinstance(value, dict) or row["kind"] != "signal_seal" or value.get("protocol") != PROTOCOL:
        raise ValueError("unknown signal observation protocol")
    source, result, config, meta = _source(store, value["screen_run_id"])
    if (value["source_fingerprint"] != fingerprint(value["screening_result"]) or
            value["source_fingerprint"] != fingerprint(source["payload"]) or value["snapshot_id"] != meta.snapshot_id or
            row["snapshot_id"] != meta.snapshot_id or StrategyConfig.model_validate(
                json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]).config_id != config.config_id or
            value["screen_created_at"] != source["created_at"] or timestamp(source["created_at"]) > timestamp(row["created_at"])):
        raise ValueError("sealed signal source/configuration/timestamp binding mismatch")
    if value.get("calendar_hash", meta.table_hashes.get("calendar")) != meta.table_hashes.get("calendar"):
        raise ValueError("sealed exchange calendar binding mismatch")
    if timestamp(row["created_at"]) > datetime.now(CHINA):
        raise ValueError("sealed timestamp is in the future")
    return value, source, result, config, meta


def _view(store, row, calendars=None):
    _, source, result, config, meta = _sealed(store, row)
    calendars = {} if calendars is None else calendars
    key = meta.table_hashes.get("calendar") or meta.snapshot_id
    if key not in calendars:
        calendars[key] = sorted(set(store.load_snapshot(meta.snapshot_id, table_names=("calendar",))["calendar"]["trade_date"].astype(str)))
    sessions = calendars[key]
    if str(result.as_of) not in sessions:
        raise ValueError("signal date is absent from the sealed exchange calendar")
    next_day = next((day for day in sessions if day > str(result.as_of)), None)
    deadline = datetime.combine(date.fromisoformat(next_day), time(9, 15), CHINA) if next_day else None
    source_time, recorded = timestamp(source["created_at"]), timestamp(row["created_at"])
    source_on_time = source_time < deadline if deadline else None
    seal_on_time = recorded < deadline if deadline else None
    status = ("unknown_calendar" if deadline is None else "late_publication" if not source_on_time else
              "late_seal" if not seal_on_time else "on_time")
    targets = select_targets(result.candidates, config)
    weights = {item["symbol"]: item["target_weight"] for item in targets}
    industry = defaultdict(float)
    for item in targets:
        industry[item["industry"]] += item["target_weight"]
    return {"seal_run_id": row["run_id"], "screen_run_id": result.run_id, "snapshot_id": result.snapshot_id,
        "signal_date": str(result.as_of), "strategy_version": config.version, "config_id": config.config_id,
        "universe_id": result.universe_id, "membership_id": result.membership_id,
        "source_fingerprint": fingerprint(source["payload"]), "signal_identity": _identity(result),
        "signal_created_at": source_time.isoformat(), "recorded_at": recorded.isoformat(),
        "next_session": next_day, "research_deadline": deadline.isoformat() if deadline else None,
        "admission_policy": POLICY, "source_available_before_auction": source_on_time,
        "sealed_before_auction": seal_on_time, "prospective_eligible": status == "on_time", "timing_status": status,
        "rebalance_due": next_day[:7] != str(result.as_of)[:7] if next_day else None,
        "coverage": result.coverage, "candidates": result.candidates, "target_weights_reference": weights,
        "target_cash_weight_reference": round(1 - sum(weights.values()), 12),
        "target_industry_weights_reference": dict(sorted(industry.items())),
        "historical_validation_passed": result.research_eligible,
        "note": "信号与目标权重观察，不是成交或持仓；非月末不据此生成每日调仓，未计算账户收益。"}


def seal_signal(store: Store, run_id: str):
    source, result, config, meta = _source(store, run_id)
    if timestamp(source["created_at"]) > datetime.now(CHINA):
        raise ValueError("signal source timestamp is in the future")
    identity = _identity(result)
    rejected = []
    with (store.root / ".observations.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another signal observation is being recorded; retry without recollecting data") from exc
        for row in reversed(store.runs("signal_seal")):
            try:
                _, _, previous, _, _ = _sealed(store, row)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                rejected.append({"seal_run_id": row["run_id"], "issue": str(exc)[:300]})
                continue
            if _identity(previous) == identity:
                view = _view(store, row)
                break
        else:
            payload = {"protocol": PROTOCOL, "screen_run_id": run_id, "screen_created_at": source["created_at"],
                "snapshot_id": meta.snapshot_id, "source_fingerprint": fingerprint(source["payload"]),
                "screening_result": source["payload"], "calendar_hash": meta.table_hashes.get("calendar"),
                "note": "Local immutable copy of an already published signal; no execution or return is inferred."}
            seal_id = store.save_run("signal_seal", meta.snapshot_id, config, payload)
            view = _view(store, store.run(seal_id))
        view["rejected_existing"] = rejected
        # Exports can be recreated; the authoritative timestamp and payload stay in SQLite.
        folder = store.artifact_dir(view["seal_run_id"])
        (folder / "observation.json").write_text(json.dumps(view, ensure_ascii=False, indent=2) + "\n")
        return view


def observation_report(store: Store, config: StrategyConfig, start=None, end=None):
    if start and end and start > end:
        raise ValueError("observation start must not exceed end")
    rows, rejected, calendars = [], [], {}
    for position, row in enumerate(store.runs("signal_seal")):
        try:
            view = _view(store, row, calendars)
            if view["config_id"] == config.config_id and (not start or view["signal_date"] >= str(start)) and (
                    not end or view["signal_date"] <= str(end)):
                rows.append({**view, "_stored_order": -position})
        except (ValueError, TypeError, KeyError, OSError) as exc:
            rejected.append({"seal_run_id": row["run_id"], "issue": str(exc)[:300]})
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["signal_date"]].append(row)
    timeline, previous = [], None
    for day, versions in sorted(by_day.items()):
        ordered = sorted(versions, key=lambda item: (item["recorded_at"], item["_stored_order"]))
        eligible = [row for row in ordered if row["prospective_eligible"]]
        chosen = dict((eligible or ordered)[-1])
        chosen.pop("_stored_order")
        chosen.update(revisions=len(versions) - 1, initial_recorded_at=ordered[0]["recorded_at"],
            version_seal_ids=[row["seal_run_id"] for row in ordered])
        current = {row["symbol"]: row for row in chosen["candidates"]}
        old = {row["symbol"]: row for row in previous["candidates"]} if previous else {}
        chosen.update(entered=[symbol for symbol in current if symbol not in old] if previous else [],
            exited=[symbol for symbol in old if symbol not in current] if previous else [],
            rank_changes=[{"symbol": symbol, "previous_rank": old[symbol]["rank"], "rank": row["rank"],
                "rank_change": old[symbol]["rank"] - row["rank"], "score_change": row["score"] - old[symbol]["score"]}
                for symbol, row in current.items() if symbol in old], target_turnover_reference=None,
            compared_with_signal_date=previous["signal_date"] if previous else None)
        chosen["previous_timing_status"] = previous["timing_status"] if previous else None
        chosen["comparison_is_prospective"] = bool(previous and previous["prospective_eligible"] and chosen["prospective_eligible"])
        chosen["intervening_sessions"] = sorted({day for values in calendars.values() for day in values
            if previous and previous["signal_date"] < day < chosen["signal_date"]})
        if previous:
            before, after = previous["target_weights_reference"], chosen["target_weights_reference"]
            change = sum(abs(after.get(symbol, 0) - before.get(symbol, 0)) for symbol in set(before) | set(after))
            change += abs(chosen["target_cash_weight_reference"] - previous["target_cash_weight_reference"])
            chosen["target_turnover_reference"] = round(change / 2, 12)
        timeline.append(chosen)
        previous = chosen
    now = datetime.now(CHINA)
    completed_day = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
    lower = str(start) if start else timeline[0]["signal_date"] if timeline else None
    upper = min(str(end), str(completed_day)) if end else timeline[-1]["signal_date"] if timeline else None
    sessions = sorted({day for values in calendars.values() for day in values})
    missing = [day for day in sessions if lower and upper and lower <= day <= upper and day not in by_day]
    return {"protocol": PROTOCOL, "admission_policy": POLICY, "strategy_version": config.version,
        "config_id": config.config_id, "universe_id": universe_id(True), "generated_at": now.isoformat(),
        "code_version": code_version(), "start": lower, "end": upper, "observed_sessions": len(timeline),
        "prospective_sessions": sum(row["prospective_eligible"] for row in timeline),
        "late_publication_sessions": sum(row["timing_status"] == "late_publication" for row in timeline),
        "late_seal_sessions": sum(row["timing_status"] == "late_seal" for row in timeline),
        "unknown_calendar_sessions": sum(row["timing_status"] == "unknown_calendar" for row in timeline),
        "missing_signal_sessions": missing, "rejected": rejected, "timeline": timeline,
        "calendar_range": {"start": sessions[0] if sessions else None, "end": sessions[-1] if sessions else None},
        "calendar_range_incomplete": bool(lower and upper and (not sessions or lower < sessions[0] or upper > sessions[-1])),
        "portfolio_returns": None, "actual_executions": None,
        "note": "按固定策略分开统计；相同日期只计一次，保留所有版本。目标权重差异不是实际换手或收益，账户交易另看日志。"}


def render_observations(report):
    lines = ["# FinSight 前瞻信号观察", f"策略：{report['strategy_version']} · 配置：{report['config_id']}",
        f"已记录 {report['observed_sessions']} 个交易日；开盘集合竞价前固定的信号 {report['prospective_sessions']} 个。",
        "研究截点为下一交易日北京时间 09:15；未知日历、晚发布或晚记录单列。人工成交与账户收益尚未计入。", "",
        "| 信号日 | 覆盖率 | 时间状态 | 原信号时间 | 记录时间 | 入榜/出榜 | 参考权重差异 | 月末调仓日 |",
        "|---|---:|---|---|---|---|---:|---|"]
    for row in report["timeline"]:
        turnover = row["target_turnover_reference"]
        coverage = row["coverage"].get("coverage")
        coverage_text = "未知" if coverage is None else format(coverage, ".2%")
        lines.append(f"| {row['signal_date']} | {coverage_text} | {TIMING_LABELS[row['timing_status']]} | "
            f"{row['signal_created_at']} | {row['recorded_at']} | {', '.join(row['entered'])} / {', '.join(row['exited'])} | "
            f"{'—' if turnover is None else format(turnover, '.2%')} | {row['rebalance_due']} |")
    lines += ["", report["note"], "缺失信号交易日：" + (", ".join(report["missing_signal_sessions"]) or "无"),
        f"无法验证的记录：{len(report['rejected'])}。", "", "同日修订只选截点前最新有效版本；截点后的修订不替换既有前瞻信号。"]
    return "\n".join(lines) + "\n"


def export_report(store, config, report):
    run_id = store.save_run("observation_report", "", config, report)
    folder = store.artifact_dir(run_id)
    (folder / "observations.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (folder / "observations.md").write_text(render_observations(report))
    (folder / "observations.csv").write_text(observations_csv(report))
    return {**report, "run_id": run_id, "exports": str(folder)}


def observations_csv(report):
    stream = io.StringIO()
    fields = ("signal_date", "seal_run_id", "screen_run_id", "snapshot_id", "signal_created_at", "recorded_at",
        "timing_status", "prospective_eligible", "research_deadline", "rebalance_due", "target_turnover_reference", "revisions")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows({key:row[key] for key in fields} for row in report["timeline"])
    return "\ufeff" + stream.getvalue()
