"""Identities describe selection policy; membership hashes describe a particular day's members."""
from __future__ import annotations

import pandas as pd

from finsight.models import fingerprint

MAINBOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")


def universe_id(mainboard: bool, symbols: list[str] | None = None, limit: int | None = None) -> str:
    policy = {"market": "SH-SZ-mainboard", "version": 1,
              "selection": "mainboard" if mainboard and symbols is None else "explicit",
              "symbols": sorted(set(symbols or [])) if not mainboard or symbols else [], "limit": limit}
    return ("mainboard-" if mainboard else "sample-") + fingerprint(policy)


def current_members(frame: pd.DataFrame) -> list[str]:
    latest = frame.sort_values("known_on", kind="stable").drop_duplicates("symbol", keep="last")
    if "is_member" in latest:
        latest = latest[latest["is_member"].fillna(False)]
    return sorted(set(latest["symbol"]))


def membership_id(symbols: list[str]) -> str:
    return fingerprint(sorted(set(symbols)))
