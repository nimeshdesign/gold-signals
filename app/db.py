"""SQLite storage shared by the engine (writer) and the website (reader, plus login sessions)."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
    rsi             REAL,
    strategy        TEXT NOT NULL DEFAULT 'session_breakout'
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);


-- 15-minute price candles (open time, UTC ISO) for the chart page.
CREATE TABLE IF NOT EXISTS candles (
    time  TEXT PRIMARY KEY,
    open  REAL NOT NULL,
    high  REAL NOT NULL,
    low   REAL NOT NULL,
    close REAL NOT NULL
);

-- Every Telegram message goes through here, so a failed send is retried instead of lost.
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat            TEXT NOT NULL,
    text            TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- 'signal', 'update' (reply to a signal) or 'info'
    signal_id       INTEGER,                -- the signal this message is, or replies to
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending / sent / failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_try        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    sent_at         TEXT,
    msg_id          INTEGER,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, next_try);

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
        # Upgrade older databases in place.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
        if "strategy" not in cols:
            conn.execute("ALTER TABLE signals ADD COLUMN strategy TEXT NOT NULL DEFAULT 'session_breakout'")


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
                  last_checked=None, strategy: str = "session_breakout", main: bool = True) -> int | None:
    """Insert a signal. Returns the new id, or None if this setup already signalled on this candle.

    `last_checked` is the open time of the last 15-min candle that is already in the past at entry;
    the tracker starts with the candle after it. Defaults to `bar_time` (15-min signals).
    Extra setups get "#<strategy>" appended to the idempotency key so two setups can fire on one candle.
    """
    key = bar_time if main else f"{bar_time}#{strategy}"
    cur = conn.execute(
        """INSERT OR IGNORE INTO signals
           (symbol, direction, entry, sl, tp1, tp2, bar_time, created_at, last_checked, atr, rsi, strategy)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, direction, entry, sl, tp1, tp2, key, created_at, last_checked or bar_time, atr, rsi, strategy),
    )
    return cur.lastrowid if cur.rowcount else None


def signal_exists(conn, key: str) -> bool:
    """Whether a signal with this idempotency key (bar_time, or bar_time#strategy) was already recorded."""
    return conn.execute("SELECT 1 FROM signals WHERE bar_time = ?", (key,)).fetchone() is not None


def set_signal_message(conn, signal_id: int, msg_id: int | None) -> None:
    conn.execute("UPDATE signals SET telegram_msg_id = ? WHERE id = ?", (msg_id, signal_id))


def open_signals(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM signals WHERE status IN ('open', 'tp1') ORDER BY id").fetchall()


def update_signal(conn, signal_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE signals SET {cols} WHERE id = ?", (*fields.values(), signal_id))


def all_signals(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM signals ORDER BY created_at DESC, id DESC").fetchall()


# ---------- Telegram outbox ----------

OUTBOX_MAX_ATTEMPTS = 8


def enqueue(conn, chat: str, text: str, kind: str = "info", signal_id: int | None = None) -> int:
    now = utcnow_iso()
    cur = conn.execute("INSERT INTO outbox (chat, text, kind, signal_id, next_try, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (str(chat), text, kind, signal_id, now, now))
    return cur.lastrowid


def due_messages(conn, now_iso: str | None = None) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM outbox WHERE status = 'pending' AND next_try <= ? ORDER BY id",
                        (now_iso or utcnow_iso(),)).fetchall()


def message_sent(conn, msg_id_row: int, telegram_msg_id: int | None) -> None:
    conn.execute("UPDATE outbox SET status = 'sent', sent_at = ?, msg_id = ?, attempts = attempts + 1 WHERE id = ?",
                 (utcnow_iso(), telegram_msg_id, msg_id_row))


def message_failed(conn, msg_id_row: int, error: str, retry_in_seconds: float) -> str:
    """Record a failed attempt; give up after OUTBOX_MAX_ATTEMPTS. Returns the new status."""
    row = conn.execute("SELECT attempts FROM outbox WHERE id = ?", (msg_id_row,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    status = "failed" if attempts >= OUTBOX_MAX_ATTEMPTS else "pending"
    next_try = (datetime.now(timezone.utc) + timedelta(seconds=retry_in_seconds)).isoformat(timespec="seconds")
    conn.execute("UPDATE outbox SET attempts = ?, status = ?, next_try = ?, error = ? WHERE id = ?",
                 (attempts, status, next_try, error[:500], msg_id_row))
    return status


# ---------- candles ----------

def upsert_candles(conn, df) -> int:
    """Save 15-min candles (DataFrame indexed by UTC open time). Returns rows written."""
    rows = [(ts.tz_convert("UTC").isoformat(), float(r.open), float(r.high), float(r.low), float(r.close))
            for ts, r in zip(df.index, df.itertuples())]
    conn.executemany("INSERT OR REPLACE INTO candles (time, open, high, low, close) VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def candle_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]


def load_candles(conn, start: str, end: str):
    """Candles with start <= open time < end (UTC ISO strings) as a DataFrame."""
    import pandas as pd

    rows = conn.execute("SELECT time, open, high, low, close FROM candles WHERE time >= ? AND time < ? ORDER BY time",
                        (start, end)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows], columns=["time", "open", "high", "low", "close"])
    df.index = pd.to_datetime(df.pop("time"), utc=True, format="ISO8601")
    df.index.name = "datetime"
    return df


def candle_days(conn) -> list[str]:
    """Dates (YYYY-MM-DD, UTC) that have candles, oldest first."""
    return [r[0] for r in conn.execute("SELECT DISTINCT substr(time, 1, 10) FROM candles ORDER BY 1")]


# ---------- key/value (e.g. Telegram update offset) ----------

def kv_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
