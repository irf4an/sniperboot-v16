"""Probe manual Radar Pons V2 — verifikasi tanpa mengirim Telegram.

Menarik 3000 blok terakhir dari factory Pons V2 ke `pons_queue`, menilai
kandidat (heuristik + LLM), lalu mencetak alert yang AKAN dikirim. Tidak
memanggil `notify` dan tidak menyentuh pipa beli.

Pakai:
    PYTHONPATH=. python3 tools/pons_probe.py

Catatan: probe MENGUBAH watermark `pons_last_block` sementara (mundur 3000
blok) lalu mengembalikannya ke head pada akhir siklus.
"""
import time

from sniper import db, pons_radar

before_pos = db._conn().execute("SELECT COUNT(*) FROM positions").fetchone()[0]
before_calls = db._conn().execute("SELECT COUNT(*) FROM calls").fetchone()[0]

w3 = pons_radar._w3()
head = int(w3.eth.block_number)
db.set_("pons_last_block", str(head - 3000))
print(f"HEAD={head} watermark sementara={head - 3000}")

n = pons_radar.scan_new_launches()
print(f"event_baru={n}")

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
assert after_pos == before_pos and after_calls == before_calls, \
    "RADAR MENYENTUH TABEL TRADING — ini bug kritis"
print("OK: radar tidak menyentuh positions/calls")
