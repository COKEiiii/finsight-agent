"""Fixed, reproducible 30-case local research evaluation; human review stays explicit."""
from __future__ import annotations

from copy import deepcopy

from finsight.data.store import Store
from finsight.models import ScreeningResult

SCENARIOS = ("actual_candidate", "missing_roe", "stale_report", "negative_growth", "unit_conversion")
CASE_VERSION = "research_cases_v0.3"


def build_cases(result: ScreeningResult, *, allow_demo: bool = False) -> list[dict]:
    if result.synthetic and not allow_demo:
        raise ValueError("30-case model evaluation requires real candidates; use allow_demo only for tests")
    if result.publication_status != "published":
        raise ValueError("evaluation requires a published screening run")
    if len(result.candidates) < 10:
        raise ValueError("evaluation needs ten screened candidates")
    cases = []
    for index, original in enumerate(result.candidates[:10]):
        for scenario in SCENARIOS:
            if scenario != "actual_candidate" and index >= 5:
                continue
            candidate = deepcopy(original)
            if scenario == "missing_roe":
                candidate.pop("roe", None)
            elif scenario == "stale_report":
                candidate["report_period"] = f"{result.as_of.year - 3}-12-31"
            elif scenario == "negative_growth":
                candidate["revenue_growth"] = -abs(float(candidate["revenue_growth"])) - 1
            elif scenario == "unit_conversion":
                candidate["amount_20"] = float(candidate["amount_20"]) * 10000
            cases.append({"case_id": f"{scenario}-{index + 1:02d}", "scenario": scenario,
                          "case_version": CASE_VERSION,
                          "symbol": candidate["symbol"], "candidate": candidate,
                          "source_run_id": result.run_id, "source_snapshot_id": result.snapshot_id,
                          "synthetic_modification": scenario != "actual_candidate"})
    if len(cases) != 30:
        raise AssertionError("evaluation suite must contain exactly 30 cases")
    return cases


def _scenario_pass(scenario: str, card) -> bool:
    if card.status != "completed" or card.content is None:
        return False
    if scenario == "missing_roe":
        text = " ".join(card.content.information_gaps + [item.text for item in card.content.risks])
        return "ROE" in text.upper() and "无法判断" in text
    if scenario == "stale_report":
        text = " ".join(card.content.information_gaps + [item.text for item in card.content.risks])
        return any(word in text for word in ("年报", "财报", "报告期")) and any(word in text for word in ("陈旧", "过期", "时效"))
    if scenario == "negative_growth":
        text = " ".join(item.text for item in card.content.risks)
        return any(word in text for word in ("营收", "收入")) and any(word in text for word in ("下降", "负增长", "下滑"))
    return True


def evaluate_run(store: Store, run_id: str, *, models: tuple[str, ...] = (),
                 limit: int = 30, allow_demo: bool = False) -> dict:
    from finsight.evaluation_jobs import prepare_suite, run_suite, suite_status
    suite = prepare_suite(store, run_id, models=models, limit=limit, allow_demo=allow_demo)
    if models:
        return run_suite(store, suite["suite_id"])
    return suite_status(store, suite["suite_id"])
