"""Bounded pages from the public endpoint also used by AKShare's annual report API."""
from __future__ import annotations

from datetime import date
from numbers import Integral

import pandas as pd
import requests


def _validate_period(report_date: str) -> None:
    period = date.fromisoformat(report_date)
    if period.isoformat() != report_date or (period.month, period.day) != (12, 31):
        raise ValueError("annual report date must be YYYY-12-31")


def annual_page(report_date: str, page: int, page_size: int = 500, scope: str = "ashare_v1") -> pd.DataFrame:
    _validate_period(report_date)
    if type(page) is not int or not 1 <= page <= 1000 or type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError("invalid annual pagination")
    if scope != "ashare_v1":
        raise ValueError("unsupported annual scope")
    response = requests.get("https://datacenter-web.eastmoney.com/api/data/v1/get", params={
        "sortColumns": "SECURITY_CODE", "sortTypes": "1", "pageSize": str(page_size),
        "pageNumber": str(page), "reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
        "filter": f"(REPORTDATE='{report_date}')(SECURITY_TYPE_CODE=\"058001001\")"}, timeout=25)
    response.raise_for_status()
    result = response.json().get("result")
    if not isinstance(result, dict):
        raise ValueError("annual page has no result")
    pages, count = result.get("pages"), result.get("count")
    if (type(pages) is not int or not 1 <= pages <= 1000 or type(count) is not int
            or not pages <= count <= pages * page_size or page > pages):
        raise ValueError("invalid annual page metadata")
    frame = pd.DataFrame(result.get("data") or [])
    if frame.empty or not {"SECURITY_CODE", "REPORTDATE", "SECURITY_TYPE_CODE"}.issubset(frame.columns):
        raise ValueError("annual page rows missing")
    if not frame["REPORTDATE"].astype(str).str.fullmatch(report_date + r"(?: 00:00:00)?").all():
        raise ValueError("annual page contains another report period")
    if not frame["SECURITY_TYPE_CODE"].eq("058001001").all():
        raise ValueError("annual page contains another security type")
    frame["_response_page"], frame["_response_pages"], frame["_response_count"] = page, pages, count
    frame["_query_scope"] = scope
    return frame


def annual_pages(call, report_date: str, progress=None, *, page_size: int = 500) -> pd.DataFrame:
    """Each page is an independent broker checkpoint, including its first observation time."""
    _validate_period(report_date)
    if type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError("invalid annual page size")
    pages, total, parts = 1, None, []
    page = 1
    while page <= pages:
        frame = call("eastmoney_annual_page", report_date=report_date, page=page, page_size=page_size, scope="ashare_v1")
        required = {"SECURITY_CODE", "REPORTDATE", "SECURITY_TYPE_CODE", "WEIGHTAVG_ROE", "YSTZ", "UPDATE_DATE",
            "NOTICE_DATE", "_response_page", "_response_pages", "_response_count", "_query_scope"}
        if frame.empty or not required.issubset(frame.columns):
            raise ValueError("annual page schema incomplete")
        page_counts = frame["_response_pages"].unique()
        row_counts = frame["_response_count"].unique()
        if len(page_counts) != 1 or len(row_counts) != 1:
            raise ValueError("annual page metadata inconsistent")
        n, count = page_counts[0], row_counts[0]
        if (not isinstance(n, Integral) or isinstance(n, bool) or not isinstance(count, Integral) or
                isinstance(count, bool) or not 1 <= n <= 1000 or not n <= count <= n * page_size):
            raise ValueError("annual page metadata invalid")
        if not frame["_response_page"].eq(page).all() or not frame["_query_scope"].eq("ashare_v1").all():
            raise ValueError("annual page identity mismatch")
        if not frame["REPORTDATE"].astype(str).str.fullmatch(report_date + r"(?: 00:00:00)?").all() or not frame["SECURITY_TYPE_CODE"].eq("058001001").all():
            raise ValueError("annual page scope mismatch")
        if total is not None and (n != pages or count != total):
            raise ValueError("annual dataset changed during pagination")
        pages, total = int(n), int(count)
        part = frame.copy()
        part["_page_fetched_at"] = frame.attrs["fetched_at"]
        parts.append(part)
        if progress:
            progress(page, pages)
        page += 1
    combined = pd.concat(parts, ignore_index=True)
    if len(combined) != total or combined["SECURITY_CODE"].duplicated().any():
        raise ValueError("annual dataset missing or duplicate securities")
    codes = combined["SECURITY_CODE"].astype(str)
    if not codes.str.fullmatch(r"\d{6}").all() or not codes.is_monotonic_increasing:
        raise ValueError("annual dataset security ordering invalid")
    return combined
