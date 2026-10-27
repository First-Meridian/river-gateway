#!/usr/bin/env bash
# Back up the River Gateway database from the running Docker container to this
# host, without stopping the gateway. Meant for cron on a Linux server, e.g.
# every day at 03:00 (crontab -e):
#
#   0 3 * * * bash /opt/river-gateway/scripts/server-backup.sh >> /opt/river-gateway/backups/backup.log 2>&1
#
# Usage: scripts/server-backup.sh [backup_dir] [days_to_keep]
#   backup_dir    where to keep backups (default: backups/ in the project)
#   days_to_keep  delete backups older than this many days (default: 30; 0 = keep all)
set -euo pipefail

cd "$(dirname "$0")/.."
dir="${1:-backups}"
keep_days="${2:-30}"
mkdir -p "$dir"
chmod 700 "$dir"

tmp="$(mktemp "$dir/.partial-XXXXXX")"
trap 'rm -f "$tmp"' EXIT

# The gateway writes an integrity-checked snapshot to stdout; -T keeps the stream binary.
docker compose exec -T gateway river-gateway backup --output - > "$tmp"

out="$dir/gateway-$(date +%Y%m%d-%H%M%S).db"
mv "$tmp" "$out"
chmod 600 "$out"

if [ "$keep_days" -gt 0 ]; then
  find "$dir" -maxdepth 1 -name 'gateway-*.db' -mtime +"$keep_days" -delete
fi
echo "$(date '+%F %T') backup written: $out ($(du -h "$out" | cut -f1))"
