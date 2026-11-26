"""SQLite storage layer.

Schema (all tables created if missing):

tournaments(id, name, created_at, version)
matches(tournament_id, match_id,
        slot_a_type/slot_a_team/slot_a_match, slot_b_...)
results(tournament_id, match_id, version, active,
        winner, loser, score_a, score_b, note)
previews(token, tournament_id, target_match_id, new_winner,
         fingerprint, voided_json, consumed, created_at)

Only rows with active=1 feed later matches. Every correction inserts new
result rows (version increments) instead of mutating history, so the voided
records remain auditable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("BRACKET_DB", "/data/bracket.db")

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction():
    """Process-wide write lock + DEFERRED transaction for serialised writes."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
    match_id INTEGER NOT NULL,
    slot_a_type TEXT NOT NULL,
    slot_a_team TEXT,
    slot_a_match INTEGER,
    slot_b_type TEXT NOT NULL,
    slot_b_team TEXT,
    slot_b_match INTEGER,
    PRIMARY KEY (tournament_id, match_id)
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    match_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    active INTEGER NOT NULL,
    winner TEXT NOT NULL,
    loser TEXT NOT NULL,
    score_a INTEGER NOT NULL,
    score_b INTEGER NOT NULL,
    note TEXT,
    UNIQUE (tournament_id, match_id, version)
);
CREATE TABLE IF NOT EXISTS previews (
    token TEXT PRIMARY KEY,
    tournament_id INTEGER NOT NULL,
    target_match_id INTEGER NOT NULL,
    new_winner TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    voided_json TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------- small helpers ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_matches(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM matches WHERE tournament_id=? ORDER BY match_id",
        (tournament_id,),
    ).fetchall()


def active_results(conn: sqlite3.Connection, tournament_id: int) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM results WHERE tournament_id=? AND active=1",
        (tournament_id,),
    ).fetchall()
    return {r["match_id"]: r for r in rows}


def max_result_version(conn: sqlite3.Connection, tournament_id: int, match_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM results "
        "WHERE tournament_id=? AND match_id=?",
        (tournament_id, match_id),
    ).fetchone()
    return row["v"]


def insert_preview(conn: sqlite3.Connection, token: str, tournament_id: int,
                   target_match_id: int, new_winner: str, fingerprint: str,
                   voided: list[dict]) -> None:
    # prune consumed preview rows so the table stays bounded; unconsumed
    # tokens are kept for their re-review/fingerprint role
    conn.execute("DELETE FROM previews WHERE consumed=1")
    conn.execute(
        "INSERT INTO previews(token, tournament_id, target_match_id, new_winner, "
        "fingerprint, voided_json, consumed, created_at) VALUES (?,?,?,?,?,?,0,?)",
        (token, tournament_id, target_match_id, new_winner, fingerprint,
         json.dumps(voided, ensure_ascii=False, sort_keys=True), now_iso()),
    )
