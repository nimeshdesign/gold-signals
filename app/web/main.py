"""Website: landing page, public track record, pricing, Stripe checkout and Telegram access.

Run:  uvicorn app.web.main:app --host 0.0.0.0 --port 8000
"""
import hmac
import json
import logging
import secrets
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import settings
from ..strategies import DEFAULT_PARAMS
from ..telegram_bot import TelegramClient, TelegramError
from . import analytics, auth, chart
from .payments import Stripe, StripeError, verify_webhook
from .stats import compute_stats

log = logging.getLogger("web")
HERE = Path(__file__).parent

# Stripe subscription statuses that keep channel access (past_due = card retry window).
ACCESS_STATUSES = {"active", "trialing", "past_due"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title=settings.brand_name, docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(brand=settings.brand_name, symbol=settings.display_symbol,
                             support_email=settings.support_email, price_label=settings.price_label,
                             site_banner=settings.site_banner, strategy_name=settings.strategy_name,
                             payments_enabled=bool(settings.stripe_secret_key and settings.stripe_price_id),
                             sp={**DEFAULT_PARAMS.get(settings.strategy_name, {}), **settings.strategy_params},
                             params=settings.strategy, news_before=settings.news_block_before,
                             news_after=settings.news_block_after, pip_size=settings.pip_size)


def _fmt_dt(value: str | None) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")


templates.env.filters["dt"] = _fmt_dt


def telegram() -> TelegramClient:
    return TelegramClient(settings.telegram_bot_token, dry_run=settings.dry_run)


def stripe() -> Stripe:
    try:
        return Stripe(settings.stripe_secret_key)
    except StripeError as exc:
        raise HTTPException(503, "Payments are not configured yet") from exc


# ---------- owner login ----------

# Reachable without signing in: the login page itself, health checks, Stripe's server-to-server
# webhook, and the stylesheet the login page needs.
OPEN_PATHS = {"/login", "/healthz", "/stripe/webhook"}
REMEMBER_SECONDS = 30 * 24 * 3600
SESSION_SECONDS = 12 * 3600
limiter = auth.LoginLimiter()
# Without SESSION_SECRET in .env, sessions still work but end whenever the website restarts.
_fallback_secret = secrets.token_hex(32)


def _secret() -> str:
    return settings.session_secret or _fallback_secret


def current_user(request: Request) -> str | None:
    return auth.read_session(request.cookies.get(auth.COOKIE), _secret())


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
    user_ok = hmac.compare_digest(username.strip().lower(), settings.admin_username.lower())
    if not (auth.verify_password(password, settings.admin_password_hash) and user_ok):
        limiter.failed(ip)
        log.warning("Failed login for %r from %s", username, ip)
        return fail("Wrong username or password.", 401)

    limiter.succeeded(ip)
    log.info("Signed in: %s from %s", settings.admin_username, ip)
    max_age = REMEMBER_SECONDS if remember else SESSION_SECONDS
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_session(settings.admin_username, _secret(), max_age),
                    max_age=max_age if remember else None, httponly=True, samesite="lax",
                    secure=settings.site_url.startswith("https://"))
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---------- public pages ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db.session() as conn:
        stats = compute_stats(db.all_signals(conn))
    return templates.TemplateResponse(request, "index.html", {"stats": stats})


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
    """Closed signals only; live levels are for subscribers."""
    with db.session() as conn:
        rows = db.all_signals(conn)
    fields = ("id", "symbol", "direction", "entry", "sl", "tp1", "tp2", "created_at", "status", "result_r", "closed_at")
    return JSONResponse([{k: r[k] for k in fields} for r in rows if r["status"] not in ("open", "tp1")])


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    return templates.TemplateResponse(request, "pricing.html", {})


@app.get("/disclaimer", response_class=HTMLResponse)
def disclaimer(request: Request):
    return templates.TemplateResponse(request, "disclaimer.html", {})


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    """Owner dashboard: is the engine alive, what does the strategy see, what is open."""
    with db.session() as conn:
        snap = json.loads(db.kv_get(conn, "engine_status") or "null")
        heartbeat = db.kv_get(conn, "engine_heartbeat")
        last_error = json.loads(db.kv_get(conn, "engine_last_error") or "null")
        open_rows = db.open_signals(conn)
        recent = db.all_signals(conn)[:15]
        subs = conn.execute("SELECT status, COUNT(*) AS n FROM subscribers GROUP BY status").fetchall()
        stats = compute_stats(db.all_signals(conn))
    now = datetime.now(timezone.utc)
    age_min = (now - datetime.fromisoformat(heartbeat)).total_seconds() / 60 if heartbeat else None
    return templates.TemplateResponse(request, "status.html", {
        "snap": snap,
        "heartbeat": heartbeat,
        "age_min": age_min,
        "engine_ok": age_min is not None and age_min < 20,
        "service": _service_state("gold-engine"),
        "last_error": last_error,
        "open_rows": open_rows,
        "recent": recent,
        "subs": {r["status"]: r["n"] for r in subs},
        "stats": stats,
        "dry_run": settings.dry_run,
        "channel_set": bool(settings.telegram_channel_id),
        "now": now,
    })


