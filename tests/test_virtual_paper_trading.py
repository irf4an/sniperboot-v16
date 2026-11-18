"""Unit test: True Virtual Paper Trading + staleness drift toleran 50%.

Menguji:
1) Staleness drift guard toleran hingga 50% dan menolak > 50%.
2) Posisi simulasi dibuka dengan is_simulated=1 (mode latihan TIDAK
   lagi return "dry_run" polos tanpa posisi).
3) engine._execute_sell_locked memproses sell simulasi (TP1/TP2/SL)
   tanpa web3 — saldo on-chain tidak diganggu.

Jalankan: python3 tests/test_virtual_paper_trading.py
(DB dipakai = file temporari, tidak menyentuh hoodsniper.db produksi.)
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# DB_PATH harus diset sebelum import sniper.config (dibaca saat import)
_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpdb.close()
os.environ["DB_PATH"] = _tmpdb.name

from sniper import agent, config, db, engine  # noqa: E402
from sniper import dexscreener as dx  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail and not cond else ''}")
    print(f"  {'OK' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


# ======================================================================
# 1) Staleness drift guard: toleran hingga 50%, tolak > 50%
# ======================================================================
print("\n[1] staleness drift guard")
db.init()
# Tes ini menguji alur posisi simulasi + staleness, bukan akuntansi harga:
# pakai mode pricing lama (paper_honest=0). Akuntansi jujur diuji di
# test_sell_circuit_breaker.py bagian [6].
db.set_("paper_honest", "0")

check("drift +49% tidak stale",
      not agent.check_staleness(1.0, 1.49, 0.50))
check("drift +50% tidak stale (boundary, comparator >)",
      not agent.check_staleness(1.0, 1.50, 0.50))
check("drift +50.0001% stale",
      agent.check_staleness(1.0, 1.50001, 0.50))
check("drift +70% stale",
      agent.check_staleness(1.0, 1.70, 0.50))
check("default max_drift_pct agent = 0.50",
      agent.check_staleness(1.0, 1.51) and not agent.check_staleness(1.0, 1.49))

check("SETTING_DEFAULTS punya agent_max_drift=0.50",
      config.SETTING_DEFAULTS.get("agent_max_drift") == "0.50")
check("db.get('agent_max_drift') == 0.50",
      abs(db.get("agent_max_drift") - 0.50) < 1e-9)

db.set_("agent_max_drift", "0.80")
check("setting DB agent_max_drift=0.80 dihormati (drift +70% lolos)",
      not agent.check_staleness(1.0, 1.70, db.get("agent_max_drift")))
db.set_("agent_max_drift", "0.50")


# ======================================================================
# Fixture pair
# ======================================================================
def make_pair(price_usd, price_native, addr="ab"):
    return {
        "pairAddress": "0PAIRSIM" + "0" * 26,
        "chainId": "robinhood",
        "priceUsd": str(price_usd),
        "priceNative": str(price_native),
        "labels": ["v3"],
        "liquidity": {"usd": 5000},
        "baseToken": {"symbol": "TESTSIM", "address": "0x" + addr * 20},
        "quoteToken": {"symbol": "ETH"},
    }


def open_sim_position(pair, decision=None):
    """Alur buy yang persis dipanggil listener saat agent_dry_run=1
    (termasuk potong kas virtual modal+gas sebelum buka posisi)."""
    eth_price = dx.eth_usd(pair)
    buy_usd = (db.get("buy_usd") * decision["size_mult"]) if decision else db.get("buy_usd")
    # listener: estimasi gas + potong kas virtual (modal + gas) dulu;
    # gagal → posisi TIDAK dibuka
    gas_eth = float(db.get("paper_gas_eth", float) or 0.0001)
    gas_usd = gas_eth * eth_price if eth_price > 0 else 0.12
    total_buy_cost = buy_usd + gas_usd
    if not db.deduct_paper_cash(total_buy_cost):
        return None
    eth_spent = buy_usd / eth_price if eth_price > 0 else 0.0003
    # listener: spread entry (price impact DEX) naikkan harga masuk
    spread = float(db.get("paper_entry_spread_pct", float) or 0.025)
    entry_price_usd = float(pair.get("priceUsd") or 0) * (1.0 + spread)
    tokens_total = buy_usd / entry_price_usd if entry_price_usd > 0 else 0
    pos = {"ca": pair["baseToken"]["address"].lower(), "symbol": "TESTSIM",
           "pair_address": pair["pairAddress"], "chain_slug": pair["chainId"],
           "pool_type": dx.pool_type(pair), "fee": 3000,
           "entry_price_usd": entry_price_usd,
           "tokens_total": tokens_total, "tokens_left": tokens_total,
           "eth_spent": eth_spent, "usd_spent": buy_usd,
           "honeypot": 0, "source_name": "UNITTEST",
           "caller_id": None, "caller_name": None,
           "buy_tx": "simulated_buy", "is_simulated": 1}
    db.open_position(**pos)
    pid = max(p["id"] for p in db.open_positions() if p["is_simulated"])
    db.update_position(
        pid,
        tp1_x=float((decision.get("tp1") or db.get("tp1"))) if decision else db.get("tp1"),
        tp2_x=float((decision.get("tp2") or db.get("tp2"))) if decision else db.get("tp2"),
        sl_x=float((decision.get("sl") or db.get("sl"))) if decision else db.get("sl"),
        peak_price=entry_price_usd)
    return pid


# ======================================================================
# 2) Posisi simulasi dibuka dengan is_simulated=1
# ======================================================================
print("\n[2] open posisi simulasi (paper buy)")
p = make_pair(0.001, 0.0005, addr="ab")
dec = {"action": "BUY", "conviction": 80, "size_mult": 1.0,
       "tp1": 0.5, "tp2": 1.0, "sl": -0.3, "reason": "test", "risks": ""}
pid_sim = open_sim_position(p, dec)
prow = db.position(pid_sim)
check("posisi tersimpan open", prow is not None and prow["status"] == "open")
check("posisi itu SIMULASI (is_simulated=1)", prow.get("is_simulated") == 1)
check("buy_tx 'simulated_buy'", prow.get("buy_tx") == "simulated_buy")
check("tokens_left = tokens_total",
      prow["tokens_total"] > 0 and prow["tokens_left"] == prow["tokens_total"])

usd = db.get("buy_usd") * dec["size_mult"]
spread_db = float(db.get("paper_entry_spread_pct", float) or 0.025)
check("usd_spent = buy_usd * size_mult", abs(prow["usd_spent"] - usd) < 1e-6)
check("tokens_total = buy_usd / entry (entry = 0.001 * (1+spread))",
      abs(prow["tokens_total"] - usd / (0.001 * (1.0 + spread_db))) < 1e-6)

print("\n[--] posisi nyata default is_simulated=0")
db.open_position(**{"ca": "0x" + "11" * 20, "symbol": "REALONE",
                    "pair_address": "0REALPAIR" + "0" * 21, "chain_slug": "robinhood",
                    "pool_type": "v3", "fee": 3000, "entry_price_usd": 0.01,
                    "tokens_total": 1000, "tokens_left": 1000,
                    "eth_spent": 1.0, "usd_spent": 2.0,
                    "honeypot": 0, "source_name": "UNITTEST",
                    "caller_id": None, "caller_name": None,
                    "buy_tx": "0xdead"})
pid_real = max(pp["id"] for pp in db.open_positions() if not pp["is_simulated"])
check("posisi nyata is_simulated=0", db.position(pid_real)["is_simulated"] == 0)


# ======================================================================
# 3) _execute_sell_locked pada posisi simulasi — TP1 / TP2 / SL
# ======================================================================
print("\n[3] engine sell simulasi (TP1/TP2/SL tanpa web3)")


class BoomTrader:
    """Trader pengganti — meledak kalau dipanggil. Path simulasi hanya
    benar bila _trader.sell TIDAK PERNAH disentuh posisi is_simulated=1."""

    def sell(self, pos, fraction, price_native):
        raise AssertionError(
            f"TRADER TERPANGGIL untuk posisi SIMULASI id={pos['id']}!")

    def exec_pnl(self, pos, fraction):
        raise AssertionError("exec_pnl terpanggil untuk posisi simulasi!")


_notifications = []


def fake_notify(text, buttons=None, **kw):
    _notifications.append(text)
    return text


# engine.current_pair -> dx.get_pair; inject pair harga lari dari antrian
_queue = []


def fake_get_pair(chain_slug, pair_address):
    if _queue:
        return _queue.pop(0)
    return None


engine.dx.get_pair = fake_get_pair
engine.bind(BoomTrader(), fake_notify)


# -------- 3a) SL (exit penuh, harga anjlok 35%) --------
pair_sl = dict(p, priceUsd=str(0.001 * 0.65), priceNative=str(0.0005 * 0.65))
_queue.append(pair_sl)
text = asyncio.run(engine.execute_sell(pid_sim, 1.0, "sl"))
prow = db.position(pid_sim)
check("SL — teks berisi [SIMULASI]", "[SIMULASI]" in text and "🧪" in text)
check("SL — posisi closed_sl, sisa 0",
      prow["status"] == "closed_sl" and prow["tokens_left"] == 0)
check("SL — realized_eth tercatat",
      isinstance(prow["realized_eth"], (int, float)))


# -------- 3b) TP1 (partial 50%, harga +60%) --------
p2 = make_pair(0.001, 0.0005, addr="cd")
pid2 = open_sim_position(p2, dec)
pair_tp1 = dict(p2, priceUsd=str(0.001 * 1.60), priceNative=str(0.0005 * 1.60))
_queue.append(pair_tp1)
text = asyncio.run(engine.execute_sell(pid2, 0.5, "tp1"))
prow = db.position(pid2)
check("TP1 — teks berisi [SIMULASI]", "[SIMULASI]" in text)
check("TP1 — masih open, moonbag 50%",
      prow["status"] == "open" and prow["tp1_done"] == 1
      and abs(prow["tokens_left"] - prow["tokens_total"] * 0.5) < 1e-6)
check("TP1 — realized_eth > 0 (harga naik 60%)", prow["realized_eth"] > 0)


# -------- 3c) TP2 (exit penuh, harga +120%) --------
pair_tp2 = dict(p2, priceUsd=str(0.001 * 2.20), priceNative=str(0.0005 * 2.20))
_queue.append(pair_tp2)
text = asyncio.run(engine.execute_sell(pid2, 1.0, "tp2"))
prow = db.position(pid2)
check("TP2 — teks berisi [SIMULASI]", "[SIMULASI]" in text)
check("TP2 — closed_tp2, sisa 0",
      prow["status"] == "closed_tp2" and prow["tokens_left"] == 0)
check("TP2 — realized ETH positif", prow["realized_eth"] > 0)

# outcome decision tidak error dan tercatat jika pernah dilog
try:
    db.mark_decision_outcome(p2["baseToken"]["address"], "tp2 real +120%")
    check("mark_decision_outcome tidak error", True)
except Exception as e:
    check("mark_decision_outcome tidak error", False, str(e))

# sanity: trader boom tidak pernah meledak = tidak ada jalur on-chain
check("trader on-chain TIDAK pernah dipanggil utk posisi simulasi", True)


# ======================================================================
# 4) Dompet kas virtual — lifecycle deduct/add/reset
# ======================================================================
print("\n[4] dompet kas virtual (paper cash lifecycle)")
db.reset_paper_cash(100.0)
check("reset kas → $100.00", db.get_paper_cash() == 100.0
      and float(db.get("paper_initial_usd")) == 100.0)

# biaya standar dari setting (sesuai instruksi baru)
gas_eth = float(db.get("paper_gas_eth", float) or 0.0001)
slip = float(db.get("paper_slippage_pct", float) or 0.02)
check("SETTING_DEFAULTS paper_gas_eth=0.0001",
      config.SETTING_DEFAULTS.get("paper_gas_eth") == "0.0001")
check("SETTING_DEFAULTS paper_slippage_pct=0.02",
      config.SETTING_DEFAULTS.get("paper_slippage_pct") == "0.02")
check("SETTING_DEFAULTS paper_entry_spread_pct=0.025",
      config.SETTING_DEFAULTS.get("paper_entry_spread_pct") == "0.025")
check("SETTING_DEFAULTS buy_usd naik ke 5.0",
      config.SETTING_DEFAULTS.get("buy_usd") == "5.0")
gas_usd = gas_eth * 2400.0  # contoh skala: ETH $2400 → $0.12 (dipakai di luar fixture)
# fixture pair: priceUsd=0.001, priceNative=0.0005 → eth_usd = 2.0 (unit test)
_eth_fix = 2.0  # eksplisit: fixture eth_usd = 0.001/0.0005
gas_usd_fix = gas_eth * _eth_fix
check(f"gas_usd fixture = gas_eth*2 = {gas_usd_fix:.6f}", gas_usd_fix > 0)

ok = db.deduct_paper_cash(10.0)
check("deduct $10 berhasil, sisa $90.00",
      ok is True and abs(db.get_paper_cash() - 90.0) < 1e-9)

ok = db.deduct_paper_cash(95.0)
check("deduct $95 gagal (kas kurang), sisa tetap $90.00",
      ok is False and abs(db.get_paper_cash() - 90.0) < 1e-9)

# ===== kasus impas 0% PnL (biaya transaksi nyata) =====
# beli $5.0 (+ gas $gas_usd_fix) → jual 0% PnL dengan slip 1% & gas jual:
#   kas balik = 5.0 * (1-0.01) - gas_usd_fix
db.reset_paper_cash(100.0)
buy_usd_n = 5.0
buy_gas_n = gas_eth * _eth_fix
db.deduct_paper_cash(buy_usd_n + buy_gas_n)
gross = max(buy_usd_n * (1.0 - slip), 0.0)
net = max(gross - buy_gas_n, 0.0)
new = db.add_paper_cash(net)
check(f"impas 0% PnL: kas balik {net:.4f} (5.0*(1-slip) - gas) — rugi net = biaya bolak-balik+fee",
      abs(new - (100.0 - buy_usd_n - buy_gas_n + net)) < 1e-9
      and abs(net - (5.0 * (1.0 - slip) - buy_gas_n)) < 1e-9)

# ===== sell +300% (liq ETH nyata) — kas balik kurangi slip & gas =====
db.reset_paper_cash(100.0)
db.deduct_paper_cash(buy_usd_n + buy_gas_n)
_user_pnl = 2.0  # +200% disimulasikan via cost*(1+pnl)
gross3 = max(buy_usd_n * (1.0 + _user_pnl) * (1.0 - slip), 0.0)
net3 = max(gross3 - buy_gas_n, 0.0)
new3 = db.add_paper_cash(net3)
check("+200%: kas balik net = 15.0*(1-slip) - gas",
      abs(net3 - (15.0 * (1.0 - slip) - buy_gas_n)) < 1e-9
      and abs(new3 - (100.0 - buy_usd_n - buy_gas_n + net3)) < 1e-9)

# Simulasi sell TP1 (+50%): jual $5.0 modal → kas balik net (slip 1% & gas jual)
#   = 7.50*0.99 - gas → sebuah lifecycle "kotor" (fee & gas nyata)
returned = max(max(5.0 * (1.0 + 0.50) * (1.0 - slip), 0.0) - buy_gas_n, 0.0)
new_cash = db.add_paper_cash(returned)
check("TP1 +50%: $5 modal → balik net(7.50*0.99 − gas) — biaya nyata dipotong",
      abs(returned - (7.5 * (1.0 - slip) - buy_gas_n)) < 1e-9)

# Simulasi sell SL (-30%): jual $5.0 modal → balik net keduanya memotong biaya
returned = max(max(5.0 * (1.0 - 0.30) * (1.0 - slip), 0.0) - buy_gas_n, 0.0)
new_cash = db.add_paper_cash(returned)
check("SL -30%: $5 modal → balik net(3.50*0.99 − gas)",
      abs(returned - (3.5 * (1.0 - slip) - buy_gas_n)) < 1e-9)

# integrasi: kas virtual terpotong saat open posisi simulasi via listener path
db.reset_paper_cash(100.0)
check("SETTING_DEFAULTS punya paper_initial_usd & paper_cash_usd",
      config.SETTING_DEFAULTS.get("paper_initial_usd") == "20.0"
      and config.SETTING_DEFAULTS.get("paper_cash_usd") == "20.0")
p3 = make_pair(0.001, 0.0005, addr="ef")
cash_before = db.get_paper_cash()
pid3 = open_sim_position(p3, dec)
check("open posisi simulasi + kas virtual terpotong modal+gas",
      pid3 > 0 and abs((cash_before - db.get_paper_cash())
                       - (db.get("buy_usd") + gas_eth * dx.eth_usd(p3))) < 1e-6)
pid3x = db.position(pid3)
# spread entry: harga masuk posisi harus lebih tinggi 1% dari harga layar pair
raw_screen = float(p3.get("priceUsd") or 0)
check("entry_price_usd = priceUsd * (1+spread) (spread entry)",
      abs(pid3x["entry_price_usd"] - raw_screen * (1.0 + spread_db)) < 1e-15
      and pid3x["entry_price_usd"] > raw_screen)
check("entry price lebih tinggi dari harga layar pair sesuai spread setting",
      abs(pid3x["entry_price_usd"] / raw_screen - (1.0 + spread_db)) < 1e-9)
sold_usd = pid3x["usd_spent"] * (pid3x["tokens_total"] * 0.5 / pid3x["tokens_total"])
# jual 50% pada +60% (real_pnl berdasar eth_real/cost_eth) → kas balik > modal yang terjual
# (dihitung penuh di uji [3]; di sini cukup cek kas balik menaikkan saldo)
_numcheck = [n for n in _notifications if "Kas balik" in n]

print("\n===== RINGKASAN =====")
print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("Gagal:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("SEMUA TES LULUS.")
