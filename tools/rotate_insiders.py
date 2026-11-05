#!/usr/bin/env python3
"""🔄 tools/rotate_insiders.py (v16.3) — Rotasi wallet insider berkala.

Sindikat sering ganti dompet (burner rot), paus bisa memburuk, dan koin
runner baru melahirkan insider baru. Skrip ini (dipanggil CLI maupun
diimpor oleh tracker.auto_rotation_loop tiap 24 jam) menjalankan 3 tahap:

  1. PRUNE — wallet boncos/lunglai dinonaktifkan (db.prune_stale_insiders):
     last_seen > 0, tak aktif > 3 hari, win_count < 2 → enabled = 0.
  2. HARVEST — ambil koin runner terbaru (Dexscreener search Robinhood),
     petik pembeli awal (Blockscout transfers → pick_buyers).
  3. RECRUIT — irisan silang wallet yang terbentur ≥2 koin pemenang
     didaftarkan sebagai insider baru (label auto_rotate, win_count =
     jumlah koin pemenang).

Pemakaian:
  python tools/rotate_insiders.py            # satu putaran rotasi
  python tools/rotate_insiders.py --dry-run  # tampil tanpa menulis DB
  python tools/rotate_insiders.py --limit 20 # ambil 20 koin runner

Return rotate(): dict ringkasan
  {"added": n, "disabled": n, "total_active": n, "runners": n, "dry": bool}
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper import db  # noqa: E402
import tools.screen_insiders as si  # noqa: E402

DEFAULT_RUNNER_LIMIT = 15
DEFAULT_MAX_INACTIVE_DAYS = 3


def rotate(runner_limit: int = DEFAULT_RUNNER_LIMIT,
           max_inactive_days: int = DEFAULT_MAX_INACTIVE_DAYS,
           dry: bool = False) -> dict:
    """Satu putaran rotasi lengkap. Bisa dipanggil CLI (main) maupun
    diimpor (asyncio.to_thread dari tracker.auto_rotation_loop)."""
    db.init()

    # 1. Prune: nonaktifkan wallet lunglai/boncos & klaster wash-trading sybil
    disabled = 0 if dry else db.prune_stale_insiders(max_inactive_days)
    if not dry and (db.get("sybil_cluster_guard_on", str) or "1") == "1":
        db.detect_and_purge_sybil_clusters()
    # 1b. Buang funder/launcher: wallet yang membiayai banyak deployer bukan
    #     trader organik (sinyalnya = dev beli koin sendiri). Langsung di-ban
    #     permanen supaya tidak direkrut ulang putaran berikutnya.
    funder_ban = {"banned": 0} if dry else db.purge_operator_funders()

    # 2. Ambil koin runner terbaru
    runners = si.fetch_gainers(runner_limit)
    # 3. + 4. Petik pembeli early lalu iris silang (≥2 koin pemenang)
    new_rows = si.cross_check(runners) if runners else []

    # 5. Daftarkan kandidat sebagai insider baru — HANYA yang lolos gate
    #    skor 2 sumber (skor ≥2,0 dan ≥2 CA terverifikasi Blockscout).
    candidates = [r for r in new_rows if r.get("qualifies")]
    added = 0
    if not dry:
        existing = {r["address"] for r in db.list_insiders()}
        for r in candidates:
            addr = r["address"]
            db.add_insider_wallet(
                addr, label="auto_rotate", win_count=r["n_ca"],
                notes="auto: rotasi koin runner",
                win_cas=",".join(r["verified_cas"]),
                update_if_exists=True, set_wins=True,
                score=r["score"], sources=r["sources"],
                last_verified=0 if r["needs_retry"] else int(time.time()),
                enabled=1)
            if addr not in existing:
                added += 1

    total_active = 0 if dry else len(db.list_insiders(only_enabled=True))

    summary = {"added": added,
               "funder_banned": funder_ban["banned"],
               "disabled": disabled,
               "total_active": total_active,
               "runners": len(runners),
               "new_candidates": len(new_rows),
               "qualified": len(candidates),
               "dry": dry}
    _print_summary(runners, new_rows, summary)
    return summary


def _print_summary(runners, new_rows, summary):
    print("\n🔄 ROTASI INSIDER — ringkasan")
    print(f"  koin runner dipakai : {summary['runners']}")
    print(f"  kandidat (≥2 CA)    : {summary['new_candidates']}")
    print(f"  lolos gate skor     : {summary['qualified']}")
    for r in new_rows[:10]:
        print(f"    {'✅' if r.get('qualifies') else '· '} {r['address']}  "
              f"skor={r['score']:.2f}  win={r['n_ca']}")
    print(f"  wallet dinonaktifkan (prune) : {summary['disabled']}")
    print(f"  funder/launcher di-ban       : {summary.get('funder_banned', 0)}")
    print(f"  total wallet aktif sekarang  : {summary['total_active']}")
    if summary["dry"]:
        print("  (DRY-RUN — tidak ada perubahan DB)")


def main():
    ap = argparse.ArgumentParser(description="Rotasi wallet insider: prune + rekrut")
    ap.add_argument("--limit", type=int, default=DEFAULT_RUNNER_LIMIT,
                    help=f"max koin runner (default {DEFAULT_RUNNER_LIMIT})")
    ap.add_argument("--days", type=int, default=DEFAULT_MAX_INACTIVE_DAYS,
                    help="maks hari tak aktif sebelum di-prune (default 3)")
    ap.add_argument("--dry-run", action="store_true",
                    help="tampil saja, tidak menulis DB")
    args = ap.parse_args()
    rotate(args.limit, args.days, dry=args.dry_run)


if __name__ == "__main__":
    main()
