"""Offline synthetic screening/cache benchmark; no collection or model requests."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pandas as pd

from finsight.data.demo import make_demo
from finsight.data.store import Store, code_version
from finsight.data.universe import current_members, membership_id
from finsight.models import fingerprint
from finsight.screening import screen


def synthetic_universe(count):
    bundle = make_demo(date(2025,2,28), date(2024,1,2))
    source_symbols = list(bundle["universe"]["symbol"])
    symbols = [f"{600000+i:06d}.SH" if i < 1000 else f"{601000+i-1000:06d}.SH" if i < 2000
               else f"{i-1999:06d}.SZ" for i in range(count)]
    mapping = pd.DataFrame({"_old": [source_symbols[i % len(source_symbols)] for i in range(count)], "symbol": symbols})
    for name in ("universe", "prices", "financials", "valuations"):
        original = bundle[name].rename(columns={"symbol": "_old"})
        if name in {"prices", "valuations"}:
            original = original.groupby("_old").tail(61)
        bundle.tables[name] = mapping.merge(original, on="_old").drop(columns="_old")
    bundle.meta.schema_version = 2
    bundle.meta.membership_symbols = current_members(bundle["universe"])
    bundle.meta.membership_id = membership_id(bundle.meta.membership_symbols)
    bundle.meta.universe_id = "synthetic-cache-benchmark"
    return bundle


def benchmark(count=3000):
    bundle = synthetic_universe(count)
    with tempfile.TemporaryDirectory(prefix="finsight-cache-benchmark-") as folder:
        store = Store(folder)
        original = store.connect
        counters = Counter()

        @contextmanager
        def measured():
            counters["connections"] += 1
            with original() as db:
                def trace(statement):
                    counters[statement.split(None,1)[0].upper()] += 1
                db.set_trace_callback(trace)
                yield db
        store.connect = measured
        runs = []
        for label in ("cold", "warm"):
            counters.clear()
            start = time.perf_counter()
            result = screen(bundle, store=store)
            elapsed = time.perf_counter() - start
            assert result.eligible_count == count and not result.research_eligible
            runs.append({"kind": label, "seconds": elapsed, "sqlite": dict(counters),
                "factor_cache_hits": result.factor_cache_hits,
                "output_fingerprint": fingerprint({"candidates": result.candidates, "exclusions": result.exclusions})})
        assert runs[0]["output_fingerprint"] == runs[1]["output_fingerprint"]
    return {"synthetic": True, "code_version": code_version(), "stocks": count,
        "price_rows": len(bundle["prices"]), "valuation_rows": len(bundle["valuations"]),
        "scope": "In-memory 61-session input; factors, cache access and ranking; excludes collection, snapshot loading and exports.",
        "runs": runs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stocks", type=int, default=3000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.stocks <= 3000:
        parser.error("stocks must be 1..3000")
    report = benchmark(args.stocks)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
