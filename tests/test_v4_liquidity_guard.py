"""Level 4 — guard keamanan v4 di trader.buy(): liquidity & pool_key.

Strategy: PANGG(panggil trader.buy) nyata dengan trader yang _send() &
quoter di-mock — memastikan
1. liquidity 0  → raise '[skip] v4 liquidity 0 (pool kosong)'
   (kasus grok)
   web3 error / None → juga ditolak (fail-closed)
3. pool_key tidak ketemu → raise '[skip] ... v4_poolkey_notfound'
4. liquidity > 0 & poolketemu → lewat, tapi execute() dipanggil via _send()
   (tidak ada transaksi nyata; semua di-serialize via mock).
"""
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpdb.close()
os.environ["DB_PATH"] = _tmpdb.name

from sniper import db          # noqa: E402
from sniper import trader, v4  # noqa: E402
from sniper.config import RPC_URL, CHAIN_ID   # noqa: E402

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
_pk = v4.PoolKey(ETH, GROK_CA, 10000, 200,
                 "0x0000000000000000000000000000000000000000")

_PAIR = {
    "chainId": "robinhood",
    "pairAddress": GROK_POOL_ID,
    "baseToken": {"address": GROK_CA, "symbol": "GROK"},
    "priceNative": "5e-8",
    "priceUsd": "0.0000189",
    "labels": ["v4"],
}


# ============================================================
# Helper: Trader buatan (bypass koneksi nyata & private key)
# ============================================================

def make_mock_trader(liquidity_ret, poolkey_ret, w3):
    """Trader palsu — tak ada wallet nyata, tapi buat buy(path v4) jalan
    melalui cabang yang HARUS diuji."""
    t = MagicMock()
    t.w3 = w3
    return t


def _build_buy(patch_v4_liquidity, patch_v4_poolkey, w3=None, **extra):
    """Panggil trader.Trader.buy() sebenarnya tapi dengan semua bagian jaringan
    di-mock. Melalui patch konsep nyata._send juga di-stub supaya fn.call()
    simumulasi tetap terpicu TAPI tx tidak benar-benar dikirim.

    patch_v4_liquidity / patch_v4_poolkey adalah patch context-manager.
    """
    with patch.object(trader.Trader, "__init__", return_value=None):
        t = trader.Trader()
    # minimal atribut yang dipakai buy() sebelum jalur v4
    t.addr = "0x1234567890abcdef1234567890abcdef12345678"
    t.w3 = w3 or MagicMock()
    # web3 helpers yang dipakai sebelum cabang v4
    t.w3.to_wei = real_ = lambda a, u: int(a * 10**18)
    t.w3.eth.get_balance.return_value = 10**18  # 1 ETH saldo
    erc20_mock = MagicMock()
    erc20_mock.functions.decimals.return_value = MagicMock(call=MagicMock(return_value=18))
    erc20_mock.functions.symbol.return_value = MagicMock(call=MagicMock(return_value=b"GROK"))
    erc20_mock.functions.balanceOf.return_value = MagicMock(
        call=MagicMock(return_value=0))
    t.erc20 = MagicMock(return_value=erc20_mock)
    t.router3 = MagicMock()
    t.router3.functions.WETH9.return_value = MagicMock(
        call=MagicMock(return_value="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"))
    t.router2 = None
    # mock _send supaya tidak kirim tx nyata
    t._send = MagicMock(return_value=MagicMock(
        transactionHash=MagicMock(hex=MagicMock(return_value="0xdeadbeef"))))
    # stub dx.eth_usd & _can_sell
    return t, getattr(t, "_send", None)


def _call_buy(liquidity_val, poolkey_val=None):
    """Panggil trader.buy() melalui _send & web3 di-mock; menjalankan DIAGNOS
    cabang v4. Return (result, exception)."""
    import eth_account
    from sniper.trader import Trader

    calls = []

    with patch.object(Trader, "__init__", return_value=None), \
         patch.object(Trader, "_send") as send_mock, \
         patch("sniper.v4.liquidity", return_value=liquidity_val), \
         patch("sniper.v4.pool_key", return_value=poolkey_val), \
         patch("sniper.trader.time.time", return_value=1_700_000_000):
        t = Trader()
        t.addr = "0x1234567890abcdef1234567890abcdef12345678"
        t.w3 = MagicMock()
        t.w3.to_wei = lambda a, u: int(float(a) * 1e18)
        t.w3.eth.get_balance.return_value = 2 * 10**18
        tok = MagicMock()
        tok.functions.decimals.return_value.call.return_value = 18
        tok.functions.symbol.return_value.call.return_value = "GROK"
        tok.functions.balanceOf.return_value.call.side_effect = [0, 5 * 10**17]
        t.erc20 = MagicMock(return_value=tok)
        t.router3 = MagicMock()
        t.router2 = None
        send_mock.return_value = MagicMock(
            transactionHash=MagicMock(hex=MagicMock(return_value="0xdead")))
        try:
            t._can_sell = MagicMock(return_value=True)
            t._ensure_approval = MagicMock()
            with patch("sniper.dexscreener.eth_usd", return_value=3000.0):
                res = t.buy(GROK_CA, _PAIR, "test", 5.0)
            return res, None
        except Exception as e:
            return None, e


