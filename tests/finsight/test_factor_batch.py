import json
import sqlite3
from collections import Counter
from contextlib import contextmanager

import pytest
from test_v2_pipeline import version2

from finsight.screening import screen


def test_batch_rows_use_one_connection_and_keep_legacy_keys_and_input_order(store, monkeypatch):
    legacy = {"symbol": "600000.SH", "roe": 10.0}
    store.factor("legacy", legacy)
    rows = [("legacy", legacy)] + [(f"new-{i}", {"value": i}) for i in range(800)]
    original = store.connect
    calls, statements = [], Counter()
    @contextmanager
    def measured():
        calls.append(True)
        with original() as db:
            db.set_trace_callback(lambda sql: statements.update([sql.split(None,1)[0]]))
            yield db
    monkeypatch.setattr(store, "connect", measured)
    result, hits = store.factor_rows(rows)
    assert result == [row for _, row in rows] and hits == 1
    assert len(calls) == 1 and statements["SELECT"] == 3 and statements["COMMIT"] == 1
    calls.clear()
    again, hits = store.factor_rows(rows[::-1])
    assert again == result[::-1] and hits == 801 and len(calls) == 1


@pytest.mark.parametrize("payload", ['not-json', '[]', '{"roe": true}', '{"roe": 999}'])
def test_bad_row_cache_is_replaced_without_changing_computed_values(store, payload):
    with store.connect() as db:
        db.execute("INSERT INTO factor_cache VALUES ('row',?)", (payload,))
    expected = {"roe": 1.0}
    result, hits = store.factor_rows([("row", expected)])
    assert result == [expected] and type(result[0]["roe"]) is float and hits == 0
    assert store.factor("row") == expected


def test_cache_write_failure_rolls_back_the_entire_batch(store):
    with store.connect() as db:
        db.execute("CREATE TRIGGER reject_row BEFORE INSERT ON factor_cache WHEN NEW.cache_key='reject' "
                   "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        store.factor_rows([("good", {"value": 1}), ("reject", {"value": 2})])
    assert store.factor("good") is None and store.factor("reject") is None


@pytest.mark.parametrize("rows", [[("good", {"value": 1}), ("bad", {"value": float('nan')})],
    [("same", {"value": 1}), ("same", {"value": 2})]])
def test_invalid_input_is_rejected_before_any_cache_transaction(store, monkeypatch, rows):
    monkeypatch.setattr(store, "connect", lambda: pytest.fail("must validate before opening SQLite"))
    with pytest.raises(ValueError):
        store.factor_rows(rows)


def test_empty_batch_needs_no_connection(store, monkeypatch):
    monkeypatch.setattr(store, "connect", lambda: pytest.fail("empty batch must not open SQLite"))
    assert store.factor_rows([]) == ([], 0)


@pytest.mark.parametrize("damage", ["invalid_json", "wrong_shape", "changed_value"])
def test_raw_batch_corruption_recomputes_without_changing_rankings(bundle, store, monkeypatch, damage):
    import finsight.screening as screening
    value = version2(bundle)
    original = screen(value, store=store)
    with store.connect() as db:
        row = db.execute("SELECT cache_key,payload FROM factor_cache WHERE payload LIKE '%content_id%'").fetchone()
        payload = json.loads(row["payload"])
        if damage == "invalid_json":
            corrupted = 'invalid'
        elif damage == "wrong_shape":
            corrupted = json.dumps({"rows": "wrong", "exclusions": []})
        else:
            payload["rows"][0]["roe"] = 999999
            corrupted = json.dumps(payload)
        db.execute("UPDATE factor_cache SET payload=? WHERE cache_key=?", (corrupted, row["cache_key"]))
    calls = []
    compute = screening.compute_factors
    def counted(*args, **kwargs):
        calls.append(True)
        return compute(*args, **kwargs)
    monkeypatch.setattr(screening, "compute_factors", counted)
    revised = screen(value, store=store)
    assert revised.candidates == original.candidates and revised.exclusions == original.exclusions
    assert revised.factor_cache_hits == original.eligible_count and len(calls) == 1
