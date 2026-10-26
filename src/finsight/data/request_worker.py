"""Long-lived, replaceable workers: one AKShare import, many bounded requests."""
import contextlib
import io
import time


def request_worker(connection):
    with contextlib.redirect_stdout(io.StringIO()):
        import akshare as ak
        import requests
    while True:
        try:
            message = connection.recv()
        except EOFError:
            break
        if message is None:
            break
        received = [0]
        original = requests.Session.send

        def measured(session, *args, **kwargs):
            response = original(session, *args, **kwargs)
            received[0] += len(response.content)
            return response

        requests.Session.send = measured
        started = time.perf_counter()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                frame = getattr(ak, message["method"])(**message["kwargs"])
            connection.send({"ok": True, "frame": frame, "http_bytes": received[0] or None,
                             "elapsed": time.perf_counter() - started})
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            connection.send({"ok": False, "kind": "rate_limit" if status == 429 else "upstream",
                             "error": type(exc).__name__, "http_status": status,
                             "http_bytes": received[0] or None, "elapsed": time.perf_counter() - started})
        finally:
            requests.Session.send = original
    connection.close()
