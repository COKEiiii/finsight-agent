from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import baostock as bs
import baostock.common.contants as constants
import baostock.common.context as context
import baostock.util.socketutil as socketutil
import pytest
from baostock.data.resultset import ResultData

from finsight.data.request_worker import (
    AlternateUnavailable,
    ReceivingSocket,
    alternate_query,
    request_worker,
)
from finsight.data.requests import RequestBroker, RequestFailure, SourceUnavailable
from finsight.jobs import Jobs

KWARGS = {"code": "sh.600036", "start_date": "2026-10-28", "end_date": "2026-10-29", "adjustflag": "3"}


class Socket:
    def __init__(self, blocks=()):
        self.blocks = iter(blocks)
        self.reads = 0
        self.closed = False
        self.sent = []

    def recv(self, size):
        self.reads += 1
        # Bound a regressed SDK empty-read loop so this test cannot hang.
        if self.reads > 2:
            raise AssertionError("SDK kept reading a closed connection")
        return next(self.blocks, b"")

    def send(self, block):
        self.sent.append(block)
        return len(block)

    def close(self):
        self.closed = True


class Pipe:
    def __init__(self):
        self.messages = iter([{"method": "baostock_kline", "kwargs": KWARGS}, None])
        self.output = []

    def recv(self):
        return next(self.messages)

    def send(self, message):
        self.output.append(message)

    def close(self):
        pass


@pytest.fixture
def session(monkeypatch):
    socket = Socket()
    monkeypatch.setattr(context, "default_socket", socket, raising=False)
    monkeypatch.setattr(context, "user_id", "anonymous", raising=False)
    monkeypatch.setattr(alternate_query, "logged_in", True, raising=False)
    yield socket
    # reset_alternate_session deliberately removes this attribute. Restore the fake
    # before monkeypatch restores the original module state.
    socket.close()
    context.default_socket = socket


def test_receiving_socket_preserves_bytes_and_forwards_send_and_close():
    socket = Socket([b"first", b"second"])
    guarded = ReceivingSocket(socket)
    assert guarded.recv(8192) == b"first" and guarded.recv(8192) == b"second"
    assert guarded.send(b"request") == 7
    guarded.close()
    assert socket.closed and socket.sent == [b"request"]


def test_closed_socket_fails_after_one_empty_read_and_resets_sdk_session(session):
    original = socketutil.send_msg
    pipe = Pipe()
    request_worker(pipe)  # Real SDK parser and transport, with a controlled closed socket.
    result = pipe.output[0]
    assert not result["ok"] and result["kind"] == "provider_unavailable"
    assert "empty response" in result["error"] and "frame" not in result
    assert result["network_requests"] == 1 and session.reads == 1
    assert session.closed and not hasattr(context, "default_socket")
    assert not alternate_query.logged_in and socketutil.send_msg is original


@pytest.mark.parametrize("reply", [None, "", "   "])
def test_empty_next_page_cannot_become_a_successful_partial_frame(session, monkeypatch, reply):
    response = ResultData()
    response.error_code, response.fields = "0", ["date", "close"]
    response.data = [["2026-10-28", "10"], ["2026-10-29", "11"]]
    response.cur_page_num, response.msg_type = "1", constants.MESSAGE_TYPE_GETKDATAPLUS_REQUEST
    response.msg_body = constants.MESSAGE_SPLIT.join(["query", "anonymous", "1"])
    monkeypatch.setattr(constants, "BAOSTOCK_PER_PAGE_COUNT", 2)
    monkeypatch.setattr(bs, "query_history_k_data_plus", lambda *a, **k: response)
    monkeypatch.setattr(socketutil, "send_msg", lambda *a, **k: reply)
    pipe = Pipe()
    request_worker(pipe)
    assert not pipe.output[0]["ok"] and "frame" not in pipe.output[0]
    assert pipe.output[0]["kind"] == "provider_unavailable"
    assert pipe.output[0]["network_requests"] == 1
    assert session.closed and not alternate_query.logged_in


@pytest.mark.parametrize("code,exception", [("10002007", AlternateUnavailable), ("10001001", ValueError)])
def test_pagination_error_discards_prior_rows_and_requires_a_fresh_login(session, monkeypatch, code, exception):
    class Response:
        error_code = "0"
        fields = ["close"]
        row = 0

        def next(self):
            if self.row:
                self.error_code = code
                return False
            self.row += 1
            return True

        def get_row_data(self):
            return ["10"]

    monkeypatch.setattr(bs, "query_history_k_data_plus", lambda *a, **k: Response())
    with pytest.raises(exception, match=code):
        alternate_query("baostock_kline", KWARGS)
    assert session.closed and not alternate_query.logged_in
    assert not hasattr(context, "default_socket")

    logins = []
    monkeypatch.setattr(bs, "login", lambda: logins.append(1) or SimpleNamespace(error_code="0"))
    valid = ResultData()
    valid.error_code, valid.fields, valid.data = "0", ["close"], [["11"]]
    monkeypatch.setattr(bs, "query_history_k_data_plus", lambda *a, **k: valid)
    assert alternate_query("baostock_kline", KWARGS)["close"].tolist() == ["11"]
    assert logins == [1] and alternate_query.logged_in


@pytest.mark.parametrize("code", ["10002007", "10001001"])
def test_failed_login_closes_its_connection(session, monkeypatch, code):
    alternate_query.logged_in = False
    monkeypatch.setattr(bs, "login", lambda: SimpleNamespace(error_code=code))
    with pytest.raises(AlternateUnavailable, match=f"login: {code}"):
        alternate_query("baostock_kline", KWARGS)
    assert session.closed and not alternate_query.logged_in


def test_none_query_response_is_a_source_failure(session, monkeypatch):
    monkeypatch.setattr(bs, "query_history_k_data_plus", lambda *a, **k: None)
    with pytest.raises(AlternateUnavailable, match="no response"):
        alternate_query("baostock_kline", KWARGS)
    assert session.closed and not alternate_query.logged_in


def test_circuit_opening_does_not_leave_waiting_requests_running(store, monkeypatch):
    job = Jobs(store).create({"case": "queued-source-failure"})
    broker = RequestBroker(store, job["job_id"], api=object(), interval=0, retries=0)
    broker.api = None
    all_recorded = Barrier(4)
    record = broker._record

    def synced_record(*args, **kwargs):
        record(*args, **kwargs)
        if args[3] == "running":
            all_recorded.wait(timeout=10)

    monkeypatch.setattr(broker, "_record", synced_record)

    class Pool:
        calls = 0

        def execute(self, *args, **kwargs):
            self.calls += 1
            raise RequestFailure("provider_unavailable", "baostock_kline", "network receive error")

    broker.pool = Pool()

    def request(index):
        with pytest.raises((RequestFailure, SourceUnavailable)):
            broker.call("baostock_kline", code=f"sh.{600000 + index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(request, range(4)))
    with store.connect() as db:
        rows = db.execute("SELECT status,attempts,error_kind FROM job_requests WHERE job_id=?", (job["job_id"],)).fetchall()
    assert len(rows) == 4 and all(row["status"] == "failed" for row in rows)
    assert sum(row["attempts"] for row in rows) == broker.pool.calls == 3
    assert sum(row["error_kind"] == "source_unavailable" for row in rows) == 1
