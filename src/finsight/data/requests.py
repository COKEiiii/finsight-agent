from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import queue
import threading
import time
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from finsight.data.request_worker import request_worker
from finsight.data.store import Store
from finsight.jobs import Jobs
from finsight.models import fingerprint


class RequestFailure(ValueError):
    def __init__(self, kind: str, method: str, detail: str = "", http_bytes: int = 0):
        self.kind, self.detail, self.http_bytes = kind, detail, http_bytes
        super().__init__(f"{method}: {kind}")


class WorkerPool:
    def __init__(self, size: int = 4, target=request_worker):
        self.size, self.target = size, target
        self.available = queue.Queue()
        self.all_workers = []
        for _ in range(size):
            self.available.put(self._new())

    def _new(self):
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=self.target, args=(child,), daemon=True)
        process.start()
        child.close()
        worker = (process, parent)
        self.all_workers.append(worker)
        return worker

    def execute(self, method: str, kwargs: dict, timeout: float) -> dict:
        process, pipe = self.available.get()
        broken = False
        try:
            pipe.send({"method": method, "kwargs": kwargs})
            if not pipe.poll(timeout):
                broken = True
                raise RequestFailure("timeout", method)
            result = pipe.recv()
            if not result["ok"]:
                detail = result.get("error", "") + (f": HTTP {result['http_status']}" if result.get("http_status") else "")
                raise RequestFailure(result["kind"], method, detail, result.get("http_bytes") or 0)
            return result
        except (EOFError, BrokenPipeError, OSError) as exc:
            broken = True
            raise RequestFailure("worker_crash", method) from exc
        finally:
            if broken:
                process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
                pipe.close()
                self.available.put(self._new())
            else:
                self.available.put((process, pipe))

    def close(self):
        for process, pipe in self.all_workers:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
            pipe.close()


