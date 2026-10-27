from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from finsight.models import MarketSnapshot, StrategyConfig

TABLES = ("universe", "prices", "financials", "valuations", "calendar", "benchmark", "actions")


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", value):
        raise ValueError("invalid artifact identifier")
    return value


def code_version() -> str:
    package = Path(__file__).resolve().parents[1]
    runtime = b"".join(path.relative_to(package).as_posix().encode() + path.read_bytes()
                       for path in sorted(package.rglob("*.py")))
    digest = hashlib.sha256(runtime).hexdigest()[:12]
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=package, stderr=subprocess.DEVNULL).decode().strip()
        return head + "+runtime-" + digest
    except (OSError, subprocess.CalledProcessError):
        from finsight import __version__
        return f"finsight-{__version__}+runtime-" + digest


@dataclass
class SnapshotBundle:
    meta: MarketSnapshot
    tables: dict[str, pd.DataFrame]

    def __getitem__(self, key: str) -> pd.DataFrame:
        return self.tables[key]


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, captured_at TEXT NOT NULL,
                    source TEXT NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    config TEXT NOT NULL, code_version TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS strategies (version TEXT PRIMARY KEY, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS journal (event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO schema_migrations(version) VALUES (2);
                CREATE TABLE IF NOT EXISTS factor_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, identity TEXT NOT NULL, status TEXT NOT NULL,
                    config TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, pid INTEGER,
                    snapshot_id TEXT, error TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_identity ON jobs(identity)
                    WHERE status IN ('queued','running','cancelling');
                CREATE TABLE IF NOT EXISTS job_requests (
                    job_id TEXT NOT NULL, request_id TEXT NOT NULL, method TEXT NOT NULL,
                    kwargs TEXT NOT NULL, status TEXT NOT NULL, path TEXT, fetched_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0,
                    bytes INTEGER NOT NULL DEFAULT 0, cache_hit INTEGER NOT NULL DEFAULT 0,
                    error_kind TEXT, error TEXT, PRIMARY KEY(job_id,request_id));
                CREATE TABLE IF NOT EXISTS request_cache (
                    cache_key TEXT PRIMARY KEY, path TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    sha256 TEXT NOT NULL, bytes INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS collector_control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS publications (
                    universe_id TEXT NOT NULL, trade_date TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    screen_run_id TEXT NOT NULL, coverage REAL NOT NULL,
                    PRIMARY KEY(universe_id,trade_date));
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(job_requests)")}
            for name, definition in {"loaded_at": "TEXT", "http_bytes": "INTEGER NOT NULL DEFAULT 0",
                                     "error_detail": "TEXT", "network_requests": "INTEGER NOT NULL DEFAULT 0",
                                     "response_rows": "INTEGER"}.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE job_requests ADD COLUMN {name} {definition}")
            db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)")
            db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")
            db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)")

    @contextmanager
    def connect(self):
        with sqlite3.connect(self.root / "finsight.sqlite", timeout=30) as db:
            db.row_factory = sqlite3.Row
            yield db

    def register_strategy(self, config: StrategyConfig):
        payload = json.dumps(config.model_dump(), sort_keys=True)
        with self.connect() as db:
            row = db.execute("SELECT config FROM strategies WHERE version=?", (config.version,)).fetchone()
            if row and json.loads(row["config"]) != config.model_dump():
                raise ValueError("strategy parameters changed: use a new strategy version")
            db.execute("INSERT OR IGNORE INTO strategies VALUES (?, ?)", (config.version, payload))

    def save_snapshot(self, bundle: SnapshotBundle) -> str:
        bundle.meta.collector_version = code_version()
        snapshot_id = safe_id(bundle.meta.snapshot_id)
        parent = self.root / "snapshots"
        parent.mkdir(exist_ok=True)
        target = parent / snapshot_id
        if target.exists():
            raise ValueError("snapshots are immutable; create a new snapshot_id")
        stage = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=parent))
        try:
            hashes = {}
            manifests = {}
            for name in TABLES:
                if bundle.meta.schema_version >= 2:
                    manifests[name] = self._save_partitions(name, bundle[name])
                    hashes[name] = hashlib.sha256(json.dumps(manifests[name], sort_keys=True).encode()).hexdigest()
                    continue
                file = stage / f"{name}.parquet"
                bundle[name].to_parquet(file, index=False)
                hashes[name] = hashlib.sha256(file.read_bytes()).hexdigest()
            bundle.meta.table_hashes = hashes
            if manifests:
                (stage / "manifest.json").write_text(json.dumps({"version": 2, "tables": manifests}, sort_keys=True), encoding="utf-8")
            (stage / "metadata.json").write_text(bundle.meta.model_dump_json(indent=2), encoding="utf-8")
            stage.rename(target)
            with self.connect() as db:
                db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)", (
                    snapshot_id, str(bundle.meta.as_of), bundle.meta.captured_at.isoformat(),
                    bundle.meta.source, bundle.meta.model_dump_json(),
                ))
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            if target.exists():
                with self.connect() as db:
                    saved = db.execute("SELECT 1 FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
                if not saved:
                    shutil.rmtree(target)
            raise
        return snapshot_id

    def snapshots(self) -> list[MarketSnapshot]:
        with self.connect() as db:
            rows = db.execute("SELECT metadata FROM snapshots ORDER BY as_of DESC, captured_at DESC").fetchall()
        return [MarketSnapshot.model_validate_json(r["metadata"]) for r in rows]

    def load_snapshot(self, snapshot_id: str | None = None, date_range: tuple[str, str] | None = None,
                      columns: dict[str, list[str]] | None = None, table_names: tuple[str, ...] | None = None) -> SnapshotBundle:
        if snapshot_id is None:
            snapshots = self.snapshots()
            if not snapshots:
                raise ValueError("no snapshots; run finsight sync --provider demo first")
            snapshot_id = snapshots[0].snapshot_id
        path = self.root / "snapshots" / safe_id(snapshot_id)
        meta = MarketSnapshot.model_validate_json((path / "metadata.json").read_text(encoding="utf-8"))
        manifest = json.loads((path / "manifest.json").read_text()) if (path / "manifest.json").exists() else None
        tables = {}
        for name in TABLES:
            if table_names is not None and name not in table_names:
                continue
            if manifest:
                refs = manifest["tables"][name]
                if hashlib.sha256(json.dumps(refs, sort_keys=True).encode()).hexdigest() != meta.table_hashes[name]:
                    raise ValueError(f"snapshot integrity check failed: {name} manifest")
                frames = []
                for ref in refs:
                    if date_range and name in {"prices", "valuations", "benchmark"} and ref.get("date_min"):
                        if ref["date_max"] < date_range[0] or ref["date_min"] > date_range[1]:
                            continue
                    file = self.root / "objects" / (ref["sha256"] + ".parquet")
                    if hashlib.sha256(file.read_bytes()).hexdigest() != ref["sha256"]:
                        raise ValueError(f"snapshot integrity check failed: {name} object")
                    wanted = [c for c in (columns or {}).get(name, ref["columns"]) if c in ref["columns"]]
                    frames.append(pd.read_parquet(file, columns=wanted))
                frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=refs[0]["columns"])
                if date_range and name in {"prices", "valuations", "benchmark"}:
                    frame = frame[frame["trade_date"].between(*date_range)]
                tables[name] = canonical(frame)
                continue
            file = path / f"{name}.parquet"
            if hashlib.sha256(file.read_bytes()).hexdigest() != meta.table_hashes[name]:
                raise ValueError(f"snapshot integrity check failed: {name}")
            tables[name] = pd.read_parquet(file)
            if date_range and name in {"prices", "valuations", "benchmark"}:
                tables[name] = tables[name][tables[name]["trade_date"].between(*date_range)]
        return SnapshotBundle(meta, tables)

    def _save_partitions(self, name: str, frame: pd.DataFrame) -> list[dict]:
        folder = self.root / "objects"
        folder.mkdir(exist_ok=True)
        frame = canonical(frame)
        groups = []
        if not frame.empty and "trade_date" in frame and name in {"prices", "valuations", "benchmark"}:
            work = frame.assign(_month=frame["trade_date"].str[:7])
            work["_bucket"] = work["symbol"].str[:6].astype(int) % 32 if "symbol" in work else 0
            groups = [part.drop(columns=["_month", "_bucket"]) for _, part in work.groupby(["_month", "_bucket"], sort=True)]
        elif not frame.empty and "symbol" in frame:
            groups = [part.drop(columns="_bucket") for _, part in frame.assign(
                _bucket=frame["symbol"].str[:6].astype(int) % 32).groupby("_bucket", sort=True)]
        else:
            groups = [frame]
        refs = []
        for part in groups:
            part.attrs = {}
            # Serialise in memory before assigning the content address; existing objects remain untouched.
            data = part.to_parquet(index=False)
            digest = hashlib.sha256(data).hexdigest()
            file = folder / (digest + ".parquet")
            if not file.exists():
                temp = folder / (".object-" + uuid4().hex)
                temp.write_bytes(data)
                temp.replace(file)
            refs.append({"sha256": digest, "rows": len(part), "columns": list(part.columns),
                "date_min": part["trade_date"].min() if "trade_date" in part and len(part) else None,
                "date_max": part["trade_date"].max() if "trade_date" in part and len(part) else None})
        return refs

    def latest(self, universe_id: str | None = None, published_only: bool = False) -> MarketSnapshot | None:
        matches = [s for s in self.snapshots() if (not universe_id or s.universe_id == universe_id) and
                   (not published_only or s.publishable and s.publication_status == "published")]
        if universe_id is None and published_only:
            return next((s for s in matches if s.universe_complete and not s.synthetic), None) or (matches[0] if matches else None)
        return matches[0] if matches else None

    def factor(self, key: str, payload: dict | None = None) -> dict | None:
        with self.connect() as db:
            if payload is not None:
                db.execute("INSERT OR REPLACE INTO factor_cache VALUES (?,?)", (key, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
                return payload
            row = db.execute("SELECT payload FROM factor_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def reserve_request(self, interval: float) -> float:
        from finsight.data.rate import reserve
        return reserve(str(self.root / "finsight.sqlite"), interval)

    def save_run(self, kind: str, snapshot_id: str, config: StrategyConfig, payload: dict,
                 run_id: str | None = None) -> str:
        self.register_strategy(config)
        run_id = safe_id(run_id or uuid4().hex)
        version = payload.get("code_version", code_version())
        text = json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False)
        with self.connect() as db:
            db.execute("INSERT INTO runs(run_id,kind,snapshot_id,config,code_version,payload) VALUES (?,?,?,?,?,?)",
                       (run_id, kind, snapshot_id, config.model_dump_json(), version, text))
        return run_id

    def runs(self, kind: str | None = None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM runs WHERE (? IS NULL OR kind=?) ORDER BY rowid DESC", (kind, kind)).fetchall()
        return [dict(row) for row in rows]

    def run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("unknown run_id")
        return {**dict(row), "payload": json.loads(row["payload"]), "config": json.loads(row["config"])}

    def cached(self, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM research_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def cache(self, key: str, value: dict[str, Any]):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO research_cache VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def artifact_dir(self, run_id: str) -> Path:
        path = self.root / "exports" / safe_id(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path


def canonical(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.attrs = {}
    keys = [c for c in ("symbol", "trade_date", "known_on", "report_period", "available_on", "ex_date") if c in frame]
    if keys:
        frame = frame.sort_values(keys, kind="stable")
    return frame.reindex(columns=sorted(frame.columns)).reset_index(drop=True)
