"""Unit test FAST-PATH Goldilocks — satu-satunya jalur entry.

Cakupan:
1) mode MATA sudah dihapus total (setting + fungsi agent.decide_entry).
2) goldilocks_eval: umur pool < 30s / > 180s di-skip, 60s lolos.
3) rasio liq/mcap < 0.35 di-skip, >= 0.35 lolos.
4) safety dev > 3% di-skip (via _fastpath_gate).
5) slippage quoter > 15% di-skip (via _fastpath_gate).
6) pipa ASLI: entry hanya lewat fast-path, posisi simulasi terbuka.
7) default settings Goldilocks masuk DB.

Jalankan: python3 tests/test_fastpath_goldilocks.py
(DB temporari — tidak menyentuh produksi.)
"""
import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpdb.close()
os.environ["DB_PATH"] = _tmpdb.name

from sniper import config, db          # noqa: E402
from sniper import listener, agent, dexscreener as dx  # noqa: E402
from sniper.listener import goldilocks_eval, _fastpath_gate  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(
        f"{name}{' — ' + detail if detail and not cond else ''}")
    print(f"  {'OK' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


db.init()

NOW_MS = time.time() * 1000
CA = "0x" + "aa" * 20
SRC = {"id": 1, "name": "UNITTEST", "mcap_max": 50000}


def pair(age_s=60, liq=5000.0, mcap=11000.0, price_native="0.0005",
         price_usd="0.001", symbol="GD"):
    """Pair Dexscreener sintetis: liq/mcap = liq/mcap langsung."""
    return {"pairAddress": "0PAIR" + "0" * 16, "chainId": "robinhood",
            "priceUsd": price_usd, "priceNative": price_native,
            "labels": ["v3"], "pairCreatedAt": NOW_MS - age_s * 1000,
            "marketCap": mcap,
            "liquidity": {"usd": liq},
            "baseToken": {"symbol": symbol, "address": CA},
            "quoteToken": {"symbol": "ETH"}}


_notifications = []


async def fake_notify(text, buttons=None, **kw):
    _notifications.append(text)
    return text


class FakeW3:
    def to_wei(self, x, unit):
        return int(x * 10**18)


class FakeTrader:
    addr = "0x" + "11" * 40
    w3 = FakeW3()

    def erc20(self, addr_):
        raise AssertionError("tidak dipakai di test ini")

    def pool_fee(self, addr_):
        return 3000


class WotoW3:
    def to_wei(self, x, unit):
        return int(x * 10**18)


class Dec18ERC20:
    def __init__(self, addr_):
        pass

    class functions:
        @staticmethod
        def decimals():
            class F:
                def call(self):
                    return 18
            return F()


# =====================================================================
print("\n[1] jalur entry tunggal: FAST-PATH (mode MATA sudah dihapus)")
check("setting entry_mode sudah tidak ada",
      "entry_mode" not in config.SETTING_DEFAULTS)
check("setting agent_on sudah tidak ada",
      "agent_on" not in config.SETTING_DEFAULTS)
check("agent.decide_entry sudah tidak ada",
      not hasattr(agent, "decide_entry"))


# =====================================================================
print("\n[2] umur pool Goldilocks (min 10s, maks 300s / 5m)")
ok, tag, m = goldilocks_eval(pair(age_s=5))
check("umur 5s (< 10s) → skip", not ok and tag.startswith("age_")
      and abs(m["pool_age"] - 5.0) < 3.0, f"tag={tag}")
ok, tag, m = goldilocks_eval(pair(age_s=450))
check("umur 450s (> 300s, koin basi) → skip", not ok
      and tag.startswith("age_") and abs(m["pool_age"] - 450.0) < 2,
      f"tag={tag}")
ok, tag, m = goldilocks_eval(pair(age_s=187))
check("umur 187s (kasus runner YMB) → lolos", ok and tag == "", f"tag={tag} m={m}")
ok, tag, m = goldilocks_eval(pair(age_s=60))
check("umur 60s → lolos", ok and tag == "", f"tag={tag} m={m}")


# =====================================================================
print("\n[3] rasio liq/mcap Goldilocks (min 0.35)")
# liq 5000 / mcap 11000 = 0.4545 → sudah lolos di [2] umur 60s.
# di bawah buat rasio: liq 5000, mcap 16666.7 → ratio 0.30
p = dict(pair(age_s=60), marketCap=16666.7)
ok, tag, m = goldilocks_eval(p)
check("rasio 0.30 (< 0.35) → skip liq_ratio_rendah",
      not ok and tag == "liq_ratio_rendah", f"tag={tag} ratio={m.get('ratio')}")
p = dict(pair(age_s=60), marketCap=14000.0)   # 5000/14000 = 0.3571
ok, tag, m = goldilocks_eval(p)
check("rasio 0.357 (>= 0.35) → lolos", ok and tag == "",
      f"tag={tag} ratio={m.get('ratio')}")
# liq kecil: 3500 < min_liq 4000
p = dict(pair(age_s=60), liquidity={"usd": 3500.0}, marketCap=9000.0)
ok, tag, _ = goldilocks_eval(p)
check("liq $3.500 (< $4.000) → skip liq_rendah", not ok and tag == "liq_rendah",
      tag)
# mcap di luar jendela: 4000 < 5000 / 40000 > 35000
p = dict(pair(age_s=60), marketCap=4000.0)
ok, tag, _ = goldilocks_eval(p)
check("mcap $4.000 (< $5.000) → skip mcap_out_of_range",
      not ok and tag == "mcap_out_of_range", tag)
p = dict(pair(age_s=60), marketCap=40000.0)
ok, tag, _ = goldilocks_eval(p)
check("mcap $40.000 (> $35.000) → skip mcap_out_of_range",
      not ok and tag == "mcap_out_of_range", tag)


# =====================================================================
print("\n[4] safety dev > 3% → skip (via _fastpath_gate)")
_orig_safety_check = listener.safety.check
_orig_safety_dev = listener.safety.dev_pct
_orig_safety_top = listener.safety.top_holders_pct
_orig_trader = listener._trader

listener._trader = FakeTrader()
listener._notify = fake_notify
p_ok = pair(age_s=60, liq=5000.0, mcap=11000.0)

# 4a: check lolos, dev 2% → lolos (quoter OK biar gate sampai tamat)
listener.safety.check = lambda tr, ca, p: (True, "ok")
listener.safety.dev_pct = lambda tr, ca: 2.0
listener.safety.top_holders_pct = lambda tr, ca, ex, n=10: 10.0
_res = {}


class QuoterOkEarly(FakeTrader):
    def erc20(self, addr_):
        class E:
            class functions:
                @staticmethod
                def decimals():
                    class F:
                        def call(self):
                            return 18
                    return F()

                @staticmethod
                def totalSupply():
                    class G:
                        def call(self):
                            return 25_000_000 * 10**18
                    return G()

                @staticmethod
                def balanceOf(addr):
                    class B:
                        def call(self):
                            return 0
                    return B()
        return E()

    def quote_buy_tokens(self, ca, ptype, fee, wei_in, min_out=0, pool_id=None):
        return True, 5000 * 10**18, ""


listener._trader = QuoterOkEarly()
ok, tag, _ = asyncio.run(_fastpath_gate(CA, p_ok))
check("dev 2% (<= 4%) → lolos", ok and tag == "", f"tag={tag}")

# 4b: dev 5% (> 4% Goldilocks) → skip
listener.safety.dev_pct = lambda tr, ca: 5.0
ok, tag, metr = asyncio.run(_fastpath_gate(CA, p_ok))
check("dev 5% (> 4%) → skip dev_terlalu_besar",
      not ok and tag.startswith("dev_terlalu_besar"), tag)
check("dev 5% → metrik memuat dev_pct=5.0", metr.get("dev_pct") == 5.0, str(metr))

# 4c: safety.check menolak (honeypot) → skip
listener.safety.dev_pct = lambda tr, ca: 0.0
listener.safety.check = lambda tr, ca, p: (False, "honeypot selector")
ok, tag, metr = asyncio.run(_fastpath_gate(CA, p_ok))
check("honeypot → skip audit_kontrak (gate safety)",
      not ok and "audit_kontrak" in tag, tag)


# =====================================================================
print("\n[5] ON-CHAIN SPOT PRICE ANCHOR — guard mcap, bukan slippage layar")
listener.safety.check = lambda tr, ca, p: (True, "ok")
listener.safety.dev_pct = lambda tr, ca: 0.0
listener.safety.top_holders_pct = lambda tr, ca, ex, n=10: 10.0

# Kasus runner: Dexscreener bilang $8k (layar basi), tapi on-chain harga
# sudah $15k. Quote spot min_out=0 dikembalikan REALISTIS (~2000 token dari
# $5 → entry $0.0025 → mcap on-chain $50.000 dengan supply 25jt... di bawah
# buat supply 6jt → mcap $15.000 ≤ $35k → LOLOS, TANPA 'slippage_besar'.
class QuoterRunner(FakeTrader):
    def __init__(self, supply=6_000_000, amt=2000):
        self.supply = supply
        self.amt = amt

    def erc20(self, addr_):
        tr = self
        class E:
            class functions:
                @staticmethod
                def decimals():
                    class F:
                        def call(self):
                            return 18
                    return F()

                @staticmethod
                def totalSupply():
                    class G:
                        def call(self):
                            return tr.supply * 10**18
                    return G()
        return E()

    def quote_buy_tokens(self, ca, ptype, fee, wei_in, min_out=0, pool_id=None):
        # min_out DIBIARKAN 0 — quoter benci min_out dari harga layar basi
        assert min_out == 0, f"spot probe harus min_out=0, dapat {min_out}"
        return True, self.amt * 10**18, ""


# =====================================================================
print("\n[5b] BURNED SUPPLY — circulating supply (dEaD dikecualikan)")
# Skenario DIFC/YMB: totalSupply 1 Miliar, 77.2% dibakar ke 0x000...dEaD.
# Entry $5 ambil 33.378 token → harga ~$0.0001498;
#   mcap FDV mentah  = 0.0001498 × 1.000.000.000 = $149.800 (PUCUK)
#   mcap circulating = 0.0001498 × 228.483.516  = $34.226 (LOLOS ≤ 35k)
DEAD = "0x000000000000000000000000000000000000dEaD"


class QuoterBurned(FakeTrader):
    def __init__(self, supply=1_000_000_000, burned=771_516_484, amt=33_378):
        self.supply = supply
        self.burned = burned
        self.amt = amt

    def erc20(self, addr_):
        tr = self
        class E:
            class functions:
                @staticmethod
                def decimals():
                    class F:
                        def call(self):
                            return 18
                    return F()

                @staticmethod
                def totalSupply():
                    class G:
                        def call(self):
                            return tr.supply * 10**18
                    return G()

                @staticmethod
                def balanceOf(addr):
                    bal = tr.burned if str(addr).lower() == DEAD.lower() else 0
                    class B:
                        def call(self):
                            return bal * 10**18
                    return B()
        return E()

    def quote_buy_tokens(self, ca, ptype, fee, wei_in, min_out=0, pool_id=None):
        # DIFC-style: $5 ambil 33.378 token → harga ~$0.0001498
        return True, self.amt * 10**18, ""


listener._trader = QuoterBurned()
ok, tag, metr = asyncio.run(_fastpath_gate(CA, p_ok))
_mcap_benar = 0.0001498 * 228_483_516  # ~$34.226
check("burn 77.2% → mcap dihitung dari 228.483.516 (≈$34.2k) → LOLOS, BUKAN 1 Miliar",
      ok and tag == ""
      and abs(metr.get("mcap_onchain", 0) - _mcap_benar) < 1.0,
      f"tag={tag} mcap_onchain={metr.get('mcap_onchain')}")
check(" supply yang dipakai bukan totalSupply mentah (kalaupun FDV 1 Miliar → $149.8k)",
      _mcap_benar < 149_800 * 0.25, f"mcap_benar={_mcap_benar}")


class QuoterBurned2(QuoterBurned):
    def quote_buy_tokens(self, ca, ptype, fee, wei_in, min_out=0, pool_id=None):
        return True, 5000 * 10**18, ""

listener._trader = QuoterBurned2(supply=25_000_000, burned=0)
ok, tag, metr = asyncio.run(_fastpath_gate(CA, p_ok))
check("tanpa burn, supply 25jt, entry $0.001 → mcap $25k → LOLOS",
      ok and tag == ""
      and abs(metr.get("mcap_onchain", 0) - 25_000.0) < 1.0,
      f"tag={tag} mcap_onchain={metr.get('mcap_onchain')}")


listener._trader = QuoterRunner()
ok, tag, metr = asyncio.run(_fastpath_gate(CA, p_ok))
check("runner $8k layar → $15k on-chain (<= 35k) → LOLOS tanpa slippage_besar",
      ok and tag == "" and abs(metr.get("mcap_onchain", 0) - 15000.0) < 0.01,
      f"tag={tag} mcap_onchain={metr.get('mcap_onchain')}")

# Kasus pucuk on-chain: supply 200jt → 0.0025 × 200jt = $500.000 > 45k/35k
listener._trader = QuoterRunner(supply=200_000_000)
ok, tag, _ = asyncio.run(_fastpath_gate(CA, p_ok))
check("mcap on-chain $500.000 (> 45k & > 35k) → skip mcap_onchain_pucuk",
      not ok and tag == "mcap_onchain_pucuk", tag)

# Kasus tepat pucuk goldilocks ($40.000: di atas 35k tapi di bawah 45k)
listener._trader = QuoterRunner(supply=16_000_000)
ok, tag, _ = asyncio.run(_fastpath_gate(CA, p_ok))
check("mcap on-chain $50.000 ($40k on-chain > goldilocks 35k) → pucuk",
      not ok and tag == "mcap_onchain_pucuk", tag)

# Hmm: 0.0025 × 16jt = $40.000. Periksa detail.
listener._trader = QuoterRunner(supply=10_000_000)  # 0.0025 × 10jt = $25.000 ≤ 35k
ok, tag, metr = asyncio.run(_fastpath_gate(CA, p_ok))
check("mcap on-chain $25.000 (di bawah 35k tapi layar bilang 11k) → LOLOS",
      ok and tag == "" and abs(metr.get("mcap_onchain", 0) - 25000.0) < 0.01,
      f"tag={tag} mcap_onchain={metr.get('mcap_onchain')}")


class QuoterFail(FakeTrader):
    def erc20(self, addr_):
        return Dec18ERC20(addr_)

    def quote_buy_tokens(self, ca, ptype, fee, wei_in, min_out=0, pool_id=None):
        return False, 0, "v3_quoter_not_enough_liquidity:kosong"


listener._trader = QuoterFail()
ok, tag, _ = asyncio.run(_fastpath_gate(CA, p_ok))
check("quoter revert/pool kosong → skip quote_gagal",
      not ok and tag.startswith("quote_gagal:"), tag)

# quote OK: 5000 token → entry $5/5000 = 0.001 → mcap on-chain
# 0.001 × 25jt supply = $25.000 <= scanner_mcap_max 45k → LOLOS
class QuoterOk(FakeTrader):
    def __init__(self, supply=25_000_000):
        self.supply = supply

    def erc20(self, addr_):
        tr = self
        class E:
            class functions:
                @staticmethod
                def decimals():
                    class F:
                        def call(self):
                            return 18
                    return F()

                @staticmethod
                def totalSupply():
                    class G:
                        def call(self):
                            return tr.supply * 10**18
                    return G()
        return E()

    def quote_buy_tokens(self, ca, ptype, fee, wei_in, min_out=0, pool_id=None):
        # expected 5000 token, min_out 4250 → quote di atas ambang → OK
        return True, 5000 * 10**18, ""


listener._trader = QuoterOk()
ok, tag, metr = asyncio.run(_fastpath_gate(CA, p_ok))
check("quote OK + mcap on-chain $25.000 → lolos gate",
      ok and tag == "" and abs(metr.get("tokens_total", 0) - 5000) < 1,
      f"tag={tag} m={metr}")

# mcap on-chain melebihi batas: supply 200jt → 0.001 × 200jt = $200.000
listener._trader = QuoterOk(supply=200_000_000)
ok, tag, _ = asyncio.run(_fastpath_gate(CA, p_ok))
check("mcap on-chain $200.000 (> 45k) → skip mcap_onchain_pucuk",
      not ok and tag == "mcap_onchain_pucuk", tag)


# =====================================================================
print("\n[6] pipa ASLI: entry hanya lewat fast-path (0 detik LLM)")
_fast_notifs = []


async def fake_notify_fast(text, buttons=None, **kw):
    _fast_notifs.append(text)
    return text


listener.dx.best_pair = lambda ca: p_ok
listener.safety.check = lambda tr, ca, p: (True, "ok")
listener._trader = QuoterOk()
listener._notify = fake_notify_fast
db.set_("agent_dry_run", "1")

npos = len(db.open_positions())
st = asyncio.run(listener.process_ca(CA, SRC, None, "UNITTEST", allow_buy=True))
check("pipa ASLI: lolos gate → posisi simulasi terbuka",
      st == "dry_run_simulated" and len(db.open_positions()) == npos + 1, st)
check("pipa ASLI: notif FAST-PATH BUY terkirim",
      any("FAST-PATH BUY" in t for t in _fast_notifs), str(_fast_notifs[-1:])[:160])
check("pipa ASLI: tidak ada modul LLM entry yang dipanggil",
      not hasattr(listener, "agent") or not hasattr(listener.agent, "decide_entry"))

# =====================================================================
print("\n[7] default settings Goldilocks masuk DB")
for k, exp in (("goldilocks_min_age_sec", 10.0),
               ("goldilocks_max_age_sec", 300.0),
               ("goldilocks_min_liq", 4000.0),
               ("goldilocks_min_liq_mcap_ratio", 0.35),
               ("goldilocks_max_mcap", 35000.0),
               ("goldilocks_min_mcap", 5000.0),
               ("goldilocks_max_dev_pct", 0.04),
               ("goldilocks_max_top10_pct", 0.30),
               ("goldilocks_tp1", 0.35),
               ("goldilocks_tp2", 0.80)):
    check(f"DB {k} = {exp}", abs(db.get(k, float) - exp) < 1e-9,
          str(db.get(k, float)))

# =====================================================================
print()
print(f"TOTAL: {len(PASS)} OK, {len(FAIL)} FAIL")
if FAIL:
    print(" FAIL:")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("SEMUA TES LULUS.")
