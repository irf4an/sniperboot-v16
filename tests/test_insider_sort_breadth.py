"""🧪 Uji ORDER BY Log Beli Insider (v16.9 Opsi A) — tanpa jaringan.

Premis uji: peringkat utama HARUS breadth (jumlah dompet unik), bukan
jumlah transaksi. Kalau 1-2 bot wash-trading menembak CA yang sama
puluhan kali, koin itu TIDAK boleh menggeser koin yang dikeroyok
banyak dompet berbeda.

Lingkup:
  1. CA "wash" (60 beli / 2 wallet) kalah dari CA "crowd" (11 wallet / 12 beli).
  2. Wallet=NULL/kosong tidak dihitung sebagai dompet unik.
  3. Tie wallet_count → buy_count menentukan.
  4. Tie wallet_count & buy_count → last_ts terbaru menang.
  5. Window memfilter transaksi lama.
  6. Panel teks menyebut urutan breadth (bukan "paling sering dibeli").
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import config, db            # noqa: E402

WASH = "0x" + "aa" * 20
CROWD = "0x" + "bb" * 20
TIE_A = "0x" + "cc" * 20
TIE_B = "0x" + "dd" * 20


def _w(n):
    """Alamat fixture valid: 16 byte awal non-nol supaya lolos guard burn."""
    return "0x" + "a1" * 16 + f"{n:08x}"


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old = config.DB_PATH
    config.DB_PATH = tmp.name
    db.init()
    return tmp.name, old


def test_breadth_beats_volume():
    path, old = _fresh_db()
    try:
        now = int(time.time())
        # wash: 60 transaksi tapi cuma 2 dompet unik
        for i in range(60):
            db.record_insider_buy(WASH, wallet=_w(i % 2 + 1), symbol="WASH",
                                  mcap=30000, liq=9000, ts=now - 10)
        # crowd: 12 transaksi, 11 dompet unik
        for i in range(12):
            db.record_insider_buy(CROWD, wallet=_w(i % 11 + 1), symbol="CROWD",
                                  mcap=53000, liq=12000, ts=now - 20)

        rows = db.top_insider_buys(window_sec=3600, limit=10, now=now)
        assert [r["symbol"] for r in rows[:2]] == ["CROWD", "WASH"], rows
        assert rows[0]["wallet_count"] == 11 and rows[0]["buy_count"] == 12
        assert rows[1]["wallet_count"] == 2 and rows[1]["buy_count"] == 60
        print("PASS Opsi A: 11 dompet unik mengalahkan 60 beli dari 2 dompet")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_empty_wallet_not_counted():
    path, old = _fresh_db()
    try:
        now = int(time.time())
        for _ in range(30):
            db.record_insider_buy(WASH, wallet="", symbol="WASH",
                                  ts=now - 5)
        db.record_insider_buy(CROWD, wallet=_w(1), symbol="CROWD", ts=now - 15)
        db.record_insider_buy(CROWD, wallet=_w(2), symbol="CROWD", ts=now - 6)

        rows = db.top_insider_buys(window_sec=3600, limit=10, now=now)
        assert rows[0]["symbol"] == "CROWD", rows
        assert rows[0]["wallet_count"] == 2
        assert rows[1]["wallet_count"] == 0 and rows[1]["buy_count"] == 30
        print("PASS wallet kosong tidak dihitung sebagai dompet unik")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_tiebreaks():
    path, old = _fresh_db()
    try:
        now = int(time.time())
        db.record_insider_buy(TIE_A, wallet=_w(101), symbol="A", ts=now - 30)
        db.record_insider_buy(TIE_B, wallet=_w(201), symbol="B", ts=now - 30)
        db.record_insider_buy(TIE_B, wallet=_w(202), symbol="B", ts=now - 20)
        db.record_insider_buy(TIE_A, wallet=_w(102), symbol="A", ts=now - 10)

        rows = db.top_insider_buys(window_sec=3600, limit=10, now=now)
        # wallet_count sama (2), buy_count sama (2) → last_ts terbaru menang
        assert [r["symbol"] for r in rows] == ["A", "B"], rows

        # buy_count menentukan saat wallet_count seri
        db.record_insider_buy(TIE_B, wallet=_w(202), symbol="B", ts=now - 5)
        rows = db.top_insider_buys(window_sec=3600, limit=10, now=now)
        assert rows[0]["symbol"] == "B" and rows[0]["buy_count"] == 3, rows
        print("PASS tiebreak: buy_count lalu last_ts")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_window_filter():
    path, old = _fresh_db()
    try:
        now = int(time.time())
        db.record_insider_buy(WASH, wallet=_w(101), symbol="OLD", ts=now - 7200)
        db.record_insider_buy(CROWD, wallet=_w(201), symbol="NEW", ts=now - 60)

        rows = db.top_insider_buys(window_sec=600, limit=10, now=now)
        assert [r["symbol"] for r in rows] == ["NEW"], rows
        print("PASS window memfilter transaksi lama")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_panel_caption_says_breadth():
    from sniper import control_bot
    path, old = _fresh_db()
    try:
        now = int(time.time())
        db.record_insider_buy(CROWD, wallet=_w(201), symbol="X", ts=now - 30)
        db.record_insider_buy(CROWD, wallet=_w(202), symbol="X", ts=now - 20)
        text, _buttons = control_bot.insider_buys_panel(window_sec=3600)
    finally:
        os.unlink(path)
        config.DB_PATH = old
    assert "dompet unik" in text, text[:400]
    assert "paling sering dibeli" not in text
    print("PASS caption panel menyebut urutan dompet unik")


if __name__ == "__main__":
    test_breadth_beats_volume()
    test_empty_wallet_not_counted()
    test_tiebreaks()
    test_window_filter()
    test_panel_caption_says_breadth()
    print("\n✅ SEMUA TES OPSI A LULUS")
