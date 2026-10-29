"""Evidence-bound weekly review; classifications are facts/questions, not causes."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from finsight.models import ScreeningResult, StrategyConfig, fingerprint
from finsight.observations import CHINA, observation_report, signal_identity, timestamp
from finsight.portfolio import select_targets

VERSION = "weekly_review_v1"
LABELS = {"strategy": "策略", "data": "数据", "execution": "执行", "manual": "人工操作"}
STATUSES = {"attention": "待核实", "pending": "缺少证据", "info": "已有记录"}


def notes_input(notes=None, reviewer=None):
    notes = {} if notes is None else notes
    if not isinstance(notes, dict) or set(notes) - set(LABELS) or any(not isinstance(v, str) for v in notes.values()):
        raise ValueError("review notes must map strategy/data/execution/manual to text")
    if any(len(text) > 4000 for text in notes.values()):
        raise ValueError("each review note must be at most 4000 characters")
    if reviewer is not None and not isinstance(reviewer, str):
        raise ValueError("reviewer must be text")
    cleaned = {key:value.strip() for key,value in notes.items() if value.strip()}
    name = (reviewer or "").strip()
    if len(name) > 200 or cleaned and not name:
        raise ValueError("provide a reviewer when recording human review notes")
    return {"reviewer": name or None, "notes": cleaned, "noted_categories": [k for k in LABELS if k in cleaned],
        "status": "notes_recorded" if cleaned else "pending",
        "note": "人工填写的复盘记录；不会自动消除检查项或授予策略／模型质量验收通过。"}


def build_weekly_review(store, bundle, config, account, entries, *, notes=None, reviewer=None):
    manual = notes_input(notes, reviewer)
    visible = [entry for entry in entries if entry.trade_date <= bundle.meta.as_of]
    execution = account["execution_review"]
    sessions = set(bundle["calendar"]["trade_date"].astype(str))
    recent = [entry for entry in visible if execution["period_start"] and
              execution["period_start"] <= str(entry.trade_date) <= execution["period_end"]]
    findings = []
    def add(category, code, summary, evidence, next_step, *, status="pending", subject=None):
        facts = {"category": category, "code": code, "summary": summary, "status": status,
            "subject": subject, "evidence": evidence, "next_step": next_step}
        findings.append({"finding_id": fingerprint({**facts, "snapshot": bundle.meta.snapshot_id,
            "account": account["account_id"], "config": config.config_id}), **facts})

    add("strategy", "monitor_config", "本期监控使用冻结的策略配置。", {"config_id": config.config_id,
        "strategy_version": config.version}, "结合原选股记录阅读；规则监控不等于收益归因。", status="info")
    fills = [entry for entry in visible if entry.status in {"filled", "partial"}]
    if not fills:
        add("strategy", "no_execution_history", "尚无截至快照日期的实际模拟成交，策略执行表现待观察。",
            {"visible_execution_events": 0}, "完成真实人工演练后再记录实际成交；不要把目标组合当成已持有。")
    for symbol, weight in sorted(account["weights"].items()):
        if weight > config.max_stock_weight:
            add("strategy", "single_weight", "单股市值权重超过监控上限。", {"symbol": symbol,
                "weight": weight, "limit": config.max_stock_weight}, "核对价格漂移与实际成交；月度调仓时复核。", status="attention", subject=symbol)
    if sum(account["weights"].values()) > config.max_equity_weight:
        add("strategy", "equity_weight", "总股票市值权重超过监控上限。", {"weight": sum(account["weights"].values()),
            "limit": config.max_equity_weight}, "核对持仓与价格变化；保留偏离记录。", status="attention")
    counts = Counter(account["holding_industries"].values())
    if len(account["holdings"]) > config.max_holdings:
        add("strategy", "holding_count", "持仓数量超过监控上限。", {"count": len(account["holdings"]),
            "limit": config.max_holdings}, "核对未成交卖单、手工操作与目标名单。", status="attention")
    for industry, count in sorted(counts.items()):
        if industry != "unknown" and count > config.max_industry_names:
            add("strategy", "industry_count", "同一行业持仓数量超过监控上限。", {"industry": industry,
                "count": count, "limit": config.max_industry_names}, "核对行业口径与实际持仓；不自动归因为策略问题。", status="attention", subject=industry)

    if bundle.meta.synthetic:
        add("data", "synthetic", "本报告使用合成演示数据。", {"snapshot_id": bundle.meta.snapshot_id}, "仅验收软件流程，真实观察另行积累。")
    if not bundle.meta.publishable or bundle.meta.publication_status != "published":
        add("data", "unpublished", "当前快照未达到正式发布条件。", {"status": bundle.meta.publication_status},
            "先处理质量报告中的缺口。", status="attention")
    coverage = bundle.meta.coverage
    add("data", "coverage", "快照覆盖率与股票范围单独记录。", {"universe_id": bundle.meta.universe_id,
        "complete_mainboard": bundle.meta.universe_complete, "coverage": coverage},
        "样本覆盖率不能替代全主板覆盖率；缺失数据不能计作规则排除。", status="info" if coverage else "pending")
    if not bundle.meta.research_eligible:
        add("data", "historical_checks", "历史当时可得性尚未通过全部核验。", {"checks": bundle.meta.research_checks},
            "历史结果维持研究演示；继续积累真实前瞻记录。")
    if not account["corporate_actions_verified"]:
        add("data", "corporate_actions", "分红送转数据尚未完成核验。", {"corporate_actions_verified": False},
            "对照账户及公司行动核实持仓数量、现金与应收。")
    for symbol, day in sorted(account["holding_quote_dates"].items()):
        if day != account["as_of"]:
            add("data", "stale_quote", "持仓估值沿用了较早报价。", {"symbol": symbol, "quote_date": day,
                "as_of": account["as_of"]}, "核对停牌及数据缺失，避免把旧报价当成当日成交价。", status="attention", subject=symbol)
    for symbol, industry in sorted(account["holding_industries"].items()):
        if industry == "unknown":
            add("data", "unknown_holding_industry", "持仓行业在截止日不可确认。", {"symbol": symbol, "as_of": account["as_of"]},
                "核实当时可用的行业信息，不用较晚记录代替。", status="attention", subject=symbol)
    if account["performance"].get("flags"):
        add("data", "nav_price_flags", "净值序列存在报价提示。", {"flags": account["performance"]["flags"]},
            "逐日核对数据提示，保留原始标记。", status="attention")
    if bundle.meta.quality_issues:
        add("data", "source_notes", "采集快照保留了来源与质量提示。", {"issues": bundle.meta.quality_issues},
            "区分接口提示、修订与无法判定项，参照原质量报告。", status="info")

    if not execution["fill_events"]:
        add("execution", "no_week_fills", "最近五个交易日没有记录到实际模拟成交。", {"period_start": execution["period_start"],
            "period_end": execution["period_end"], "fill_events": 0}, "确认是否无需调仓、尚未执行或尚未补记日志。")
    else:
        add("execution", "recorded_costs", "本期成交费用按实际日志汇总。", {"fill_events": execution["fill_events"],
            "transaction_cost": execution["transaction_cost"], "known_adverse_price_impact": execution["known_adverse_price_impact"]},
            "逐笔核对费用、计划价与成交价；价差本身不证明原因。", status="info")
    if execution["open_order_count"]:
        add("execution", "open_orders", "截至快照仍有待处理委托。", {"count": execution["open_order_count"],
            "orders": [row for row in execution["orders"] if row["is_open"]]}, "核实剩余委托、撤单或增量成交，再生成下一份计划。", status="attention")
    if execution["unreferenced_fill_events"]:
        add("execution", "missing_plan_price", "部分成交缺少计划价格，无法计算其价差。",
            {"count": execution["unreferenced_fill_events"]}, "补核计划依据；未知价差保持未知。")
    if execution["known_adverse_price_impact"] is not None and execution["known_adverse_price_impact"] > 0:
        add("execution", "adverse_price", "已有计划价的成交存在不利价差。", {"amount": execution["known_adverse_price_impact"]},
            "核对实际报单时刻、参考价格与成交记录，不自动判断人为失误。", status="attention")
    for order in execution["orders"]:
        if order["flags"]:
            add("manual", "order_flags", "订单日志存在需要核对的状态或数量提示。", {"order": order},
                "按委托与增量成交逐笔核实；保留原始事件。", status="attention", subject=order["order_id"] or order["standalone_event_id"])

    comparison = account["account_comparison"]
    for field in ("cash", "holdings"):
        if comparison[field] is None:
            add("manual", "missing_actual_" + field, "未提供实际" + ("现金" if field == "cash" else "完整持仓") + "，该项尚未对账。",
                {"provided": False}, "填写相同截止日的账户数据，或明确确认无股票持仓。")
    if comparison["as_of"] is None and (comparison["cash"] is not None or comparison["holdings"] is not None):
        add("manual", "unknown_account_date", "账户核对数据未注明时点。", {"as_of": None}, "确认与报告截止日一致后重存报告。")
    if account["actual_cash_difference"] is not None and abs(account["actual_cash_difference"]) > .01 + 1e-8:
        add("manual", "cash_difference", "输入现金与日志账本存在差异。", {"difference": account["actual_cash_difference"],
            "tolerance": .01}, "核对漏记成交、费用、公司行动、委托冻结资金与输入时点；不要直接改账本凑平。", status="attention")
    if account["actual_holdings_difference"]:
        add("manual", "holdings_difference", "输入持仓与日志账本存在差异。", {"difference": account["actual_holdings_difference"]},
            "核对完整持仓、增量成交和分红送转。", status="attention")
    if comparison["cash"] is not None and comparison["holdings"] is not None and comparison["as_of"] and (
            abs(account["actual_cash_difference"]) <= .01 + 1e-8 and not account["actual_holdings_difference"]):
        add("manual", "inputs_match", "账本与本次输入的账户数据在一分现金容差内一致。", {"as_of": comparison["as_of"],
            "source": "user_supplied", "cash_tolerance": .01}, "输入数据仍需人工对照账户核实。", status="info")
    if execution["unlinked_event_count"]:
        add("manual", "unlinked_orders", "部分事件未填写委托编号，无法自动关联生命周期。",
            {"count": execution["unlinked_event_count"]}, "核对券商编号；不猜测旧事件关联。")
    if account["future_events"]:
        add("manual", "later_events", "另有晚于报告截止日的日志，未计入本期账本。", {"count": account["future_events"]},
            "历史报告按自身截止日阅读；最新账户请使用相应新快照。", status="info")

    sources, observation_cache = {}, {}
    for entry in visible:
        if entry.run_id in sources:
            continue
        source = store.run(entry.run_id)
        if source["kind"] != "screen":
            raise ValueError("weekly review journal source must be a screening run")
        result, source_config = ScreeningResult.model_validate(source["payload"]), StrategyConfig.model_validate(source["config"])
        if (result.run_id != entry.run_id or source["snapshot_id"] != result.snapshot_id or result.config_id != source_config.config_id or
                result.strategy_version != source_config.version or result.publication_status != "published"):
            raise ValueError("weekly review screening/configuration binding mismatch")
        sources[entry.run_id] = {"run_id": entry.run_id, "snapshot_id": result.snapshot_id,
            "source_fingerprint": fingerprint(source["payload"]), "config_id": result.config_id,
            "strategy_version": result.strategy_version, "as_of": str(result.as_of), "created_at": source["created_at"],
            "published_at": timestamp(source["created_at"]).isoformat()}
        linked = [row for row in visible if row.run_id == entry.run_id]
        if any(row.strategy_version != result.strategy_version or row.trade_date <= result.as_of for row in linked):
            raise ValueError("weekly review journal source strategy/date mismatch")
        recent_linked = [row for row in recent if row.run_id == entry.run_id]
        if not recent_linked:
            continue
        calendar = store.load_snapshot(result.snapshot_id, table_names=("calendar",))["calendar"]
        next_day = next((day for day in sorted(calendar["trade_date"].astype(str)) if day > str(result.as_of)), None)
        target_symbols = {row["symbol"] for row in select_targets(result.candidates, source_config)}
        for row in recent_linked:
            if row.action == "buy" and row.symbol not in target_symbols:
                add("manual", "outside_targets", "买入事件不在所关联信号的固定目标名单中。", {"event_id": row.event_id,
                    "screen_run_id": row.run_id, "symbol": row.symbol}, "核实手工调整、输入或信号关联，并记录理由。", status="attention", subject=row.event_id)
            if str(row.trade_date) not in sessions:
                add("manual", "non_session_event", "日志日期不在本快照的交易日历中。", {"event_id": row.event_id,
                    "trade_date": str(row.trade_date)}, "核对计划日期及交易日历。", status="attention", subject=row.event_id)
            if row.status in {"filled", "partial", "submitted"}:
                if next_day is None:
                    add("data", "unknown_next_session", "原信号日历没有下一交易日，执行计划时点无法核验。",
                        {"screen_run_id": result.run_id}, "保留原日历缺口，不事后伪造当时信息。", subject=row.event_id)
                elif next_day[:7] == str(result.as_of)[:7]:
                    add("manual", "non_month_end_signal", "事件关联的信号不是月末调仓信号。", {"event_id": row.event_id,
                        "signal_date": str(result.as_of)}, "核对是否为风险处理、手工调整或关联错误，记录具体理由。", status="attention", subject=row.event_id)
                elif str(row.trade_date) != next_day:
                    add("execution", "execution_date_deviation", "执行记录日期偏离原信号的下一交易日。", {"event_id": row.event_id,
                        "expected": next_day, "recorded": str(row.trade_date)}, "核对延迟执行、补记日期或信号关联。", status="attention", subject=row.event_id)
        if not result.synthetic:
            if source_config.config_id not in observation_cache:
                observation_cache[source_config.config_id] = observation_report(store, source_config, end=bundle.meta.as_of)
            observed = observation_cache[source_config.config_id]
            match = next((row for row in observed["timeline"] if row["signal_identity"] == signal_identity(result)), None)
            if match:
                sources[entry.run_id].update(prospective_seal_id=match["seal_run_id"],
                    prospective_source_run_id=match["screen_run_id"], prospective_eligible=match["prospective_eligible"],
                    prospective_seal_fingerprint=fingerprint(store.run(match["seal_run_id"])),
                    recorded_at=match["recorded_at"])
            if not match or not match["prospective_eligible"]:
                add("data", "signal_timing_unverified", "未找到该信号合格的前瞻留档，开盘前可用性尚未证实。",
                    {"screen_run_id": result.run_id, "seal_run_id": match["seal_run_id"] if match else None,
                     "timing_status": match["timing_status"] if match else "missing"},
                    "对照实际记录时刻核实；日期日志不足以证明成交发生在信号之后。", subject=result.run_id)
            if observed["rejected"]:
                add("data", "rejected_signal_records", "前瞻留档有无法验证的记录。", {"records": observed["rejected"]},
                    "核对原始记录，保持未验证状态。", subject=result.run_id)
            if next_day and not (match and match["prospective_eligible"]) and timestamp(source["created_at"]) >= datetime.fromisoformat(next_day + "T09:15:00").replace(tzinfo=CHINA):
                add("execution", "late_source", "信号发布时间晚于计划开盘前截点，实际执行时序需核实。",
                    {"screen_run_id": result.run_id, "published_at": timestamp(source["created_at"]).isoformat(), "next_session": next_day},
                    "核对实际成交时刻；只有日期不能判断盘中操作是否晚于信号。", status="attention", subject=result.run_id)
    if any(source["config_id"] != config.config_id for source in sources.values()):
        add("manual", "mixed_strategy_sources", "账户日志关联了与本期监控不同的策略配置。", {"monitor_config_id": config.config_id,
            "source_configs": sorted({source["config_id"] for source in sources.values()})},
            "逐笔区分正式策略和实验组合；整体账户收益不自动归属单个策略。", status="attention")

    groups = []
    for category, label in LABELS.items():
        items = [row for row in findings if row["category"] == category]
        status = "attention" if any(row["status"] == "attention" for row in items) else "pending" if any(
            row["status"] == "pending" for row in items) else "info"
        groups.append({"category": category, "label": label, "status": status,
            "attention_count": sum(row["status"] == "attention" for row in items),
            "pending_count": sum(row["status"] == "pending" for row in items), "finding_count": len(items)})
    return {"version": VERSION, "period_start": execution["period_start"], "period_end": execution["period_end"],
        "snapshot_id": bundle.meta.snapshot_id, "account_id": account["account_id"], "monitor_config_id": config.config_id,
        "groups": groups, "findings": findings, "source_screens": list(sources.values()), "manual_review": manual,
        "status": "awaiting_first_execution" if not fills else "needs_review",
        "model_calls": 0, "note": "按事实和待核实事项分类；分类不认定盈亏原因，人工笔记不替代事实、对账或研究验收。"}


def latest_report(store, snapshot_id, account, config):
    for row in store.runs("portfolio_report"):
        if row["snapshot_id"] != snapshot_id:
            continue
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict) or "account_id" not in payload:
            raise ValueError("saved weekly report payload is invalid")
        if payload.get("account_id") != account or StrategyConfig.model_validate_json(row["config"]).config_id != config.config_id:
            continue
        meta = store.load_snapshot(snapshot_id, table_names=()).meta
        if (payload.get("snapshot_id") != snapshot_id or payload.get("as_of") != str(meta.as_of) or
                payload.get("monitor_config_id", config.config_id) != config.config_id):
            raise ValueError("saved weekly report snapshot/configuration binding mismatch")
        review = payload.get("weekly_review")
        if review is not None and (not isinstance(review, dict) or review.get("snapshot_id") != snapshot_id or
                review.get("account_id") != account or review.get("monitor_config_id") != config.config_id):
            raise ValueError("saved four-category review binding mismatch")
        if payload.get("portfolio_review_version") not in {None, "portfolio_review_v2_classified"}:
            raise ValueError("unknown saved weekly review version")
        if review is not None or payload.get("portfolio_review_version") == "portfolio_review_v2_classified":
            if (not isinstance(review, dict) or review.get("version") != VERSION or
                    payload.get("weekly_review_fingerprint") != fingerprint(review) or
                    StrategyConfig.model_validate(payload.get("strategy_config")).config_id != config.config_id or
                    payload.get("snapshot_hashes") != meta.table_hashes):
                raise ValueError("saved weekly review integrity/configuration check failed")
            for source in review["source_screens"]:
                current = store.run(source["run_id"])
                if (current["kind"] != "screen" or fingerprint(current["payload"]) != source["source_fingerprint"] or
                        current["snapshot_id"] != source["snapshot_id"] or current["created_at"] != source["created_at"] or
                        StrategyConfig.model_validate(current["config"]).config_id != source["config_id"]):
                    raise ValueError("saved weekly review screening source changed")
                if source.get("prospective_seal_id") and fingerprint(store.run(source["prospective_seal_id"])) != source["prospective_seal_fingerprint"]:
                    raise ValueError("saved weekly review prospective seal changed")
        return {**payload, "run_id": row["run_id"], "saved_at": row["created_at"]}
    return None


def render_findings(review):
    lines = ["", "## 四类复盘检查", review["note"]]
    for category, label in LABELS.items():
        lines += ["", "### " + label]
        for row in review["findings"]:
            if row["category"] == category:
                lines += [f"- [{STATUSES[row['status']]}] {row['summary']}",
                    "  - 证据：" + json.dumps(row["evidence"], ensure_ascii=False, sort_keys=True), "  - 下一步：" + row["next_step"]]
        note = review["manual_review"]["notes"].get(category)
        lines += ["", "人工笔记：" + (note if note else "尚未填写。")]
    lines += ["", "笔记填写人：" + (review["manual_review"]["reviewer"] or "尚未填写。")]
    return "\n".join(lines) + "\n"
