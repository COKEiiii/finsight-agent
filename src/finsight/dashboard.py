"""Local single-user UI. All business operations go through the same service as the CLI."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from datetime import date, datetime
from uuid import uuid4

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from finsight.collection_status import elapsed_seconds, recovery_status
from finsight.data.quality import audit
from finsight.jobs import Jobs
from finsight.models import ScreeningResult, StrategyConfig, TradeJournalEntry
from finsight.portfolio import import_journal, journal_entries, portfolio_plan
from finsight.research import ChatAdapter, render_card
from finsight.service import FinSight


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", default=os.getenv("FINSIGHT_DATA_DIR", "data/finsight"))
    args, _ = parser.parse_known_args()
    st.set_page_config(page_title="FinSight A-Share", page_icon="📊", layout="wide")
    st.markdown("""<style>
      .block-container {padding-top: 2rem; max-width: 1400px;}
      h1 {letter-spacing: -.04em;} [data-testid="stMetric"] {background: rgba(100,140,160,.08);
      padding: 18px; border-radius: 12px;} .eyebrow {color:#2c9f88; letter-spacing:.16em; font-weight:600;}
      </style><div class="eyebrow">FINANCIAL RESEARCH WORKSPACE</div>""", unsafe_allow_html=True)
    st.title("FinSight A-Share")
    st.caption("A 股多因子筛选 · 本地 AI 研究 · 模拟组合复盘")
    service = FinSight(args.data_dir)
    jobs = Jobs(service.store)
    with st.sidebar:
        st.header("研究工作区")
        st.caption("本地单用户 · strategy_v0.1")
        if st.button("创建离线演示数据", width="stretch"):
            with st.spinner("准备合成样例…"):
                created = service.sync("demo")
            st.session_state["snapshot_choice"] = created["snapshot_id"]
            st.rerun()
        st.caption("演示数据使用虚构公司和工作日日历。")
        universe = st.selectbox("真实数据范围", ["20 只样本", "主板验证 · 100 只", "主板验证 · 500 只", "沪深主板"])
        if st.button("更新 AKShare 数据", width="stretch"):
            try:
                job = service.sync("akshare", mainboard=universe != "20 只样本",
                    limit={"主板验证 · 100 只": 100, "主板验证 · 500 只": 500}.get(universe), background=True)
                st.session_state["job_choice"] = job["job_id"]
                st.rerun()
            except (ValueError, OSError, KeyError) as exc:
                st.error(str(exc))
        @st.fragment(run_every="5s")
        def collection_status():
            recent = jobs.list()[:20]
            if not recent:
                return
            latest_published = service.store.latest(published_only=True)
            recent.sort(key=lambda item: (item["status"] in {"queued", "running", "cancelling"},
                item["config"].get("mainboard", False) and not item["config"].get("limit") and not item["config"].get("symbols"),
                latest_published is not None and item["snapshot_id"] == latest_published.snapshot_id,
                item["config"].get("mainboard", False), item["created_at"]), reverse=True)
            by_job = {job["job_id"]: job for job in recent}
            job_labels = {"interrupted": "已中断", "running": "采集中", "queued": "等待启动", "completed": "已完成",
                          "partial": "待完善", "failed": "失败", "cancelled": "已取消", "cancelling": "正在取消"}
            job_id = st.selectbox("采集任务", list(by_job), key="job_choice",
                format_func=lambda identifier: f"{job_labels.get(by_job[identifier].get('observed_status', by_job[identifier]['status']), by_job[identifier]['status'])} · {by_job[identifier]['config']['universe_id'][:12]} · {identifier[:8]}")
            job = by_job[job_id]
            progress, requests = job["progress"], job["requests"]
            elapsed = elapsed_seconds(job)
            recovery = recovery_status(service.store, job)
            done, total = progress.get("symbols_completed", 0), progress.get("symbols_total", 0)
            if total:
                st.progress(min(done / total, 1.0), text=f"股票 {done}/{total} · {progress.get('phase', job['status'])}")
                if progress.get("symbols_with_errors"):
                    st.caption(f"有采集提示或接口问题的股票：{progress['symbols_with_errors']} 只；按质量报告统一判定。")
            st.caption(f"请求成功 {requests['completed']} · 失败 {requests['failed']} · 缓存命中 {requests['cache_hits']} · 任务历时 {elapsed / 60:.1f} 分钟 · 累计请求 {requests['request_seconds']:.1f} 秒")
            if progress.get("phase") == "done":
                st.caption("股票处理已完成，数据已保存在本地；正式发布另按日期与覆盖率判定。")
            coverage = progress.get("coverage", {})
            if coverage:
                st.caption(f"覆盖率 {coverage.get('coverage', 0):.2%} · {progress.get('publication_status', '预览')}")
            if job["error"]:
                st.error(job["error"])
            interrupted = job.get("interrupted", job["status"] in {"running", "cancelling"} and not jobs.alive(job["pid"]))
            if interrupted:
                st.warning("采集进程已停止，成功请求保留在本地，可恢复该任务。")
            if recovery.get("message"):
                st.info(recovery["message"])
            if recovery["action"] == "new_incremental":
                earliest = recovery.get("earliest_capture_at")
                ready = not earliest or datetime.now().astimezone() >= datetime.fromisoformat(earliest)
                if earliest:
                    st.caption(f"最早评估新的交易日：{recovery['next_session']} 18:00 后，再进行增量更新。")
                if st.button("新建增量采集", key=f"incremental_{job_id}", disabled=not ready):
                    config = job["config"]
                    new_job = service.sync("akshare", symbols=config.get("symbols"),
                        mainboard=config.get("mainboard", False), limit=config.get("limit"), background=True)
                    st.session_state["job_choice"] = new_job["job_id"]
                    st.rerun()
            if recovery["can_resume"] and (interrupted or job["status"] in {"failed", "cancelled", "partial"}) and st.button("恢复该任务", key=f"resume_{job_id}"):
                service.sync("akshare", resume=job_id, background=True)
                st.rerun()
            if job["status"] in {"queued", "running"} and st.button("取消该任务", key=f"cancel_{job_id}"):
                jobs.cancel(job_id)
                st.rerun()
            if job["snapshot_id"] and st.button("查看任务快照", key=f"open_{job_id}"):
                st.session_state["snapshot_choice"] = job["snapshot_id"]
                st.rerun()
        collection_status()
        snapshots = service.store.snapshots()
        if not snapshots:
            st.info("先创建演示数据，或更新真实数据。")
            st.stop()
        by_id = {s.snapshot_id: s for s in snapshots}
        preferred = service.store.latest(published_only=True)
        choices = list(by_id)
        if preferred and preferred.snapshot_id in choices:
            choices.remove(preferred.snapshot_id)
            choices.insert(0, preferred.snapshot_id)
            st.caption(f"最新可用：{preferred.as_of} · {preferred.universe_scope}")
            if st.button("查看最新可用榜单", width="stretch"):
                st.session_state["snapshot_choice"] = preferred.snapshot_id
        snapshot_id = st.selectbox("数据快照", choices, format_func=lambda s: (
            f"{'演示' if by_id[s].synthetic else 'AKShare'} · {by_id[s].as_of} · "
            f"{'待复核' if by_id[s].schema_version < 2 and not by_id[s].synthetic else by_id[s].publication_status} · {s[-6:]}"), key="snapshot_choice")
        st.caption(f"快照：{snapshot_id}")
    metadata = by_id[snapshot_id]
    quality_file = service.store.root / "snapshots" / snapshot_id / "quality.json"
    full = service.store.load_snapshot(snapshot_id, table_names=("calendar",))
    sessions = sorted(full["calendar"]["trade_date"].loc[full["calendar"]["trade_date"] <= str(metadata.as_of)])
    lower = sessions[max(0, len(sessions) - 61)] if sessions else str(metadata.as_of)
    bundle = service.store.load_snapshot(snapshot_id, date_range=(lower, str(metadata.as_of)))
    quality = json.loads(quality_file.read_text(encoding="utf-8")) if quality_file.exists() else audit(bundle)
    if quality.get("config_id") != service.config.config_id:
        stored_counts = quality.get("table_rows")
        quality = audit(bundle, service.config)
        if stored_counts:
            quality["table_rows"] = stored_counts
    if bundle.meta.synthetic:
        st.info("当前为合成演示数据；图表与选股结果用于展示软件流程。")
    if not quality["research_eligible"]:
        st.warning("历史回测验证未通过，相关回测标记为研究演示；当日选股按当前数据质量单独判定。")
    st.caption(f"快照截止日期：{bundle.meta.as_of} · 抓取时间：{bundle.meta.captured_at:%Y-%m-%d %H:%M} · 范围：{bundle.meta.universe_scope}")
    columns = st.columns(4)
    columns[0].metric("数据日期", str(bundle.meta.as_of))
    columns[1].metric("股票范围", len(bundle.meta.membership_symbols) if bundle.meta.membership_symbols else len(set(bundle["universe"]["symbol"])))
    columns[2].metric("最近 61 交易日行情", f"{len(bundle['prices']):,}")
    columns[3].metric("发布状态", {"published": "已发布", "preview": "仅预览", "blocked": "阻止发布"}.get(
        quality.get("publication_status"), "可选股" if quality["publishable"] else "数据待修复"))
    data_tab, rank_tab, research_tab, portfolio_tab = st.tabs(["数据状态", "候选排行榜", "研究卡", "组合与绩效"])
    screen_runs = [r for r in service.store.runs("screen") if r["snapshot_id"] == snapshot_id]
    selected = None
    if screen_runs:
        selected = ScreeningResult.model_validate_json(screen_runs[0]["payload"])
        service.config = StrategyConfig.model_validate_json(screen_runs[0]["config"])
    with data_tab:
        st.subheader("数据覆盖与可追溯性")
        full_job = next((job for job in jobs.list() if job["config"].get("mainboard") and
                         not job["config"].get("limit") and not job["config"].get("symbols")), None)
        if full_job:
            with st.expander("全主板初始化与采集性能", expanded=full_job["status"] == "partial"):
                report = service.collection_report(full_job["job_id"])
                if report["processing_complete"]:
                    st.success(f"全主板股票处理已完成：{report['symbols_processed']}/{report['symbols_total']}，数据已保存。")
                else:
                    st.write(f"全主板股票处理：{report['symbols_processed']}/{report['symbols_total']}")
                st.caption(f"此处为全主板任务 {report['job_id']}；下方覆盖率对应当前所选快照。")
                if report["recovery"].get("message"):
                    st.info(report["recovery"]["message"])
                st.write("本地记录数：", report["local_tables"])
                if report["rollout"]:
                    st.caption(f"全主板连续达标交易日：{report['rollout']['consecutive_days']}/{report['rollout']['required_days']}；仅统计真实正式发布的交易日。")
                st.caption(f"任务历时 {report['wall_elapsed_seconds'] / 60:.1f} 分钟；累计请求耗时可重叠，不等于任务历时。")
                if report["methods"]:
                    st.dataframe(pd.DataFrame(report["methods"]), hide_index=True, width="stretch")
                st.caption("HTTP 字节不含未计量的 BaoStock TCP 字节；返回行数为空表示旧记录未计量。")
                st.download_button("下载全主板采集验收报告", json.dumps(report, ensure_ascii=False, indent=2),
                    file_name="collection_report.json", mime="application/json")
        st.write(bundle.meta.universe_scope)
        st.dataframe(pd.DataFrame([{"数据表": k, "记录数": v} for k, v in quality["table_rows"].items()]),
                     hide_index=True, width="stretch")
        coverage = quality.get("coverage", {})
        if coverage:
            st.metric("可完整判定的覆盖率", f"{coverage.get('coverage', 0):.2%}", help="已确认的 ST、新股等规则排除项不进入分母；数据缺失和接口失败仍进入分母。")
            st.caption(f"判定 {coverage.get('evaluated', 0)} / 应判定 {coverage.get('required', 0)} · 发布门槛 98%")
            industry = coverage.get("industries", {})
            if industry:
                st.dataframe(pd.DataFrame([{"行业": key, **value} for key, value in industry.items()]), hide_index=True, width="stretch")
            missing = coverage.get("missing", [])
            if missing:
                with st.expander(f"数据缺口（{len(missing)} 只）"):
                    st.dataframe(pd.DataFrame(missing), hide_index=True, width="stretch")
        for issue in quality["issues"]:
            st.write("• " + issue)
        st.write("尚未通过的历史核验项：", ", ".join(quality["missing_research_checks"]) or "无")
        st.download_button("下载数据质量报告", json.dumps(quality, ensure_ascii=False, indent=2),
                           file_name="quality.json", mime="application/json")
        with st.expander("查看行情与财务样本"):
            st.dataframe(bundle["prices"].tail(20), hide_index=True)
            st.dataframe(bundle["financials"].tail(20), hide_index=True)
    with rank_tab:
        st.subheader("固定规则候选股")
        st.caption("年报质量 40% · 趋势 40% · 估值 20%；同一快照与配置可复现。")
        if quality.get("publication_status") == "blocked":
            if any("no stock state known at cutoff" in issue for issue in quality["issues"]):
                st.error(f"股票状态首次取得日晚于 {bundle.meta.as_of}，当前快照不能用于该日选股。请从左侧查看最新可用榜单，或采集抓取当日收盘数据。")
            else:
                st.error("当前快照未通过批次校验。请查看“数据状态”中的原因，或从左侧切换到最新可用榜单。")
        if st.button("运行选股", type="primary", disabled=quality.get("publication_status") == "blocked"):
            try:
                selected = service.screen(snapshot_id, preview=not quality["publishable"])
                st.rerun()
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
        if selected:
            st.caption(f"选股记录：{selected.run_id} · {selected.publication_status} · 合格 {selected.eligible_count}/{selected.universe_count}")
            if selected.publication_status != "published":
                st.warning("当前榜单仅供预览，不能生成正式模拟调仓指令。")
            if selected.candidates:
                table = pd.DataFrame(selected.candidates)
                st.dataframe(table[["rank", "symbol", "name", "industry", "score", "roe", "revenue_growth",
                                    "momentum_60", "ma_ratio", "pe_ttm", "pb"]], hide_index=True, width="stretch")
                st.bar_chart(table.set_index("symbol")[["score"]])
                st.caption("候选行业分布（按股票数量）")
                st.bar_chart(table.groupby("industry").size().rename("数量"))
                st.download_button("下载候选股 CSV", table.to_csv(index=False).encode("utf-8-sig"),
                                   file_name="candidates.csv", mime="text/csv")
            else:
                st.info("没有股票满足当前数据与规则，请检查剔除原因。")
            st.download_button("下载完整选股记录", selected.model_dump_json(indent=2),
                               file_name="screening.json", mime="application/json")
            with st.expander(f"剔除原因（{len(selected.exclusions)} 只）"):
                st.dataframe(pd.DataFrame(selected.exclusions), hide_index=True, width="stretch")
        else:
            st.info("运行选股后，在此查看候选股与各因子得分。")
    with research_tab:
        st.subheader("有证据的中文研究卡")
        st.caption("点击生成才调用模型；刷新页面不会触发新请求。模型不会改变选股得分。")
        provider = st.selectbox("研究模型来源", ["ollama", "demo", "deepseek"],
                                format_func=lambda p: {"ollama": "Ollama · 本地模型", "demo": "离线模板 · 无模型调用", "deepseek": "DeepSeek · API"}[p])
        default_model = ChatAdapter(provider if provider != "demo" else "ollama")
        model = st.text_input("模型名称", value=default_model.model, disabled=provider == "demo")
        default_model.close()
        limit = st.number_input("本次生成股票数量", min_value=1, max_value=10, value=1)
        if st.button("生成研究卡", disabled=selected is None or not selected.candidates):
            with st.spinner("读取证据并生成报告…"):
                cards = service.research(selected.run_id, provider, model, int(limit))
            failed = sum(c.status == "failed" for c in cards)
            if failed:
                st.error(f"{failed} 张研究卡未成功生成，选股结果已保留。")
            else:
                st.success("研究卡已保存")
        if provider == "deepseek":
            usage = service.research_usage("deepseek", model)
            st.caption(f"本项目该模型已记录：输入 {usage['input_tokens']:,} + 输出 {usage['output_tokens']:,} = {usage['reported_tokens']:,} tokens；缓存复用 {usage['cache_hits']} 次。")
            st.caption("此计数包含失败研究记录返回的用量；不包含连接测试及其他应用，不能作为平台剩余额度。")
        research_runs = [r for r in service.store.runs("research") if selected and
                         json.loads(r["payload"]).get("screen_run_id") == selected.run_id]
        if research_runs:
            record = json.loads(research_runs[0]["payload"])
            from finsight.models import ResearchCard
            cards = [ResearchCard.model_validate(c) for c in record["cards"]]
            for card in cards:
                with st.expander(f"{card.symbol} · {card.model} · {card.status}", expanded=len(cards) == 1):
                    st.markdown(render_card(card))
                    if card.error:
                        with st.expander("诊断详情"):
                            st.code(card.error, language=None)
                    st.caption(f"模型请求 {card.llm_calls} · 工具执行 {card.tool_calls} · 缓存 {'命中' if card.cache_hit else '未命中'}")
                    st.caption(f"本次记录 tokens：输入 {card.input_tokens:,} · 输出 {card.output_tokens:,}")
            st.download_button("下载研究报告 Markdown", "\n\n---\n\n".join(render_card(c) for c in cards), file_name="research.md")
        else:
            st.info("先运行选股，再选择本地模型或离线模板生成报告。")
    with portfolio_tab:
        st.subheader("模拟组合与研究表现")
        capital = st.number_input("初始模拟资金（元）", min_value=1000.0, value=100000.0, step=10000.0)
        if selected:
            plan = portfolio_plan(selected, service.config, capital, bundle["calendar"])
            st.write("月度调仓状态：", "应在下一个交易日执行" if plan["rebalance_due"] else "非月末，当前仅供研究参考")
            st.dataframe(pd.DataFrame(plan["targets"])[["symbol", "industry", "target_weight", "reference_quantity"]]
                         if plan["targets"] else pd.DataFrame(), hide_index=True)
            st.download_button("下载组合计划", json.dumps(plan, ensure_ascii=False, indent=2), file_name="portfolio_plan.json")
        with st.expander("运行历史回测"):
            start = st.date_input("回测开始", value=date(2023, 1, 1))
            end = st.date_input("回测结束", value=min(date(2024, 12, 31), bundle.meta.as_of))
            if st.button("运行回测"):
                try:
                    with st.spinner("计算组合、动量基线和指数对照…"):
                        service.backtest(snapshot_id, start, end, capital)
                    st.success("回测报告已保存")
                except (ValueError, KeyError) as exc:
                    st.error(str(exc))
        tests = [r for r in service.store.runs("backtest") if r["snapshot_id"] == snapshot_id]
        if tests:
            bt = json.loads(tests[0]["payload"])
            st.write(bt["status"])
            curve = pd.DataFrame(bt["strategy"]["curve"])[["trade_date", "nav"]].merge(pd.DataFrame(bt["benchmark"]), on="trade_date")
            curve["momentum_nav"] = [p["nav"] for p in bt["momentum_baseline"]["curve"]]
            st.line_chart(curve.set_index("trade_date"))
            st.json(bt["strategy"]["metrics"])
            st.download_button("下载回测 JSON", json.dumps(bt, ensure_ascii=False, indent=2), file_name="backtest.json")
        st.divider()
        st.write("人工模拟交易日志")
        account = st.text_input("模拟账户标识", value="paper")
        st.caption("上方为目标仓位参考；账户调仓计划会使用日志还原的当前净值，扣除已有持仓后计算买卖差额。")
        if selected and st.button("生成账户调仓计划"):
            try:
                proposal = service.rebalance(selected.run_id, capital, account)
                label = {"blocked": "暂不可生成", "ready": "待人工核对", "reference": "非月末参考", "requires_review": "需要人工复核"}[proposal["status"]]
                st.write(f"账户净值 {proposal['account_nav']:,.2f} 元 · 计划状态：{label}")
                for blocker in proposal["blockers"]:
                    st.error(blocker)
                for warning in proposal["warnings"]:
                    st.warning(warning)
                if proposal["status"] == "reference":
                    st.info("非月末，仅保留目标参考，不生成买卖指令。")
                st.dataframe(pd.DataFrame(proposal["orders"]), hide_index=True)
                st.caption("提案按收盘价估算；次日重新核对资金、交易状态和价格。不会下单或写入交易日志。")
                st.download_button("下载账户调仓计划", json.dumps(proposal, ensure_ascii=False, indent=2), file_name="rebalance_plan.json")
            except ValueError as exc:
                st.error(str(exc))
        upload = st.file_uploader("导入日志 CSV（partial/filled 的数量均为本次增量成交量）", type=["csv"])
        if upload and st.button("导入交易日志"):
            try:
                st.json(import_journal(service.store, upload.getvalue().decode("utf-8-sig")))
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
        if selected and selected.candidates:
            event_key = f"manual_event_{account}"
            if event_key not in st.session_state:
                st.session_state[event_key] = uuid4().hex
            if st.button("使用新的事件编号"):
                st.session_state[event_key] = uuid4().hex
            with st.form("manual_trade"):
                event_id = st.text_input("事件编号（重复提交同一事件时保持不变）", key=event_key)
                order_id = st.text_input("委托编号（同一交易日内的计划、委托和成交保持一致）")
                symbol = st.selectbox("记录股票", sorted(set(bundle["universe"]["symbol"])))
                next_session = next((d for d in bundle["calendar"]["trade_date"] if d > str(bundle.meta.as_of)), None)
                trading_date = st.date_input("记录日期", value=date.fromisoformat(next_session) if next_session else bundle.meta.as_of)
                action = st.selectbox("方向", ["buy", "sell"])
                status = st.selectbox("事件状态", ["submitted", "filled", "partial", "cancelled", "rejected", "planned"])
                quantity = st.number_input("本次数量", min_value=0, value=100, step=100,
                    help="planned/submitted 填委托总量；partial/filled 填本笔增量成交量。每条新事件使用新的事件编号。")
                planned = st.number_input("计划价格", min_value=.01, value=10.0, step=.01)
                executed = st.number_input("实际成交价格（成交事件使用）", min_value=.01, value=10.0, step=.01)
                fee = st.number_input("实际费用", min_value=0.0, value=5.0, step=.01)
                reason = st.text_input("交易理由")
                risk = st.text_input("风险与执行偏差")
                if st.form_submit_button("保存日志事件"):
                    try:
                        entry = TradeJournalEntry(event_id=event_id.strip(), account_id=account,
                            order_id=order_id.strip() or None,
                            trade_date=trading_date, symbol=symbol, action=action, status=status,
                            quantity=int(quantity), planned_price=planned,
                            executed_price=executed if status in {"filled", "partial"} else None,
                            transaction_cost=fee if status in {"filled", "partial"} else 0,
                            run_id=selected.run_id, strategy_version=selected.strategy_version,
                            selection_reason=reason, risk_notes=risk)
                        stream = io.StringIO()
                        writer = csv.DictWriter(stream, fieldnames=list(entry.model_dump()))
                        writer.writeheader()
                        writer.writerow(entry.model_dump(mode="json"))
                        st.json(import_journal(service.store, stream.getvalue()))
                    except ValueError as exc:
                        st.error(str(exc))
        events = journal_entries(service.store, account)
        if events:
            st.dataframe(pd.DataFrame([e.model_dump(mode="json") for e in events]), hide_index=True)
            st.download_button("导出日志 CSV", pd.DataFrame([e.model_dump(mode="json") for e in events]).to_csv(index=False).encode("utf-8-sig"),
                               file_name="journal.csv", mime="text/csv")
        if st.button("生成持仓与周度检查报告"):
            try:
                report = service.portfolio_report(snapshot_id, capital, account)
                review = report["execution_review"]
                st.write(f"截至快照待处理委托 {review['open_order_count']} 笔 · 最近五个交易日成交事件 {review['fill_events']} 笔 · 实际费用 {review['transaction_cost']:.2f} 元")
                if review["orders"]:
                    st.dataframe(pd.DataFrame(review["orders"]), hide_index=True)
                st.json(report)
                if report["performance"]["curve"]:
                    st.line_chart(pd.DataFrame(report["performance"]["curve"]).set_index("trade_date")[["nav"]])
                st.download_button("下载检查报告", json.dumps(report, ensure_ascii=False, indent=2), file_name="portfolio_review.json")
            except ValueError as exc:
                st.error(str(exc))


if __name__ == "__main__":
    main()
