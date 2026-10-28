# Local operation

## Daily collection

Run from the repository root with the project's Python environment. `finsight watch --once` checks the real exchange calendar and skips a date that already has a publishable snapshot. After 18:00 Asia/Shanghai it may capture the current session; earlier runs can only target the previous completed session. It never substitutes a weekday calendar for an unavailable exchange calendar.

For a simple foreground process:

```bash
source .venv/bin/activate
finsight watch
```

For macOS, use `finsight schedule install` to install and start the collector, and `finsight schedule status` to inspect it. The installer preserves an existing schedule if startup fails, and refuses to overwrite a schedule for another checkout.

For manual macOS launchd setup, customize the repository and executable paths in `scripts/com.finsight.daily.plist.example`, copy it into `~/Library/LaunchAgents/com.finsight.daily.plist`, and load it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.finsight.daily.plist
```

Remove the job using `launchctl bootout gui/$(id -u)/com.finsight.daily`. The supplied job runs at login and daily at 18:05; the timestamp is local system time, so use it on a machine set to Singapore/China time. Runtime checks use Asia/Shanghai explicitly. Installation stores the runtime package path in PYTHONPATH. Source/editable installs follow source updates; reinstall a non-editable package after changing source. No API keys are written into the plist.

Linux/macOS cron alternative (replace paths):

```cron
5 18 * * 1-5 cd /absolute/path/finsight-agent && /absolute/path/finsight-agent/.venv/bin/finsight watch --once >> /absolute/path/finsight-agent/data/finsight-watch.log 2>&1
```

Collection after a missed day downloads available historical bars but cannot reconstruct that day's uncaptured financial revisions or stock state. Quality reports keep gaps visible. Completed collection jobs automatically validate and screen the snapshot. Subthreshold results are marked as previews. Model calls and trading remain manual.

## Mainboard jobs and recovery

Collection jobs expose `observed_status`, `process_alive` and `interrupted` separately from the stored `status`. A missing coordinator is displayed as interrupted even if a database outage prevented it from recording failure. Observation is read-only; it does not silently restart workers or change the signal cutoff. Interrupted elapsed time ends at the last stored update, not an invented exit time. Verify the original PID has gone before explicitly resuming its checkpoints. A database-open failure must be diagnosed separately; correcting the status display does not establish its underlying cause.

Start with `sync --provider akshare --universe mainboard --limit 100 --background`, then validate 500 stocks before a complete initialization. Use `jobs list`, `jobs show JOB_ID` and `jobs resume JOB_ID` to inspect and resume the same durable job. Successful raw responses retain their original observation time. Resume retries failed requests and reuses successful checkpoints; a new trading date creates a new snapshot with an explicit universe definition and member hash.

A finalized snapshot blocked because its first stock-state observations came after the cutoff cannot be repaired by replaying the same checkpoints. Its recovery action is a new incremental collection after a new session completes. Creating that job reuses the saved data bottom-up. Source failures and interrupted jobs retain normal checkpoint recovery. A partial snapshot with no failed source requests also requires a fresh incremental check instead of a pointless replay.

Before the next eligible session closes, `watch --once` reports `waiting_new_session`; an ordinary `sync` also rejects reinitializing the already completed temporal failure before launching workers. An explicit `--start` still permits an intentional historical-range experiment; it does not grant publication or historical-validation permission.

`jobs profile JOB_ID` exports JSON, Markdown and per-endpoint CSV under a versioned run directory. New completed collections export the same report automatically and save its run ID in job progress. The Dashboard shows completed stock processing separately from publication coverage and the five-session rollout gate. Terminal elapsed time stops at `finished_at` (older jobs fall back to their last update), even when a later report link is attached.

SQLite migration 5 adds nullable `response_rows` to request records. Legacy counts stay unknown; new responses and cache reads record the returned frame size without changing its original fetch time. Forced refresh only bypasses a stale checkpoint/cache when the valuation endpoint lacks the requested session. Old content-addressed raw files remain available. Measured HTTP bytes exclude unmeasured BaoStock TCP payloads; cumulative request time can overlap and is not total wall time.

Prices and valuations are stored locally as immutable Parquet partitions referenced by snapshot manifests. Daily jobs request the missing range plus ten overlapping sessions to check revisions. Eastern Money's valuation endpoint still returns the stock's entire valuation history before local slicing; the BaoStock alternate requests a date range. Industry classification is cached for seven days. Financial metrics are checked daily and retain the original weighted annual ROE definition; BaoStock average ROE is not accepted as an equivalent.

The first complete mainboard snapshot enables a separate 18:15 pilot job. The existing 18:05 sample collector is promoted only after five distinct consecutive real mainboard sessions publish at or above 98% coverage. Batch validation, previews and demo data do not count. A launchd setup error is reported separately from an already saved ranking.

## Reproducible use

After installing a new version or changing Python data models, stop and restart the existing Streamlit process before refreshing the browser. A browser refresh or a healthy `/_stcore/health` response does not prove that the server has reloaded its imported models. A stale process can report extra-field validation errors when opening newer snapshots. The data collector runs separately and does not need to be stopped for a UI restart. Keep dashboard output in `data/finsight/dashboard.log` when running it in the background.

1. Sync a snapshot and review its quality report.
2. Run screening against its explicit snapshot ID; save the run ID.
3. Generate local research cards for that run. Start with `--limit 1`.
4. Review the month-end portfolio plan and enter orders manually in the simulation account.
5. Record all order/partial-fill/cancellation events with stable event IDs and actual fees. Each partial/filled record represents an incremental execution, not a cumulative order total.
6. Reconcile the complete journal from an initially empty account, supplying initial cash and optional actual cash/holdings.
7. Save exports and review data, strategy, execution and human deviations weekly. Parameters remain frozen; changes get a new version.

Run `finsight --data-dir PATH ...` to isolate experiments. Database/Parquet files are local and excluded from Git. For backups, stop collection/UI writes and copy the entire data directory so SQLite, snapshots and manifests remain consistent.

Order lifecycle reports group events by account, trading date and `order_id`. Planned/submitted quantities are order totals; partial/filled quantities are incremental executions. Import in actual event order. Cancelling a partially filled order closes its remaining quantity without reversing fills. A reused broker ticket on another date is a separate order. An identity conflict within a ticket (symbol, direction, linked signal or strategy) rolls back the entire import. Legacy events without a ticket remain explicitly unlinked.

The manual form keeps a stable `event_id` for retrying the same submission. Choose a new event ID for the next lifecycle event, keeping its order ID unchanged. Order aggregates and executions are separate exports; use the Dashboard's original event CSV for journal re-import (`journal export` currently prints JSON). The older `unexecuted_events` JSON field remains a count of non-fill events for compatibility; use `execution_review.open_order_count` for currently pending orders. Weekly execution metrics use the latest five saved trading sessions, while order status is cumulative through the snapshot cutoff. Missing plan prices and absent account reconciliation inputs stay unknown.

Holding marks must be positive and finite; unavailable marks stop reconciliation rather than fabricating account values. Industry exposure uses only states known by the report cutoff. A historical selection audits the complete source snapshot, then computes factors at the requested date; this does not grant historical research eligibility or permit future stock states to become visible.

Use `journal plan --run SCREEN_RUN_ID --account paper --capital 100000` for account-aware monthly proposals, or the equivalent Dashboard action. Starting cash is not current NAV: the service reconstructs the account from one fixed journal view, values it at the screening snapshot cutoff, and subtracts existing positions from NAV-based targets. The stored signal configuration is used even when the current CLI configuration differs. Rebalance JSON records journal event IDs and their fingerprint so later imports cannot silently alter the original proposal.

Orders are proposed only at month-end for the next saved trading session. Non-month-end runs export a reference and an empty CSV with headers; unresolved/pending orders, later journal events, missing current holding marks, unknown industries or unpublished signals block new orders. Exit proposals come first, followed by ranked buy differences, with whole-lot/odd-lot rules, dated research fees, slippage, cash and post-cost position caps. Estimated sale proceeds are conditional; buys that depend on them are labeled. Dividend receivables contribute to NAV but never to spendable cash. CLI returns 2 for blocked/review-required proposals. No proposal inserts journal events or sends orders. Recheck actual opening prices, suspension, price limits, T+1, corporate actions and available funds before manual execution.

## Local observations across validation batches

A new universe can reuse per-stock prices, valuations and financial observations from up to eight recent real universe definitions. Synthetic data and future captures are excluded. Only data tables are shared: target membership, current names and risk states are collected again. `seed_snapshot_ids` in the job configuration and `data_parent_snapshot_ids` in the snapshot preserve lineage. An existing snapshot for the same definition remains the primary incremental base.

Unchanged numerical observations retain their original fetch time even when Parquet or nullable values change integer/float representation. The current 20-stock scheduler also uses exchange identities and the common industry source when the individual metadata endpoint is unavailable.

## Provider failures

Upstream requests run in terminable subprocesses, have finite retries, and only successful responses enter the raw cache. Whole-batch failures return a nonzero exit code. Partial per-stock failures are shown in quality/exclusion reports. A real source is never replaced with synthetic data automatically.

The default 20-stock sample is a data integration probe, not a representative investable universe or evidence of stock-selection skill. Full mainboard collection is opt-in and may take considerable time due to upstream rate limits.

Ollama must already be serving a downloaded tool-capable model. Check with `ollama list`. Model/schema errors produce failed cards with request counters. DeepSeek is only contacted when explicitly selected by configuration or CLI; no test requires a paid API key.

Qwen 3.5/3.6 final structured answers use Ollama's `/api/generate` endpoint to avoid chat-parser compatibility problems. Thinking stays disabled and the same three-request / 1,500-output-token limits apply. Other local models keep `/api/chat`; tool turns and DeepSeek retain their provider-specific formats. Endpoint compatibility does not replace application-side validation.

Current field-level research asks the model for qualitative explanations. Exact quantities are represented by field tokens and observed signs in the model input; original values, units, dates, provenance and snapshot IDs remain in the saved card. If a token is used, Python renders its original value only after checking the metric and citation. The moving-average ratio compares two averages, not closing price versus an average. Failed cards show a Chinese explanation and expandable technical diagnostics. Refreshing the page does not call the model; changing the prompt creates a separate cache entry, and a cache hit is revalidated before reuse.

Field-level repair uses the original evidence and the current list of validation failures, without replaying invalid assistant JSON. This reduces repeated copying of the same incorrect answer and lets independent issues be corrected together within the request budget.

The default local model is `qwen3:8b`. If free explanations still fail, the final request uses a constrained schema of grounded text/citation pairs. A completed card explicitly declares template assistance; numbers and field-presence gaps are rendered by Python. This path still requires a valid model response within the same three requests and never turns a network failure into a successful card. The smaller `qwen3.5:4b` remains selectable but has not shown stable factual compliance in the local real-case probes.

DeepSeek-compatible platforms can be selected with `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` and `DEEPSEEK_API_KEY`. Reload the application process after updating its environment; the default provider remains local unless explicitly selected. A credentials check does not require generating a full research batch. `finsight usage --provider deepseek [--model NAME]` reads saved research records without making an API request. Counts include reported usage from failed cards, and cached generation counters are not charged again. These project records exclude standalone connection probes, other applications and unreported failed requests; they do not query or enforce the platform's remaining allowance.

Human verification questions such as asking whether valuation is reasonable are checked as questions, while unsupported conclusions remain rejected. Explicit statements that strategy rank does not represent market capitalization are not treated as invented market-cap data. Price-versus-average checks stop at clause boundaries and a new average subject, so a positive price trend followed by a comparison of two averages is not misread as a closing-price comparison.

## Local thirty-case model comparison

`evaluate --audit SUITE_ID` revalidates an ended suite offline into a separate audit run. The original checkpoint, benchmark scores and manual-review CSV are preserved byte-for-byte. Bindings and stored facts (including dates, units and provenance) must match the frozen cases. Original failed responses that now validate remain original failures in the baseline; the audit reports their recoverability separately. The versioned rubric recognizes report-specific “过时” wording, explicit missing-ROE disclosure, and observed negative growth in risks rather than only reasons or hypothetical future conditions. It does not contact Ollama or certify human quality acceptance.

`evaluate --run SCREEN_RUN_ID` prepares ten actual candidates and twenty explicitly modified cases without model calls. Add `--models qwen3.5:4b,qwen3:8b` to execute sequentially, or later use `evaluate --resume SUITE_ID --models ...`. `evaluate --status SUITE_ID` reads checkpoints without contacting Ollama. New suites can use `--limit N` for partial probes; resume preserves the original limit and model order. Old cases-only exports remain readable as files but need a new suite for the durable protocol.

Every completed/failed case commits an atomic checkpoint before derived reports. A process ID plus its live exclusive lock identifies an active evaluator; a leftover file or reused PID alone does not. Completed cases are skipped on resume. An in-flight case left by interruption is marked as unknown and is not automatically retried, so its three-request budget cannot silently reset. Its unknown usage is explicit, and it prevents the full automated gate from passing. Three consecutive local-provider failures stop the attempt without discarding earlier results.

Suites freeze the source screening fingerprint, case data, prompt/validator engine, endpoint and model weight digests. Digest checks bracket each case. Changes require a new suite rather than mixing comparable results. Evaluation-specific cache scopes leave product cards untouched; a new suite starts independently of product cache. Freeform versus assisted completion and cache hits are separate dimensions. Fresh timing includes failed attempts; cached generation counters are excluded. Modified evidence is marked as evaluation-only inside saved cards and is never published as normal research.

`evaluation.md`, per-model JSON and `summary.json` expose partial progress and automated gates. `review_template.csv` provides manual factual-error, evidence-completeness, revision-count and reading-time fields; generated checkpoints never overwrite reviewer edits. The current summary keeps human review pending even if all automated checks pass. An automated 95% rate alone does not certify research quality or investment validity. Evaluation tokens are reported in their own suite, separately from ordinary research usage; this workflow never selects DeepSeek.
