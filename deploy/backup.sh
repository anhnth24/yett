#!/usr/bin/env bash
# Backup state (3 SQLite) + workspace + config. Dùng: bash deploy/backup.sh [dest_dir]
set -euo pipefail
DEST="${1:-./backups}"
mkdir -p "$DEST"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/yett-backup-$TS.tar.gz"
# Không backup secret (nằm ở env/keyring, không trên đĩa).
tar -czf "$OUT" \
  --exclude='workspace/**/pending' \
  state workspace config/harness.yaml config/pricing.yaml 2>/dev/null || \
tar -czf "$OUT" state workspace config
echo "Đã backup: $OUT"
