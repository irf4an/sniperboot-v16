"""Unit test helper bersama: sniper/fmt.py & sniper/hexutil.py.

Kedua modul ini dipakai lintas-kanal (listener, tracker, control_bot,
scanner, v4), jadi formatnya dikunci di sini supaya tidak melenceng lagi.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import fmt  # noqa: E402
from sniper.hexutil import topic_addr  # noqa: E402

ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  OK {name}")
    else:
        fail += 1
        print(f"  FAIL {name}: got={got!r} want={want!r}")


print("=== fmt.usd_compact ===")
check("juta", fmt.usd_compact(1_234_567), "$1.2M")
check("ribu bulat tanpa .0", fmt.usd_compact(52_000), "$52k")
check("ribu desimal", fmt.usd_compact(3_100), "$3.1k")
check("ratusan", fmt.usd_compact(812), "$812")
check("nol", fmt.usd_compact(0), "$0")
check("None → fallback", fmt.usd_compact(None), "—")
check("None → fallback custom", fmt.usd_compact(None, "?"), "?")
check("string angka", fmt.usd_compact("1500"), "$1.5k")

print("=== fmt.liq ===")
check("None (v4 tanpa data)", fmt.liq(None), "liq n/a (v4 on-chain)")
check("nilai", fmt.liq(12_345.6), "liq $12,346")

print("=== fmt.short_addr ===")
addr = "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"
check("label db menang", fmt.short_addr(addr, "manual", {addr.lower(): "WHALE"}), "WHALE")
check("label operator", fmt.short_addr(addr, " manual "), "manual")
check("potong", fmt.short_addr(addr), "0xabcd…ef01")
check("kosong", fmt.short_addr(""), "?")
check("label kosong diabaikan", fmt.short_addr(addr, "   "), "0xabcd…ef01")

print("=== hexutil.topic_addr ===")
word = "0x" + "00" * 12 + "abcdef0123456789abcdef0123456789abcdef01"
check("hex string padding", topic_addr(word),
      "0xabcdef0123456789abcdef0123456789abcdef01")
check("bytes", topic_addr(bytes.fromhex("00" * 12 + "ab" * 20)),
      "0x" + "ab" * 20)
check("huruf besar → kecil", topic_addr("0x" + "00" * 12 + "AB" * 20),
      "0x" + "ab" * 20)


class _HB:
    """Tiruan HexBytes (punya .hex())."""

    def __init__(self, s):
        self._s = s

    def hex(self):
        return self._s


check("objek .hex()", topic_addr(_HB("00" * 12 + "cd" * 20)), "0x" + "cd" * 20)

print(f"\nTOTAL: {ok} OK, {fail} FAIL")
sys.exit(1 if fail else 0)
