from __future__ import annotations

import time
from datetime import date
from hashlib import sha256
from uuid import uuid4

import pandas as pd

from finsight.data.store import SnapshotBundle, Store, code_version
from finsight.factors import coverage_report, evaluate
from finsight.models import FACTORS, ScreeningResult, StrategyConfig, fingerprint


def compute_factors(bundle: SnapshotBundle, as_of: date, config: StrategyConfig):
    return evaluate(bundle, as_of, config)


def screen(bundle: SnapshotBundle, config: StrategyConfig | None = None, as_of: date | None = None,
           baseline: bool = False, run_id: str | None = None, preview: bool = False,
           store: Store | None = None) -> ScreeningResult:
    started = time.perf_counter()
    if not bundle.meta.publishable and not preview:
        raise ValueError("snapshot failed batch validation; a formal ranking cannot be published")
    config, as_of = config or StrategyConfig(), as_of or bundle.meta.as_of
    engine_version = code_version()
    input_hashes = {name: {"columns": list(bundle[name].columns), "hash": sha256(
        pd.util.hash_pandas_object(bundle[name], index=True).to_numpy().tobytes()).hexdigest()}
        for name in ("universe", "prices", "financials", "valuations", "calendar")} if store else {}
    raw_key = fingerprint({"kind": "raw_factor_batch", "tables": input_hashes,
        "as_of": str(as_of), "config": config.config_id, "engine": engine_version,
        "source": bundle.meta.source, "captured_at": str(bundle.meta.captured_at),
        "universe": bundle.meta.universe_id, "members": bundle.meta.membership_id,
        "requested_members": bundle.meta.membership_symbols}) if store else None
    raw_cached = store.factor(raw_key) if raw_key else None
    if raw_cached is not None:
        frame, exclusions = pd.DataFrame(raw_cached["rows"]), raw_cached["exclusions"]
    else:
        frame, exclusions = compute_factors(bundle, as_of, config)
        if raw_key:
            store.factor(raw_key, {"rows": frame.to_dict("records"), "exclusions": exclusions})
    coverage = coverage_report(bundle, frame, exclusions)
    published = bundle.meta.publishable and (coverage['meets_threshold'] or bundle.meta.schema_version < 2)
    if not published and not preview:
        raise ValueError("data coverage below 98%; a formal ranking cannot be published (use --preview)")
    hits = len(frame) if raw_cached is not None else 0
    if not frame.empty:
        if store and raw_cached is None:
            rows = []
            for row in frame.to_dict('records'):
                key = fingerprint({'config': config.config_id, 'raw_factor_row': row})
                cached = store.factor(key)
                if cached:
                    row, hits = cached, hits + 1
                else:
                    store.factor(key, row)
                rows.append(row)
            frame = pd.DataFrame(rows)
        frame['score'] = 0.0
        for factor in FACTORS:
            frame[f'{factor}_score'] = frame[factor].rank(method='average', pct=True,
                ascending=factor not in {'pe_ttm', 'pb'}) * 100
            frame['score'] += config.weights[factor] * frame[f'{factor}_score']
        if baseline:
            frame['score'] = frame['momentum_60_score']
        frame = frame.sort_values(['score', 'symbol'], ascending=[False, True]).head(config.top_n)
        frame['rank'] = range(1, len(frame) + 1)
    return ScreeningResult(
        run_id=run_id or uuid4().hex, snapshot_id=bundle.meta.snapshot_id, as_of=as_of,
        strategy_version=config.version, config_id=config.config_id, code_version=engine_version,
        candidates=frame.to_dict('records'), exclusions=exclusions, eligible_count=coverage['eligible'],
        universe_count=coverage['required'] + coverage['confirmed_exclusions'], synthetic=bundle.meta.synthetic,
        research_eligible=bundle.meta.research_eligible and published, schema_version=bundle.meta.schema_version,
        universe_id=bundle.meta.universe_id, membership_id=bundle.meta.membership_id,
        publication_status='published' if published else 'preview',
        coverage={key: value for key, value in coverage.items() if key != 'missing'} | {
            'missing_symbols': [row['symbol'] for row in coverage['missing']]},
        factor_cache_hits=hits, elapsed_seconds=time.perf_counter() - started,
    )
