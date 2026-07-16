"""Guard: hot lookup predicates stay index-backed.

The partial unique indexes (one_current_node_version, one_current_edge_version)
only cover current-version rows. Everything that walks history or follows
edges by endpoint -- the semantic-relation conflict check in
store/service.py, and purge's blast-radius queries -- previously fell back
to full table scans.

Plans are asserted via EXPLAIN QUERY PLAN, so a schema change that silently
drops an index back to a scan fails here rather than in production latency.
"""

from __future__ import annotations

import sqlite3

import pytest

from imprint.store import ImprintStore
from imprint.store.schema import SCHEMA_SQL


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_SQL)
    yield connection
    connection.close()


def _plan(conn, sql: str) -> str:
    parameters = tuple("x" for _ in range(sql.count("?")))
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
    return " | ".join(row[3] for row in rows)


def test_edge_endpoint_lookups_are_index_backed(conn):
    plan = _plan(conn, "SELECT edge_id FROM edges WHERE source_id=? OR target_id=?")
    assert "SCAN edges" not in plan
    assert "edges_by_source" in plan and "edges_by_target" in plan


def test_version_history_lookups_are_index_backed(conn):
    node_plan = _plan(conn, "SELECT DISTINCT event_id FROM node_versions WHERE node_id IN (?)")
    assert "SCAN node_versions" not in node_plan
    assert "node_versions_by_node" in node_plan

    edge_plan = _plan(conn, "SELECT DISTINCT event_id FROM edge_versions WHERE edge_id IN (?)")
    assert "SCAN edge_versions" not in edge_plan
    assert "edge_versions_by_edge" in edge_plan


def test_existing_store_gains_indexes_on_next_initialize(tmp_path):
    """Stores created before these indexes existed must pick them up without a
    migration: initialize() re-runs SCHEMA_SQL on existing stores and every
    compile/CLI path calls it."""
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    added = {"edges_by_source", "edges_by_target", "node_versions_by_node", "edge_versions_by_edge"}
    with store.connect() as connection:
        for name in added:
            connection.execute(f"DROP INDEX {name}")
        remaining = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert not added & remaining, "drop failed; upgrade test would be vacuous"

    store.initialize()
    with store.connect() as connection:
        restored = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert added <= restored
