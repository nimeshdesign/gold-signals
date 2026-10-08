"""Private owner dashboard: live status, open trades, chart, analytics and track record.

Run:  uvicorn app.web.main:app --host 127.0.0.1 --port 8000 --proxy-headers
"""
import json
import logging
import secrets
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, market
from ..config import settings
from ..strategies import DEFAULT_PARAMS
from . import analytics, auth, chart
from .stats import compute_stats

log = logging.getLogger("web")
HERE = Path(__file__).parent
SETUP_LABELS = {"session_breakout": "Asian breakout", "orb": "NY open breakout"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title=settings.brand_name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(brand=settings.brand_name, symbol=settings.display_symbol,
                             site_banner=settings.site_banner, strategy_name=settings.strategy_name,
                             sp={**DEFAULT_PARAMS.get(settings.strategy_name, {}), **settings.strategy_params},
                             params=settings.strategy, pip_size=settings.pip_size, lot_size=settings.lot_size)


def _fmt_dt(value: str | None) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")


templates.env.filters["dt"] = _fmt_dt


# ---------- owner login ----------

# Reachable without signing in: the login page, the health check and the login page's stylesheet.
OPEN_PATHS = {"/login", "/healthz"}
REMEMBER_SECONDS = 30 * 24 * 3600
SESSION_SECONDS = 12 * 3600
limiter = auth.LoginLimiter()
# Without SESSION_SECRET in .env, sessions still work but end whenever the website restarts.
_fallback_secret = secrets.token_hex(32)


def _secret() -> str:
    return settings.session_secret or _fallback_secret


def current_user(request: Request) -> str | None:
    session = auth.read_session(request.cookies.get(auth.COOKIE), _secret())
    if not session:
        return None
    user, sid = session
    with db.session() as conn:
        return user if auth.session_active(conn, sid) else None


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in OPEN_PATHS or path.startswith("/static/") or current_user(request):
        return await call_next(request)
    if request.method == "GET":
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    return JSONResponse({"detail": "Sign in required"}, status_code=401)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse(auth.safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": auth.safe_next(next), "error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(""), password: str = Form(""),
          remember: str = Form(""), next: str = Form("/")):
    ip = _client_ip(request)
    target = auth.safe_next(next)

    def fail(message: str, status: int):
        return templates.TemplateResponse(request, "login.html", {"next": target, "error": message,
                                                                  "username": username}, status_code=status)

    if not settings.admin_password_hash:
        return fail("Login isn't set up yet. Run: python -m scripts.set_password", 503)
    locked = limiter.seconds_locked(ip)
    if locked:
        return fail(f"Too many wrong attempts. Try again in {locked // 60 + 1} minutes.", 429)
    user_ok = auth.same_text(username.strip().lower(), settings.admin_username.lower())
    if not (auth.verify_password(password, settings.admin_password_hash) and user_ok):
        limiter.failed(ip)
        log.warning("Failed login for %r from %s", username, ip)
        return fail("Wrong username or password.", 401)

    limiter.succeeded(ip)
    log.info("Signed in: %s from %s", settings.admin_username, ip)
    max_age = REMEMBER_SECONDS if remember else SESSION_SECONDS
    with db.session() as conn:
        sid = auth.start_session(conn, max_age)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_session(settings.admin_username, sid, _secret(), max_age),
                    max_age=max_age if remember else None, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https" or settings.site_url.startswith("https://"))
    return resp


@app.post("/logout")
def logout(request: Request):
    """Sign out this device: the session is removed on the server, so a copied cookie stops working too."""
    session = auth.read_session(request.cookies.get(auth.COOKIE), _secret())
    if session:
        with db.session() as conn:
            auth.end_session(conn, session[1])
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---------- dashboard ----------

@app.get("/")
def home():
    return RedirectResponse("/status", status_code=303)


@app.get("/performance", response_class=HTMLResponse)
def performance(request: Request):
    with db.session() as conn:
        rows = db.all_signals(conn)
    stats = compute_stats(rows)
    closed = [r for r in rows if r["status"] not in ("open", "tp1")]
    return templates.TemplateResponse(request, "performance.html", {
        "stats": stats,
        "signals": closed,
        "equity_json": json.dumps(stats.equity),
    })


@app.get("/api/signals")
def api_signals():
    with db.session() as conn:
        rows = db.all_signals(conn)
    fields = ("id", "strategy", "symbol", "direction", "entry", "sl", "tp1", "tp2", "created_at", "status",
              "result_r", "closed_at")
    return JSONResponse([{k: r[k] for k in fields} for r in rows])


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    """Is the engine alive, what does the strategy see, and where do open trades stand right now."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db.session() as conn:
        snap = json.loads(db.kv_get(conn, "engine_status") or "null")
        heartbeat = db.kv_get(conn, "engine_heartbeat")
        last_error = json.loads(db.kv_get(conn, "engine_last_error") or "null")
        open_rows = db.open_signals(conn)
        live = json.loads(db.kv_get(conn, "live_price") or "null")
        recent = db.all_signals(conn)[:15]
        stats = compute_stats(db.all_signals(conn))
        credits = int(db.kv_get(conn, f"credits_{today}", "0"))
        unsent = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]
    now = datetime.now(timezone.utc)
    age_min = (now - datetime.fromisoformat(heartbeat)).total_seconds() / 60 if heartbeat else None
    live_age = (now - datetime.fromisoformat(live["time"])).total_seconds() / 60 if live else None
    return templates.TemplateResponse(request, "status.html", {
        "snap": snap,
        "heartbeat": heartbeat,
        "age_min": age_min,
        "engine_ok": age_min is not None and age_min < 20,
        "service": _service_state("gold-engine"),
        "last_error": last_error,
        "open_rows": open_rows,
        "live": live,
        "live_age": live_age,
        "live_trades": [live_trade(r, live) for r in open_rows] if live else [],
        "recent": recent,
        "stats": stats,
        "credits": credits,
        "credit_limit": settings.daily_credit_limit,
        "unsent": unsent,
        "market_open": market.is_open(now),
        "dry_run": settings.dry_run,
        "channel_set": bool(settings.telegram_channel_id),
        "now": now,
    })


def live_trade(row, live: dict) -> dict:
    """Where an open trade stands at the latest price: P/L and distance to each level, in pips and $.

    After TP1, half the position is already banked at TP1 and the stop on the other half sits at entry,
    so P/L = half at TP1 + half at the current price, and the stop distance is measured to entry.
    """
    pip, oz = settings.pip_size, settings.lot_size * settings.contract_oz
    d = 1 if row["direction"] == "BUY" else -1
    after_tp1 = row["status"] == "tp1"
    # Closing a BUY sells at the bid (chart price); closing a SELL buys at the ask (chart price + spread).
    exit_price = live["price"] + (settings.live_spread if d == -1 else 0.0)
    move = d * (exit_price - row["entry"])                      # per ounce, whole position
    banked = d * (row["tp1"] - row["entry"])
    pnl = 0.5 * banked + 0.5 * move if after_tp1 else move     # per ounce of the original position
    risk = abs(row["entry"] - row["sl"])
    stop = row["entry"] if after_tp1 else row["sl"]
    span = abs(row["tp2"] - row["sl"])
    position = (d * (exit_price - row["sl"])) / span if span else 0  # 0 = at original SL, 1 = at TP2

    def away(level: float, toward_profit: bool) -> float:
        gap = d * (level - exit_price)
        return round((gap if toward_profit else -gap) / pip)

    return {
        "row": row, "price": round(exit_price, 2), "pnl_pips": round(pnl / pip),
        "pnl_r": round(pnl / risk, 2) if risk else 0.0,
        "pnl_usd": round(pnl * oz) if oz else None,
        "stop": stop, "to_sl": away(stop, False), "to_tp1": away(row["tp1"], True), "to_tp2": away(row["tp2"], True),
        "position": max(0.0, min(1.0, position)),
        "entry_pos": (risk / span) if span else 0, "tp1_pos": (risk + abs(row["tp1"] - row["entry"])) / span if span else 0,
        "setup": SETUP_LABELS.get(row["strategy"], row["strategy"]),
    }


def _service_state(name: str) -> str:
    """systemd state of a service ('active', 'failed', ...), or 'unknown' when not on systemd."""
    try:
        out = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, src: str = "live", period: str = "all", side: str = "all", show: str = "all"):
    """Profitable trades, stop losses, and when they happen. Live or backtest data."""
    src = "backtest" if src == "backtest" else "live"
    path = settings.backtest_database_path if src == "backtest" else settings.database_path
    rows = []
    if path.exists():
        with db.session(path) as conn:
            rows = db.all_signals(conn)
    end = datetime.now(timezone.utc) if src == "live" else None  # backtests end in the past
    a = analytics.compute(analytics.filter_frame(analytics.to_frame(rows), period, side,
                                                 end=pd.Timestamp(end) if end else None),
                          offset_hours=settings.display_tz_offset, tz_name=settings.display_tz_name,
                          risk_usd=settings.lot_size * settings.contract_oz * settings.pip_size * 100)
    table = a.rows
    if show == "sl":
        table = [r for r in table if r["is_sl"]]
    elif show == "win":
        table = [r for r in table if r["result_r"] > 0]
    return templates.TemplateResponse(request, "analytics.html", {
        "a": a, "src": src, "period": period, "side": side, "show": show, "table": table[:500],
        "table_total": len(table), "has_backtest": settings.backtest_database_path.exists(),
        "tz_name": settings.display_tz_name,
        "equity_json": json.dumps(a.equity),
        "monthly_json": json.dumps([[m["month"], m["net_r"], m["trades"], m["win_rate"]] for m in a.monthly]),
    })


@app.get("/chart", response_class=HTMLResponse)
def chart_page(request: Request, src: str = "live", day: str | None = None,
               signal: int | None = Query(None, ge=1, le=2**62)):
    """Price chart for one day with the strategy's levels and any signal's entry/SL/TP."""
    src = "backtest" if src == "backtest" else "live"
    path = settings.backtest_database_path if src == "backtest" else settings.database_path
    params = {**DEFAULT_PARAMS["session_breakout"], **settings.strategy_params}
    view, days, chosen = None, [], None
    if path.exists():
        with db.session(path) as conn:
            days = db.candle_days(conn)
            chosen = (chart.signal_day(conn, signal) if signal else None) or chart.parse_day(day, days)
            if chosen:
                view = chart.day_view(conn, chosen, params, settings.strategy.tp1_r, settings.strategy.tp2_r,
                                      settings.display_tz_offset)
    prev_day, next_day = chart.neighbours(days, chosen) if chosen else (None, None)
    payload = None
    if view:
        payload = json.dumps({"candles": view["candles"], "levels": view["levels"], "markers": view["markers"]})
    return templates.TemplateResponse(request, "chart.html", {
        "src": src, "view": view, "day": chosen, "prev_day": prev_day, "next_day": next_day,
        "first_day": days[0] if days else None, "last_day": days[-1] if days else None,
        "payload": payload, "tz_name": settings.display_tz_name, "tz_offset": settings.display_tz_offset,
        "delta": timedelta(hours=settings.display_tz_offset),
        "range_start": params["range_start"], "range_end": params["range_end"], "window_end": params["window_end"],
    })


@app.get("/healthz")
def healthz():
    return {"ok": True}
