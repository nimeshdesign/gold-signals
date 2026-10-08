"""Set the website login password (stores only a hash in .env).

  python -m scripts.set_password                      # asks for the new password twice
  echo "new-password" | python -m scripts.set_password --stdin
  python -m scripts.set_password --username nimesh    # also change the username
  python -m scripts.set_password --keep-sessions      # don't sign other devices out

By default a new SESSION_SECRET is created, so every device (including a stolen cookie) must sign in again.

Restart the website afterwards (sudo systemctl restart gold-web) so it picks up the change.
"""
import argparse
import getpass
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.web.auth import hash_password  # noqa: E402

MIN_LENGTH = 12


def set_env(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    new, n = re.subn(rf"^{key}=.*$", lambda _: line, text, flags=re.M)
    return new if n else text.rstrip("\n") + f"\n{line}\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--username")
    ap.add_argument("--stdin", action="store_true", help="read the password from standard input")
    ap.add_argument("--keep-sessions", action="store_true", help="keep the current SESSION_SECRET")
    args = ap.parse_args()

    if args.stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat password: ") != password:
            print("Passwords don't match.")
            return 1
    if len(password) < MIN_LENGTH:
        print(f"Use at least {MIN_LENGTH} characters.")
        return 1

    env = Path(args.env)
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    text = set_env(text, "ADMIN_PASSWORD_HASH", hash_password(password))
    if args.username:
        text = set_env(text, "ADMIN_USERNAME", args.username.strip())
    if not args.keep_sessions or not re.search(r"^SESSION_SECRET=.+$", text, flags=re.M):
        text = set_env(text, "SESSION_SECRET", secrets.token_hex(32))
    env.write_text(text, encoding="utf-8")
    print(f"Password saved to {env} (as a hash). Restart the website to apply it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