# ============================================================
# 1. liquidity 0 (kasus grok)
# ============================================================
print("\n=== 1. liquidity 0 ===")
res, err = _call_buy(0)
check("liquidity 0 → raise", err is not None)
check(f'msg: "v4 liquidity 0 (pool kosong)"',
      err is not None and "v4 liquidity 0 (pool kosong)" in str(err),
      str(err))
check('msg [skip] prefix', err is not None and "[skip]" in str(err), str(err))

# ============================================================
# 2. liquidity None (web3 / kontrak error)
# ============================================================
print("\n=== 2. liquidity None ===")
res, err = _call_buy(None)
check("liquidity None → juga ditolak", err is not None)
check('msg v4 liquidity 0/kosong',
      err is not None and "v4 liquidity 0" in str(err), str(err))

# ============================================================
# 3. pool_key tidak ditemukan
# ============================================================
print("\n=== 3. poolkey notfound ===")
res, err = _call_buy(5_000_000_000_000_000_000, None)
check("pool_key None → raise", err is not None)
check('msg "v4_poolkey_notfound"',
      err is not None and "v4_poolkey_notfound" in str(err), str(err))

# ============================================================
# 4. liquidity >0 + poolkey ada → jalur kecil ke execute() UniversalRouter
# ============================================================
print("\n=== 4. liquidity>0 & pool_ok → execute() ===")
res, err = _call_buy(5353138358500152822, _pk)
check("jalur normal → tidak raise (result dict)", err is None and res is not None, str(err))
check("pool_type 'v4'", res is not None and res.get("pool_type") == "v4", str(res))
if res is not None:
    check("fee = 10000 (dari PoolKey)", res.get("fee") == 10000, str(res.get("fee")))
    check("pair_address = PoolId",
          res.get("pair_address") == GROK_POOL_ID, str(res.get("pair_address")))

# ============================================================
# 5. dexscreener.ai pada wrapper-pool-type: Dexscreener masih null-liquidity
# ============================================================
print("\n=== 5. listener.process_ca v4 null liquidity → filter:v4_pool_kosong ===")
# (Listener-path test: on-chain 0 → tolak keras)
from unittest.mock import patch as _p
import asyncio

from sniper import listener, dexscreener as dx

db.set_("min_liq_usd", 500)
db.set_("paused", 0)

# pair dengan liquidity null
_pair_null = dict(_PAIR)
_pair_null["liquidity"] = None


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


class _N:
    async def __call__(self, msg, *a, **k):
        return None


with _p.object(listener, "_trader", MagicMock(w3=MagicMock())), \
     _p.object(listener, "_notify", _N()), \
     _p.object(dx, "best_pair", return_value=_pair_null), \
     _p.object(dx, "social_ok", return_value=(True, "off")), \
     _p.object(dx, "pool_type", return_value="v4"), \
     _p.object(dx, "mcap_of", return_value=1000.0), \
     _p.object(v4, "liquidity", return_value=0):
    src = {"id": 0, "name": "SCANNER", "mcap_max": 45000}
    db.record_call("test_liq0", 0)  # biar seen tak balik dup
    _saved_id = db.record_call("test_liq0", 0)
    async def _go():
        try:
            return await listener.process_ca(
                GROK_CA, src, None, "test")
        except Exception as e:
            return f"EXC:{e}"
    db.set_("scanner_on", "1")
    db.set_("agent_dry_run", "1")
    try:
        db.remove_pending_pool("test_liq0")
    except Exception:
        pass
    st = _run(_go())
    check("on-chain 0 → status filtered:v4_pool_kosong atau EXC skip",
          "v4_pool_kosong" in str(st) or "skip" in str(st).lower()
          or "liquidity" in str(st).lower(), str(st))

print()
print(f"TOTAL: {len(PASS)} OK, {len(FAIL)} FAIL")
if FAIL:
    print(" FAIL:")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("SEMUA LULUS ✅")
