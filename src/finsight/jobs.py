from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from finsight.data.store import Store, safe_id
from finsight.models import fingerprint


def now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


class JobCancelled(Exception):
    pass


class Jobs:
    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.RLock()

    def create(self, config: dict) -> dict:
        identity = fingerprint({**config, "creation_day": now_iso()[:10]})
        existing_id = None
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE identity=? AND status IN "
                "('queued','running','cancelling','failed','cancelled','partial') "
                "ORDER BY created_at DESC LIMIT 1", (identity,)).fetchone()
            if existing:
                existing_id = existing["job_id"]
            else:
                job_id = uuid4().hex
                db.execute("INSERT INTO jobs(job_id,identity,status,config,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                           (job_id, identity, "queued", json.dumps(config, ensure_ascii=False), now_iso(), now_iso()))
        if existing_id:
            return self.resume(existing_id)
        return self.get(job_id)

    @staticmethod
    def alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def get(self, job_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (safe_id(job_id),)).fetchone()
        if row is None:
            raise ValueError("unknown collection job")
        result = dict(row)
        result["config"], result["progress"] = json.loads(result["config"]), json.loads(result["progress"])
        with self.store.connect() as db:
            requests = db.execute("SELECT count(*) AS total,sum(status='done') AS completed,sum(status='failed') AS failed,"
                "sum(attempts) AS network_attempts,sum(cache_hit) AS cache_hits,sum(bytes) AS serialized_bytes,"
                "sum(http_bytes) AS http_bytes,sum(network_requests) AS observed_network_requests,"
                "sum(error_kind='timeout') AS unmeasured_timeouts,sum(elapsed) AS request_seconds "
                "FROM job_requests WHERE job_id=?", (job_id,)).fetchone()
        result["requests"] = {key: value or 0 for key, value in dict(requests).items()}
        return result

    def list(self) -> list[dict]:
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 100")]
        return [self.get(job_id) for job_id in ids]

    def update(self, job_id: str, *, status: str | None = None, progress: dict | None = None, **fields):
        allowed = {"config", "snapshot_id", "error", "pid"}
        if set(fields) - allowed:
            raise ValueError("invalid job update")
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT progress FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("unknown collection job")
            values = {"updated_at": now_iso(), **fields}
            if status:
                values["status"] = status
            if progress:
                values["progress"] = json.dumps({**json.loads(row[0]), **progress}, ensure_ascii=False)
            if "config" in values:
                values["config"] = json.dumps(values["config"], ensure_ascii=False)
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE job_id=?",
                       [*values.values(), job_id])

    def check_cancelled(self, job_id: str):
        with self.store.connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row and row[0] in {"cancelling", "cancelled"}:
            raise JobCancelled("collection cancelled; successful requests remain resumable")

    def resume(self, job_id: str) -> dict:
        row = self.get(job_id)
        if row["status"] in {"running", "queued", "cancelling"} and self.alive(row["pid"]):
            return row
        if row["status"] == "completed":
            return row
        self.update(job_id, status="queued", error=None, pid=None)
        return self.get(job_id)

    def launch(self, job_id: str) -> dict:
        import fcntl
        folder = self.store.root / "jobs"
        folder.mkdir(exist_ok=True)
        with (folder / f".{safe_id(job_id)}.launch.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._launch_locked(job_id, folder)

    def _launch_locked(self, job_id: str, folder: Path) -> dict:
        row = self.get(job_id)
        if row["status"] == "completed":
            return row
        if row["status"] in {"queued", "running"} and self.alive(row["pid"]):
            return row
        if row["status"] == "cancelling":
            raise ValueError("job is cancelling; resume after cancellation completes")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        with (folder / f"{safe_id(job_id)}.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "finsight.job_worker", "--data-dir", str(self.store.root),
                "--job", job_id], stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        self.update(job_id, pid=process.pid)
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict:
        row = self.get(job_id)
        if row["status"] in {"queued", "running"}:
            self.update(job_id, status="cancelling")
            if row["pid"]:
                try:
                    os.kill(row["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    self.update(job_id, status="cancelled", pid=None)
            else:
                self.update(job_id, status="cancelled")
        return self.get(job_id)
