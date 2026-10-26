# Architecture and acceptance

## Boundaries

The `FinSight` service is shared by the CLI and the Streamlit application. Screening and portfolio construction are Python-only. Research adapters translate the same bounded evidence workflow into Ollama or DeepSeek wire formats. Financial tools have no filesystem, shell, order submission, network search, or ranking mutation capability.

Existing root-level learning files remain callable. Their executor accepts an injected registry; the financial research workflow uses its own narrowly scoped tools and processes every call in a model turn. It preserves tool IDs for the DeepSeek protocol.

## Storage and provenance

- `snapshots/<id>/`: immutable Parquet tables, metadata, hashes, quality report.
- `raw/<request-hash>/`: successful upstream responses and capture-time request metadata. Cache identity includes the capture day; later captures preserve earlier raw versions.
- `finsight.sqlite`: snapshots, immutable run payloads, strategy definitions, research cache, journal events.
- `exports/<run-id>/`: CSV, JSON and Markdown artifacts.

Every screening record identifies its snapshot, strategy configuration and code revision. The code revision contains the Git HEAD and a digest of working changes, so uncommitted local development is distinguished from a clean release. Strategy registration compares complete validated configurations and prevents accidental reuse of a version with different settings.

Financial revisions preserve first-observed availability. An announcement date alone does not prove that the returned values are the originally published ones. A freshly retrieved value is usable no earlier than its first observation. Existing captured states/financial versions are merged forward; values are never silently backdated.

## Financial assumptions

- Annual ROE and annual revenue growth share the latest available annual reporting period. Growth may be negative; PE-TTM and PB must be positive. Incomplete 61-session price windows are excluded.
- Percentile rank is Pandas average rank divided by eligible count, multiplied by 100. Lower positive PE/PB receive higher ranks. Cross-industry valuation differences remain visible through industry exposures.
- Orders use preceding month-end signals and reference close prices to determine shares. Execution uses the next trading session's raw opening price with adverse tick-rounded slippage; no current close is available to execution logic.
- Buys obey lot sizes, cash and exposure limits. Sells use FIFO sellable lots, never same-day acquisitions. Unexecuted orders expire that day; they are not silently carried to the following session.
- Dividends are receivables from ex-date until payment. Splits adjust existing lots. Unsupported fractional entitlement settlement fails explicitly. Default results use gross dividends without individual dividend tax.
- Unknown limit prices cancel simulated orders. The live AKShare adapter currently supplies no verified limit-price or corporate-action series. This prevents historical real-data fills from being represented as credible trading outcomes.
- Benchmarks distinguish 100% HS300 price index and an initial 80% index/20% cash allocation. The index excludes dividends. Momentum uses the same eligibility universe and execution assumptions.

## Acceptance gates

Product checks exercise the complete offline workflow and the two provider protocols without paid network calls. Demo fixtures are synthetic and use weekdays rather than a real exchange calendar.

Historical research cannot pass unless all named data checks pass: historical universe, historical state, delistings, original financial versions, dated valuations, corporate actions and adjustment integrity. Synthetic data never passes. Current live ingestion leaves these checks false; there is no CLI switch to bypass them. Stale held prices and missing benchmark observations also prevent a historical run from passing.

The current release supports building and auditing the workflow. It does not provide a historically certified free A-share dataset. Eight to twelve weeks of prospective snapshots and actual simulated executions are a separate observation phase, not a substitute label for historical validation.

Evidence IDs and structured schemas are mechanically validated. Semantic correctness of model prose still requires human spot checks; citations alone do not prove an interpretation correct. Local model failures are saved as failed cards and do not replace or modify screening results.

## Follow-up versions

After frozen-version prospective observation, compare three analyst roles against the single-agent baseline for factual accuracy, evidence completeness, reading time and runtime. Keep quantitative portfolio selection fixed during that comparison. Industry-relative valuation or different weights require a new strategy version and an independently evaluated experiment.
