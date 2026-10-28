"""Fixed, reproducible 30-case local research evaluation; human review stays explicit."""
from __future__ import annotations

from copy import deepcopy

from finsight.data.store import Store
from finsight.models import ScreeningResult
from finsight.research_requirements import discloses_missing, number, observed_decline, warns_stale

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


def scenario_check(scenario: str, content, facts) -> bool:
    """Use the same cited disclosures as generation, for live and offline scoring."""
    if scenario == "missing_roe":
        return any(discloses_missing(text, "financials.roe") for text in content.information_gaps)
    if scenario == "stale_report":
        return any(warns_stale(claim) for claim in content.risks)
    if scenario == "negative_growth":
        return any(observed_decline(claim) for claim in content.risks)
    if scenario == "unit_conversion":
        fact = facts.get("market.amount_20", {})
        return fact.get("unit") == "元/日" and number(fact.get("value"))
    return scenario == "actual_candidate"


def _scenario_pass(scenario: str, card) -> bool:
    return card.status == "completed" and card.content is not None and scenario_check(scenario, card.content, card.facts)


def evaluate_run(store: Store, run_id: str, *, models: tuple[str, ...] = (),
                 limit: int = 30, allow_demo: bool = False) -> dict:
    from finsight.evaluation_jobs import prepare_suite, run_suite, suite_status
    suite = prepare_suite(store, run_id, models=models, limit=limit, allow_demo=allow_demo)
    if models:
        return run_suite(store, suite["suite_id"])
    return suite_status(store, suite["suite_id"])
