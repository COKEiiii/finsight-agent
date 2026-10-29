"""Local collection diagnostics; raw completion never grants publication permission."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from finsight.data.store import Store, code_version

TERMINAL = {"completed", "partial", "failed", "cancelled"}


def elapsed_seconds(job: dict, now: datetime | None = None) -> float:
    end = datetime.fromisoformat(job.get("progress", {}).get("finished_at") or job["updated_at"]) if job["status"] in TERMINAL or job.get("interrupted") else (
        now or datetime.now(ZoneInfo("Asia/Shanghai")))
    return max(0., (end - datetime.fromisoformat(job["created_at"])).total_seconds())


def recovery_status(store: Store, job: dict) -> dict:
    result = {"can_resume": job["status"] in {"failed", "cancelled", "queued", "running", "partial"},
              "action": "resume", "reason": None, "next_session": None, "earliest_capture_at": None}
    if job["status"] == "completed":
        return {**result, "can_resume": False, "action": "none"}
    if job.get("interrupted"):
        return {**result, "can_resume": True, "reason": "coordinator_exited",
                "message": "采集进程已退出，成功请求仍保存在本地。可恢复原任务；当前中断历时截至最后记录，退出时刻未知。"}
    if job["status"] not in {"partial", "failed", "cancelled"} or not job.get("snapshot_id"):
        return result
    snapshot = store.load_snapshot(job["snapshot_id"], table_names=())
    temporal = any("no stock state known at cutoff" in issue for issue in snapshot.meta.quality_issues)
    if temporal:
        data = store.load_snapshot(job["snapshot_id"], table_names=("universe", "calendar"))
        members = data.meta.membership_symbols or list(data["universe"]["symbol"].unique())
        states = data["universe"].loc[data["universe"]["symbol"].isin(members)]
        first_observed = states["known_on"].dropna().max()
        dates = sorted(day for day in data["calendar"]["trade_date"] if day >= str(first_observed))
        day = dates[0] if dates else None
        return {**result, "can_resume": False, "action": "new_incremental",
                "reason": "cutoff_before_first_observation", "next_session": day,
                "earliest_capture_at": f"{day}T18:00:00+08:00" if day else None,
                "message": f"股票状态首次采于 {first_observed}，不能回填 {data.meta.as_of}。请在新的完整交易日采集增量数据。"}
    if job["status"] == "partial" and not job["requests"]["failed"]:
        return {**result, "can_resume": False, "action": "new_incremental",
                "reason": "finalized_quality_failure",
                "message": "采集结果已保存，请新建增量任务检查数据修订；恢复旧任务会重复读取相同检查点。"}
    return result


def request_profile(store: Store, job_id: str) -> list[dict]:
    groups = defaultdict(list)
    with store.connect() as db:
        rows = db.execute("SELECT method,status,attempts,elapsed,cache_hit,http_bytes,network_requests,response_rows "
                          "FROM job_requests WHERE job_id=?", (job_id,)).fetchall()
    for row in rows:
        groups[row["method"]].append(dict(row))
    output = []
    for method, records in groups.items():
        times = sorted(row["elapsed"] for row in records)
        measured = [row["response_rows"] for row in records if row["response_rows"] is not None]
        output.append({"method": method, "requests": len(records),
            "completed": sum(row["status"] == "done" for row in records),
            "failed": sum(row["status"] == "failed" for row in records),
            "latest_read_cache_hits": sum(row["cache_hit"] for row in records),
            "network_attempts": sum(row["attempts"] for row in records),
            "observed_network_requests": sum(row["network_requests"] for row in records),
            "measured_http_bytes": sum(row["http_bytes"] for row in records),
            "response_rows": sum(measured) if measured else None,
            "row_count_measurements": len(measured),
            "cumulative_request_seconds": sum(times),
            "p95_request_seconds": times[max(0, (95 * len(times) + 99) // 100 - 1)]})
    return sorted(output, key=lambda row: (-row["cumulative_request_seconds"], row["method"]))


def build_report(store: Store, job: dict, now: datetime | None = None) -> dict:
    report = {"job_id": job["job_id"], "status": job.get("observed_status", job["status"]),
        "stored_status": job["status"], "process_alive": job.get("process_alive"), "snapshot_id": job["snapshot_id"],
        "report_code_version": code_version(),
        "strategy_version": job["config"].get("strategy", {}).get("version"),
        "created_at": job["created_at"], "updated_at": job["updated_at"],
        "wall_elapsed_seconds": elapsed_seconds(job, now),
        "last_attempt_seconds": job["progress"].get("elapsed_seconds"),
        "symbols_processed": job["progress"].get("symbols_completed", 0),
        "symbols_total": job["progress"].get("symbols_total", 0),
        "processing_complete": job["progress"].get("phase") == "done" and bool(job["snapshot_id"]),
        "requests": job["requests"], "methods": request_profile(store, job["job_id"]),
        "signal_observation": job["progress"].get("signal_observation"),
        "recovery": recovery_status(store, job),
        "publication": None, "local_tables": {}, "rollout": None,
        "measurement_notes": [
            "股票处理完成与可发布覆盖率分别判定；本报告不会改变快照日期或质量状态。",
            "任务历时在任务结束后固定；最近一次运行耗时不含此前暂停及重试间隔。",
            "累计请求耗时含重试、限流等待及序列化，并发时间可重叠，不能代替任务历时。",
            "HTTP 字节不包含未计量的 BaoStock TCP 字节；返回行数为空表示旧记录未计量。",
            "缓存命中统计最近一次读取方式，实际网络请求数单独累计，包含重试和分页。"]}
    if job["snapshot_id"]:
        bundle = store.load_snapshot(job["snapshot_id"], table_names=())
        quality_path = store.root / "snapshots" / job["snapshot_id"] / "quality.json"
        quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else {}
        report["local_tables"] = quality.get("table_rows", {})
        missing = quality.get("coverage", {}).get("missing", [])
        report["publication"] = {"as_of": str(bundle.meta.as_of),
            "collector_code_version": bundle.meta.collector_version,
            "captured_at": bundle.meta.captured_at.isoformat(),
            "status": quality.get("publication_status", bundle.meta.publication_status),
            "coverage": quality.get("coverage", {}).get("coverage", bundle.meta.coverage.get("coverage")),
            "issues": quality.get("issues", bundle.meta.quality_issues),
            "missing_reason_counts": dict(Counter(code for stock in missing for code in stock.get("codes", []))),
            "historical_validation_passed": bundle.meta.research_eligible}
        if bundle.meta.universe_complete and not bundle.meta.synthetic:
            from finsight.scheduler import rollout_status
            report["rollout"] = rollout_status(store)
    return report


def render_report(report: dict) -> str:
    publication = report["publication"] or {}
    lines = ["# FinSight 采集验收报告", f"任务：{report['job_id']}",
        f"股票处理：{report['symbols_processed']} / {report['symbols_total']}",
        f"任务历时：{report['wall_elapsed_seconds'] / 60:.2f} 分钟",
        f"快照：{report['snapshot_id'] or '尚未保存'}", f"发布状态：{publication.get('status', '尚未评估')}"]
    if report["recovery"].get("message"):
        lines.append(report["recovery"]["message"])
    if report.get("signal_observation"):
        observation = report["signal_observation"]
        lines.append("信号记录：" + observation["status"] + " · " + observation.get("timing_status", observation.get("error", "")))
    lines += ["", "## 本地数据", "| 数据表 | 记录数 |", "|---|---:|"]
    lines += [f"| {name} | {count} |" for name, count in report["local_tables"].items()]
    lines += ["", "## 请求性能", "| 接口 | 请求记录 | 网络请求 | 累计秒 | HTTP 字节 | 返回行数 |", "|---|---:|---:|---:|---:|---:|"]
    lines += [f"| {row['method']} | {row['requests']} | {row['observed_network_requests']} | "
              f"{row['cumulative_request_seconds']:.2f} | {row['measured_http_bytes']} | "
              f"{row['response_rows'] if row['response_rows'] is not None else '未计量'} |" for row in report["methods"]]
    lines += ["", "## 计量说明", *[f"- {note}" for note in report["measurement_notes"]]]
    return "\n".join(lines)
