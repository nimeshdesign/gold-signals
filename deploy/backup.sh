#!/bin/sh
# Daily SQLite backup, readable only by the service user. Cron: 0 3 * * * gold /opt/gold-signals/deploy/backup.sh
# Keeps 30 days. These copies are on the same disk; for disaster recovery also copy backups/ off the server
# now and then (e.g. scp to your PC).
set -e
umask 077
mkdir -p /opt/gold-signals/backups
sqlite3 /opt/gold-signals/data/signals.db ".backup /opt/gold-signals/backups/signals-$(date +%F).db"
find /opt/gold-signals/backups -name 'signals-*.db' -mtime +30 -delete
