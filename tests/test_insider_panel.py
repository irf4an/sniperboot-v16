"""🧪 Uji PANEL INSIDER control_bot v16.6 — tanpa jaringan / Telegram.

Lingkup (urutan = fitur panel):
  0. insider_addr_valid: 0x + 40 hex, regex ketat, normalisasi lower().
  1. insaddr (scan wallet): hasil screening layar + tombol '✅ Tambah ke
     insider' — TIDAK otomatis ditambah (tombol insconf yang menambah).
  2. insider_screen_panel: paginasi 10/halaman (PAGE_SIZE), nav ◀/▶,
     tombol toggle per wallet, « Sources / « Menu, header aktif/total.
  3. instoggle: ✅/🚫 per wallet → toggle_insider → refresh.
  4. insreset: konfirmasi dua langkah (Ya = enabled 0 semua, TANPA
     hapus; Batal tidak mengubah apa pun) + insenableall (aktifkan semua).
  5. insrotate: rotate() yang ada di background, lock anti double-run
     ("sedang berjalan"), ringkasan ke chat.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Btn:
    """Stub Button.inline tanpa Telethon (hanya simpan teks)."""

    def __init__(self, text, data):
        self.text = text
        self.data = data


def _setup():
    """Stub telethon dulu (modul nyata butuh koneksi/loop), baru import cb."""
    import types
    tel = types.ModuleType("telethon")
    ev = types.ModuleType("telethon.events")
    btn_mod = types.ModuleType("telethon.Button")
    utils = types.ModuleType("telethon.utils")
    ev.CallbackQuery = lambda *a, **kw: "cbq"      # type: ignore[attr-defined]
    ev.NewMessage = lambda *a, **kw: "nm"          # type: ignore[attr-defined]
    btn_mod.inline = _Btn                          # type: ignore[attr-defined]
    tel.events = ev                                # type: ignore[attr-defined]
    tel.Button = btn_mod                           # type: ignore[attr-defined]
    tel.utils = utils                              # type: ignore[attr-defined]
    for name, mod in (("telethon", tel), ("telethon.events", ev),
                      ("telethon.Button", btn_mod), ("telethon.utils", utils)):
        sys.modules.setdefault(name, mod)
    from sniper import control_bot as cb
    return cb


cb = _setup()

from sniper import config, db  # noqa: E402


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old = config.DB_PATH
    config.DB_PATH = tmp.name
    return tmp.name, old


class _FakeEvent:
    """Event CallbackQuery/Respond mini: rekam answer() & respond()."""

    sender_id = 777

    def __init__(self):
        self.answered = []
        self.responded = []
        self.edited = []

    async def answer(self, text=None, alert=False):
        self.answered.append(text or "")

    async def respond(self, text, buttons=None):
        self.responded.append(text)
        if buttons is not None:
            self.edited.append((text, buttons))
        return self

    async def edit(self, text, buttons=None):
        self.edited.append((text, buttons))

    def clear(self):
        self.answered.clear()
        self.responded.clear()
        self.edited.clear()


def _btn_data(btn):
    d = btn.data
    return d.decode() if isinstance(d, bytes) else str(d)


def _btn_label(btn):
    """Label tombol — stub Button menyimpan teks di atribut `text`."""
    return getattr(btn, "text", "") or ""


def _all_btns(buttons):
    return [b for row in buttons for b in row]


def _find_btn(buttons, data_prefix):
    hits = [b for b in _all_btns(buttons) if _btn_data(b).startswith(data_prefix)]
    return hits[0] if hits else None


# =========================================================
# 0. Validasi alamat
# =========================================================

def test_validate_address():
    ok = cb.insider_addr_valid("0x" + "a" * 40)
    assert ok == "0x" + "a" * 40, ok
    ok = cb.insider_addr_valid("  0x" + "A" * 40 + "  ")  # hex huruf besar + spasi: ok
    assert ok == "0x" + "a" * 40, ok
    assert cb.insider_addr_valid("0X" + "a" * 40) is None  # prefix 0X ditolak (ketat)
    for bad in ("0x" + "G" * 40,            # bukan hex
                "0x" + "a" * 39,            # 39 hex
                "0x" + "a" * 41,            # 41 hex
                "abc",                      # tanpa 0x
                "0x" + "a" * 40 + " x",     # ada sisa teks
                "eth 0x" + "a" * 40,        # prefix lain
                ""):
        assert cb.insider_addr_valid(bad) is None, bad
    print("PASS insider_addr_valid: regex ketat 0x+40hex")


# =========================================================
# 1. Scan wallet (insaddr → _scan_wallet_done → insconf)
# =========================================================

def test_scan_address_flow():
    """Screening pakai cross_check yang ada, background; hasil layar +
    tombol tambah manual. Tidak ada insider baru sebelum tombol ditekan."""
    path, old = _fresh_db()
    try:
        db.init()
        ca = "0x" + "a" * 40
        winner = "0x" + "b" * 40
        calls = []

        import tools.screen_insiders as si
        orig_cc = si.cross_check
        orig_fb = si.fetch_early_buyers
        si.fetch_early_buyers = lambda ca2: (calls.append(ca2), [])[1]
        si.cross_check = lambda cas: calls.append(("cc", tuple(cas))) or \
            [(winner, 3, [ca])]
        try:
            ev = _FakeEvent()
            asyncio.run(cb._scan_wallet_done(ev, ca))
        finally:
            si.cross_check = orig_cc
            si.fetch_early_buyers = orig_fb

        assert calls and calls[0] == ca, calls        # early buyers dipakai
        assert ("cc", (ca,)) in calls, calls
        text, buttons = ev.responded[0], ev.edited[0][1]
        assert winner[:10] in text, text
        btn = _find_btn(buttons, "insconf:")
        assert btn is not None, [_btn_label(b) for b in _all_btns(buttons)]
        assert winner in _btn_data(btn), btn.data
        assert winner not in db.get_active_insiders()   # belum ditambah
        assert db.list_insiders() == [] or all(
            r["address"] != winner for r in db.list_insiders())
        print("PASS insaddr: screening background + tawaran tombol (manual)")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_scan_then_add():
    """Tombol insconf menambah insider (label manual_panel); dedup
    double-click cuma +win_count."""
    path, old = _fresh_db()
    try:
        db.init()
        addr = "0x" + "b" * 40
        ca = "0x" + "a" * 40
        ev = _FakeEvent()
        asyncio.run(cb._route(ev, f"insconf:{addr}:{ca}"))
        rows = [r for r in db.list_insiders() if r["address"] == addr]
        assert len(rows) == 1 and rows[0]["label"] == "manual_panel", rows
        assert rows[0]["win_count"] == 1 and rows[0]["enabled"] == 1, rows[0]

        asyncio.run(cb._route(ev, f"insconf:{addr}:{ca}"))
        rows = [r for r in db.list_insiders() if r["address"] == addr]
        assert len(rows) == 1 and rows[0]["win_count"] == 2, rows
        print("PASS insconf: tambah manual_panel + dedupe +win_count")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 2. Layar insider + paginasi
# =========================================================

def test_insider_screen_pagination():
    path, old = _fresh_db()
    try:
        db.init()
        for i in range(15):
            # mulai dari 1: index 0 = zero address (ditolak guard non-trader)
            db.add_insider_wallet("0x" + "c0" * 16 + f"{i + 1:08x}", label=f"w{i:02d}",
                                  win_count=15 - i)
        db.toggle_insider("0x" + "c0" * 16 + f"{6:08x}")     # 1 wallet disabled

        assert cb.PAGE_SIZE == 10, cb.PAGE_SIZE
        t, b = cb.insider_screen_panel(0)
        toggles = [b_ for b_ in _all_btns(b) if _btn_data(b_).startswith("instoggle:")]
        assert len(toggles) == 10, len(toggles)
        assert _find_btn(b, "inspagecur") and "1/2" in _btn_label(_find_btn(b, "inspagecur")), b
        assert _find_btn(b, "inspage:1"), "tombol ▶ harus ada"

        t2, b2 = cb.insider_screen_panel(1)
        t2_toggles = [b_ for b_ in _all_btns(b2)
                      if _btn_data(b_).startswith("instoggle:")]
        assert len(t2_toggles) == 5, len(t2_toggles)
        assert _find_btn(b2, "inspagecur") and "2/2" in _btn_label(_find_btn(b2, "inspagecur")), b2
        assert _find_btn(b2, "inspage:0"), "tombol ◀ harus ada"

        nav = "\n".join(_btn_label(b_) for b_ in _all_btns(b))
        assert "« Sources" in nav and "« Menu" in nav, nav
        assert "14/15" in t, t                           # aktif/total
        assert "• w00 ·" in t and "• w09 ·" in t, t      # urutan win_count
        assert "• w10 ·" not in t and "• w14 ·" in t2, (t, t2)  # halaman 0 vs 1
        print("PASS insider_screen_panel: paginasi 10/hal, nav, header")
    finally:
        os.unlink(path)
        config.DB_PATH = old


def test_source_button_and_next_page_data():
    """Layar Sources punya tombol 🕵️ Insider; inspage menggunakan rowstart
    list_insiders() (urutan sama)."""
    path, old = _fresh_db()
    try:
        db.init()
        ev = _FakeEvent()
        asyncio.run(cb._route(ev, "sources"))
        btn = _find_btn(ev.edited[0][1], "ins")
        assert btn is not None, ev.edited[0][1]
        assert "Insider" in _btn_label(btn), _btn_label(btn)

        for i in range(11):
            # mulai dari 1: index 0 = zero address (ditolak guard non-trader)
            db.add_insider_wallet("0x" + "c0" * 16 + f"{i + 1:08x}", win_count=11 - i)
        ev2 = _FakeEvent()
        asyncio.run(cb._route(ev2, "inspage:1"))
        t, b = ev2.edited[0]
        assert "2/2" in "\n".join(_btn_label(b_) for b_ in _all_btns(b)), t
        rows = db.list_insiders()
        assert rows[10]["address"] == "0x" + "c0" * 16 + f"{11:08x}"  # paling bawah (win_count=1)
        print("PASS sources tombol Insider + inspage konsisten urutan DB")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 3. Toggle per wallet
# =========================================================

def test_insider_toggle():
    path, old = _fresh_db()
    try:
        db.init()
        addr = "0x" + "c" * 40
        db.add_insider_wallet(addr, win_count=1)
        assert addr in db.get_active_insiders()

        ev = _FakeEvent()
        asyncio.run(cb._route(ev, f"instoggle:0:{addr}"))
        assert addr not in db.get_active_insiders()
        # panel di-refresh di pesan yang sama (event.edit) — bukan respond baru
        assert ev.edited, "layar harus refresh"
        t, b = ev.edited[0]
        assert _find_btn(b, f"instoggle:0:{addr}") is not None

        asyncio.run(cb._route(ev, f"instoggle:1:{addr}"))
        assert addr in db.get_active_insiders()
        print("PASS instoggle: ✅/🚫 flip + refresh layar")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 4. Reset dua langkah + enable semua
# =========================================================

def test_reset_confirm_flow():
    path, old = _fresh_db()
    try:
        db.init()
        addr = "0x" + "d" * 40
        db.add_insider_wallet(addr, win_count=2)
        ev = _FakeEvent()

        # langkah 1: layar konfirmasi, belum mengubah DB
        asyncio.run(cb._route(ev, "insreset"))
        t, b = ev.edited[0]
        yes = _find_btn(b, "insreset:yes")
        no = _find_btn(b, "insreset:no")
        assert yes and no, b
        assert "nonaktifkan" in _btn_label(yes).lower(), _btn_label(yes)
        assert "Batal" in _btn_label(no), _btn_label(no)
        assert addr in db.get_active_insiders()

        # Batal → tidak mengubah apa pun
        asyncio.run(cb._route(ev, "insreset:no"))
        assert addr in db.get_active_insiders()

        # Ya → semua disabled, tidak ada yang dihapus
        asyncio.run(cb._route(ev, "insreset"))
        asyncio.run(cb._route(ev, "insreset:yes"))
        assert db.get_active_insiders() == []
        rows = db.list_insiders()
        assert len(rows) == 1 and rows[0]["address"] == addr, rows
        assert rows[0]["win_count"] == 2 and rows[0]["enabled"] == 0, rows[0]

        # ✅ Aktifkan semua → balik
        asyncio.run(cb._route(ev, "insenableall"))
        assert addr in db.get_active_insiders()
        print("PASS insreset: konfirmasi 2-langkah, tanpa hapus, enable-all")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 5. Rotate sekarang — lock dedup + ringkasan
# =========================================================

def test_rotate_now_lock_and_summary():
    path, old = _fresh_db()
    try:
        db.init()
        calls = []

        def fake_rotate(limit=15, **kw):
            calls.append(limit)
            time.sleep(0.2)
            return {"added": 2, "disabled": 1, "total_active": 3,
                    "runners": 5, "dry": False}

        orig = sys.modules.get("tools.rotate_insiders")
        fake_mod = type(sys)("tools.rotate_insiders")
        fake_mod.rotate = fake_rotate
        sys.modules["tools.rotate_insiders"] = fake_mod
        ev = _FakeEvent()
        try:
            async def _run():
                await cb._route(ev, "insrotate")       # mulai background
                await cb._route(ev, "insrotate")       # gagal "sedang berjalan"
                await asyncio.wait_for(cb._rotate_task, 2.0)
            asyncio.run(_run())
        finally:
            if orig is not None:
                sys.modules["tools.rotate_insiders"] = orig
            else:
                sys.modules.pop("tools.rotate_insiders", None)

        assert calls == [15], calls                        # hanya sekali
        assert any("sedang berjalan" in a for a in ev.answered), ev.answered
        msg = "\n".join(ev.responded)
        assert "+2" in msg, msg
        assert "3" in msg, msg                             # total aktif
        assert "prune" in msg.lower() or "nonaktif" in msg.lower(), msg
        print("PASS insrotate: lock anti double-run + ringkasan")
    finally:
        os.unlink(path)
        config.DB_PATH = old


# =========================================================
# 6. Baris wallet
# =========================================================

def test_wallet_rows_and_last_seen():
    path, old = _fresh_db()
    try:
        db.init()
        addr = "0x" + "e" * 40
        db.add_insider_wallet(addr, win_count=0)
        db.record_insider_result(addr, won=True)
        db.record_insider_result(addr, won=False)
        db.record_insider_result(addr, won=False)
        row = [r for r in db.list_insiders() if r["address"] == addr][0]
        assert row["win_count"] == 1 and row["loss_count"] == 2, row
        assert row["last_seen"] > 0, row
        t, b = cb.insider_screen_panel(0)
        assert addr[:6] in t or "0xe" in t, t
        toggle = _find_btn(b, f"instoggle:0:{addr}")
        assert toggle is not None and _btn_label(toggle) == "🚫", toggle
        # Test Log Beli Insider panel
        db.record_insider_buy("0x" + "1" * 40, addr, symbol="RAID", mcap=394700, liq=56600)
        db.record_insider_buy("0x" + "1" * 40, "0x" + "f" * 40, symbol="RAID", mcap=395000, liq=57000)
        ev = _FakeEvent()
        asyncio.run(cb._route(ev, "insbuys:3600"))
        tb, bb = ev.edited[0]
        assert "RAID" in tb and "2x beli" in tb and "2 wallet" in tb, tb
        print("PASS insider_rows: win/loss/last_seen + status tombol + Log Beli Insider")
    finally:
        os.unlink(path)
        config.DB_PATH = old


if __name__ == "__main__":
    test_validate_address()
    test_scan_address_flow()
    test_scan_then_add()
    test_insider_screen_pagination()
    test_source_button_and_next_page_data()
    test_insider_toggle()
    test_reset_confirm_flow()
    test_rotate_now_lock_and_summary()
    test_wallet_rows_and_last_seen()
    print("ALL PASS")
