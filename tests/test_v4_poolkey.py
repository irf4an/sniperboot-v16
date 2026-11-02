"""Level 2 — decode event Initialize dari fixture hex mentah → PoolKey.

Fixture = log Initialize PoolManager yang dipulihkan Atlas langsung dari
eth_getLogs untuk pool grok (PoolId 0xb6e6...418c). BUKAN hasil decode
ulang — nilai mentah yang dikembalikan RPC, hanya di-serialisasi.

Event ABI:
  Initialize(bytes32 indexed id, address indexed currency0,
             address indexed currency1, uint24 fee, int24 tickSpacing,
             address hooks, uint160 sqrtPriceX96, int24 tick)
Topics: [topic0, id, currency0, currency1]
Data:   (fee, tickSpacing, hooks, sqrtPriceX96, tick) — masing 32 byte
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpdb.close()
os.environ["DB_PATH"] = _tmpdb.name

from sniper import db          # noqa: E402
from sniper import v4          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(
        f"{name}{' — ' + detail if detail and not cond else ''}")
    print(f"  {'OK' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if not cond else ""))


db.init()

# ============================================================
# Fixture 1: pool grok — hasil getLogs PoolManager asli
# ============================================================
print("\n=== 1. decode log Initialize grok ===")
GROK_POOL_ID = "0xb6e68c9c5b48f3c4226037dc67c83e6f5b60897429a7d9c8d3d41a2ea2cb418c"
GROK_TOPIC0 = "dd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
# data yang dikembalikan node utk event ini: (fee, tickSpacing, hooks, sqrtPriceX96, tick)
_fee_it16, _tickSpacing_it16, _hooks_it16 = 10000, 200, 0x0
grok_fee = 10000
grok_tickSpacing = 200
grok_hooks = "0x" + "00" * 20
_grok_sqrt = 679394651928963067279918579724198
_grok_tick = 181141
GROK_DATA = (
    f"{grok_fee:064x}"
    f"{grok_tickSpacing:064x}"
    f"{int(grok_hooks[2:], 16):064x}"
    f"{_grok_sqrt:064x}"
    f"{_grok_tick:064x}"
)

_log_grok = {
    "address": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    "topics": [
        "0x" + GROK_TOPIC0,
        "0x" + GROK_POOL_ID[2:].zfill(64),
        "0x" + "0" * 64,                                  # currency0 = ETH native
        "0x" + "0" * 24 + "28ad97274f345501e917d49d41c312f78fb9dc07",  # currency1 = grok
    ],
    "data": "0x" + GROK_DATA,
    "blockNumber": 1234567,
}

pk = v4._to_params(_log_grok)
check("_to_params tidak None", pk is not None)
check("currency0 = 0x0 (ETH native)",
      pk.currency0 == "0x0000000000000000000000000000000000000000", str(pk.currency0))
check("currency1 = grok ca (checksummed)",
      str(pk.currency1).lower() == "0x28ad97274f345501e917d49d41c312f78fb9dc07",
      str(pk.currency1))
check("fee = 10000", pk.fee == 10000, str(pk.fee))
check("tickSpacing = 200", pk.tickSpacing == 200, str(pk.tickSpacing))
check("hooks = 0x0 (no hook)", str(pk.hooks) == "0x0000000000000000000000000000000000000000")
check("blockNumber tersimpan", pk.block == 1234567, str(pk.block))

# ============================================================
# Fixture 2: log yang BUKAN Initialize (topics kurang dari 4)
# ============================================================
print("\n=== 2. log bukan Initialize ===")
_log_swing = dict(_log_grok)
_log_swing["topics"] = _log_grok["topics"][:2]
check("topics 2 saja → None", v4._to_params(_log_swing) is None)

# ============================================================
# Fixture 3: data terpotong → None (tidak crash)
# ============================================================
print("\n=== 3. data terpotong ===")
_log_broken = dict(_log_grok)
_log_broken["data"] = "0x" + GROK_DATA[:40]  # terpotong jahat
check("data tidak cukup → None", v4._to_params(_log_broken) is None)

# ============================================================
# 4. _is_pool_id
# ============================================================
print("\n=== 4. _is_pool_id ===")
check("PoolId 32-byte → True", v4._is_pool_id(GROK_POOL_ID))
check("PoolId tanpa 0x → True",
      v4._is_pool_id(GROK_POOL_ID.removeprefix("0x")))
check("alamat kontrak 20-byte → False",
      not v4._is_pool_id("0x28ad97274f345501e917d49d41c312f78fb9dc07"))
check("bukan hex → False", not v4._is_pool_id("0xzz"))

# ============================================================
# 5. PoolKey sendiri (tanpa w3)
# ============================================================
print("\n=== 5. PoolKey ===")
pk2 = v4.PoolKey(
    currency0="0x0000000000000000000000000000000000000000",
    currency1="0x28ad97274f345501e917d49d41c312f78fb9dc07",
    fee=10000, tickSpacing=200,
    hooks="0x0000000000000000000000000000000000000000")
check("constructor 5 arg ok", len(pk2.as_tuple) == 5)
check("tuple nilai benar",
      pk2.as_tuple[0] == "0x0000000000000000000000000000000000000000"
      and pk2.as_tuple[1].lower() == "0x28ad97274f345501e917d49d41c312f78fb9dc07"
      and pk2.as_tuple[2] == 10000 and pk2.as_tuple[3] == 200)

# ============================================================
# 6. pool_key() tanpa w3 / dengan pool_id bukan-PoolId → None
# ============================================================
print("\n=== 6. pool_key() tanpa jaringan ===")
check("w3=None → None", v4.pool_key(GROK_POOL_ID, None) is None)
check("pool_id = alamat kontrak (bukan PoolId) → None",
      v4.pool_key("0x28ad97274f345501e917d49d41c312f78fb9dc07", None) is None)
check("pool_id kosong → None", v4.pool_key("", None) is None)

print()
print(f"TOTAL: {len(PASS)} OK, {len(FAIL)} FAIL")
if FAIL:
    print(" FAIL:")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("SEMUA LULUS ✅")
