"""Long-lived, replaceable workers: one AKShare import, many bounded requests."""
import contextlib
import io
import time


class AlternateUnavailable(RuntimeError):
    pass


def alternate_query(method, kwargs):
    import baostock as bs
    import pandas as pd

    if not getattr(alternate_query, "logged_in", False):
        login = bs.login()
        if login.error_code != "0":
            raise AlternateUnavailable(f"BaoStock login: {login.error_code}")
        alternate_query.logged_in = True
    if method == "baostock_kline":
        fields = "date,code,open,high,low,close,preclose,volume,amount,tradestatus,peTTM,pbMRQ,isST" if kwargs["adjustflag"] == "3" else "date,close"
        response = bs.query_history_k_data_plus(kwargs["code"], fields, start_date=kwargs["start_date"],
            end_date=kwargs["end_date"], frequency="d", adjustflag=kwargs["adjustflag"])
    elif method == "baostock_profit":
        response = bs.query_profit_data(code=kwargs["code"], year=kwargs["year"], quarter=4)
    elif method == "baostock_industry":
        response = bs.query_stock_industry()
    elif method == "baostock_basic":
        response = bs.query_stock_basic()
    elif method == "baostock_index":
        response = bs.query_history_k_data_plus("sh.000300", "date,close", start_date=kwargs["start_date"],
            end_date=kwargs["end_date"], frequency="d", adjustflag="3")
    else:
        raise ValueError("unknown alternate data query")
    if response.error_code != "0":
        alternate_query.logged_in = False
        if response.error_code.startswith("10002"):
            raise AlternateUnavailable(f"BaoStock {method}: {response.error_code}")
        raise ValueError(f"BaoStock {method}: {response.error_code}")
    # BaoStock 0.9.4 get_data() still calls DataFrame.append, removed by Pandas 2.
    rows = []
    while response.next():
        rows.append(response.get_row_data())
    return pd.DataFrame(rows, columns=response.fields)


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
        request_count = [0]
        original = requests.Session.send
        original_socket = None
        rate = message.get("rate")

        def budget():
            if rate:
                from finsight.data.rate import reserve
                wait = reserve(rate["database"], rate["interval"])
                if wait:
                    time.sleep(wait)

        def measured(session, *args, **kwargs):
            budget()
            request_count[0] += 1
            response = original(session, *args, **kwargs)
            received[0] += len(response.content)
            return response

        requests.Session.send = measured
        if message["method"].startswith("baostock_"):
            import baostock.util.socketutil as socketutil
            original_socket = socketutil.send_msg

            def paced_socket(*args, **kwargs):
                budget()
                request_count[0] += 1
                return original_socket(*args, **kwargs)

            socketutil.send_msg = paced_socket
        started = time.perf_counter()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                frame = (alternate_query(message["method"], message["kwargs"])
                         if message["method"].startswith("baostock_") else
                         getattr(ak, message["method"])(**message["kwargs"]))
            connection.send({"ok": True, "frame": frame, "http_bytes": received[0] or None,
                             "network_requests": request_count[0],
                             "elapsed": time.perf_counter() - started})
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if message["method"].startswith("baostock_"):
                alternate_query.logged_in = False
            kind = "provider_unavailable" if isinstance(exc, AlternateUnavailable) else "rate_limit" if status == 429 else "upstream"
            connection.send({"ok": False, "kind": kind,
                             "error": str(exc) if isinstance(exc, AlternateUnavailable) else type(exc).__name__, "http_status": status,
                             "network_requests": request_count[0],
                             "http_bytes": received[0] or None, "elapsed": time.perf_counter() - started})
        finally:
            requests.Session.send = original
            if original_socket is not None:
                socketutil.send_msg = original_socket
    connection.close()
