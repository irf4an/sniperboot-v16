#!/usr/bin/env python3
"""Backup konsisten DB hoodsniper (aman walau bot sedang menulis).

Pakai `sqlite3.Connection.backup()` — API resmi SQLite untuk snapshot online.
Beda dengan `cp` yang bisa menyalin file di tengah transaksi (dan di mode WAL
justru kehilangan isi `-wal`), cara ini menghasilkan salinan utuh dan konsisten.

Retensi: simpan `--keep` salinan terbaru (default 14), sisanya dihapus.
Jalankan dari cron, mis. tiap 6 jam.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time


def backup(db_path: str, out_dir: str, keep: int = 14, verify: bool = True) -> str:
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.basename(db_path)
    dst = os.path.join(out_dir, f"{base}.bak-{stamp}")

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
    try:
        src.execute("PRAGMA busy_timeout=60000")
        dconn = sqlite3.connect(dst, timeout=60)
        try:
            src.backup(dconn, pages=1000)   # bertahap: lock singkat, tidak nge-blok bot
            dconn.commit()
            # Salinan WAL butuh -wal/-shm pendamping; untuk arsip kita mau SATU
            # file mandiri, jadi ubah hasil salinan balik ke journal_mode=delete.
            dconn.execute("PRAGMA journal_mode=delete")
            dconn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dconn.close()
    finally:
        src.close()

    if verify:
        v = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        try:
            ok = v.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            v.close()
        if ok != "ok":
            raise RuntimeError(f"backup korup: integrity_check={ok}")

    # retensi
    snaps = sorted(
        f for f in os.listdir(out_dir)
        if f.startswith(f"{base}.bak-")
    )
    for old in snaps[:-keep] if len(snaps) > keep else []:
        try:
            os.remove(os.path.join(out_dir, old))
        except OSError:
            pass
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("DB_PATH", "hoodsniper.db"))
    ap.add_argument("--out-dir", default="backups")
    ap.add_argument("--keep", type=int, default=14)
    a = ap.parse_args()
    try:
        p = backup(a.db, a.out_dir, a.keep)
    except Exception as e:
        print(f"BACKUP GAGAL: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 1
    print(f"OK {p} ({os.path.getsize(p):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
