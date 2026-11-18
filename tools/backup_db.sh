#!/bin/bash
# Backup DB hoodsniper tiap 6 jam (00:15, 06:15, 12:15, 18:15).
# Database online — pakai sqlite3 backup API, bukan cp, supaya konsisten.
set -u
cd /root/workspace/cipher/projects/hoodsniper_v16_gabungan || exit 1
OUT="/root/workspace/cipher/projects/hoodsniper_v16_gabungan/backups"
mkdir -p "$OUT"
/usr/bin/python3 tools/backup_db.py --db hoodsniper.db --out-dir "$OUT" --keep 12 \
  >> "$OUT/backup.log" 2>&1
exit 0
