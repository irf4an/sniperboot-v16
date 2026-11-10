"""🧪 Uji deteksi launch padding (satu alamat sebar token ke banyak dompet)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import config, safety  # noqa: E402

SUPPLY = 1_000_000 * 10**18
ZERO = "0x" + "0" * 40


class _Topic:
    def __init__(self, addr):
        pad = "0" * 24 + addr.lower().removeprefix("0x")
        self._b = bytes.fromhex(pad)

    def hex(self):
        return "0x" + self._b.hex()


class _Log:
    def __init__(self, frm, to, value, block):
        self.topics = [_Topic("0x" + "11" * 32), _Topic(frm), _Topic(to)]
        self.data = int(value).to_bytes(32, "big")
        self.blockNumber = block

    def __getitem__(self, key):      # web3 AttributeDict mendukung lg["..."]
        return getattr(self, key)


def _addr(i: int):
    return "0x" + f"{i:040x}"


def test_deteksi_padding_satu_pengirim():
    router = _addr(0xAA)
    logs = [_Log(ZERO, router, SUPPLY, 100)]           # mint ke router
    n = 60
    per = SUPPLY // n
    for i in range(n):
        logs.append(_Log(router, _addr(0x1000 + i), per, 101))
    res = safety._padding_from_logs(logs, SUPPLY, window_blocks=3)
    assert res is not None
    assert res["sender"] == router
    assert res["wallets"] == n, res
    assert 95 <= res["share_pct"] <= 100, res
    print(f"PASS padding terdeteksi: {res['wallets']} dompet, "
          f"{res['share_pct']:.1f}% supply")


def test_bukan_padding_kalau_penerima_sedikit():
    logs = [_Log(ZERO, _addr(1), SUPPLY, 100)]
    for i in range(4):
        logs.append(_Log(_addr(1), _addr(0x2000 + i), SUPPLY // 8, 101))
    res = safety._padding_from_logs(logs, SUPPLY, window_blocks=3)
    assert res is not None
    assert res["wallets"] == 4, res
    print(f"PASS distribusi kecil tidak dianggap padding: {res['wallets']} dompet")


def test_transfer_setelah_window_tidak_dihitung():
    router = _addr(0xBB)
    logs = [_Log(ZERO, router, SUPPLY, 100)]
    for i in range(40):
        logs.append(_Log(router, _addr(0x3000 + i), SUPPLY // 100, 101))
    # 40 dompet lain dikirimi jauh setelah window → tidak boleh menambah hitungan
    for i in range(40):
        logs.append(_Log(router, _addr(0x4000 + i), SUPPLY // 1000, 500))
    res = safety._padding_from_logs(logs, SUPPLY, window_blocks=3)
    assert res["wallets"] == 40, res
    print("PASS transfer di luar window tidak dihitung")


def test_log_kosong_atau_supply_nol():
    assert safety._padding_from_logs([], SUPPLY) is None
    assert safety._padding_from_logs([_Log(ZERO, _addr(1), 1, 1)], 0) is None
    print("PASS data kosong → None")


def test_default_config_mati():
    assert config.SETTING_DEFAULTS["pad_min_wallets"] == "0"
    assert config.SETTING_DEFAULTS["pad_max_supply_pct"] == "60"
    assert config.SETTING_DEFAULTS["pad_window_blocks"] == "3"
    assert "pad_min_wallets" in config.SETTING_LABELS
    print("PASS default filter mati + label tersedia")


if __name__ == "__main__":
    test_deteksi_padding_satu_pengirim()
    test_bukan_padding_kalau_penerima_sedikit()
    test_transfer_setelah_window_tidak_dihitung()
    test_log_kosong_atau_supply_nol()
    test_default_config_mati()
    print("\n✅ SEMUA TES LAUNCH PADDING LULUS")
