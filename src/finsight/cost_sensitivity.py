"""Predeclared execution-cost experiments on one immutable snapshot and strategy."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from finsight.backtest import Commission, run_backtest
from finsight.data.store import code_version
from finsight.models import fingerprint

VERSION = "cost_sensitivity_v1"
PROFILES = (
    ("base", "固定基准", .0003, 5.),
    ("commission_low", "较低佣金", .0001, 5.),
    ("commission_high", "较高佣金", .001, 5.),
    ("slippage_zero", "零滑点假设", .0003, 0.),
    ("slippage_high", "较高滑点", .0003, 20.),
    ("combined_stress", "佣金与滑点同时提高", .001, 20.),
)
PORTFOLIOS = ("strategy", "momentum_baseline")


def compare_costs(bundle, config, start, end, capital=100_000):
    profiles = [{"id": key, "label": label, "commission_rate": rate,
                 "minimum_commission": 5., "slippage_bps": slippage} for key, label, rate, slippage in PROFILES]
    scenarios, rows = [], []
    references = {}
    for profile in profiles:
        result = run_backtest(bundle, config, start, end, capital,
            commission=Commission(profile["commission_rate"], profile["minimum_commission"]),
            slippage_bps=profile["slippage_bps"])
        scenarios.append({"profile": profile, "backtest": result})
        for portfolio in PORTFOLIOS:
            simulation = result[portfolio]
            fills = [row for row in simulation["trades"] if row["status"] == "filled"]
            row = {"scenario": profile["id"], "label": profile["label"], "portfolio": portfolio,
                **{key:profile[key] for key in ("commission_rate", "minimum_commission", "slippage_bps")},
                **simulation["metrics"], "ending_nav": simulation["curve"][-1]["nav"],
                "fill_count": len(fills), "cancelled_count": len(simulation["trades"]) - len(fills),
                "executed_notional": sum(fill["notional"] for fill in fills),
                "informative": bool(fills), "historical_validation_passed": result["research_eligible"]}
            if profile["id"] == "base":
                references[portfolio] = row
            base = references[portfolio]
            row.update(return_difference_vs_base=row["cumulative_return"] - base["cumulative_return"],
                fee_difference_vs_base=row["transaction_costs"] - base["transaction_costs"],
                nav_difference_vs_base=row["ending_nav"] - base["ending_nav"],
                fill_difference_vs_base=row["fill_count"] - base["fill_count"])
            rows.append(row)
    all_passed = all(item["backtest"]["research_eligible"] for item in scenarios)
    sessions = sorted(day for day in bundle["calendar"]["trade_date"].astype(str) if str(start) <= day <= str(end))
    price_days = set(bundle["prices"]["trade_date"].astype(str))
    benchmark_days = set(bundle["benchmark"]["trade_date"].astype(str))
    quality = {"price_sessions_missing": [day for day in sessions if day not in price_days],
        "benchmark_sessions_missing": [day for day in sessions if day not in benchmark_days],
        "historical_checks": dict(bundle.meta.research_checks), "source_issues": list(bundle.meta.quality_issues)}
    for row in rows:
        row.update(snapshot_id=bundle.meta.snapshot_id, strategy_version=config.version, config_id=config.config_id,
            start=str(start), end=str(end), initial_capital=capital, profile_fingerprint=fingerprint(profiles),
            price_sessions_missing=len(quality["price_sessions_missing"]),
            benchmark_sessions_missing=len(quality["benchmark_sessions_missing"]))
        row["informative"] = row["informative"] and not quality["price_sessions_missing"]
    informative = all(row["informative"] for row in rows) and not quality["price_sessions_missing"]
    return {"version": VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version(), "snapshot_id": bundle.meta.snapshot_id,
        "snapshot_as_of": str(bundle.meta.as_of),
        "snapshot_table_hashes": dict(bundle.meta.table_hashes), "universe_id": bundle.meta.universe_id,
        "membership_id": bundle.meta.membership_id, "strategy_version": config.version, "config_id": config.config_id,
        "strategy_config": config.model_dump(),
        "start": str(start), "end": str(end), "initial_capital": capital, "profiles": profiles,
        "profile_fingerprint": fingerprint(profiles), "scenario_count": len(scenarios),
        "synthetic": bundle.meta.synthetic, "historical_validation_passed": all_passed,
        "status": "historical_checks_passed" if all_passed else "研究演示：历史验证未通过",
        "informative": informative, "summary": rows, "scenarios": scenarios,
        "input_quality": quality,
        "model_calls": 0, "market_requests": 0,
        "limitations": ["六组执行费用假设预先固定，策略与权重保持冻结；每组同时重跑多因子组合和纯动量对照。",
            "佣金按每笔最低五元与设定费率取较大值，税费沿用生效日期研究规则表；实际费用应以模拟盘日志为准。",
            "各情景重新计算现金、持仓、整手数量与未成交；成交路径可能改变，收益差异不保证随费用单调变化。",
            "零滑点情景仍收佣金和税费；各情景的滑点体现在成交价格和净值，交易费用列只统计佣金与税费。",
            "没有模拟成交的情景标记为无法判断成本影响；短区间、数据缺口和历史验证状态需同时阅读。",
            "情景对比不自动选择最优参数，不修改正式策略、历史报告、前瞻记录或人工交易日志。"]}


def render_costs(report):
    lines = ["# FinSight 交易成本敏感性", report["status"],
        f"快照：{report['snapshot_id']} · 策略：{report['strategy_version']} · 配置：{report['config_id']}",
        f"区间：{report['start']} 至 {report['end']} · 起始资金：{report['initial_capital']:,.2f} 元", "",
        "| 情景 | 组合 | 佣金费率 | 单边滑点 bp | 成交笔数 | 佣金与税费 | 累计收益 | 对基准收益差（百分点） |",
        "|---|---|---:|---:|---:|---:|---:|---:|"]
    for row in report["summary"]:
        label = "多因子" if row["portfolio"] == "strategy" else "纯动量"
        lines.append(f"| {row['label']} | {label} | {row['commission_rate']:.4%} | {row['slippage_bps']:g} | "
            f"{row['fill_count']} | {row['transaction_costs']:.2f} | {row['cumulative_return']:.2%} | "
            f"{row['return_difference_vs_base'] * 100:+.4f} |")
    if not report["informative"]:
        lines += ["", "存在无成交情景或缺少日行情，无法据此判断成本影响；请检查信号区间、数据与未成交原因。"]
    lines += ["", f"缺少整日行情：{len(report['input_quality']['price_sessions_missing'])} 个交易日；"
        f"缺少指数数据：{len(report['input_quality']['benchmark_sessions_missing'])} 个交易日。"]
    lines += ["", *["- " + note for note in report["limitations"]]]
    return "\n".join(lines) + "\n"


def export_costs(store, config, report):
    run_id = store.save_run("cost_sensitivity", report["snapshot_id"], config, report)
    folder = store.artifact_dir(run_id)
    (folder / "cost_sensitivity.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (folder / "cost_sensitivity.md").write_text(render_costs(report), encoding="utf-8")
    pd.DataFrame(report["summary"]).to_csv(folder / "cost_sensitivity.csv", index=False, encoding="utf-8-sig")
    return {**report, "run_id": run_id, "exports": str(folder)}
