#!/bin/bash
# One-time (and repeatable) setup of the signal engine on an Ubuntu server.
# Run on the server from the unpacked project folder:  bash deploy/setup_server.sh
# Add --web to also install the dashboard website (Caddy on ports 80/443).
# Safe to re-run after uploading a new version: it keeps the live .env and everything in data/.
#
# Ownership (so a compromised app process can't tamper with code that later runs as root):
#   code + .venv    root:root      read-only for the service user
#   .env            root:gold 640  readable by the services, writable only by root
#   data/ backups/  gold:gold 700  the only places the services can write
set -euo pipefail

APP=/opt/gold-signals
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip sqlite3 rsync >/dev/null

# 1 GB servers: add swap so pip installs and pandas never run out of memory.
if [ ! -f /swapfile ]; then
  echo "==> Adding 1 GB swap"
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> Copying code to $APP"
id gold >/dev/null 2>&1 || sudo adduser --system --group --home "$APP" --no-create-home gold
sudo mkdir -p "$APP/data" "$APP/backups"
# The server's .env is the live configuration: only copy one in on first install.
KEEP_ENV=()
[ -f "$APP/.env" ] && KEEP_ENV=(--exclude '.env')
sudo rsync -a --delete \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.git/' \
  --exclude 'data/' --exclude 'backups/' --exclude 'backtest_trades.csv' "${KEEP_ENV[@]}" \
  "$SRC/" "$APP/"
if [ ! -f "$APP/.env" ]; then
  echo "!! No .env found. Copy .env.example to $APP/.env, fill it in, then run this again." >&2
  exit 1
fi

echo "==> Installing Python packages (a few minutes on a small server)"
if [ ! -x "$APP/.venv/bin/python" ]; then
  sudo python3 -m venv "$APP/.venv"
fi
# Code and venv: root only (also fixes older installs). data/ and backups/ are left alone so the
# running services never lose write access mid-deploy.
sudo chown root:root "$APP"
sudo find "$APP" -mindepth 1 -maxdepth 1 ! -name data ! -name backups -exec chown -R root:root {} +
sudo "$APP/.venv/bin/pip" install -q --upgrade pip
sudo "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
sudo chown -R gold:gold "$APP/data" "$APP/backups"
sudo chmod 700 "$APP/data" "$APP/backups"
sudo chown root:gold "$APP/.env"
sudo chmod 640 "$APP/.env"

echo "==> Running tests"
TMP_TEST=$(mktemp -d)
sudo chown gold:gold "$TMP_TEST"
if ! sudo -u gold bash -c "cd $APP && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=$TMP_TEST/pt > $TMP_TEST/out 2>&1"; then
  sudo tail -30 "$TMP_TEST/out"
  echo "!! Tests failed: NOT restarting the services. The running version is unchanged." >&2
  exit 1
fi
sudo tail -1 "$TMP_TEST/out"
sudo rm -rf "$TMP_TEST"

echo "==> Installing the engine service"
sudo cp "$APP/deploy/gold-engine.service" /etc/systemd/system/gold-engine.service
sudo systemctl daemon-reload
sudo systemctl enable gold-engine >/dev/null 2>&1
sudo systemctl restart gold-engine

if [ "${1:-}" = "--web" ]; then
  echo "==> Installing the website (uvicorn behind Caddy)"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null
  sudo cp "$APP/deploy/gold-web.service" /etc/systemd/system/gold-web.service
  # deploy/Caddyfile.local overrides the template (holds your real domain); otherwise serve plain HTTP on the IP.
  if [ -f "$APP/deploy/Caddyfile.local" ]; then
    sudo cp "$APP/deploy/Caddyfile.local" /etc/caddy/Caddyfile
  elif ! grep -qE '^[[:space:]]*reverse_proxy[[:space:]]+127\.0\.0\.1:8000' /etc/caddy/Caddyfile; then
    printf ':80 {\n    reverse_proxy 127.0.0.1:8000\n}\n' | sudo tee /etc/caddy/Caddyfile >/dev/null
  fi
  sudo systemctl daemon-reload
  sudo systemctl enable gold-web >/dev/null 2>&1
  sudo systemctl restart gold-web
  sudo systemctl reload caddy || sudo systemctl restart caddy

  # Oracle's Ubuntu images reject everything except SSH; allow web traffic before the final REJECT rule.
  for port in 80 443; do
    if ! sudo iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null; then
      line=$(sudo iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')
      sudo iptables -I INPUT "${line:-1}" -p tcp -m state --state NEW --dport "$port" -j ACCEPT
    fi
  done
  sudo mkdir -p /etc/iptables
  sudo sh -c 'iptables-save > /etc/iptables/rules.v4'
fi

echo "==> Daily database backup at 03:00"
sudo chmod 755 "$APP/deploy/backup.sh"
echo "0 3 * * * gold $APP/deploy/backup.sh" | sudo tee /etc/cron.d/gold-backup >/dev/null

sleep 5
sudo systemctl --no-pager --lines=5 status gold-engine | sed -n '1,3p;/Engine started/p'
echo
echo "Done. Live log:  sudo journalctl -u gold-engine -f"
