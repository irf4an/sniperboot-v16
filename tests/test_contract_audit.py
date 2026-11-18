"""🧪 Uji AUDIT KONTRAK (anti-honeypot & anti-blacklist) — tanpa jaringan.

Lingkup:
  1. Kontrak ERC20 standar (clean) → lolos.
  2. Bytecode memuat selector blacklist(address) / setTradingEnabled(bool) → ditolak.
  3. ABI terverifikasi memuat freezeAccount → ditolak.
  4. Bytecode kosong (bukan kontrak) → ditolak.
Semua interaksi jaringan/RPC sebenarnya di-mock, Blockscout pun di-mock —
test deterministik membentur pencilan online.
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import config, db              # noqa: E402
from sniper import safety                  # noqa: E402
from sniper.safety import audit_contract   # noqa: E402


class _FakeW3:
    """Mock trader.w3: hanya butuh .eth.get_code."""

    def __init__(self, code: bytes):
        self._code = code
        self.eth = self

    def get_code(self, addr):
        return self._code


class _FakeTrader:
    def __init__(self, code: bytes):
        self.w3 = _FakeW3(code)


def _burn_bytecode(sig_selector_hex: str) -> bytes:
    """Bytecode palsu yang menyematkan 4-byte selector di dalamnya."""
    inner = sig_selector_hex.lower()
    return bytes.fromhex(inner.ljust(64, "0") + inner.ljust(64, "0"))


def _burn_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old = config.DB_PATH
    config.DB_PATH = tmp.name
    db.init()
    return tmp.name, old


# =========================================================
# 1. Kontrak ERC20 standar → lolos
# =========================================================
def test_clean_erfc20_passes():
    path, old = _burn_db()
    try:
        # bytecode ERC20 generik: totalTransfer/call sudah cukup, tanpa selector jahat
        code = bytes.fromhex("608060405234801561001057600080fd5b50") * 30
        ok, msg = audit_contract(_FakeTrader(code), "0x" + "a" * 40)
        assert ok, f"harus lolos, tolak: {msg}"
        assert "bytecode clean" in msg, msg
    finally:
        config.DB_PATH = old


# =========================================================
# 2. Selector berbahaya di bytecode → ditolak
# =========================================================
def test_blacklist_selector_rejected():
    path, old = _burn_db()
    try:
        sel = safety._selector("blacklist(address)")  # tanpa '0x'
        code = _burn_bytecode(sel)
        ok, msg = audit_contract(_FakeTrader(code), "0x" + "b" * 40)
        assert not ok, "harus ditolak"
        assert "audit kontrak gagal" not in msg  # pesan mentah, bungkus di check()
        assert "kontrak memiliki signature berbahaya" in msg, msg
        assert "blacklist(address)" in msg, msg
    finally:
        config.DB_PATH = old


def test_settrading_enabled_selector_rejected():
    path, old = _burn_db()
    try:
        sel = safety._selector("setTradingEnabled(bool)")
        code = _burn_bytecode(sel)
        ok, msg = audit_contract(_FakeTrader(code), "0x" + "c" * 40)
        assert not ok, "harus ditolak"
        assert "setTradingEnabled(bool)" in msg, msg
    finally:
        config.DB_PATH = old


# =========================================================
# 3. ABI terverifikasi memuat freezeAccount → ditolak
# =========================================================
def test_abi_freeze_account_rejected():
    path, old = _burn_db()
    try:
        code = bytes.fromhex("6080604052348015")  # kontrak kecil, tanpa selector jahat
        fake_abi = [
            {"type": "function", "name": "balanceOf",
             "inputs": [{"name": "account", "type": "address"}], "outputs": []},
            {"type": "function", "name": "freezeAccount",
             "inputs": [{"name": "target", "type": "address"},
                        {"name": "freeze", "type": "bool"}], "outputs": []},
        ]

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"abi": fake_abi, "is_verified": True}

        with patch.object(safety._S, "get", return_value=_Resp(), create=True) as m:
            ok, msg = audit_contract(_FakeTrader(code), "0x" + "d" * 40)
            assert m.called, "Blockscout harus ditanyak"
            assert not ok, "harus ditolak"
            assert "kontrak terverifikasi memuat fungsi berbahaya 'freezeAccount'" in msg, msg
    finally:
        config.DB_PATH = old


# =========================================================
# 4. Bytecode kosong (bukan kontrak) → ditolak
# =========================================================
def test_empty_code_rejected():
    path, old = _burn_db()
    try:
        for code in (b"", bytes.fromhex("")):
            ok, msg = audit_contract(_FakeTrader(code), "0x" + "e" * 40)
            assert not ok, "harus ditolak"
            assert msg == "bukan kontrak (bytecode kosong)", msg
    finally:
        config.DB_PATH = old


# =========================================================
# 5. Bonus: Blockscout timeout → fallback bytecode (tetap clean)
# =========================================================
def test_blockscout_timeout_falls_back():
    """Kebijakan baru: ABI tidak terbaca = fail-CLOSED by default.

    Dulu selalu fail-open ("bytecode clean"), jadi lapisan audit ABI tidak
    pernah benar-benar berjalan. Sekarang perilakunya mengikuti setting
    safety_fail_closed yang sama dengan cek holder/dev.
    """
    path, old = _burn_db()
    try:
        code = bytes.fromhex("6080604052348015605f76005b") * 10

        def _boom(*a, **kw):
            raise TimeoutError("timeout")

        # 1) default (safety_fail_closed=1) → TOLAK
        with patch.object(safety, "_get", side_effect=_boom, create=True):
            ok, msg = audit_contract(_FakeTrader(code), "0x" + "f" * 40)
        assert not ok, f"fail-closed harus menolak: {msg}"
        assert "ABI tidak terbaca" in msg, msg

        # 2) operator memilih fail-open → lolos dengan catatan
        db.set_("safety_fail_closed", "0")
        with patch.object(safety, "_get", side_effect=_boom, create=True):
            ok2, msg2 = audit_contract(_FakeTrader(code), "0x" + "f" * 40)
        assert ok2, f"fail-open harus lolos: {msg2}"
        assert "Blockscout" in msg2, msg2
    finally:
        config.DB_PATH = old


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except Exception as e:
            failed.append(fn.__name__)
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{len(fns) - len(failed)}/{len(fns)} lulus")
    if failed:
        sys.exit(1)
    print("SEMUA LULUS ✅")
