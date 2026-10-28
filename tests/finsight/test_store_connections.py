import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def test_connection_commits_and_closes_on_success(store):
    with store.connect() as db:
        db.execute("INSERT INTO collector_control VALUES ('connection-test','committed')")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    with store.connect() as reopened:
        assert reopened.execute("SELECT value FROM collector_control WHERE key='connection-test'").fetchone()[0] == "committed"


def test_connection_rolls_back_and_closes_on_exception(store):
    with pytest.raises(ValueError, match="fixture"):
        with store.connect() as db:
            db.execute("INSERT INTO collector_control VALUES ('rolled-back','no')")
            raise ValueError("fixture")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    with store.connect() as reopened:
        assert reopened.execute("SELECT value FROM collector_control WHERE key='rolled-back'").fetchone() is None


def test_many_connections_with_gc_disabled_fit_launchd_file_limit(tmp_path):
    # Isolate the low OS resource limit to a subprocess. The old transaction-only
    # context failed with SQLITE_CANTOPEN before reaching 300 reads here.
    script = """
import gc, resource, sys
from finsight.data.store import Store
store = Store(sys.argv[1])
resource.setrlimit(resource.RLIMIT_NOFILE, (256, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
gc.disable()
for _ in range(1000):
    with store.connect() as db:
        assert db.execute('SELECT 1').fetchone()[0] == 1
print('1000 connections closed')
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    result = subprocess.run([sys.executable,"-c",script,str(tmp_path)],env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert "1000 connections closed" in result.stdout
