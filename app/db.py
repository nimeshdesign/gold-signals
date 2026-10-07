"""SQLite storage shared by the engine (writer) and the website (reader + subscriber writes)."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    entry           REAL NOT NULL,
    sl              REAL NOT NULL,
    tp1             REAL NOT NULL,
    tp2             REAL NOT NULL,
    bar_time        TEXT NOT NULL UNIQUE,   -- open time of the trigger candle (idempotency key)
    created_at      TEXT NOT NULL,          -- when the signal became valid (trigger candle close)
    status          TEXT NOT NULL DEFAULT 'open',
    result_r        REAL,
    tp1_hit_at      TEXT,
    closed_at       TEXT,
    last_checked    TEXT,                   -- open time of the last candle applied by the tracker
    telegram_msg_id INTEGER,
    atr             REAL,
    rsi             REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);

CREATE TABLE IF NOT EXISTS subscribers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    email                   TEXT,
    stripe_customer_id      TEXT,
    stripe_subscription_id  TEXT UNIQUE,
    stripe_session_id       TEXT UNIQUE,
    status                  TEXT NOT NULL DEFAULT 'active',
    invite_link             TEXT UNIQUE,
    telegram_user_id        INTEGER,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- signals ----------

def insert_signal(conn, *, symbol, direction, entry, sl, tp1, tp2, bar_time, created_at, atr=None, rsi=None,
                  last_checked=None) -> int | None:
    """Insert a signal. Returns the new id, or None if a signal for this candle already exists.

    `last_checked` is the open time of the last 15-min candle that is already in the past at entry;
    the tracker starts with the candle after it. Defaults to `bar_time` (15-min signals).
    """
    cur = conn.execute(
        """INSERT OR IGNORE INTO signals
           (symbol, direction, entry, sl, tp1, tp2, bar_time, created_at, last_checked, atr, rsi)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, direction, entry, sl, tp1, tp2, bar_time, created_at, last_checked or bar_time, atr, rsi),
    )
    return cur.lastrowid if cur.rowcount else None


def set_signal_message(conn, signal_id: int, msg_id: int | None) -> None:
    conn.execute("UPDATE signals SET telegram_msg_id = ? WHERE id = ?", (msg_id, signal_id))


def open_signals(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM signals WHERE status IN ('open', 'tp1') ORDER BY id").fetchall()


def update_signal(conn, signal_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE signals SET {cols} WHERE id = ?", (*fields.values(), signal_id))


def all_signals(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM signals ORDER BY created_at DESC, id DESC").fetchall()


# ---------- subscribers ----------

def get_subscriber_by(conn, column: str, value) -> sqlite3.Row | None:
    if column not in {"id", "stripe_session_id", "stripe_subscription_id", "invite_link", "telegram_user_id"}:
        raise ValueError(column)
    return conn.execute(f"SELECT * FROM subscribers WHERE {column} = ?", (value,)).fetchone()


def upsert_subscriber_from_checkout(conn, *, session_id, email, customer_id, subscription_id) -> sqlite3.Row:
    now = utcnow_iso()
    existing = get_subscriber_by(conn, "stripe_session_id", session_id)
    if existing is None and subscription_id:
        existing = get_subscriber_by(conn, "stripe_subscription_id", subscription_id)
    if existing is None:
        conn.execute(
            """INSERT INTO subscribers
               (email, stripe_customer_id, stripe_subscription_id, stripe_session_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (email, customer_id, subscription_id, session_id, now, now),
        )
    return get_subscriber_by(conn, "stripe_session_id", session_id) or get_subscriber_by(
        conn, "stripe_subscription_id", subscription_id
    )


def update_subscriber(conn, subscriber_id: int, **fields) -> None:
    fields["updated_at"] = utcnow_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE subscribers SET {cols} WHERE id = ?", (*fields.values(), subscriber_id))


# ---------- key/value (e.g. Telegram update offset) ----------

def kv_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
