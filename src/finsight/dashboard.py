"""Local single-user UI. All business operations go through the same service as the CLI."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from datetime import date
from uuid import uuid4

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from finsight.data.quality import audit
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
    with st.sidebar:
        st.header("研究工作区")
        st.caption("本地单用户 · strategy_v0.1")
        if st.button("创建离线演示数据", width="stretch"):
            with st.spinner("准备合成样例…"):
                created = service.sync("demo")
            st.session_state["snapshot_choice"] = created["snapshot_id"]
            st.rerun()
        st.caption("演示数据使用虚构公司和工作日日历。")
        universe = st.selectbox("真实数据范围", ["20 只样本", "沪深主板"])
        if st.button("更新 AKShare 数据", width="stretch"):
            try:
                with st.spinner("采集、校验并保存快照…"):
                    quality = service.sync("akshare", mainboard=universe == "沪深主板")
                st.session_state["snapshot_choice"] = quality["snapshot_id"]
                if quality["publishable"]:
                    st.success("快照已保存")
                else:
                    st.error("快照校验未通过，请查看数据状态。")
                st.rerun()
            except (ValueError, OSError, KeyError) as exc:
                st.error(str(exc))
        snapshots = service.store.snapshots()
        if not snapshots:
            st.info("先创建演示数据，或更新真实数据。")
            st.stop()
        by_id = {s.snapshot_id: s for s in snapshots}
        snapshot_id = st.selectbox("数据快照", list(by_id), format_func=lambda s: (
            f"{'演示' if by_id[s].synthetic else 'AKShare'} · {by_id[s].as_of} · {s[-6:]}"), key="snapshot_choice")
        st.caption(f"快照：{snapshot_id}")
    bundle = service.store.load_snapshot(snapshot_id)
    quality = audit(bundle)
    if bundle.meta.synthetic:
        st.info("当前为合成演示数据；图表与选股结果用于展示软件流程。")
    if not quality["research_eligible"]:
        st.warning("历史验证未通过 · 回测结果标记为研究演示。")
    columns = st.columns(4)
    columns[0].metric("数据日期", str(bundle.meta.as_of))
    columns[1].metric("股票范围", len(set(bundle["universe"]["symbol"])))
    columns[2].metric("行情记录", f"{len(bundle['prices']):,}")
    columns[3].metric("发布状态", "可选股" if quality["publishable"] else "数据待修复")
    data_tab, rank_tab, research_tab, portfolio_tab = st.tabs(["数据状态", "候选排行榜", "研究卡", "组合与绩效"])
    screen_runs = [r for r in service.store.runs("screen") if r["snapshot_id"] == snapshot_id]
    selected = None
    if screen_runs:
        selected = ScreeningResult.model_validate_json(screen_runs[0]["payload"])
        service.config = StrategyConfig.model_validate_json(screen_runs[0]["config"])
    with data_tab:
        st.subheader("数据覆盖与可追溯性")
        st.write(bundle.meta.universe_scope)
        st.dataframe(pd.DataFrame([{"数据表": k, "记录数": v} for k, v in quality["table_rows"].items()]),
                     hide_index=True, width="stretch")
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
        if st.button("运行选股", type="primary", disabled=not quality["publishable"]):
            try:
                selected = service.screen(snapshot_id)
                st.rerun()
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
        if selected:
            st.caption(f"选股记录：{selected.run_id} · 合格 {selected.eligible_count}/{selected.universe_count}")
            if selected.candidates:
                table = pd.DataFrame(selected.candidates)
                st.dataframe(table[["rank", "symbol", "name", "industry", "score", "roe", "revenue_growth",
                                    "momentum_60", "ma_ratio", "pe_ttm", "pb"]], hide_index=True, width="stretch")
                st.bar_chart(table.set_index("symbol")[["score"]])
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
        research_runs = [r for r in service.store.runs("research") if r["snapshot_id"] == snapshot_id]
        if research_runs:
            record = json.loads(research_runs[0]["payload"])
            from finsight.models import ResearchCard
            cards = [ResearchCard.model_validate(c) for c in record["cards"]]
            for card in cards:
                with st.expander(f"{card.symbol} · {card.model} · {card.status}", expanded=len(cards) == 1):
                    st.markdown(render_card(card))
                    st.caption(f"模型请求 {card.llm_calls} · 工具执行 {card.tool_calls} · 缓存 {'命中' if card.cache_hit else '未命中'}")
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
        upload = st.file_uploader("导入日志 CSV（partial/filled 的数量均为本次增量成交量）", type=["csv"])
        if upload and st.button("导入交易日志"):
            try:
                st.json(import_journal(service.store, upload.getvalue().decode("utf-8-sig")))
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
        if selected and selected.candidates:
            with st.form("manual_trade"):
                symbol = st.selectbox("记录股票", sorted(set(bundle["universe"]["symbol"])))
                next_session = next((d for d in bundle["calendar"]["trade_date"] if d > str(bundle.meta.as_of)), None)
                trading_date = st.date_input("记录日期", value=date.fromisoformat(next_session) if next_session else bundle.meta.as_of)
                action = st.selectbox("方向", ["buy", "sell"])
                status = st.selectbox("事件状态", ["submitted", "filled", "partial", "cancelled", "rejected", "planned"])
                quantity = st.number_input("本次数量", min_value=0, value=100, step=100)
                planned = st.number_input("计划价格", min_value=.01, value=10.0, step=.01)
                executed = st.number_input("实际成交价格（成交事件使用）", min_value=.01, value=10.0, step=.01)
                fee = st.number_input("实际费用", min_value=0.0, value=5.0, step=.01)
                reason = st.text_input("交易理由")
                risk = st.text_input("风险与执行偏差")
                if st.form_submit_button("保存日志事件"):
                    try:
                        entry = TradeJournalEntry(event_id=uuid4().hex, account_id=account,
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
                st.json(report)
                if report["performance"]["curve"]:
                    st.line_chart(pd.DataFrame(report["performance"]["curve"]).set_index("trade_date")[["nav"]])
                st.download_button("下载检查报告", json.dumps(report, ensure_ascii=False, indent=2), file_name="portfolio_review.json")
            except ValueError as exc:
                st.error(str(exc))


if __name__ == "__main__":
    main()
