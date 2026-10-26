"""Manual, free-network smoke test for the optional BaoStock/Sina fallback."""
from __future__ import annotations

import tempfile
from datetime import date

import pandas as pd

from finsight.data.collector import Collector
from finsight.data.requests import RequestBroker
from finsight.data.store import Store
from finsight.jobs import Jobs


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(directory)
        job = Jobs(store).create({"source": "alternate-smoke"})
        broker = RequestBroker(store, job["job_id"], workers=2, interval=0, timeout=30, retries=0)
        try:
            collector = Collector(broker, {"mainboard": True})
            collector.fallback_market = True
            lower, day = "2025-08-19", "2026-10-23"
            calendar = pd.DataFrame({"trade_date": pd.bdate_range("2025-01-01", "2026-12-31").strftime("%Y-%m-%d")})
            for code in ("600036", "000001"):
                prices, vals, _ = collector._bars_baostock(code, lower, day)
                fin = collector.financial_history_fallback(code + (".SH" if code.startswith("6") else ".SZ"),
                    date.fromisoformat(lower), date.fromisoformat(day), calendar)
                print(code, "bars", len(prices), "valuations", len(vals), "annual", len(fin),
                    "latest", prices["trade_date"].max(), "roe", fin["roe"].tail(1).tolist(),
                    "growth", fin["revenue_growth"].tail(1).tolist())
                if len(prices) < 61 or vals.empty or fin.empty:
                    raise RuntimeError("alternate source did not supply required sample fields")
        finally:
            broker.close()


if __name__ == "__main__":
    main()
