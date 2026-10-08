"""End-to-end Telegram check using the live settings (.env).

  python -m scripts.telegram_test            # check bot + channel, send a test message and a TEST sample signal
  python -m scripts.telegram_test --quiet    # only check, send nothing

Every message sent is clearly marked TEST so nobody mistakes it for a trade.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.telegram_bot import TelegramClient, TelegramError, format_signal, format_update  # noqa: E402

RIGHTS = {"can_post_messages": "Post messages"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="check only, send no messages")
    args = ap.parse_args()

    ok = True

    def check(passed: bool, text: str) -> None:
        nonlocal ok
        ok &= passed
        print(f"  {'PASS' if passed else 'FAIL'}  {text}")

    print("Telegram connection test")
    check(bool(settings.telegram_bot_token), "bot token is set")
    check(bool(settings.telegram_channel_id), f"channel id is set ({settings.telegram_channel_id or '-'})")
    check(not settings.dry_run, "live mode (DRY_RUN=false)" if not settings.dry_run else "DRY_RUN=true: the engine is NOT posting")
    if not (settings.telegram_bot_token and settings.telegram_channel_id):
        return 1

    tg = TelegramClient(settings.telegram_bot_token)  # always real calls here, even if DRY_RUN=true
    try:
        me = tg._call("getMe")
        check(True, f"bot reachable: @{me['username']}")
        chat = tg._call("getChat", chat_id=settings.telegram_channel_id)
        check(True, f"channel reachable: {chat.get('title')}")
        check(not chat.get("username"), "channel is private" if not chat.get("username") else
              f"channel is PUBLIC (@{chat['username']}); make it private")
        member = tg._call("getChatMember", chat_id=settings.telegram_channel_id, user_id=me["id"])
        check(member["status"] == "administrator", f"bot is {member['status']}")
        for flag, label in RIGHTS.items():
            check(bool(member.get(flag)), f"permission: {label}")
    except TelegramError as exc:
        check(False, str(exc))
        return 1

    if args.quiet:
        print("\nAll checks passed." if ok else "\nSome checks failed.")
        return 0 if ok else 1

    try:
        tg.send_message(settings.telegram_channel_id,
                        f"🧪 <b>TEST</b>: {settings.brand_name} connection check. The bot can post here. Ignore this message.")
        check(True, "sent a test message")
        sample = format_signal(settings.display_symbol, "SELL", 4130.43, 4162.38, 4098.48, 4034.58,
                               settings.strategy.tp1_r, settings.strategy.tp2_r)
        msg_id = tg.send_message(settings.telegram_channel_id,
                                 "🧪 <b>TEST ONLY: NOT A REAL TRADE</b>\nThis is what a signal looks like:\n\n" + sample)
        check(True, "sent a TEST sample signal")
        tg.send_message(settings.telegram_channel_id,
                        "🧪 TEST: " + format_update("tp1", "SELL", settings.display_symbol, None), reply_to=msg_id)
        check(True, "sent a TEST reply update under it")
    except TelegramError as exc:
        check(False, f"sending failed: {exc}")

    print("\nAll checks passed. Check the channel for 3 TEST messages." if ok else "\nSome checks failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
