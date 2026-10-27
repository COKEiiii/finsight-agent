# MVP validation — 2026-10-26

Software checks:

- 87 tests passed, including the original 34 learning-project tests.
- Ruff and Git whitespace checks passed.
- Streamlit tests rendered all four pages, exercised screening, offline research, portfolio review, empty workspaces and switching to a newly created demo snapshot.
- The offline CLI workflow produced 10 candidates, 10 explicitly labeled template cards, portfolio exports and a ledger backtest with benchmark comparisons.
- A real local `qwen3.5:4b` request generated a schema- and evidence-validated research card. DeepSeek was not called. Provider wire formats and API failure paths were tested using mocked HTTP.
- The local UI responded with HTTP 200 at its health endpoint.

Real-data integration probe:

- 20 current A-share sample identities, listing dates and industries were retrieved.
- The captured dataset contained 5,680 daily bars, 5,680 dated PE-TTM/PB observations, 60 annual financial records, 284 benchmark observations and the exchange calendar.
- Latest complete market date was 2026-10-23; initial capture occurred on the morning of 2026-10-26. Current stock states cannot be backdated to the prior Friday, so the new quality guard blocks publishing that historical ranking. The next eligible first-day workflow requires the current session's completed evening snapshot.
- Historical membership, status revisions, delistings, original financial versions, adjustment integrity and corporate actions remain unverified. These datasets do not pass historical research acceptance. Full mainboard ingestion was not completed in this probe.

Local operation:

- A macOS launchd collector was installed and loaded for 18:05 local time and login. It only collects free market data and never runs a model or sends orders.
- The Streamlit application was started on the local loopback interface at port 8501.
- Eight to twelve weeks of prospective simulated execution is a subsequent time-dependent observation phase; no performance result is claimed by this release.

GitHub Actions validates Python 3.11/3.12 and runs an offline packaged CLI smoke check on every push. The current CI status is available in the repository's Actions tab.

## v0.2 mainboard work — 2026-10-26

- 116 tests passed after adding durable jobs, interruption/recovery, concurrent launch protection, exact 98% publication boundaries, incompatible financial definitions, immutable partitions, raw-factor cache invalidation, HTTP pagination pacing, source failure shutdown, retry cooldowns, and field-template rendering. Ruff passed.
- A synthetic 3,000-stock, 183,000-price-row benchmark completed factors and ranking in **1.820 seconds** from a cold factor cache and **0.236 seconds** with cached raw factors. Both runs produced identical candidates. Collection time is excluded; this is not a measurement of live full-market ingestion.
- Local data was backed up before the SQLite migration and partition format change. Existing snapshot and run readers remain compatible.
- The real 100-stock validation job `78631dbde784481483c5dd69b2ae2e1e` resumed its saved requests after an upstream failure. Snapshot `akshare-2026-10-23-4a30417741` contains 100 known industries, 26,128 price/valuation rows and 276 annual records from the original single-stock weighted-ROE endpoint. Eight current ST/new-listing/etc. exclusions are separate; all other 92 identities have dated price, valuation and weighted annual financial records. This describes field availability at capture time, not a published historical ranking.
- Current identities were first captured on October 26, after the October 23 market cutoff. The October 23 ranking remains blocked. No dates or first-observation timestamps were backdated to achieve coverage.
- Source comparison exposed a material difference between BaoStock average ROE and the existing weighted ROE field. Normal collection now retains the original weighted annual metric. Old alternate financial records with a different definition are rejected as data failures, rather than silently changing `strategy_v0.1`.
- The 500-stock job `8c94172ada98466e849b7e3be437f41d` stopped after persistent BaoStock network failures. After a successful connectivity probe it resumed the same checkpoints with serialized BaoStock requests and advanced again. The 500-stock and complete mainboard acceptance gates have **not** passed; inspect `jobs show` for current progress.
- Five consecutive real published mainboard sessions, a 30-case comparison using real candidates, human review of model facts, and prospective simulated execution remain pending. No strategy-effectiveness or 95% model-quality result is claimed.
- In one synthetic field-template smoke case, local `qwen3:8b` produced a validated card in three requests (138.04 seconds); values and units were filled by Python. The `qwen3.5:4b` attempt exhausted its three-request budget on a field-placement error. These are integration observations on one case, not a model-quality comparison. Neither run contacted DeepSeek.

## Real screening and mainboard initialization — 2026-10-27

- The October 26 incremental 500-stock snapshot `akshare-2026-10-26-7c1d1c4249` passed current-screening validation at **464/466 = 99.5708%** coverage. There were 34 confirmed rule exclusions, two data failures, 293 eligible stocks and ten ranked candidates. This is a 500-stock validation universe, not complete mainboard coverage.
- The two data failures (`002214.SZ`, `002860.SZ`) retain their raw price/turnover issues; missing values were not filled with zero. Industry coverage and all exclusion reasons are exported.
- The published snapshot contains 132,810 price rows, 132,810 dated valuations and 1,398 annual financial records. Repeating screening matched the original candidate values, rankings and exclusions exactly. End-to-end screening/export measured 0.416 seconds, then 0.258 seconds with cached factors. These measurements differ in scope from the synthetic factor-only benchmark above.
- The reference portfolio has eight names at a total target equity weight of 80%. October 26 is not a monthly rebalance date, and no trade was submitted.
- Before expanding the universe, an online SQLite backup and copies of the immutable data were saved under the local `data/finsight-backups/20261027-090251` directory.
- Complete mainboard job `bef3418a4fec4686b91428e18f420744` identified 3,194 requested members and reused local price histories for 558 symbols from explicitly recorded parent snapshots. The remaining histories are being initialized. Morning October 27 captures cannot certify October 26 stock states; initial publication and the five-session rollout gate remain pending.
- 120 tests passed, including cross-universe data reuse without membership leakage, future/synthetic seed rejection, the sample metadata fallback, and navigation from an older blocked snapshot to the newest usable ranking. Integer/float representation changes preserve original observation timestamps.
- Thirty versioned evaluation cases (`research_cases_v0.2`) were prepared from the ten real October 26 candidates, with twenty explicitly modified scenarios. The suite is `evaluation-8eca63df7e9b4cbf9ce4a7541a342514`. No models were called for preparation; the comparative model and human-review gates remain pending.

