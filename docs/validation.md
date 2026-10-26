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
