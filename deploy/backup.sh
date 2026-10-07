#!/bin/sh
# Daily SQLite backup. Cron: 0 3 * * * /opt/gold-signals/deploy/backup.sh
set -e
mkdir -p /opt/gold-signals/backups
sqlite3 /opt/gold-signals/data/signals.db ".backup /opt/gold-signals/backups/signals-$(date +%F).db"
find /opt/gold-signals/backups -name 'signals-*.db' -mtime +30 -delete
