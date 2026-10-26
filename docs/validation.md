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
