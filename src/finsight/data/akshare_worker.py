"""Isolate upstream calls so even endpoints without timeouts can be terminated."""
import contextlib
import io
import json
import sys


def main():
    request = json.loads(sys.stdin.read())
    with contextlib.redirect_stdout(io.StringIO()):
        import akshare as ak
        frame = getattr(ak, request["method"])(**request["kwargs"])
    print(frame.to_json(orient="table", index=False, date_format="iso", force_ascii=False))


if __name__ == "__main__":
    main()
