"""Reuse per-stock observations while keeping universe membership independent."""
from __future__ import annotations

from datetime import date, datetime

from finsight.data.store import Store


def select_seeds(store: Store, target_universe: str, cutoff: date, now: datetime) -> list[str]:
    selected, seen = [], set()
    for snapshot in store.snapshots():
        if (snapshot.synthetic or snapshot.source != "akshare" or snapshot.schema_version < 2 or
            snapshot.universe_id == target_universe or not snapshot.universe_id or
            snapshot.universe_id in seen or snapshot.as_of > cutoff or snapshot.captured_at > now):
            continue
        selected.append(snapshot.snapshot_id)
        seen.add(snapshot.universe_id)
        if len(selected) == 8:
            break
    return selected
