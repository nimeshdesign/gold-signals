"""Telegram Bot API client and message templates."""
import html
import logging
import sys

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
