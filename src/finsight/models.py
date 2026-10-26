from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()[:20]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


FACTORS = ("roe", "revenue_growth", "momentum_60", "ma_ratio", "pe_ttm", "pb")
RESEARCH_CHECKS = (
    "historical_universe", "historical_status", "delistings", "financial_versions",
    "valuation_history", "corporate_actions", "adjustments",
)


class StrategyConfig(StrictModel):
    version: str = "strategy_v0.1"
    top_n: int = Field(default=10, ge=1, le=100)
    min_listing_years: int = Field(default=2, ge=2)
    min_amount_20: float = Field(default=50_000_000, ge=0)
    max_report_age_days: int = Field(default=550, gt=0)
    weights: dict[str, float] = Field(default_factory=lambda: dict(zip(FACTORS, [.2, .2, .2, .2, .1, .1])))
    max_holdings: int = Field(default=8, ge=1, le=8)
    target_weight: float = Field(default=.1, gt=0, le=.15)
    max_industry_names: int = Field(default=3, ge=1, le=3)
    max_stock_weight: float = Field(default=.15, gt=0, le=.15)
    max_equity_weight: float = Field(default=.8, gt=0, le=.8)

    @model_validator(mode="after")
    def validate_weights(self):
        if set(self.weights) != set(FACTORS) or any(w < 0 for w in self.weights.values()):
            raise ValueError("weights must contain the six nonnegative factor weights")
        if abs(sum(self.weights.values()) - 1) > 1e-9:
            raise ValueError("factor weights must sum to one")
        if self.max_holdings * self.target_weight > self.max_equity_weight + 1e-9:
            raise ValueError("target equity allocation exceeds the portfolio cap")
        if self.target_weight > self.max_stock_weight:
            raise ValueError("target weight exceeds the single stock cap")
        return self

    @property
    def config_id(self) -> str:
        return fingerprint(self.model_dump())


class MarketSnapshot(StrictModel):
    snapshot_id: str
    as_of: date
    captured_at: datetime
    source: str
    universe_scope: str
    synthetic: bool = False
    publishable: bool = True
    quality_issues: list[str] = Field(default_factory=list)
    research_checks: dict[str, bool] = Field(default_factory=lambda: dict.fromkeys(RESEARCH_CHECKS, False))
    table_hashes: dict[str, str] = Field(default_factory=dict)

    @property
    def research_eligible(self) -> bool:
        return not self.synthetic and self.publishable and all(
            self.research_checks.get(k, False) for k in RESEARCH_CHECKS
        )


class ScreeningResult(StrictModel):
    run_id: str
    snapshot_id: str
    as_of: date
    strategy_version: str
    config_id: str
    code_version: str
    candidates: list[dict[str, Any]]
    exclusions: list[dict[str, Any]]
    eligible_count: int
    universe_count: int
    synthetic: bool
    research_eligible: bool


class EvidenceClaim(StrictModel):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ResearchContent(StrictModel):
    summary: str = Field(min_length=1)
    reasons: list[EvidenceClaim] = Field(min_length=1)
    risks: list[EvidenceClaim] = Field(min_length=1)
    information_gaps: list[str] = Field(min_length=1)
    human_checks: list[str] = Field(min_length=1)


class ResearchCard(StrictModel):
    run_id: str
    symbol: str
    model: str
    prompt_version: str
    status: Literal["completed", "failed", "demo"]
    content: ResearchContent | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False


class TradeJournalEntry(StrictModel):
    event_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    account_id: str = Field(default="paper", pattern=r"^[A-Za-z0-9_.-]{1,60}$")
    order_id: str | None = None
    trade_date: date
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    action: Literal["buy", "sell"]
    status: Literal["planned", "submitted", "partial", "filled", "cancelled", "rejected"]
    quantity: int = Field(ge=0)
    planned_price: float | None = Field(default=None, gt=0)
    executed_price: float | None = Field(default=None, gt=0)
    transaction_cost: float = Field(default=0, ge=0)
    run_id: str
    strategy_version: str
    selection_score: float | None = Field(default=None, ge=0, le=100)
    selection_reason: str = ""
    risk_notes: str = ""

    @model_validator(mode="after")
    def validate_execution(self):
        if self.status in {"filled", "partial"}:
            if self.executed_price is None or self.quantity <= 0:
                raise ValueError("execution events require positive quantity and executed_price")
        return self
