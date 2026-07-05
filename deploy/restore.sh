#!/usr/bin/env bash
# Restore từ backup. Dùng: bash deploy/restore.sh <backup.tar.gz>
set -euo pipefail
SRC="${1:?cần đường dẫn file backup}"
[ -f "$SRC" ] || { echo "không thấy file: $SRC" >&2; exit 1; }
echo "Khôi phục từ $SRC (ghi đè state/, workspace/, config/)..."
tar -xzf "$SRC"
echo "Xong. Nhớ đặt lại secret qua env YETT_SECRET_* (không nằm trong backup)."
