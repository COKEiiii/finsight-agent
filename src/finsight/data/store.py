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
    try:
        root = Path(__file__).resolve().parents[3]
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL).decode().strip()
        diff = subprocess.check_output(["git", "diff", "HEAD"], cwd=root)
        for path in sorted((root / "src/finsight").rglob("*.py")):
            diff += path.relative_to(root).as_posix().encode() + path.read_bytes()
        return head + "+" + hashlib.sha256(diff).hexdigest()[:12]
    except (OSError, subprocess.CalledProcessError):
        return "finsight-0.1.0"


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
            """)

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
        snapshot_id = safe_id(bundle.meta.snapshot_id)
        parent = self.root / "snapshots"
        parent.mkdir(exist_ok=True)
        target = parent / snapshot_id
        if target.exists():
            raise ValueError("snapshots are immutable; create a new snapshot_id")
        stage = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=parent))
        try:
            hashes = {}
            for name in TABLES:
                file = stage / f"{name}.parquet"
                bundle[name].to_parquet(file, index=False)
                hashes[name] = hashlib.sha256(file.read_bytes()).hexdigest()
            bundle.meta.table_hashes = hashes
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

    def load_snapshot(self, snapshot_id: str | None = None) -> SnapshotBundle:
        if snapshot_id is None:
            snapshots = self.snapshots()
            if not snapshots:
                raise ValueError("no snapshots; run finsight sync --provider demo first")
            snapshot_id = snapshots[0].snapshot_id
        path = self.root / "snapshots" / safe_id(snapshot_id)
        meta = MarketSnapshot.model_validate_json((path / "metadata.json").read_text(encoding="utf-8"))
        tables = {}
        for name in TABLES:
            file = path / f"{name}.parquet"
            if hashlib.sha256(file.read_bytes()).hexdigest() != meta.table_hashes[name]:
                raise ValueError(f"snapshot integrity check failed: {name}")
            tables[name] = pd.read_parquet(file)
        return SnapshotBundle(meta, tables)

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