## Research-card validation repair — 2026-10-27

- Reproduced the reported failure on real candidate `600901.SH`, bound to screen `eb9f8a684d824d0c87cefa2d56839e64`. Generic report-period context no longer requires an unrelated date citation. Explicit date placeholders still require the matching field; `PE` inside `report_period` is not a valuation reference.
- Exact quantities remain in immutable evidence. Model input uses field tokens, observed signs and the moving-average definition; Python renders quantities and units. Decoder regex restrictions were removed while application-side number, field, unit and unsupported-comparison checks remain active.
- Local smoke runs exposed growth-sign contradictions, missing comparison benchmarks and confusion between an average ratio and price position. These errors are rejected. Independent claim errors are returned together for repair within the existing three-request budget. Cached prose is revalidated before reuse.
- The local `qwen3.5:4b` model still exhausted its budget on the average-ratio comparison in a guarded real-case run. A successful JSON response alone is not a quality pass. Failed cards retain their evidence and diagnostic template and do not invalidate screening.
- Regression checks cover temporal context, explicit date/field mismatch, growth direction and citation, average direction, unavailable comparisons, multi-error repair feedback, stale cache rejection, local wire formats and the failed-card UI. This remains a small integration exercise; the thirty-case model comparison and human quality review are pending.
- On the same real `600901.SH` case, local `qwen3:8b` completed in three requests / 196.16 seconds using the explicitly declared template-assisted final repair. Rank, score, annual revenue growth, PE-TTM, PB and turnover were filled from the snapshot by Python. Information gaps are derived from actual field availability, so an available report date is not described as missing. Model response, original template, rendered content and usage remain traceable. DeepSeek was not called.
- The default local model is now `qwen3:8b`; the 4B model remains an explicit option. This single completed assisted card is not evidence that unrestricted model explanations or the ninety-five-percent quality target have passed.
- Final local checks: **137 tests passed**, Ruff and Git whitespace checks passed. Reopening the completed assisted card revalidated and rendered its template with zero model requests; original generation usage remains three requests.

## Completed mainboard initialization and daily-update readiness — 2026-10-27

- Full job `bef3418a4fec4686b91428e18f420744` finished processing all **3,194** requested members at 12:36 local time. Snapshot `akshare-2026-10-26-e51778888c` contains 844,251 price rows, 844,251 dated valuation rows and 8,964 annual financial rows. These counts describe locally captured data, not selection eligibility.
- All 8,971 logical request records ended successfully, with 8,994 observed network starts across attempts/pagination. Latest-read cache flags total 418. Task wall time from its first creation to completion was 12,565.00 seconds; the final coordinator attempt took 11,832.92 seconds. BaoStock price requests account for the largest cumulative request duration. HTTP byte counts exclude its unmeasured TCP payloads, and legacy returned-row counts remain unknown.
- Stock states were first observed on October 27, after the October 26 cutoff. The snapshot remains blocked, current-selection coverage remains zero, and no stock dates were backdated. The next earliest current-session check is October 27 after 18:00. Both the existing 18:05 sample schedule and the 18:15 full-mainboard pilot were confirmed loaded. Full-mainboard published sessions remain **0 / 5**.
- A SQLite online backup, physical copies of manifests/exports and hard links to immutable raw/object files were saved under local `data/finsight-backups/20261027-141511` before migration 5. Hard-linked objects must not be edited in place.
- The new acceptance report exposes processing completion, publication blockers, local row counts, recovery actions, per-endpoint metrics and rollout progress. It does not change the saved quality gate. Finalized temporal failures now direct operators to a new incremental session; actual failed requests remain resumable.
- A cross-session fixture verified morning initialization of the prior session followed by an evening increment: historical price ranges were reused, stale same-day valuation cache was refreshed, original financial visibility stayed intact, and the new session published. It made four observed source calls on the increment and exported its collection report automatically. This is a controlled integration test, not a measured live-market speedup.
- Real evening mainboard publication, five consecutive passing sessions and prospective simulation remain pending; the existing real 500-stock published ranking remains available meanwhile.
- Final local checks: **146 tests passed**, Ruff and Git whitespace checks passed. The real-data Streamlit run had no exceptions, displayed completed 3,194-stock processing alongside the published 500-stock view, and disabled the new-session increment before its earliest close. Viewing the page did not start a collector or model. A repeated pre-close sync is blocked before worker launch, and scheduled checks report `waiting_new_session` rather than restarting the completed prior-session initialization.
