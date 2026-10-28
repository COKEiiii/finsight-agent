"""Same-snapshot ranking diagnostics, with no return attribution or tuning."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone

import pandas as pd

from finsight.data.store import Store, code_version
from finsight.factors import coverage_report, evaluate
from finsight.models import FACTORS, ScreeningResult, StrategyConfig, fingerprint
from finsight.portfolio import select_targets
from finsight.screening import rank_factors

FACTOR_LABELS = {"roe": "年报 ROE", "revenue_growth": "年营收同比", "momentum_60": "60 日收益率",
    "ma_ratio": "MA20/MA60−1", "pe_ttm": "PE-TTM", "pb": "PB"}
VERSION = "ranking_diagnostics_v1"


def diagnose_run(store: Store, run_id: str) -> dict:
    source = store.run(run_id)
    if source["kind"] != "screen":
        raise ValueError("diagnostics requires a screening run")
    result = ScreeningResult.model_validate(source["payload"])
    config = StrategyConfig.model_validate(source["config"])
    if (result.run_id != run_id or result.snapshot_id != source["snapshot_id"] or
            result.config_id != config.config_id or result.strategy_version != config.version):
        raise ValueError("screening/configuration binding mismatch")
    calendar = store.load_snapshot(result.snapshot_id, table_names=("calendar",))["calendar"]
    sessions = sorted(calendar.loc[calendar["trade_date"] <= str(result.as_of), "trade_date"])
    lower = sessions[max(0, len(sessions) - 61)] if sessions else str(result.as_of)
    bundle = store.load_snapshot(result.snapshot_id, date_range=(lower, str(result.as_of)))
    frame, exclusions = evaluate(bundle, result.as_of, config)
    ranked = rank_factors(frame, config)
    coverage = coverage_report(bundle, frame, exclusions)
    # A diagnostic must explain the saved ranking, not silently recompute a different strategy/version.
    if (ranked.head(config.top_n).to_dict("records") != result.candidates or exclusions != result.exclusions or
            coverage["eligible"] != result.eligible_count or
            coverage["required"] + coverage["confirmed_exclusions"] != result.universe_count):
        raise ValueError("saved screening no longer matches recomputed snapshot/config; generate a new screening run")
    contributions = []
    for row in result.candidates:
        values = {factor: row[factor + "_score"] * config.weights[factor] for factor in FACTORS}
        contributions.append({"rank": row["rank"], "symbol": row["symbol"], "name": row["name"],
            "score": row["score"], **{factor + "_contribution": value for factor, value in values.items()}})
    matrix = []
    scores = [factor + "_score" for factor in FACTORS]
    # Pearson correlation of the average percentile ranks is Spearman correlation; no scipy dependency.
    correlation = ranked[scores].corr(method="pearson") if len(ranked) > 1 else None
    pairs = []
    for i, factor in enumerate(FACTORS):
        row = {"factor": factor}
        for j, other in enumerate(FACTORS):
            value = float(correlation.loc[scores[i], scores[j]]) if correlation is not None else math.nan
            row[other] = value if math.isfinite(value) else None
            if j > i and math.isfinite(value) and abs(value) >= .8:
                pairs.append({"factor_a": factor, "factor_b": other, "correlation": value})
        matrix.append(row)
    eligible = Counter(ranked["industry"]) if not ranked.empty else Counter()
    candidates = Counter(row["industry"] for row in result.candidates)
    targets = select_targets(result.candidates, config)
    target_names = Counter(row["industry"] for row in targets)
    industries = [{"industry": name, "eligible_names": eligible[name],
        "eligible_share": eligible[name] / len(ranked), "candidate_names": candidates[name],
        "candidate_share": candidates[name] / len(result.candidates) if result.candidates else 0,
        "target_names": target_names[name], "reference_target_weight": target_names[name] * config.target_weight}
        for name in sorted(eligible, key=lambda name: (-candidates[name], name))]
    payload = {"screen_run_id": run_id, "snapshot_id": result.snapshot_id, "as_of": str(result.as_of),
        "strategy_version": config.version, "config_id": config.config_id, "diagnostic_version": VERSION,
        "source_code_version": result.code_version, "code_version": code_version(),
        "source_fingerprint": fingerprint(source["payload"]), "generated_at": datetime.now(timezone.utc).isoformat(),
        "synthetic": result.synthetic, "publication_status": result.publication_status,
        "historical_validation_passed": result.research_eligible, "eligible_count": len(ranked),
        "coverage": {key: coverage[key] for key in ("coverage", "required", "evaluated", "confirmed_exclusions", "missing_count")},
        "factor_weights": config.weights, "factor_contributions": contributions,
        "factor_rank_correlation": matrix, "high_correlation_pairs": sorted(pairs, key=lambda row: -abs(row["correlation"])),
        "industry_exposure": industries, "reference_target_cash_weight": 1 - sum(row["target_weight"] for row in targets),
        "model_calls": 0, "limitations": ["仅解释当日合格股票池与排名分数，不是收益归因、因果关系或策略有效性验证。",
            "相关性基于合格股票池的平均百分位分数（估值方向已反转）；常数因子或少于两只股票时无法计算。",
            "行业目标仓位为固定规则参考，未使用账户实际持仓、资金、费用或成交；不是实际行业暴露或调仓指令。",
            "缺失数据股票未进入相关性与行业分母；行业覆盖率仍须结合数据质量报告阅读。"]}
    diagnostic_id = store.save_run("diagnostics", result.snapshot_id, config, payload)
    folder = store.artifact_dir(diagnostic_id)
    payload.update(run_id=diagnostic_id, exports=str(folder))
    (folder / "diagnostics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    for name, fields in (("factor_contributions", ["rank", "symbol", "name", "score", *[f + "_contribution" for f in FACTORS]]),
                         ("industry_exposure", ["industry", "eligible_names", "eligible_share", "candidate_names", "candidate_share", "target_names", "reference_target_weight"]),
                         ("factor_rank_correlation", ["factor", *FACTORS])):
        pd.DataFrame(payload[name], columns=fields).to_csv(folder / (name + ".csv"), index=False, encoding="utf-8-sig")
    (folder / "diagnostics.md").write_text(render_diagnostics(payload), encoding="utf-8")
    return payload


def render_diagnostics(report: dict) -> str:
    labels = FACTOR_LABELS
    lines = ["# FinSight 选股诊断", f"选股记录：{report['screen_run_id']} · 快照：{report['snapshot_id']}",
        f"日期：{report['as_of']} · 策略：{report['strategy_version']} · 合格股票：{report['eligible_count']}",
        *["- " + text for text in report["limitations"]], "", "## 候选分数贡献（分）",
        "| 股票 | 总分 | " + " | ".join(labels.values()) + " |", "|---|---:|" + "---:|" * len(FACTORS)]
    for row in report["factor_contributions"]:
        lines.append(f"| {row['symbol']} {row['name']} | {row['score']:.4f} | " +
            " | ".join(f"{row[f + '_contribution']:.4f}" for f in FACTORS) + " |")
    lines += ["", "## 行业分布与目标参考", "| 行业 | 合格股票 | 候选股票 | 目标参考仓位 |", "|---|---:|---:|---:|"]
    for row in report["industry_exposure"]:
        lines.append(f"| {row['industry']} | {row['eligible_names']} | {row['candidate_names']} | {row['reference_target_weight']:.1%} |")
    lines += ["", f"目标参考现金：{report['reference_target_cash_weight']:.1%}", "", "## 相关性绝对值 ≥ 0.8 的因子对"]
    for row in report["high_correlation_pairs"]:
        lines.append(f"- {labels[row['factor_a']]} / {labels[row['factor_b']]}：{row['correlation']:.4f}")
    if not report["high_correlation_pairs"]:
        lines.append("无可报告的因子对；常数因子、样本不足与未达阈值需结合完整相关矩阵区分。")
    return "\n".join(lines)
