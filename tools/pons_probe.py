"""Probe manual Radar Pons V2 — verifikasi tanpa mengirim Telegram.

Menarik kandidat dari API kurva Pons (`stage=curve&sort=progress`), menyaring
yang sudah tembus `pons_min_progress`, menilai (heuristik + LLM), lalu
mencetak alert yang AKAN dikirim. Tidak memanggil `notify` dan tidak
menyentuh pipa beli.

Pakai:
    PYTHONPATH=. python3 tools/pons_probe.py
"""
import time

from sniper import db, pons_radar


def main() -> int:
    before_pos = db._conn().execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    before_calls = db._conn().execute("SELECT COUNT(*) FROM calls").fetchone()[0]

    t = time.time()
    n = pons_radar.scan_curve_progress()
    print(f"kandidat_diproses={n} ({round(time.time() - t, 1)}s) "
          f"ambang_progress={db.get('pons_min_progress')}")

    pending = db.get_pons_pending(10)
    print(f"antrean={len(pending)}")
    if pending:
        t = time.time()
        alerts = pons_radar.process_candidates(pending)
        print(f"proses={round(time.time() - t, 1)}s alert={len(alerts)}")
        for a in alerts:
            print("\n" + pons_radar.compose_alert(a))

    after_pos = db._conn().execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    after_calls = db._conn().execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    print(f"\nposisi {before_pos}->{after_pos} · calls {before_calls}->{after_calls}")
    print("stats:", db.pons_stats())
    if after_pos != before_pos or after_calls != before_calls:
        print("GAGAL: radar menyentuh tabel trading — ini bug kritis")
        return 1
    print("OK: radar tidak menyentuh positions/calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
