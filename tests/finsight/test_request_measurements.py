from __future__ import annotations

import pandas as pd
import pytest

from finsight.collection_status import request_profile
from finsight.data.requests import RequestBroker, RequestFailure
from finsight.jobs import Jobs


class MeasuredPool:
    def __init__(self, responses):
        self.responses = iter(responses)

    def execute(self, *args, **kwargs):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def broker_with_responses(store, responses, retries=0):
    job = Jobs(store).create({"case": "request-measurements"})
    broker = RequestBroker(store, job["job_id"], api=object(), workers=1, interval=0,
        retries=retries, sleeper=lambda _: None)
    broker.api = None
    broker.pool = MeasuredPool(responses)
    return broker, job["job_id"]


@pytest.mark.parametrize("frame", [pd.DataFrame(), "not-a-dataframe"])
def test_received_empty_or_wrong_type_counts_real_network_work(store, frame):
    broker, job = broker_with_responses(store, [{"frame": frame, "http_bytes": 320, "network_requests": 2}])
    with pytest.raises(RequestFailure, match="empty_response"):
        broker.call("sample")
    requests = Jobs(store).get(job)["requests"]
    assert requests["failed"] == 1 and requests["network_attempts"] == 1
    assert requests["observed_network_requests"] == 2 and requests["http_bytes"] == 320
    profile = request_profile(store, job)[0]
    assert profile["observed_network_requests"] == 2 and profile["measured_http_bytes"] == 320
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM request_cache").fetchone()[0] == 0


def test_empty_response_retry_is_counted_once_per_observed_request(store):
    frame = pd.DataFrame({"value": [1]})
    broker, job = broker_with_responses(store, [
        {"frame": pd.DataFrame(), "http_bytes": 70, "network_requests": 2},
        {"frame": frame, "http_bytes": 180, "network_requests": 3},
    ], retries=1)
    assert len(broker.call("sample")) == 1
    requests = Jobs(store).get(job)["requests"]
    assert requests["completed"] == 1 and requests["network_attempts"] == 2
    assert requests["observed_network_requests"] == 5 and requests["http_bytes"] == 250
    # Reading the successful checkpoint cannot charge the original work again.
    broker.call("sample")
    cached = Jobs(store).get(job)["requests"]
    assert cached["cache_hits"] == 1
    assert cached["observed_network_requests"] == 5 and cached["http_bytes"] == 250


@pytest.mark.parametrize("failure", [ValueError("invalid local serialization"), OSError("disk unavailable")])
def test_local_failure_after_received_response_preserves_measurement(store, monkeypatch, failure):
    broker, job = broker_with_responses(store, [
        {"frame": pd.DataFrame({"value": [1]}), "http_bytes": 400, "network_requests": 3}])

    def reject(*args, **kwargs):
        raise failure

    monkeypatch.setattr(pd.DataFrame, "to_parquet", reject)
    with pytest.raises(RequestFailure, match="upstream_schema"):
        broker.call("sample")
    requests = Jobs(store).get(job)["requests"]
    assert requests["observed_network_requests"] == 3 and requests["http_bytes"] == 400


def test_unobserved_timeout_does_not_reuse_previous_attempt_measurement(store):
    broker, job = broker_with_responses(store, [
        {"frame": pd.DataFrame(), "http_bytes": 90, "network_requests": 2},
        RequestFailure("timeout", "sample"),
    ], retries=1)
    with pytest.raises(RequestFailure, match="timeout"):
        broker.call("sample")
    requests = Jobs(store).get(job)["requests"]
    assert requests["network_attempts"] == 2 and requests["unmeasured_timeouts"] == 1
    assert requests["observed_network_requests"] == 2 and requests["http_bytes"] == 90


def test_worker_failure_measurements_are_retained(store):
    broker, job = broker_with_responses(store, [
        RequestFailure("rate_limit", "sample", "HTTP 429", http_bytes=65, network_requests=4)])
    with pytest.raises(RequestFailure, match="rate_limit"):
        broker.call("sample")
    requests = Jobs(store).get(job)["requests"]
    assert requests["observed_network_requests"] == 4 and requests["http_bytes"] == 65
