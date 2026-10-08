"""Owner login: password hashing, signed session cookies and a simple brute-force lock-out.

Only the standard library is used:
  * Passwords are stored as PBKDF2-SHA256 hashes (ADMIN_PASSWORD_HASH in .env), never in plain text.
  * The session cookie is "<user>|<session id>|<expires>|<hmac>", signed with SESSION_SECRET, so it can't be forged
    or extended. The session id must also be in the server-side list, so signing out really ends it.
    Changing SESSION_SECRET (scripts/set_password.py does by default) signs every device out.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict

COOKIE = "gs_session"
ITERATIONS = 310_000
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def same_text(a: str, b: str) -> bool:
    """Constant-time comparison that also works for non-ASCII text."""
    return hmac.compare_digest(a.encode(), b.encode())


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(user: str, sid: str, secret: str, max_age: int, now: float | None = None) -> str:
    payload = f"{user}|{sid}|{int((time.time() if now is None else now) + max_age)}"
    return f"{payload}|{_sign(secret, payload)}"


def read_session(cookie: str | None, secret: str, now: float | None = None) -> tuple[str, str] | None:
    """(username, session id) from a valid cookie, or None if it is missing, forged or expired."""
    if not cookie or not secret:
        return None
    try:
        user, sid, expires, sig = cookie.rsplit("|", 3)
    except ValueError:
        return None
    if not hmac.compare_digest(sig.encode(), _sign(secret, f"{user}|{sid}|{expires}").encode()):
        return None
    if not expires.isdigit() or int(expires) < (time.time() if now is None else now):
        return None
    return user, sid


# Server-side session list (kv table, key "sessions": {sid: expires}). Signing out removes the id, so a
# copied cookie stops working even before it expires.

def _sessions(conn) -> dict:
    row = conn.execute("SELECT value FROM kv WHERE key = 'sessions'").fetchone()
    return json.loads(row[0]) if row else {}


def _save_sessions(conn, sessions: dict) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES ('sessions', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(sessions),))


def start_session(conn, max_age: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    sessions = {sid: exp for sid, exp in _sessions(conn).items() if exp > now}  # drop expired ones
    sid = secrets.token_urlsafe(16)
    sessions[sid] = int(now + max_age)
    _save_sessions(conn, sessions)
    return sid


def session_active(conn, sid: str, now: float | None = None) -> bool:
    return _sessions(conn).get(sid, 0) > (time.time() if now is None else now)


def end_session(conn, sid: str) -> None:
    sessions = _sessions(conn)
    if sessions.pop(sid, None) is not None:
        _save_sessions(conn, sessions)


def end_all_sessions(conn) -> None:
    _save_sessions(conn, {})


class LoginLimiter:
    """Locks an IP address out for LOCK_SECONDS after MAX_FAILURES wrong passwords in a row."""

    def __init__(self, max_failures: int = MAX_FAILURES, lock_seconds: int = LOCK_SECONDS):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self.failures: dict[str, int] = defaultdict(int)
        self.locked_until: dict[str, float] = {}

    def seconds_locked(self, ip: str, now: float | None = None) -> int:
        remaining = self.locked_until.get(ip, 0) - (time.time() if now is None else now)
        return max(0, int(remaining + 0.999))

    def failed(self, ip: str, now: float | None = None) -> None:
        self.failures[ip] += 1
        if self.failures[ip] >= self.max_failures:
            self.locked_until[ip] = (time.time() if now is None else now) + self.lock_seconds
            self.failures[ip] = 0

    def succeeded(self, ip: str) -> None:
        self.failures.pop(ip, None)
        self.locked_until.pop(ip, None)


def safe_next(target: str | None) -> str:
    """Only allow redirects to paths on this site (never to another domain)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
