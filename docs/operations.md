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

Start with `sync --provider akshare --universe mainboard --limit 100 --background`, then validate 500 stocks before a complete initialization. Use `jobs list`, `jobs show JOB_ID` and `jobs resume JOB_ID` to inspect and resume the same durable job. Successful raw responses retain their original observation time. Resume retries failed requests and reuses successful checkpoints; a new trading date creates a new snapshot with an explicit universe definition and member hash.

Prices and valuations are stored locally as immutable Parquet partitions referenced by snapshot manifests. Daily jobs request the missing range plus ten overlapping sessions to check revisions. Eastern Money's valuation endpoint still returns the stock's entire valuation history before local slicing; the BaoStock alternate requests a date range. Industry classification is cached for seven days. Financial metrics are checked daily and retain the original weighted annual ROE definition; BaoStock average ROE is not accepted as an equivalent.

The first complete mainboard snapshot enables a separate 18:15 pilot job. The existing 18:05 sample collector is promoted only after five distinct consecutive real mainboard sessions publish at or above 98% coverage. Batch validation, previews and demo data do not count. A launchd setup error is reported separately from an already saved ranking.

## Reproducible use

1. Sync a snapshot and review its quality report.
2. Run screening against its explicit snapshot ID; save the run ID.
3. Generate local research cards for that run. Start with `--limit 1`.
4. Review the month-end portfolio plan and enter orders manually in the simulation account.
5. Record all order/partial-fill/cancellation events with stable event IDs and actual fees. Each partial/filled record represents an incremental execution, not a cumulative order total.
6. Reconcile the complete journal from an initially empty account, supplying initial cash and optional actual cash/holdings.
7. Save exports and review data, strategy, execution and human deviations weekly. Parameters remain frozen; changes get a new version.

Run `finsight --data-dir PATH ...` to isolate experiments. Database/Parquet files are local and excluded from Git. For backups, stop collection/UI writes and copy the entire data directory so SQLite, snapshots and manifests remain consistent.

## Provider failures

Upstream requests run in terminable subprocesses, have finite retries, and only successful responses enter the raw cache. Whole-batch failures return a nonzero exit code. Partial per-stock failures are shown in quality/exclusion reports. A real source is never replaced with synthetic data automatically.

The default 20-stock sample is a data integration probe, not a representative investable universe or evidence of stock-selection skill. Full mainboard collection is opt-in and may take considerable time due to upstream rate limits.

Ollama must already be serving a downloaded tool-capable model. Check with `ollama list`. Model/schema errors produce failed cards with request counters. DeepSeek is only contacted when explicitly selected by configuration or CLI; no test requires a paid API key.
