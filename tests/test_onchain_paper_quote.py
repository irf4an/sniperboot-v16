"""Unit test: On-Chain Quoter di Paper Trading (v16.4).

JALUR overdose (di luar kuota): orchestrate error dulu =====

Uji:
1) Trader.quote_buy_tokens:
   a) v4: pool kosong (liquidity 0) → (False, 0, v4_quoter_not_enough_liquidity...)
   b) v4: quoter revert NotEnoughLiquidity → gagal + alasan jelas
   c) v4: neuron quoter sukses → (True, raw_out, '')
   d) v4: slippage (quote < min_out) → slippage_exceeded_onchain
   e) v2: router.getAmountsOut → sukses / revert → gagal
   f) v3: static-call revert / pool kosong → v3_quoter_...:...
2) listener paper-tracking (replikasi blok agent_dry_run):
   - quote gagal → status "buy_failed:<alasan>", NOTIF SIMULASI BUY GAGAL,
     posisi TIDAK dibuka di DB, kas virtual TIDAK terpotong
   - quote sukses → posisi dibuka dengan entry_price_usd =
     buy_usd / tokens_total (dari QUOTE on-chain) — bukan harga layar.

Jalankan: python3 tests/test_onchain_paper_quote.py
(DB temporari — tidak menyentuh produksi.)
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpdb.close()
os.environ["DB_PATH"] = _tmpdb.name

from sniper import config, db          # noqa: E402,F401
from sniper import v4 as v4mod         # noqa: E402
from sniper import listener, dexscreener as dx   # noqa: E402
from sniper.trader import Trader       # noqa: E402
from sniper.v4 import QuoteResult      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(
        f"{name}{' — ' + detail if detail and not cond else ''}")
    print(f"  {'OK' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


db.init()
db.reset_paper_cash(100.0)

CA = "0x" + "ab" * 20
W3_SENTINEL = object()   # w3 dummy: helper on-chain divia FakeTrader


# ====================================================================
print("\n[1] quote_buy_tokens — tanpa web3 asli (fakes)")
class FakeERC20:
    def functions(self):
        raise AssertionError("tidak dipakai")


class FakeTrader:
    """Trader stub tanpa __init__ (tanpa RPC) — quote_buy_tokens path
    sama, w3/router diganti fake (dipatch per cabang di bawah)."""
    router2 = None
    router3 = None
    addr = "0x" + "11" * 40
    weth_addr = "0x" + "22" * 40
    w3 = None

    quote_buy_tokens = Trader.quote_buy_tokens


ft = FakeTrader()

# ---- a) v4 pool kosong (liquidity 0 on-chain) ----
v4mod_liquidity = v4mod.liquidity
v4mod_liquidity = getattr(ft, "pool_key", None)
# patch v4.liquidity & v4.pool_key via module-level fakes
_liquidity = None


def fake_liquidity(pool_id, w3):
    return 0   # pool kosong


saved_liq, saved_pk = v4mod.liquidity, v4mod.pool_key
orig_quote_buy = v4mod.quote_buy
v4mod.liquidity = fake_liquidity
try:
    ok, out, err = ft.quote_buy_tokens(
        CA, "v4", 3000, 10**17, min_out=1, pool_id="0x" + "aa" * 32)
    check("v4 pool kosong → tolak", not ok and out == 0
          and "not_enough_liquidity" in err, err)
finally:
    v4mod.liquidity = saved_liq
    v4mod.pool_key = saved_pk

# ---- b) v4 quoter revert NotEnoughLiquidity ----


def fake_liquidity_ok(pool_id, w3):
    return 123456


def fake_pool_key_ok(pool_id, w3):
    return v4mod.PoolKey(v4mod.ZERO_ADDRESS_F, CA, 500, 20,
                         v4mod.ZERO_ADDRESS_F)


def fake_quote_buy_nev(pk, wei, w3):
    return v4mod.QuoteResult(False, 0, "NotEnoughLiquidity(bytes32)",
                             "not_enough_liquidity")


v4mod.liquidity = fake_liquidity_ok
v4mod.pool_key = fake_pool_key_ok
v4mod.quote_buy = fake_quote_buy_nev
try:
    ok, out, err = ft.quote_buy_tokens(
        CA, "v4", 500, 10**17, min_out=1, pool_id="0x" + "aa" * 32)
    check("v4 quoter NotEnoughLiquidity → (False, 0, v4_quoter_...)",
          not ok and out == 0 and err.startswith("v4_quoter_not_enough_liquidity"),
          err)
finally:
    pass

# ---- c) v4 quote sukses + d) slippage guard ----


def fake_quote_buy_ok(pk, wei, w3):
    return v4mod.QuoteResult(True, 5 * 10**18, "", "ok")


v4mod.quote_buy = fake_quote_buy_ok
try:
    ok, out, err = ft.quote_buy_tokens(
        CA, "v4", 500, 10**17, min_out=1, pool_id="0x" + "aa" * 32)
    check("v4 quoter sukses → (True, raw_out, '')",
          ok and out == 5 * 10**18 and err == "", f"{ok},{out},{err}")
    ok2, out2, err2 = ft.quote_buy_tokens(
        CA, "v4", 500, 10**17, min_out=10 * 10**18, pool_id="0x" + "aa" * 32)
    check("v4 quote < min_out → slippage_exceeded_onchain",
          not ok2 and err2 == "slippage_exceeded_onchain" and out2 == 5 * 10**18,
          f"{ok2},{out2},{err2}")
finally:
    # restore v4mod asli
    v4mod.liquidity = saved_liq
    v4mod.pool_key = saved_pk
    v4mod.quote_buy = orig_quote_buy

# ---- e) v2 dinonaktifkan (mode v4-only) ----
ft2 = FakeTrader()
ok, out, err = ft2.quote_buy_tokens(CA, "v2", 0, 10**17, min_out=1)
check("v2 pool dinonaktifkan → (False, 0, 'v2_pool_disabled')",
      not ok and out == 0 and err == "v2_pool_disabled", err)

# ---- f) v3 ----


class FakeRouter3Revert:
    class functions:
        @staticmethod
        def exactInputSingle(params):
            class F:
                def call(self, base=None):
                    raise RuntimeError(
                        "execution reverted: NotEnoughLiquidity()")
            return F()


ft3 = FakeTrader()
ft3.router3 = FakeRouter3Revert()
ok, out, err = ft3.quote_buy_tokens(CA, "v3", 3000, 10**17, min_out=1)
check("v3 revert NotEnoughLiquidity → v3_quoter_not_enough_liquidity",
      not ok and err.startswith("v3_quoter_not_enough_liquidity"), err)


class FakeRouter3OK:
    class functions:
        @staticmethod
        def exactInputSingle(params):
            class F:
                def call(self, base=None):
                    return 2 * 10**18
            return F()


ft3.router3 = FakeRouter3OK()
ok, out, err = ft3.quote_buy_tokens(CA, "v3", 3000, 10**17, min_out=1)
check("v3 static-call sukses → (True, out, '')",
      ok and out == 2 * 10**18 and err == "", f"{ok},{out},{err}")
ok, out, err = ft3.quote_buy_tokens(CA, "v3", 3000, 10**17, min_out=8 * 10**18)
check("v3 slippage guard", not ok and err == "slippage_exceeded_onchain",
      f"{ok},{err}")

# ====================================================================
print("\n[2] listener paper-trading blok — replikasi blok dry_run")
_notifications = []


def fake_notify(text, buttons=None, **kw):
    _notifications.append(text)
    return text


async def fake_notify_async(text, buttons=None, **kw):
    _notifications.append(text)
    return text


listener._trader = FakeTrader()
listener._notify = fake_notify

src = {"id": 1, "name": "UNITTEST", "mcap_max": 99999}
pair = {"pairAddress": "0PAIRSIM" + "0" * 24, "chainId": "robinhood",
        "priceUsd": "0.001", "priceNative": "0.0005",
        "labels": ["v3"], "liquidity": {"usd": 5000},
        "baseToken": {"symbol": "QT", "address": CA},
        "quoteToken": {"symbol": "ETH"}}
decision = {"action": "BUY", "conviction": 80, "size_mult": 1.0,
            "tp1": 0.5, "tp2": 1.0, "sl": -0.3, "reason": "test", "risks": ""}


def paper_entry(pair, decision, trader, quote_result):
    """Replikasi persis blok agent_dry_run di listener.process_ca
    (on-chain quote → open atau batal). quote_result: (ok, raw, err)."""
    listener._trader = trader
    eth_price = dx.eth_usd(pair)
    buy_usd = db.get("buy_usd") * decision["size_mult"]
    eth_spent = buy_usd / eth_price if eth_price > 0 else 0.0003
    ptype_dx = dx.pool_type(pair)
    fee_dx = 3000
    wei_in = trader.w3.to_wei(eth_spent, "ether") if eth_spent > 0 else 0
    dec = 18
    price_native = float(pair.get("priceNative") or 0)
    expected = (eth_spent / price_native) if price_native > 0 else 0
    slip = db.get("buy_slippage")
    min_out_raw = int(expected * (1.0 - slip) * (10 ** dec)) \
        if expected > 0 else 0
    ok_q, amt_raw, err_reason = quote_result
    if not ok_q or amt_raw <= 0:
        reason_line = err_reason or "quoter_gagal"
        _notifications.append(
            f"❌ **SIMULASI BUY GAGAL (On-Chain Revert)** via {src['name']}\n"
            f"{CA}\nAlasan on-chain: {reason_line}")
        return "buy_failed:" + reason_line
    tokens_total = amt_raw / (10 ** dec)
    entry_price_usd = buy_usd / tokens_total if tokens_total > 0 else 0
    if tokens_total <= 0 or entry_price_usd <= 0:
        return "buy_failed:quoter_output_kosong"
    # potong kas dulu
    gas_usd = float(db.get("paper_gas_eth")) * eth_price
    if not db.deduct_paper_cash(buy_usd + gas_usd):
        return "buy_failed:saldo_virtual_kurang"
    pos = {"ca": CA.lower(), "symbol": "QT",
           "pair_address": pair["pairAddress"], "chain_slug": "robinhood",
           "pool_type": ptype_dx, "fee": fee_dx,
           "entry_price_usd": entry_price_usd,
           "tokens_total": tokens_total, "tokens_left": tokens_total,
           "eth_spent": eth_spent, "usd_spent": buy_usd,
           "honeypot": 0, "source_name": src["name"],
           "buy_tx": "simulated_buy", "is_simulated": 1}
    db.open_position(**pos)
    _notifications.append(
        f"🧪 **[SIMULASI] BUY QT** entry ${entry_price_usd:.10f} "
        f"(on-chain quote) tokens {tokens_total:,.0f}")
    return "dry_run_simulated"


class BoomW3:
    def to_wei(self, x, unit):
        return int(x * 10**18)


# ---- 2a) pool kosong → tolak & tidak ada posisi ----
tr = FakeTrader()
tr.w3 = BoomW3()
_q = patched = None


def trader_quote_fail(ca, ptype, fee, wei_in, min_out=0, pool_id=None):
    return False, 0, "v3_quoter_not_enough_liquidity:empty"


tr.quote_buy_tokens = trader_quote_fail
n_pos_before = len(db.open_positions())
cash_before = db.get_paper_cash()
st = paper_entry(pair, decision, tr, trader_quote_fail(
    CA, "v3", 3000, 10**17))
check("pool kosong → status buy_failed:...", st.startswith("buy_failed:"),
      st)
check("posisi TIDAK dibuka saat quoter gagal",
      len(db.open_positions()) == n_pos_before)
check("kas virtual tidak berubah saat gagal",
      db.get_paper_cash() == cash_before,
      f"{cash_before} -> {db.get_paper_cash()}")
last = _notifications[-1]
check("notif SIMULASI BUY GAGAL berisi alasan on-chain",
      "SIMULASI BUY GAGAL" in last and "not_enough_liquidity" in last, last)


# ---- 2b) slippage jebol → tolak ----
def trader_quote_slip(ca, ptype, fee, wei_in, min_out=0, pool_id=None):
    return False, int(min_out * 0.5), "slippage_exceeded_onchain"


st = paper_entry(pair, decision, tr, trader_quote_slip(
    CA, "v3", 3000, 10**17))
check("slippage_exceeded_onchain → buy_failed", st == "buy_failed:slippage_exceeded_onchain", st)
check("posisi tetap tidak dibuka (slippage)",
      len(db.open_positions()) == n_pos_before)

# ---- 2c) quote normal → buka posisi dengan entry on-chain quote ----
# harga layar = 0.001 → expected 1 ETH modal = 2000 token; quote RIIL:
# price impact → cuma dapat 1.6 juta/1e18... angka riil:
#   buy $5 → eth $2 → eth_spent 2.5 ETH (sein lolos) → raw = 1.6e18
BUY = 5.0
RAW_OK = int(0.8e6 * (10**18) * (BUY * 5 / 5))  # raw besar, hitung presisi
# expected di harga layar: eth_spent=2.5 → expected_raw = 2.5/0.0005 = 5000 token
# min_out = 5000*(1-0.3) = 3500 token *1e18
raw_ok = int(4000 * 10**18)  # quote riil 4000 token < expected 5000
raw_ok = int(4000 * 10**18)


def trader_quote_ok(ca, ptype, fee, wei_in, min_out=0, pool_id=None):
    return True, raw_ok, ""


n_pos_before = len(db.open_positions())
st = paper_entry(pair, decision, tr, trader_quote_ok(
    CA, "v3", 3000, 10**17))
check("quote正常 → dry_run_simulated", st == "dry_run_simulated", st)
pos_row = max(db.open_positions(), key=lambda p: p["id"])
check("posisi dibuka (on-chain quote)",
      len(db.open_positions()) == n_pos_before + 1)
tokens_total = raw_ok / (10**18)
entry_usd = BUY / tokens_total
check("tokens_total dari QUOTE on-chain (4000 token)",
      abs(pos_row["tokens_total"] - 4000.0) < 1e-6,
      f"got {pos_row['tokens_total']}")
check("entry_price_usd = buy_usd / tokens_total (harga riil price impact)",
      abs(pos_row["entry_price_usd"] - BUY / 4000.0) < 1e-12,
      f"got {pos_row['entry_price_usd']}")
check("entry_price_usd lebih tinggi dari harga layar 0.001 (price impact)",
      pos_row["entry_price_usd"] > 0.001)
notif_txt = [t for t in _notifications if "[SIMULASI] BUY QT" in t][-1]
check("notif memuat entry (on-chain quote)",
      "on-chain quote" in notif_txt, notif_txt)

# ====================================================================
print("\n[3] PIPA ASLI listener.process_ca (mode latihan, quoter di-mock)")
# Persis pipa production: agent skip-filter, safety dipanggil, dsb.
db.set_("agent_dry_run", "1")   # entry = FAST-PATH (mode MATA sudah dihapus)
db.set_("social_filter", "off")
db.set_("min_liq_usd", "100")
db.set_("scanner_mcap_max", "99999")


class FakeAgent:
    """DEC2 ABI stub — diberi safeties (v3)."""


class FullFakeTrader:
    """Trader dengan ERC20 ABI stub + on-chain quoters di-mock."""
    addr = "0x" + "11" * 40
    weth_addr = "0x" + "22" * 40
    w3 = BoomW3()
    router2 = None
    router3 = None
    # totalSupply on-chain (raw) — dipakai listener mcap guard real-time.
    total_supply_raw = 25_000_000_000 * 10**18   # 25 miliar token (dec 18)

    def erc20(self, addr):
        ts_raw = self.total_supply_raw

        class Fn:
            def totalSupply(self):
                class F:
                    def call(self):
                        return ts_raw
                return F()

            def decimals(self):
                class F:
                    def call(self):
                        return 18
                return F()

        class ERC20StubFull:
            functions = Fn()

        return ERC20StubFull()


class ERC20Stub:
    def __init__(self, addr):
        pass

    class functions:
        @staticmethod
        def decimals():
            class F:
                def call(self):
                    return 18
            return F()


_src = {"id": 1, "name": "UNITTEST", "mcap_max": 99999}
_ca = "0x" + "cd" * 20
_pair_qfail = {"pairAddress": "0PAIRFAIL" + "0" * 15, "chainId": "robinhood",
               "priceUsd": "0.001", "priceNative": "0.0005",
               "labels": ["v3"], "liquidity": {"usd": 5000},
               "baseToken": {"symbol": "QF", "address": _ca},
               "quoteToken": {"symbol": "ETH"}}
tools_saved = (listener.dx.best_pair, listener.dx.eth_usd, listener.dx.pool_type,
               listener.safety.check)

import sniper.listener as L          # noqa: E402

_orig_best_pair = dx.best_pair
_orig_safety = L.safety.check
_orig_gate = L._fastpath_gate


class FakeERC20Full:
    def functions(self):
        raise AssertionError("x")

    def __init__(self, *a):
        pass

    def call(self, base=None):
        return 18


async def _fake_gate_ok(ca, pair, *args, **kwargs):
    """Fast-path gate selalu lolos — yang diuji di sini adalah jalur paper
    (quote on-chain, mcap guard), bukan ambang Goldilocks."""
    return (True, "ok", {"liq": 5000.0, "mcap": 1000.0,
                         "mcap_onchain": 1000.0, "ratio": 0.5,
                         "pool_age": 60.0})


def setup_pipe(trader, quote_fn, pair):
    """Panggil register pipa: best_pair, safety, fast-path gate, quoter di-stub."""
    L.dx.best_pair = lambda ca: pair
    L.safety.check = lambda tr, ca, p: (True, "ok")
    L._fastpath_gate = _fake_gate_ok
    trader.quote_buy_tokens = quote_fn


def run_pipe(trader, ca, src):
    L._trader = trader
    L._notify = fake_notify_async
    return asyncio.run(L.process_ca(ca, src, None, "UNITTEST", allow_buy=True))


try:
    # ---------- 3a) quoter revert → buy_failed + no position ----------
    tr_a = FullFakeTrader()
    qfail = (False, 0, "v3_quoter_not_enough_liquidity:pool kosong")
    setup_pipe(tr_a, lambda *a, **k: qfail,
               dict(_pair_qfail, marketCap=1000))
    cash_a = db.get_paper_cash()
    npos_a = len(db.open_positions())
    st = run_pipe(tr_a, _ca, _src)
    check("pipa ASLI: quoter gagal → status buy_failed:on-chain",
          st.startswith("buy_failed:") and "quoter_not_enough_liquidity"
          in st, st)
    check("pipa ASLI: no posisi baru", len(db.open_positions()) == npos_a)
    check("pipa ASLI: kas tidak berubah", db.get_paper_cash() == cash_a)
    last = _notifications[-1]
    check("pipa ASLI: notif SIMULASI BUY GAGAL",
          "SIMULASI BUY GAGAL" in last, last[:200])

    # ---------- 3b) quote normal → posisi dibuka (on-chain quote) ----------
    # v16.5 mcap guard: supply on-chain 25jt token →
    # mcap on-chain = entry 0.00125 × 25jt = $31.250 < $45k → LOLOS.
    _ca2 = "0x" + "ee" * 20
    _pair_qok = dict(_pair_qfail, baseToken={"symbol": "QK", "address": _ca2},
                     pairAddress="0PAIROK" + "0" * 15, marketCap=1000)
    tr_b = FullFakeTrader()
    tr_b.total_supply_raw = 25_000_000 * 10**18
    qok = (True, int(4000 * 10**18), "")
    setup_pipe(tr_b, lambda *a, **k: qok, _pair_qok)
    cash_b = db.get_paper_cash()
    st = run_pipe(tr_b, _ca2, _src)
    check("pipa ASLI: quote sukses → dry_run_simulated",
          st == "dry_run_simulated", st)
    pos_row = max(db.open_positions(), key=lambda p: p["id"])
    check("pipa ASLI: tokens_total 4000 (dari QUOTE)",
          abs(pos_row["tokens_total"] - 4000.0) < 1e-6,
          f"got {pos_row['tokens_total']}")
    _spread = float(db.get("paper_entry_spread_pct", float) or 0.025)
    check("pipa ASLI: entry = (5/4000) * (1+paper_entry_spread_pct) (on-chain, after spread)",
          abs(pos_row["entry_price_usd"] - (5.0 / 4000.0) * (1.0 + _spread)) < 1e-12,
          f"got {pos_row['entry_price_usd']}")
    check("pipa ASLI: kas terpotong (modal+gas)",
          db.get_paper_cash() < cash_b)
    notif2 = [t for t in _notifications if "[SIMULASI] BUY QK" in t][-1]
    check("pipa ASLI: notif entry (on-chain quote)",
          "on-chain quote" in notif2, notif2)

    # ---------- 3c) mcap on-chain < batas → LOLOS (Solusi B a) ----------
    # supply 25 Mrd token, quote 4000 token → entry 5/4000 = 0.00125 USD
    # → mcap on-chain = 0.00125 × 25e9 = $31.250.000.000 / supply salah —
    # perjelas: supply TOTAL 25 mrd; entry 0.00125 → mcap $31.25jt >
    # 45k? TIDAK: entry 0.00125 × 25,000,000,000 = $31,250,000. Jadi
    # perkecil supply: 25,000,000 token → mcap = $31.250 < $45k → LOLOS.
    db.set_("scanner_mcap_max", "45000")
    _ca3 = "0x" + "ab" * 20
    _pair_qlo = dict(_pair_qfail, baseToken={"symbol": "QL", "address": _ca3},
                     pairAddress="0PAIRLO" + "0" * 15, marketCap=1000)
    tr_lo = FullFakeTrader()
    tr_lo.total_supply_raw = 25_000_000 * 10**18   # 25 juta token
    setup_pipe(tr_lo, lambda *a, **k: qok, _pair_qlo)
    npos_c = len(db.open_positions())
    st = run_pipe(tr_lo, _ca3, _src)
    check("mcap on-chain $31,250 (< $45k) → dry_run_simulated (LOLOS)",
          st == "dry_run_simulated", st)
    check("mcap guard lolos → posisi dibuka",
          len(db.open_positions()) == npos_c + 1)

    # ---------- 3d) mcap on-chain > batas → DITOLAK (Solusi B b) ----------
    _ca4 = "0x" + "ef" * 20
    _pair_qhi = dict(_pair_qfail, baseToken={"symbol": "QH", "address": _ca4},
                     pairAddress="0PAIRHI" + "0" * 15, marketCap=1000)
    tr_hi = FullFakeTrader()
    tr_hi.total_supply_raw = 100_000_000 * 10**18   # 100 juta token
    # mcap on-chain = 0.00125 × 100jt = $125.000 > $45k → harus ditolak
    setup_pipe(tr_hi, lambda *a, **k: qok, _pair_qhi)
    npos_d = len(db.open_positions())
    cash_d = db.get_paper_cash()
    st = run_pipe(tr_hi, _ca4, _src)
    check("mcap on-chain $125,000 (> $45k) → buy_failed:mcap_onchain_pucuk",
          st == "buy_failed:mcap_onchain_pucuk", st)
    check("mcap guard tolak → TIDAK ada posisi baru",
          len(db.open_positions()) == npos_d)
    check("mcap guard tolak → kas virtual tidak terpotong",
          db.get_paper_cash() == cash_d,
          f"{cash_d} -> {db.get_paper_cash()}")
    last = _notifications[-1]
    check("notif Mcap On-Chain Pucuk jelas",
          "Mcap On-Chain Pucuk" in last and "mcap_onchain_pucuk" in last,
          last[:300])

    # ---------- 3e) totalSupply on-chain gagal → fallback Dexscreener ----
    _ca5 = "0x" + "99" * 20
    # mcap DX $40.000, price DX $0.001 → supply est 40jt →
    # mcap on-chain = 0.00125 × 40jt = $50.000 … masih > 45k? Ya — pakai
    # marketCap $30.000 → supply est 30jt → mcap = $37.500 < $45k → LOLOS.
    _pair_fb = dict(_pair_qfail, baseToken={"symbol": "QF2", "address": _ca5},
                    pairAddress="0PAIRFB" + "0" * 15, marketCap=30000)
    tr_fb = FullFakeTrader()

    class BoomERC20:
        def functions(self):
            raise RuntimeError("call revert")

    tr_fb.erc20 = lambda addr: BoomERC20()   # totalSupply selalu gagal
    setup_pipe(tr_fb, lambda *a, **k: qok, _pair_fb)
    npos_e = len(db.open_positions())
    st = run_pipe(tr_fb, _ca5, _src)
    check("totalSupply gagal → fallback Dexscreener → mcap $37,500 LOLOS",
          st == "dry_run_simulated", st)
    check("fallback Dexscreener → posisi dibuka",
          len(db.open_positions()) == npos_e + 1)
finally:
    L.dx.best_pair = _orig_best_pair
    L.safety.check = _orig_safety
    L._fastpath_gate = _orig_gate

# ====================================================================
print()
print(f"TOTAL: {len(PASS)} OK, {len(FAIL)} FAIL")
if FAIL:
    print(" FAIL:")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("SEMUA TES LULUS.")
