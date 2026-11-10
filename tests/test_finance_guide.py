"""🧪 Uji panduan dana (top up / tarik) — murni teks + guard, tanpa jaringan.

Fokus:
  1. Panduan selalu memuat alamat wallet & peringatan jaringan yang benar.
  2. Jawaban deterministik tetap jalan walau LLM mati.
  3. Preview penarikan menolak: alamat salah, nominal > batas, saldo kurang.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import finance_guide as fg  # noqa: E402

ADDR = "0xC47E999e30e39C45ADa3836d176a0A5D2E6769b4"


def test_topup_guide_isi_penting():
    t = fg.topup_guide(ADDR, 0.0)
    assert ADDR in t, "alamat wallet harus muncul"
    assert "Arbitrum" in t, "harus menyebut jaringan jembatan"
    assert "Binance Web3 Wallet" in t, "harus membedakan exchange vs Web3 wallet"
    assert "ROUTE A" in t and "ROUTE B" in t, "dua rute harus ada"
    print("PASS topup guide lengkap")


def test_cex_guide():
    t = fg.cex_guide(ADDR)
    for name in ("Bitrue", "OKX", "Bitget", "KuCoin", "LBank", "Weex"):
        assert name in t, name
    assert "Binance Exchange" in t and ADDR in t
    print("PASS cex guide (6 exchange direct)")


def test_withdraw_guide_isi_penting():
    t = fg.withdraw_guide(ADDR, 0.05, 0.4)
    assert ADDR in t and "Arbitrum" in t and "BCA" in t
    assert "0.05" in t
    print("PASS withdraw guide lengkap")


def test_keyword_fallback():
    assert "TOP UP" in fg._keyword_answer("cara topup dong", ADDR, 0.0)
    assert "TARIK" in fg._keyword_answer("gimana withdraw", ADDR, 0.0)
    assert "Bridge" in fg._keyword_answer("mau bridge", ADDR, 0.0)
    assert "0.0123" in fg._keyword_answer("saldo berapa", ADDR, 0.0123)
    assert "EXCHANGE" in fg._keyword_answer("binance bisa ga", ADDR, 0.0)
    assert fg._keyword_answer("resep nasi goreng", ADDR, 0.0) is None
    print("PASS keyword fallback")


def test_ask_tanpa_llm():
    out = fg.ask("resep nasi goreng", ADDR, 0.0, None, None)
    assert "top up" in out.lower()
    print("PASS ask tanpa LLM tidak error")


class _FakeEth:
    gas_price = 1_000_000_000  # 1 gwei
    block_number = 100

    def __init__(self, balance_wei):
        self._bal = balance_wei

    def get_balance(self, addr):
        return self._bal


class _FakeW3:
    def __init__(self, bal):
        self.eth = _FakeEth(bal)

    def to_wei(self, val, unit):
        return int(float(val) * 1e18)


class _FakeAcct:
    address = ADDR


class _FakeTrader:
    def __init__(self, bal_eth):
        self.w3 = _FakeW3(int(bal_eth * 1e18))
        self.acct = _FakeAcct()
        self.addr = ADDR


def test_preview_guard():
    t = _FakeTrader(0.01)
    # alamat salah
    assert not fg.withdraw_preview(t, "0x123", "0.001")["ok"]
    # nominal melebihi batas
    assert not fg.withdraw_preview(t, ADDR, str(fg.WITHDRAW_MAX_ETH + 1))["ok"]
    # saldo kurang
    assert not fg.withdraw_preview(t, ADDR, "0.5")["ok"]
    # normal
    ok = fg.withdraw_preview(t, ADDR, "0.002")
    assert ok["ok"], ok
    assert abs(ok["amount_eth"] - 0.002) < 1e-12
    assert ok["left_eth"] > 0
    # all → sisa = 0
    al = fg.withdraw_preview(t, ADDR, "all")
    assert al["ok"] and al["left_eth"] < 1e-12
    print("PASS preview guard (alamat/batas/saldo/all)")


def test_start_guide():
    t = fg.start_guide(ADDR, 0.0)
    assert "Generate" in t and ADDR in t and "Arbitrum" in t and "Tarik" in t
    print("PASS start guide (key baru → topup → tarik)")


if __name__ == "__main__":
    test_topup_guide_isi_penting()
    test_withdraw_guide_isi_penting()
    test_cex_guide()
    test_start_guide()
    test_keyword_fallback()
    test_ask_tanpa_llm()
    test_preview_guard()
    print("\n✅ SEMUA TES PANDUAN DANA LULUS")