def _service_state(name: str) -> str:
    """systemd state of a service ('active', 'failed', ...), or 'unknown' when not on systemd."""
    try:
        out = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, src: str = "live", period: str = "all", side: str = "all", show: str = "all"):
    """Owner analytics: profitable trades, stop losses, and when they happen. Live or backtest data."""
    src = "backtest" if src == "backtest" else "live"
    path = settings.backtest_database_path if src == "backtest" else settings.database_path
    rows = []
    if path.exists():
        with db.session(path) as conn:
            rows = db.all_signals(conn)
    a = analytics.compute(analytics.filter_frame(analytics.to_frame(rows), period, side))
    table = a.rows
    if show == "sl":
        table = [r for r in table if r["is_sl"]]
    elif show == "win":
        table = [r for r in table if r["result_r"] > 0]
    return templates.TemplateResponse(request, "analytics.html", {
        "a": a, "src": src, "period": period, "side": side, "show": show, "table": table[:500],
        "table_total": len(table), "has_backtest": settings.backtest_database_path.exists(),
        "equity_json": json.dumps(a.equity),
        "monthly_json": json.dumps([[m["month"], m["net_r"], m["trades"], m["win_rate"]] for m in a.monthly]),
    })


@app.get("/chart", response_class=HTMLResponse)
def chart_page(request: Request, src: str = "live", day: str | None = None, signal: int | None = None):
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


# ---------- checkout ----------

@app.post("/checkout")
def checkout():
    if not settings.stripe_price_id:
        raise HTTPException(503, "STRIPE_PRICE_ID is not set")
    session = stripe().create_checkout_session(
        settings.stripe_price_id,
        success_url=f"{settings.site_url}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.site_url}/pricing",
    )
    return RedirectResponse(session["url"], status_code=303)


@app.get("/success", response_class=HTMLResponse)
def success(request: Request, session_id: str):
    try:
        session = stripe().retrieve_checkout_session(session_id)
    except StripeError as exc:
        raise HTTPException(404, "Checkout session not found") from exc
    if session.get("status") != "complete":
        return RedirectResponse("/pricing", status_code=303)
    sub = activate_from_checkout(session)
    return templates.TemplateResponse(request, "success.html", {"sub": sub, "session_id": session_id})


@app.post("/portal")
def portal(session_id: str = Form(...)):
    """Send the customer to Stripe's billing portal to update their card or cancel."""
    with db.session() as conn:
        sub = db.get_subscriber_by(conn, "stripe_session_id", session_id)
    if not sub or not sub["stripe_customer_id"]:
        raise HTTPException(404, "Subscription not found")
    portal_session = stripe().create_portal_session(sub["stripe_customer_id"], f"{settings.site_url}/")
    return RedirectResponse(portal_session["url"], status_code=303)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    if not verify_webhook(payload, request.headers.get("stripe-signature", ""), settings.stripe_webhook_secret):
        raise HTTPException(400, "Invalid signature")
    event = json.loads(payload)
    obj = event["data"]["object"]
    kind = event["type"]
    log.info("Stripe event %s", kind)

    if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
        activate_from_checkout(obj)
    elif kind in ("customer.subscription.updated", "customer.subscription.deleted"):
        status = "canceled" if kind.endswith("deleted") else obj["status"]
        sync_subscription_status(obj["id"], status)
    return {"received": True}


# ---------- subscriber lifecycle ----------

def activate_from_checkout(session: dict):
    """Create (or find) the subscriber for a completed checkout and make sure they have an invite link."""
    details = session.get("customer_details") or {}
    with db.session() as conn:
        sub = db.upsert_subscriber_from_checkout(
            conn,
            session_id=session["id"],
            email=details.get("email"),
            customer_id=session.get("customer"),
            subscription_id=session.get("subscription"),
        )
        if sub["status"] == "active" and not sub["invite_link"] and not sub["telegram_user_id"]:
            try:
                link = telegram().create_join_request_link(settings.telegram_channel_id, f"sub-{sub['id']}")
                db.update_subscriber(conn, sub["id"], invite_link=link)
            except TelegramError:
                log.exception("Could not create invite link for subscriber #%d", sub["id"])
        return db.get_subscriber_by(conn, "id", sub["id"])


def sync_subscription_status(subscription_id: str, stripe_status: str) -> None:
    with db.session() as conn:
        sub = db.get_subscriber_by(conn, "stripe_subscription_id", subscription_id)
        if not sub:
            log.warning("Subscription %s not in database", subscription_id)
            return
        active = stripe_status in ACCESS_STATUSES
        db.update_subscriber(conn, sub["id"], status="active" if active else stripe_status)
        if active:
            return
        tg = telegram()
        try:
            if sub["telegram_user_id"]:
                tg.remove_member(settings.telegram_channel_id, sub["telegram_user_id"])
                log.info("Removed Telegram user %s (subscription %s)", sub["telegram_user_id"], stripe_status)
            elif sub["invite_link"]:
                tg.revoke_invite_link(settings.telegram_channel_id, sub["invite_link"])
        except TelegramError:
            log.exception("Could not revoke Telegram access for subscriber #%d", sub["id"])
