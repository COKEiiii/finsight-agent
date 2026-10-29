"""User-supplied account comparisons; absence is never an empty account."""
import csv
import io
import math
import re


def actual_holdings(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("actual holdings must be an object of symbol -> integer shares")
    result = {}
    for symbol, quantity in value.items():
        if not isinstance(symbol, str):
            raise ValueError("holding symbol must be a string")
        symbol = symbol.strip().upper()
        if re.fullmatch(r"[0-9]{6}", symbol):
            symbol += ".SH" if symbol[0] in "569" else ".BJ" if symbol[0] in "48" else ".SZ"
        if not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", symbol):
            raise ValueError("holding symbol must contain six digits and an exchange suffix")
        if symbol in result:
            raise ValueError("duplicate normalized holding symbol")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise ValueError("actual holding quantity must be a nonnegative integer")
        result[symbol] = quantity
    return dict(sorted(result.items()))


def holdings_csv(text):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
    if reader.fieldnames is None or len(reader.fieldnames) != 2 or set(reader.fieldnames) != {"symbol", "quantity"}:
        raise ValueError("holdings CSV requires exactly symbol,quantity columns")
    values = {}
    try:
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("malformed holdings CSV row")
            symbol, quantity = row["symbol"].strip(), row["quantity"].strip()
            if not quantity.isascii() or not quantity.isdigit():
                raise ValueError("holding quantity must be a nonnegative integer")
            if symbol in values:
                raise ValueError("duplicate holding symbol")
            values[symbol] = int(quantity)
    except csv.Error as exc:
        raise ValueError("malformed holdings CSV") from exc
    if not values:
        raise ValueError("holdings CSV has no rows; explicitly confirm an empty account if applicable")
    return actual_holdings(values)


def validate_actual_cash(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                              not math.isfinite(value) or value < 0):
        raise ValueError("actual cash must be finite and nonnegative")
    return value
