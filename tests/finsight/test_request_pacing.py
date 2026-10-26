import os
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import akshare
import pandas as pd
import pytest
import requests

from finsight.data.request_worker import request_worker
from finsight.data.requests import RequestBroker, RequestFailure, SourceUnavailable
from finsight.data.store import Store
from finsight.jobs import Jobs


def test_http_pagination_obeys_the_shared_network_budget(tmp_path, monkeypatch):
    store = Store(tmp_path)
    starts = []

    def send(session, request, **kwargs):
        starts.append(time.monotonic())
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response

    def pages():
        for page in range(2):
            requests.get(f"https://offline.invalid/page/{page}")
        return pd.DataFrame({"value": [1, 2]})

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(akshare, "finsight_test_pages", pages, raising=False)

    class Pipe:
        def __init__(self):
            self.messages = iter([{"method": "finsight_test_pages", "kwargs": {},
                "rate": {"database": str(store.root / "finsight.sqlite"), "interval": .04}}, None])
            self.output = []

        def recv(self):
            return next(self.messages)

        def send(self, value):
            self.output.append(value)

        def close(self):
            pass

    pipe = Pipe()
    request_worker(pipe)
    assert starts[1] - starts[0] >= .03
    assert pipe.output[0]["ok"] and pipe.output[0]["network_requests"] == 2
    assert pipe.output[0]["http_bytes"] == 4


def test_persistent_source_failure_stops_batch_without_retrying_every_stock(tmp_path):
    class Unavailable:
        calls = 0

        def baostock_kline(self, **kwargs):
            self.calls += 1
            raise RequestFailure("provider_unavailable", "baostock_kline", "BaoStock login: 10002007")

    store = Store(tmp_path)
    job = Jobs(store).create({"source": "fixture"})
    api, pauses = Unavailable(), []
    broker = RequestBroker(store, job["job_id"], api=api, retries=2, interval=0, sleeper=pauses.append)
    with pytest.raises(SourceUnavailable, match="checkpoints preserved"):
        broker.call("baostock_kline", code="sh.600036")
    assert api.calls == 3 and pauses == [5, 30]
    with pytest.raises(SourceUnavailable):
        broker.call("baostock_kline", code="sz.000001")
    assert api.calls == 3


def test_simultaneous_launches_start_only_one_worker(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = Jobs(store).create({"source": "fixture"})
    started = []

    def spawn(*args, **kwargs):
        started.append(1)
        time.sleep(.03)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr("finsight.jobs.subprocess.Popen", spawn)
    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(executor.map(lambda _: Jobs(store).launch(job["job_id"]), range(2)))
    assert len(started) == 1 and rows[0]["pid"] == rows[1]["pid"]
