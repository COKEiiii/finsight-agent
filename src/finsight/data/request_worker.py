"""Long-lived, replaceable workers: one AKShare import, many bounded requests."""
import contextlib
import io
import time


class AlternateUnavailable(RuntimeError):
    pass


class ReceivingSocket:
    """Treat EOF as a broken connection instead of letting the SDK spin on empty recv()."""
    def __init__(self, socket):
        self.socket = socket

    def recv(self, *args, **kwargs):
        block = self.socket.recv(*args, **kwargs)
        if not block:
            raise ConnectionError("BaoStock connection closed during response")
        return block

    def __getattr__(self, name):
        return getattr(self.socket, name)


def reset_alternate_session():
    import baostock.common.context as context

    alternate_query.logged_in = False
    socket = getattr(context, "default_socket", None)
    if socket is not None:
        with contextlib.suppress(OSError):
            socket.close()
        delattr(context, "default_socket")


def checked_alternate_result(response, method):
    if response is None:
        reset_alternate_session()
        raise AlternateUnavailable(f"BaoStock {method}: no response")
    if response.error_code != "0":
        reset_alternate_session()
        if method == "login" or response.error_code.startswith("10002"):
            raise AlternateUnavailable(f"BaoStock {method}: {response.error_code}")
        raise ValueError(f"BaoStock {method}: {response.error_code}")


def alternate_query(method, kwargs):
    import baostock as bs
    import pandas as pd

    if not getattr(alternate_query, "logged_in", False):
        login = bs.login()
        checked_alternate_result(login, "login")
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
    checked_alternate_result(response, method)
    # BaoStock 0.9.4 get_data() still calls DataFrame.append, removed by Pandas 2.
    rows = []
    while response.next():
        checked_alternate_result(response, method)
        rows.append(response.get_row_data())
    checked_alternate_result(response, method)
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
                import baostock.common.context as context
                socket = getattr(context, "default_socket", None)
                if socket is not None and not isinstance(socket, ReceivingSocket):
                    context.default_socket = ReceivingSocket(socket)
                response = original_socket(*args, **kwargs)
                # ResultData.next() otherwise silently treats an empty transport reply as EOF.
                if not isinstance(response, str) or not response.strip():
                    raise AlternateUnavailable("BaoStock transport: empty response")
                return response

            socketutil.send_msg = paced_socket
        started = time.perf_counter()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                if message["method"] == "eastmoney_annual_page":
                    from finsight.data.eastmoney import annual_page
                    frame = annual_page(**message["kwargs"])
                else:
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
                reset_alternate_session()
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
