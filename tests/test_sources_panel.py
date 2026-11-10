"""🧪 Uji panel Sources: tombol tambah harus selalu ada.

Bug yang dicegah: saat `userbot_enabled=0`, panel Sources dulu menyembunyikan
daftar source DAN tombol tambah, jadi operator tidak bisa menyiapkan grup
sebelum userbot dinyalakan.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import control_bot, db  # noqa: E402


def _btn_texts(buttons):
    return [b.text for row in buttons for b in row]


def test_tombol_tambah_ada_saat_userbot_off():
    old = db.get("userbot_enabled", str)
    try:
        db.set_("userbot_enabled", "0")
        text, buttons = control_bot.sources_panel()
        labels = _btn_texts(buttons)
        assert any("Tambah Source" in t for t in labels), labels
        assert "NON-AKTIF" in text
        assert any("Aktifkan Userbot" in t for t in labels), labels
        print("PASS tombol tambah source ada saat userbot OFF")
    finally:
        db.set_("userbot_enabled", old or "0")


def test_tombol_tambah_ada_saat_userbot_on():
    old = db.get("userbot_enabled", str)
    try:
        db.set_("userbot_enabled", "1")
        text, buttons = control_bot.sources_panel()
        labels = _btn_texts(buttons)
        assert any("Tambah Source" in t for t in labels), labels
        assert "AKTIF" in text
        assert not any("Aktifkan Userbot" in t for t in labels), labels
        print("PASS tombol tambah source ada saat userbot ON")
    finally:
        db.set_("userbot_enabled", old or "0")


def test_source_tersimpan_tampil_di_panel():
    old = db.get("userbot_enabled", str)
    try:
        db.set_("userbot_enabled", "0")
        text, _ = control_bot.sources_panel()
        rows = db.sources()
        if rows:
            for s in rows:
                assert s["name"] in text, f"source {s['name']} tidak tampil"
                assert str(abs(int(s["chat_id"]))) in text.replace("-", ""), s["chat_id"]
            print(f"PASS {len(rows)} source tersimpan tampil walau userbot OFF")
        else:
            print("PASS (tidak ada source tersimpan — dilewati)")
    finally:
        db.set_("userbot_enabled", old or "0")


if __name__ == "__main__":
    test_tombol_tambah_ada_saat_userbot_off()
    test_tombol_tambah_ada_saat_userbot_on()
    test_source_tersimpan_tampil_di_panel()
    print("\n✅ SEMUA TES PANEL SOURCES LULUS")
