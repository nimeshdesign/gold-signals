"""Telegram Bot API client and message templates."""
import html
import logging
import sys

import pandas as pd
import requests

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, dry_run: bool = False, session: requests.Session | None = None):
        self.token = token
        self.dry_run = dry_run or not token
        self.http = session or requests.Session()

    def _call(self, method: str, http_timeout: float = 30, **params):
        if self.dry_run:
            log.info("[DRY RUN] telegram.%s %s", method, params)
            return None
        resp = self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params, timeout=http_timeout)
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"{method} failed: {data.get('description')}")
        return data["result"]

    def send_message(self, chat_id, text: str, reply_to: int | None = None) -> int | None:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if self.dry_run:
            out = f"\n--- Telegram -> {chat_id} ---\n{text}\n"
            enc = sys.stdout.encoding or "utf-8"  # Windows consoles can't always print emoji
            print(out.encode(enc, errors="replace").decode(enc))
            return None
        result = self._call("sendMessage", **params)
        return result["message_id"]

    def create_join_request_link(self, chat_id, name: str) -> str:
        """Invite link that sends a join request (which the engine approves) instead of joining directly."""
        if self.dry_run:
            return f"https://t.me/+DRYRUN_{name}"
        result = self._call("createChatInviteLink", chat_id=chat_id, name=name[:32], creates_join_request=True)
        return result["invite_link"]

    def revoke_invite_link(self, chat_id, link: str) -> None:
        self._call("revokeChatInviteLink", chat_id=chat_id, invite_link=link)

    def approve_join_request(self, chat_id, user_id: int) -> None:
        self._call("approveChatJoinRequest", chat_id=chat_id, user_id=user_id)

    def decline_join_request(self, chat_id, user_id: int) -> None:
        self._call("declineChatJoinRequest", chat_id=chat_id, user_id=user_id)

    def remove_member(self, chat_id, user_id: int) -> None:
        """Kick without a permanent ban, so the user can come back if they resubscribe."""
        self._call("banChatMember", chat_id=chat_id, user_id=user_id)
        self._call("unbanChatMember", chat_id=chat_id, user_id=user_id, only_if_banned=True)

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        if self.dry_run:
            return []
        params = {"timeout": timeout, "allowed_updates": ["chat_join_request"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", http_timeout=timeout + 10, **params) or []


# ---------- message templates ----------

def _p(x: float) -> str:
    return f"{x:,.2f}"


def format_signal(symbol: str, direction: str, entry: float, sl: float, tp1: float, tp2: float,
                  tp1_r: float, tp2_r: float) -> str:
    icon = "🟢" if direction == "BUY" else "🔴"
    risk = abs(entry - sl)
    return (
        f"{icon} <b>{html.escape(symbol)} {direction}</b>\n\n"
        f"Entry: <code>{_p(entry)}</code>\n"
        f"SL: <code>{_p(sl)}</code>\n"
        f"TP1: <code>{_p(tp1)}</code> ({tp1_r:g}R)\n"
        f"TP2: <code>{_p(tp2)}</code> ({tp2_r:g}R)\n\n"
        f"Risk per ounce: ${_p(risk)}\n"
        f"Plan: close half at TP1 and move SL to entry.\n"
        f"Risk no more than 1–2% of your account per trade.\n\n"
        f"<i>Not financial advice.</i>"
    )


def format_update(event: str, direction: str, symbol: str, result_r: float | None) -> str:
    r = f"{result_r:+.2f}R" if result_r is not None else ""
    return {
        "tp1": f"✅ {symbol} {direction}: TP1 hit. Half closed, move SL to entry.",
        "tp2": f"🎯 {symbol} {direction}: TP2 hit. Trade closed at {r}.",
        "be": f"➖ {symbol} {direction}: stopped at entry after TP1. Trade closed at {r}.",
        "sl": f"❌ {symbol} {direction}: stop loss hit. Trade closed at {r}.",
        "expired": f"⏱ {symbol} {direction}: time limit reached, closed at market. Result {r}.",
    }[event]


def _local(ts, fmt: str = "%I:%M %p") -> str:
    """Format a UTC time in the display timezone (IST by default)."""
    from .config import settings

    t = pd.Timestamp(ts).tz_convert("UTC") + pd.Timedelta(hours=settings.display_tz_offset)
    return f"{t.strftime(fmt).lstrip('0')} {settings.display_tz_name}"


def _hour_local(hour_utc: int) -> str:
    return _local(pd.Timestamp("2000-01-03", tz="UTC") + pd.Timedelta(hours=hour_utc))


def format_daily_plan(symbol: str, snap: dict, now, events) -> str:
    rng, trend = snap["range"], snap.get("trend") or {}
    up = trend.get("direction") == "UP"
    level = rng["high"] if up else rng["low"]
    lines = [
        f"📊 <b>{html.escape(symbol)} daily plan</b>: {pd.Timestamp(now).strftime('%a %d %b')}",
        "",
        f"Asian range: <code>{_p(rng['low'])}</code> – <code>{_p(rng['high'])}</code> ({_p(rng['high'] - rng['low'])} wide)",
        f"Trend: <b>{'UP 📈' if up else 'DOWN 📉'}</b> (daily close {_p(trend.get('close', 0))} "
        f"{'above' if up else 'below'} its 20-day average {_p(trend.get('ema', 0))})",
        f"Gold now: <code>{_p(snap['price'])}</code>",
        "",
        f"👀 Watching for a <b>{'BUY' if up else 'SELL'}</b> if a 15-min candle closes "
        f"{'above' if up else 'below'} <code>{_p(level)}</code>",
        f"⏰ Until {_hour_local(rng['window_end'])}. At most one signal today.",
    ]
    plan = snap.get("planned")
    if plan:
        lines += [
            "",
            "🎯 If it triggers (approx.):",
            f"SL ~<code>{_p(plan['sl'])}</code> · TP1 ~<code>{_p(plan['tp1'])}</code> · TP2 ~<code>{_p(plan['tp2'])}</code>",
            f"<i>Risk about ${_p(plan['risk'])}/oz. Exact levels come with the signal.</i>",
        ]
    if events:
        lines += ["", "⚠️ High-impact USD news today (no new signals 30 min either side):"]
        lines += [f"• {_local(e.time)}: {html.escape(e.title)}" for e in events]
    return "\n".join(lines)


def format_day_end(symbol: str, snap: dict, today_rows) -> str:
    rng = snap["range"]
    head = f"🌙 <b>{html.escape(symbol)} day end</b>"
    if not today_rows:
        moves = snap.get("window_moves") or {}
        text = [head, "", "No signal today: gold did not close outside the Asian range in the trend direction."]
        if moves:
            text.append(f"Range {_p(rng['low'])} – {_p(rng['high'])}; during the trading window gold moved "
                        f"between {_p(moves['low'])} and {_p(moves['high'])}.")
        if snap.get("skip_note"):
            text.append(f"Note: {html.escape(snap['skip_note'])}")
        text += ["", f"Next window opens tomorrow at {_hour_local(rng['end'])}."]
        return "\n".join(text)
    states = {"open": "open, waiting for TP1 or SL", "tp1": "TP1 hit, stop moved to entry, still running",
              "sl": "stop loss hit", "be": "TP1 hit, then closed at entry", "tp2": "TP1 and TP2 hit",
              "expired": "closed at the time limit"}
    text = [head, ""]
    for r in today_rows:
        res = f" ({r['result_r']:+.2f}R)" if r["result_r"] is not None else ""
        text.append(f"Today's signal: <b>{r['direction']}</b> at <code>{_p(r['entry'])}</code>: "
                    f"{states.get(r['status'], r['status'])}{res}")
    text += ["", "Open trades keep being tracked overnight; updates arrive as replies to the signal."]
    return "\n".join(text)


def format_week_summary(symbol: str, rows, monday) -> str:
    closed = [r for r in rows if r["result_r"] is not None and r["status"] in ("sl", "be", "tp2", "expired")]
    wins = sum(1 for r in closed if r["result_r"] > 0)
    stops = sum(1 for r in closed if r["status"] == "sl")
    net = sum(r["result_r"] for r in closed)
    still_open = len(rows) - len(closed)
    text = [f"🗓 <b>{html.escape(symbol)} week of {pd.Timestamp(monday).strftime('%d %b')}</b>", ""]
    if not rows:
        text.append("No signals this week: gold never closed outside its Asian range in the trend direction.")
        return "\n".join(text)
    text += [f"Signals: {len(rows)}" + (f" ({still_open} still open)" if still_open else "")]
    if closed:
        text += [
            f"Profitable: {wins} · Stop loss: {stops} · Other: {len(closed) - wins - stops}",
            f"Net result: <b>{net:+.2f}R</b> (win rate {wins / len(closed) * 100:.0f}%)",
        ]
    text += ["", "<i>1R = the amount risked per signal. Past results don't guarantee future ones.</i>"]
    return "\n".join(text)
