"""One request-start budget shared by HTTP pagination and persistent workers."""
import sqlite3
import time


def reserve(database: str, interval: float) -> float:
    with sqlite3.connect(database, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM collector_control WHERE key='next_request'").fetchone()
        now = time.time()
        start = max(now, float(row[0]) if row else now)
        db.execute("INSERT OR REPLACE INTO collector_control VALUES ('next_request',?)", (str(start + interval),))
    return max(0, start - now)
