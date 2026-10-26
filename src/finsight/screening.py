from __future__ import annotations

import time
from datetime import date
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
    frame, exclusions = compute_factors(bundle, as_of, config)
    coverage = coverage_report(bundle, frame, exclusions)
    published = bundle.meta.publishable and (coverage['meets_threshold'] or bundle.meta.schema_version < 2)
    if not published and not preview:
        raise ValueError("data coverage below 98%; a formal ranking cannot be published (use --preview)")
    hits = 0
    if not frame.empty:
        if store:
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
        strategy_version=config.version, config_id=config.config_id, code_version=code_version(),
        candidates=frame.to_dict('records'), exclusions=exclusions, eligible_count=coverage['eligible'],
        universe_count=coverage['required'] + coverage['confirmed_exclusions'], synthetic=bundle.meta.synthetic,
        research_eligible=bundle.meta.research_eligible and published, schema_version=bundle.meta.schema_version,
        universe_id=bundle.meta.universe_id, membership_id=bundle.meta.membership_id,
        publication_status='published' if published else 'preview',
        coverage={key: value for key, value in coverage.items() if key != 'missing'} | {
            'missing_symbols': [row['symbol'] for row in coverage['missing']]},
        factor_cache_hits=hits, elapsed_seconds=time.perf_counter() - started,
    )
