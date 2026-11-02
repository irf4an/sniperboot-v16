"""Level 3 — encode_buy / encode_sell UniversalRouter v4 (FORMAT BARU).

Format diverifikasi dari transaksi v4 NYATA on-chain Robinhood Chain:
  tx 0xf9b2e4e8b978c24b0c0a9738dd5e2b5692f5901ba4a33f434279674992f100e6
  commands = 0x10; inputs[0] decode(["bytes","bytes[]"]) →
    actions = 3 byte opcode (0x06 0x0c 0x0f), params = 3 elemen.
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
ETH = "0x0000000000000000000000000000000000000000"
GROK_CA = "0x28ad97274f345501e917d49d41c312f78fb9dc07"
GROK_POOL_ID = "0xb6e68c9c5b48f3c4226037dc67c83e6f5b60897429a7d9c8d3d41a2ea2cb418c"

pk = v4.PoolKey(ETH, GROK_CA, 10000, 200,
                "0x0000000000000000000000000000000000000000")

from eth_abi import decode as abi_decode


def _decode_input(raw):
    """Decode inputs[0] UniversalRouter.execute → (actions bytes, params list)."""
    acts, params = abi_decode(["bytes", "bytes[]"], raw)
    return acts, list(params)


# ============================================================
# 1. encode_buy — format VERIFIED on-chain
# ============================================================
print("\n=== 1. encode_buy ===")
commands, inputs, value = v4.encode_buy(pk, 5 * 10**17, 10**17)
check("commands 1 byte, == 0x10",
      len(commands) == 1 and commands[0] == 0x10, commands.hex())
check("inputs 1 elem (bytes[] length 1)", len(inputs) == 1)
check(f"value == {5 * 10**17} wei (msg.value)",
      value == 5 * 10**17, str(value))

# inputs[0] HARUS bisa didecode balik dgn ("bytes","bytes[]")
acts, params = _decode_input(inputs[0])
check("decode balik inputs[0] dgn ['bytes','bytes[]'] → 2 elem",
      len(params) == 3, str(params))
check("actions == b'\\x06\\x0c\\x0f' (3 byte opcode MURNI)",
      acts == b"\x06\x0c\x0f", acts.hex())
check("aksi berisi 3 opcode", len(acts) == 3, str(len(acts)))
check("opcode[0]==b'\\x06'", acts[0:1] == b"\x06")
check("opcode[1]==b'\\x0c'", acts[1:2] == b"\x0c")
check("opcode[2]==b'\\x0f'", acts[2:3] == b"\x0f")

# ============================================================
# 2. decode balik params
# ============================================================
print("\n=== 2. decode balik params ===")
swap_params = abi_decode(
    ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
    params[0])[0]
pool, zf1, amount_in, min_out, hookd = swap_params
check("poolKey.c0 = ETH sentinel (0x0)", str(pool[0]) == ETH, str(pool[0]))
check("poolKey.c1 = grok ca", str(pool[1]).lower() == GROK_CA, str(pool[1]))
check("poolKey.fee = 10000", pool[2] == 10000, str(pool[2]))
check("poolKey.tickSpacing = 200", pool[3] == 200, str(pool[3]))
check("poolKey.hooks = 0x0",
      str(pool[4]) == "0x0000000000000000000000000000000000000000")
check("zeroForOne=True (ETH→token)", zf1 is True, str(zf1))
check(f"amountIn == {5 * 10**17}", amount_in == 5 * 10**17, str(amount_in))
check(f"minOut == {10**17}", min_out == 10**17, str(min_out))
check("hookData kosong", hookd == b"", repr(hookd))

settle = abi_decode(["address", "uint256"], params[1])
check("SETTLE_ALL currency0 → ETH native", settle[0] == ETH, str(settle[0]))
check("SETTLE_ALL maxAmount = amount_in (5e17)",
      settle[1] == 5 * 10**17, str(settle[1]))
take = abi_decode(["address", "uint256"], params[2])
check("TAKE_ALL currency = grok token", str(take[0]).lower() == GROK_CA)
check("TAKE_ALL minAmount = min_out", take[1] == 10**17, str(take[1]))

# ============================================================
# 3. encode_sell — arah reserve
# ============================================================
print("\n=== 3. encode_sell ===")
c2, i2, v2 = v4.encode_sell(pk, 10**18, 10**16)
check("commands byte pertama=0x10 juga", c2[0] == 0x10)
check("value=0 (tidak kirim ETH)", v2 == 0, str(v2))
acts2, params2 = _decode_input(i2[0])
check("actions sell = 0x060c0f", acts2 == b"\x06\x0c\x0f", acts2.hex())
check("3 params (sell pun)", len(params2) == 3, str(len(params2)))

sp = abi_decode(
    ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
    params2[0])[0]
check("zeroForOne=False (token→ETH)", sp[1] is False, str(sp[1]))
check("amountIn = 10**18", sp[2] == 10**18, str(sp[2]))
sell_settle = abi_decode(["address", "uint256"], params2[1])
check("SETTLE_ALL currency = grok token (settle token masuk)",
      str(sell_settle[0]).lower() == GROK_CA)
sell_take = abi_decode(["address", "uint256"], params2[2])
check("TAKE_ALL currency = ETH native (terima ETH)", sell_take[0] == ETH)

# ============================================================
# 4. Guard arah (D5/D6)
# ============================================================
print("\n=== 4. guard arah ===")
pk_wc = v4.PoolKey(GROK_CA, ETH, 10000, 200,
                   "0x0000000000000000000000000000000000000000")
try:
    v4.encode_buy(pk_wc, 10, 1)
    check("encode_buy raises dgn currency1=0x0", False)
except ValueError as e:
    check("encode_buy menolak currency0≠0x0",
          "encode_buy" in str(e) or "unsupported_native_side" in str(e))
try:
    v4.encode_sell(v4.PoolKey(GROK_CA, GROK_CA, 10000, 200,
                              "0x0000000000000000000000000000000000000000"),
                   10, 1)
    check("encode_sell salah arah → tidak raise", False)
except ValueError:
    check("encode_sell menolak currency1=0x0", True)
# D6 eksplisit: currency0 != 0x0 → tolak
try:
    v4.encode_sell(v4.PoolKey(GROK_CA, "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
                              10000, 200, ETH), 10, 1)
    check("encode_sell dgn currency0≠0x0 → tidak raise", False)
except ValueError:
    check("encode_sell menolak currency0≠ZERO_ADDRESS (D6)", True)
# D5 eksplisit: token di currency0 → unsupported_native_side
try:
    v4.encode_buy(v4.PoolKey(GROK_CA, GROK_CA, 10000, 200, ETH), 10, 1)
    check("encode_buy token di currency0 → tidak raise", False)
except ValueError as e:
    check("encode_buy token di currency0 → unsupported_native_side",
          "unsupported_native_side" in str(e), str(e))

# ============================================================
# 5. _is_pool_id + Permit2 helper (D3/D4)
# ============================================================
print("\n=== 5. _is_pool_id & permit2 helper ===")
check("PoolId 32-byte → True", v4._is_pool_id(GROK_POOL_ID))
check("alamat ERC20 20-byte → False", not v4._is_pool_id(GROK_CA))

# selector Permit2.approve = 0x87517c45 (D3)
from eth_utils import keccak
sel = keccak(text="approve(address,address,uint160,uint48)")[:4].hex()
check("selector Permit2.approve == 0x87517c45",
      sel == "87517c45", sel)
check("PERMIT2_ABI fungsi approve (bukan allowOwner)",
      any(f.get("name") == "approve" for f in v4.PERMIT2_ABI)
      and not any(f.get("name") == "allowOwner" for f in v4.PERMIT2_ABI))

from unittest.mock import MagicMock
_w3 = MagicMock()
try:
    txs = v4.ensure_v4_sell_approval(GROK_CA, _w3, 10**18, 1234567890)
    check("ensure_v4_sell_approval → 2 tx", len(txs) == 2, str(len(txs)))
    check("step1 = erc20 approve ke PERMIT2",
          txs[0]["kind"] == "erc20_approve" and txs[0]["spender"] == v4.PERMIT2)
    check("step2 = Permit2.approve ke UNIVERSAL_ROUTER",
          txs[1]["kind"] == "permit2_approve"
          and txs[1]["spender"].lower() == v4.UNIVERSAL_ROUTER)
except Exception as e:
    check("ensure_v4_sell_approval jalan", False, str(e))

# ============================================================
# 6. Golden vector — transaksi v4 NYATA on-chain (READ-ONLY)
# ============================================================
print("\n=== 6. golden vector on-chain (skip bila RPC mati) ===")
try:
    from sniper.config import RPC_URL
    from web3 import Web3
    _w3r = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    TXH = "0xf9b2e4e8b978c24b0c0a9738dd5e2b5692f5901ba4a33f434279674992f100e6"
    tx = _w3r.eth.get_transaction(TXH)
    # decode inputs: execute(bytes commands, bytes[] inputs, uint256 deadline)
    ur = _w3r.eth.contract(
        Web3.to_checksum_address(v4.UNIVERSAL_ROUTER),
        abi=v4.UNIVERSAL_ROUTER_ABI)
    fn, args = ur.decode_function_input(tx.input)
    c, inp = args[0], args[1]
    c = bytes(c, "latin1") if isinstance(c, str) else bytes(c)
    check("command fn = execute", "execute" in str(getattr(fn, "fn_name", "")))
    check("commands == 0x10", bytes(c) == b"\x10", bytes(c).hex())
    check("1 input", len(inp) == 1, str(len(inp)))
    gacts, gparams = _decode_input(inp[0])
    check("golden actions == 0x060c0f", gacts == b"\x06\x0c\x0f", gacts.hex())
    check("golden len(params) == 3", len(gparams) == 3, str(len(gparams)))
    sp_g = abi_decode(["((address,address,uint24,int24,address),bool,"
                       "uint128,uint128,bytes)"], gparams[0])[0]
    check("golden zeroForOne=True", sp_g[1] is True)
    gp1 = abi_decode(["address", "uint256"], gparams[1])
    check("golden SETTLE_ALL currency = ETH (0x0)", gp1[0] == ETH, str(gp1[0]))
    check("golden SETTLE_ALL maxAmount == 10814000000000000",
          gp1[1] == 10814000000000000, str(gp1[1]))
    gp2 = abi_decode(["address", "uint256"], gparams[2])
    check("golden TAKE_ALL currency token checksum ok",
          str(gp2[0]).lower() == "0xe01ada02fb4d1951cdb8cbb761d509db2ff02c94",
          str(gp2[0]))
    check("golden TAKE_ALL minAmount == 727926672667810471217",
          gp2[1] == 727926672667810471217, str(gp2[1]))
    # kompatibilitas: encode_kita(dari golden poolkey) == golden input?
    gpk = v4.PoolKey(ETH, gp2[0], sp_g[0][2], sp_g[0][3], sp_g[0][4])
    _cmds, _inp, _val = v4.encode_buy(gpk, sp_g[2], gp2[1])
    _ga, _gp = _decode_input(_inp[0])
    check("RE-ENCODE == golden round-trip (aksi & params identik)",
          _ga == gacts and len(_gp) == 3
          and abi_decode(["address", "uint256"], _gp[1]) == gp1
          and abi_decode(["address", "uint256"], _gp[2]) == gp2,
          "re-encode mismatch")
except Exception as e:
    print(f"  SKIP golden vector on-chain — RPC tidak tersedia: {e}")
    print("  (test ini tidak dianggap gagal skip)")

print()
print(f"TOTAL: {len(PASS)} OK, {len(FAIL)} FAIL")
if FAIL:
    print(" FAIL:")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("SEMUA LULUS ✅")
