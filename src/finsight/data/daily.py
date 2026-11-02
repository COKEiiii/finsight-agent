"""Validate whole-day raw quotes without inferring missing securities or values."""
from datetime import date

import pandas as pd

DAILY_FIELDS = {"date", "code", "open", "high", "low", "close", "preclose", "volume", "amount",
                "tradestatus", "peTTM", "pbMRQ", "isST", "adjustflag"}


def validate_daily(frame: pd.DataFrame, day: str) -> pd.DataFrame:
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("daily date must be YYYY-MM-DD")
    if frame.empty or not DAILY_FIELDS.issubset(frame.columns):
        raise ValueError("daily raw quote schema incomplete")
    if not frame["date"].eq(day).all() or not frame["adjustflag"].eq("3").all():
        raise ValueError("daily raw quote date or adjustment mismatch")
    if (not frame["code"].astype(str).str.fullmatch(r"(?:sh|sz|bj)\.\d{6}").all()
            or frame["code"].duplicated().any()):
        raise ValueError("daily raw quote security identity invalid")
    return frame[sorted(DAILY_FIELDS)].copy()
