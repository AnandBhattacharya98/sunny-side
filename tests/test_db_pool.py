"""Postgres connection pool. Runs only when TEST_DATABASE_URL points at a Postgres server."""
import importlib
import os
import sys

import pytest

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def pgdb(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("DB_POOL_MAX", "2")
    import db
    mod = importlib.reload(db)
    yield mod
    monkeypatch.delenv("DATABASE_URL")
    importlib.reload(db)


def _backend_pid(conn):
    return conn.execute("SELECT pg_backend_pid()").fetchone()[0]


def test_connections_are_reused(pgdb):
    c1 = pgdb.get_conn()
    pid = _backend_pid(c1)
    c1.close()
    c2 = pgdb.get_conn()
    assert _backend_pid(c2) == pid
    c2.close()


def test_full_pool_falls_back_to_direct_connection(pgdb):
    held = [pgdb.get_conn() for _ in range(3)]
    assert len({_backend_pid(c) for c in held}) == 3
    for c in held:
        c.close()
        c.close()  # closing twice is harmless


def test_uncommitted_work_is_rolled_back_on_return(pgdb):
    c = pgdb.get_conn()
    c.execute("CREATE TEMP TABLE IF NOT EXISTS t (x int)")
    c.commit()
    c.execute("INSERT INTO t VALUES (1)")
    c.close()
    c = pgdb.get_conn()
    assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    c.close()


def test_dead_connection_is_replaced(pgdb):
    c = pgdb.get_conn()
    pid = _backend_pid(c)
    c.close()
    killer = pgdb.get_conn()
    killer.close()
    other = pgdb.psycopg2.connect(URL)
    other.autocommit = True
    other.cursor().execute("SELECT pg_terminate_backend(%s)", (pid,))
    other.close()
    c = pgdb.get_conn()
    assert c.execute("SELECT 1").fetchone()[0] == 1
    c.close()


def test_forgotten_close_returns_connection(pgdb):
    for _ in range(5):
        c = pgdb.get_conn()
        c.execute("SELECT 1")
        del c
    assert len(pgdb._get_pool()._used) == 0