class RequestBroker:
    """Durable per-request checkpoints, original observation timestamps and a global rate budget."""

    def __init__(self, store: Store, job_id: str, workers: int = 4, interval: float = 1,
                 timeout: float = 30, retries: int = 2, api=None, clock=None, sleeper=time.sleep):
        self.store, self.job_id, self.jobs = store, job_id, Jobs(store)
        self.workers, self.interval, self.timeout, self.retries = workers, interval, timeout, retries
        self.api, self.clock, self.sleep = api, clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai"))), sleeper
        self.pool = WorkerPool(workers) if api is None else None
        self.lock, self.condition = threading.RLock(), threading.Condition()
        self.cancel_event = threading.Event()
        self.active, self.limit, self.failure_streak = 0, workers, 0

    def close(self):
        if self.pool:
            self.pool.close()

    def _ttl(self, method: str) -> float:
        return 7 * 86400 if method in {"stock_individual_info_em", "baostock_industry"} else 86400

    def _read(self, row, loaded: str) -> pd.DataFrame | None:
        if not row or not row["path"]:
            return None
        path = self.store.root / row["path"]
        try:
            expected = row["sha256"] if "sha256" in row.keys() else path.stem
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return None
            frame = pd.read_parquet(path)
            frame.attrs["fetched_at"], frame.attrs["loaded_at"] = row["fetched_at"], loaded
            return frame
        except (OSError, ValueError):
            return None

    def _record(self, request_id, method, kwargs, status, *, path=None, fetched_at=None,
                attempts=0, elapsed=0., size=0, hit=False, error_kind=None,
                loaded_at=None, http_bytes=0, error_detail=None):
        with self.lock, self.store.connect() as db:
            db.execute("INSERT INTO job_requests(job_id,request_id,method,kwargs,status,path,fetched_at,attempts,elapsed,bytes,cache_hit,error_kind,error,loaded_at,http_bytes,error_detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id,request_id) DO UPDATE SET "
                "status=excluded.status,path=excluded.path,fetched_at=excluded.fetched_at,attempts=job_requests.attempts+excluded.attempts,"
                "elapsed=job_requests.elapsed+excluded.elapsed,bytes=excluded.bytes,cache_hit=excluded.cache_hit,error_kind=excluded.error_kind,error=excluded.error,"
                "loaded_at=excluded.loaded_at,http_bytes=job_requests.http_bytes+excluded.http_bytes,error_detail=excluded.error_detail",
                (self.job_id, request_id, method, json.dumps(kwargs, sort_keys=True), status, path, fetched_at,
                 attempts, elapsed, size, int(hit), error_kind, error_kind, loaded_at, http_bytes, error_detail))

    def call(self, method: str, *, cache_ttl: float | None = None, **kwargs) -> pd.DataFrame:
        if self.cancel_event.is_set():
            from finsight.jobs import JobCancelled
            raise JobCancelled("collection interrupted; resume its job ID")
        self.jobs.check_cancelled(self.job_id)
        now = self.clock()
        loaded = now.isoformat()
        key = fingerprint({"method": method, "kwargs": kwargs})
        with self.store.connect() as db:
            checkpoint = db.execute("SELECT * FROM job_requests WHERE job_id=? AND request_id=? AND status='done'", (self.job_id, key)).fetchone()
            cached = db.execute("SELECT * FROM request_cache WHERE cache_key=?", (key,)).fetchone()
        # A resumed fixed-date job keeps successful original observations, even across midnight.
        frame = self._read(checkpoint, loaded)
        if frame is not None:
            self._record(key, method, kwargs, "done", path=checkpoint["path"], fetched_at=checkpoint["fetched_at"],
                         size=checkpoint["bytes"], hit=True, loaded_at=loaded)
            return frame
        ttl = self._ttl(method) if cache_ttl is None else cache_ttl
        fresh = cached and (now - datetime.fromisoformat(cached["fetched_at"])).total_seconds() < ttl
        if fresh and (method not in {"stock_individual_info_em", "baostock_industry"} or ttl <= 86400):
            fresh = cached["fetched_at"][:10] == loaded[:10]
        frame = self._read(cached, loaded) if fresh else None
        if frame is not None:
            self._record(key, method, kwargs, "done", path=cached["path"], fetched_at=cached["fetched_at"],
                         size=cached["bytes"], hit=True, loaded_at=loaded)
            return frame
        self._record(key, method, kwargs, "running")
        failure = "upstream"
        for attempt in range(self.retries + 1):
            self.jobs.check_cancelled(self.job_id)
            if attempt:
                self.sleep((5, 30)[min(attempt - 1, 1)])
            with self.condition:
                self.condition.wait_for(lambda: self.active < self.limit)
                self.active += 1
            started = time.perf_counter()
            try:
                wait = self.store.reserve_request(self.interval)
                if wait:
                    self.sleep(wait)
                self.jobs.check_cancelled(self.job_id)
                timeout = max(self.timeout, 120) if method in {"stock_yjbb_em", "stock_info_sh_name_code", "stock_info_sz_name_code", "stock_zh_a_spot_em"} else self.timeout
                if self.api is None:
                    response = self.pool.execute(method, kwargs, timeout)
                    frame = response["frame"]
                    http_bytes = response.get("http_bytes") or 0
                else:
                    frame = getattr(self.api, method)(**kwargs)
                    http_bytes = 0
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    raise RequestFailure("empty_response", method)
                for column in frame.select_dtypes(include=["object"]).columns:
                    types = {type(v) for v in frame[column].dropna()}
                    if str in types and len(types) > 1:
                        frame[column] = frame[column].astype("string")
                fetched = self.clock().isoformat()
                frame.attrs = {"fetched_at": fetched}
                data = frame.to_parquet(index=False)
                digest = hashlib.sha256(data).hexdigest()
                relative = f"raw-v2/{digest}.parquet"
                path = self.store.root / relative
                path.parent.mkdir(exist_ok=True)
                temporary = path.parent / (".response-" + uuid4().hex)
                temporary.write_bytes(data)
                temporary.replace(path)
                with self.lock, self.store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO request_cache VALUES (?,?,?,?,?)", (key, relative, fetched, digest, len(data)))
                self._record(key, method, kwargs, "done", path=relative, fetched_at=fetched, attempts=1,
                             elapsed=time.perf_counter() - started, size=len(data), loaded_at=fetched,
                             http_bytes=http_bytes)
                frame.attrs["loaded_at"] = fetched
                with self.condition:
                    self.failure_streak = 0
                return frame
            except (ValueError, OSError) as exc:
                failure = exc.kind if isinstance(exc, RequestFailure) else "upstream_schema"
                self._record(key, method, kwargs, "failed", attempts=1, elapsed=time.perf_counter() - started,
                             error_kind=failure, loaded_at=self.clock().isoformat(),
                             http_bytes=exc.http_bytes if isinstance(exc, RequestFailure) else 0,
                             error_detail=exc.detail if isinstance(exc, RequestFailure) else type(exc).__name__)
                with self.condition:
                    self.failure_streak += 1
                    if failure in {"timeout", "rate_limit"} or self.failure_streak >= 3:
                        self.limit = max(1, self.limit // 2)
                self.jobs.update(self.job_id, progress={"effective_workers": self.limit})
            finally:
                with self.condition:
                    self.active -= 1
                    self.condition.notify_all()
        raise RequestFailure(failure, method)
