"""Telegram setup helper.

  python -m scripts.telegram_setup                 # check the bot token, list channels the bot was added to
  python -m scripts.telegram_setup --save -100123  # check that channel, save it to .env, send a test message

Reads TELEGRAM_BOT_TOKEN from .env.
"""
import argparse
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

ENV = ROOT / ".env"
NEEDED = {
    "can_post_messages": "Post messages",
    "can_invite_users": "Invite users via link",
    "can_restrict_members": "Ban users",
}


def call(token: str, method: str, **params):
    data = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30).json()
    if not data.get("ok"):
        raise SystemExit(f"Telegram said: {data.get('description')} (method {method})")
    return data["result"]


def find_chats(token: str) -> dict[int, str]:
    """Channels/groups the bot has seen recently (being added as admin, or posts in a channel)."""
    chats = {}
    for upd in call(token, "getUpdates", allowed_updates=["my_chat_member", "channel_post", "chat_member"]):
        for key in ("my_chat_member", "channel_post", "chat_member"):
            chat = (upd.get(key) or {}).get("chat")
            if chat and chat["type"] in ("channel", "supergroup", "group"):
                chats[chat["id"]] = f"{chat.get('title', '?')} ({chat['type']})"
    return chats


def save_env(key: str, value: str) -> None:
    text = ENV.read_text(encoding="utf-8")
    line = f"{key}={value}"
    text, n = re.subn(rf"^{key}=.*$", line, text, flags=re.M)
    if not n:
        text += f"\n{line}\n"
    ENV.write_text(text, encoding="utf-8")


def check_channel(token: str, chat_id: str, bot_id: int) -> bool:
    chat = call(token, "getChat", chat_id=chat_id)
    member = call(token, "getChatMember", chat_id=chat_id, user_id=bot_id)
    print(f"\nChannel: {chat.get('title')}  (id {chat['id']}, type {chat['type']})")
    if chat.get("username"):
        print("  ! This channel is PUBLIC (has a @username). Make it private so only subscribers see signals.")
    if member["status"] not in ("administrator", "creator"):
        print("  ✗ The bot is not an admin of this channel. Add it as an administrator.")
        return False
    ok = True
    for flag, label in NEEDED.items():
        has = member.get(flag, False)
        print(f"  {'✓' if has else '✗'} {label}")
        ok &= bool(has)
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="CHAT_ID", help="check this channel id and save it as TELEGRAM_CHANNEL_ID")
    ap.add_argument("--public", metavar="CHAT_ID", help="optional public results channel to save")
    args = ap.parse_args()

    token = settings.telegram_bot_token
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is empty. Paste the token from @BotFather into .env first.")
    me = call(token, "getMe")
    print(f"✓ Bot token works: @{me['username']} ({me['first_name']})")

    if not args.save:
        chats = find_chats(token)
        if not chats:
            print("\nNo channels found yet. Add the bot to your channel as an admin, post any message in the\n"
                  "channel, then run this again.")
            return
        print("\nChannels the bot can see:")
        for cid, title in chats.items():
            print(f"  {cid}   {title}")
        print("\nNext: python -m scripts.telegram_setup --save <id from above>")
        return

    if not check_channel(token, args.save, me["id"]):
        print("\nFix the items marked ✗ (channel > Administrators > your bot), then run this again.")
        return
    save_env("TELEGRAM_CHANNEL_ID", args.save)
    print(f"\n✓ Saved TELEGRAM_CHANNEL_ID={args.save} to .env")
    if args.public:
        call(token, "getChat", chat_id=args.public)
        save_env("TELEGRAM_PUBLIC_CHANNEL_ID", args.public)
        print(f"✓ Saved TELEGRAM_PUBLIC_CHANNEL_ID={args.public} to .env")

    call(token, "sendMessage", chat_id=args.save, parse_mode="HTML",
         text=f"✅ <b>{settings.brand_name}</b> bot connected. Signals will appear here.")
    print("✓ Test message sent. Check the channel.")
    print("\nTo start paper trading: set DRY_RUN=false in .env, then run: python -m app.engine")


if __name__ == "__main__":
    main()
