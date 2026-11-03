"""Engine jual (dipakai monitor TP/SL & tombol manual di control bot) + loop monitor."""
import asyncio
import traceback
from . import config, db, dexscreener as dx

# ---------- 🔒 LOCK EKSKLUSIF SELL (anti double-sell) ----------
# Engine Penjaga (monitor TP/SL/rug), TANGAN-AI (agent_review_loop), dan
# tombol manual bisa memanggil execute_sell untuk posisi yang sama hampir
# bersamaan → double-sell/oversell. Lock per-posisi menjamin hanya SATU
# alur yang mengeksekusi swap jual dalam satu waktu; yang lain menunggu,
# lalu (bila posisi sudah closed) berhenti tanpa swap kedua.
_sell_locks = {}
_sell_locks_guard = asyncio.Lock()

async def _sell_lock(pos_id: int) -> asyncio.Lock:
    async with _sell_locks_guard:
        lock = _sell_locks.get(pos_id)
        if lock is None:
            lock = asyncio.Lock()
            _sell_locks[pos_id] = lock
        return lock


def _txlink(h):
    return f"[🔍 tx]({config.EXPLORER_URL}/tx/{h})" if h else ""


# ---------- 🎭 Kebijakan CA vanity ...7777 (v16.9, Opsi B) ----------
def is_vanity_7777(ca) -> bool:
    """True bila alamat kontrak berakhiran 7777 (abaikan prefix 0x).
    Hanya dipakai untuk kebijakan opsional — data 107 posisi menunjukkan
    sufiks ini BUKAN prediktor rug (19% vs 18%)."""
    h = str(ca or "").strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    return h.endswith("7777")


def effective_max_hold(pos, base_sec: int) -> int:
    """Time-stop efektif untuk satu posisi.

    Default: nilai global `max_hold_sec` apa adanya (vanity7777_enabled=0).
    Bila operator menyalakan `vanity7777_enabled=1`, posisi dengan CA
    berakhiran 7777 memakai batas yang lebih pendek (min dari keduanya).
    """
    try:
        base = int(base_sec or 0)
    except (TypeError, ValueError):
        base = 0
    if base <= 0 or not is_vanity_7777(pos.get("ca")):
        return base
    try:
        if int(float(db.get("vanity7777_enabled", str) or 0)) != 1:
            return base
    except (TypeError, ValueError):
        return base
    try:
        cap = int(float(db.get("vanity7777_max_hold_sec", str) or 0))
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return base
    return min(base, cap)

_trader = None
_notify = None  # async fn(text, buttons=None, **kw)
_public_notify = None  # async fn(text, buttons=None) → channel log PUBLIK (opsional)
_thin_warned = set()  # pos_id yang sudah dikasih warning pool tipis (anti-spam)
_liq_last = {}       # pos_id -> liq USD terakhir (rug monitor)


def bind(trader, notify, public_notify=None):
    global _trader, _notify
    _trader, _notify = trader, notify
    if public_notify is not None:
        bind_public_notify(public_notify)


def bind_public_notify(fn):
    """Pasang/lepas callback siaran hasil exit ke channel publik."""
    global _public_notify
    _public_notify = fn


# ---------- SIARAN HASIL EXIT KE PUBLIK ----------
# Atas permintaan operator (2026-10-29): bukan cuma log filter yang
# disiarkan, HASIL EXIT posisi juga (teks execute_sell: TP1/TP2/SL/
# trail/timeout/rug/inactive + pengumuman emergency exit + exit oleh
# TANGAN + kegagalan exit). Yang TIDAK disiarkan dari modul ini:
# rekap harian, insting agent, dan peringatan "TP ditahan (pool tipis)"
# — itu bukan hasil exit.
# Versi publik DISENSOR satu hal: angka "Saldo kas: $X" (saldo kas
# virtual absolut) dibuang; PnL terealisasi & kas balik per posisi
# tetap tampil (itu memang isi hasil exit yang diminta untuk disiarkan).
import re as _re

_SALDO_KAS_RE = _re.compile(r"\s*\|\s*Saldo kas: \*\*\$[\d.,]+\*\*")
_SALDO_KAS_RE2 = _re.compile(r"Saldo kas: \*{0,2}\$[\d.,]+\*{0,2}")


def public_exit_text(text):
    """Bersihkan teks hasil exit untuk publik: buang angka saldo kas."""
    out = _SALDO_KAS_RE.sub("", text or "")
    out = _SALDO_KAS_RE2.sub("", out)
    return out


async def _notify_exit(text, buttons=None):
    """Fan-out SATU hasil exit: admin (lengkap) + publik (disensor).

    Tombol untuk publik dipangkas listener.public_buttons (hanya URL).
    Kegagalan kirim ke publik tidak pernah menggagalkan notif admin
    maupun monitor posisi."""
    if _notify is not None:
        await _notify(text, buttons=buttons)
    if _public_notify is not None:
        try:
            from .listener import public_buttons
            await _public_notify(public_exit_text(text),
                                 buttons=public_buttons(buttons))
        except Exception as e:
            print(f"[public-exit fail] {e}")


async def current_pair(pos):
    return await asyncio.to_thread(dx.get_pair, pos["chain_slug"], pos["pair_address"])


async def execute_sell(pos_id: int, fraction: float, reason: str):
    """Jual fraction dari sisa posisi. reason: tp1|tp2|sl|manual|rug|trail.

    SEMUA alur jual (Engine Penjaga, TANGAN-AI, tombol manual) lewat
    fungsi ini → di sini dipasang lock per-posisi: tidak ada dua swap
    jual untuk posisi yang sama yang jalan bersamaan (anti double-sell).
    Pemanggil menunggu giliran; bila saat giliran tiba posisi sudah
    closed, fungsi berhenti TANPA swap kedua.
    """
    lock = await _sell_lock(pos_id)
    async with lock:
        result = await _execute_sell_locked(pos_id, fraction, reason)
    if pos_id not in _cleanup_marks and _cleanup_done(pos_id):
        _cleanup(pos_id)
        _sell_locks.pop(pos_id, None)
        _cleanup_marks.discard(pos_id)
    return result


async def _execute_sell_locked(pos_id: int, fraction: float, reason: str):
    """Implementasi inti execute_sell — HARUS dipanggil dalam lock per-posisi."""
    pos = db.position(pos_id)
    if not pos or pos["status"] != "open":
        return "posisi sudah closed / tidak ditemukan"
    pair = await current_pair(pos)
    if not pair and reason == "rug":
        # Rug saat Dexscreener down/glitch: jangan gagalkan exit — lanjut
        # tanpa harga layar (realized dihitung dari quote on-chain untuk
        # simulasi, dari wallet asli untuk posisi riil).
        price, price_native = 0.0, 0.0
    elif not pair:
        return "gagal ambil harga pair dari Dexscreener"
    else:
        price = float(pair.get("priceUsd") or 0)
        price_native = float(pair.get("priceNative") or 0)
    spot_pnl = price / pos["entry_price_usd"] - 1 if price > 0 else 0

    # ---------- 🧪 POSISI SIMULASI (virtual paper trading) ----------
    # Tidak ada swap on-chain: saldo wallet = 0 untuk token simulasi, jadi
    # hasil jual dihitung dari sisa token × harga native saat ini. Alur
    # bookkeeping (realized PnL, tokens_left, status, outcome decision)
    # TETAP identik dengan posisi nyata.
    if pos.get("is_simulated"):
        sold = pos["tokens_left"] * fraction
        # 🧪 Hasil jual dihitung dari quote on-chain RIIL (quoter pool asli),
        # bukan harga Dexscreener yang basi 20-40 detik — supaya realized PnL
        # simulasi mencerminkan hasil Nx yang sebenarnya bisa didapat.
        eth_real = None
        pool_drained_onchain = False
        if _trader:
            try:
                amt_raw = int(sold * 10**(
                    await asyncio.to_thread(
                        _trader.erc20(pos["ca"]).functions.decimals().call)))
                if amt_raw > 0:
                    q_eth = await asyncio.to_thread(
                        _trader.quote_sell_eth, pos["ca"], pos["pool_type"],
                        pos["fee"], amt_raw, pool_id=pos.get("pair_address"))
                    if q_eth and q_eth > 0:
                        eth_real = q_eth
                    elif pos.get("pool_type") == "v4" and pos.get("pair_address"):
                        from . import v4 as v4mod
                        onchain_l = await asyncio.to_thread(
                            v4mod.liquidity, pos["pair_address"], _trader.w3)
                        if onchain_l is not None and onchain_l <= 0:
                            pool_drained_onchain = True
            except Exception:
                if reason == "rug":
                    # revert/probing gagal total saat rug → asumsi pool mati
                    eth_real = 0.0
        if eth_real is None:
            if reason == "rug" or pool_drained_onchain:
                # 💀 RUG PULL / LP Kosong On-Chain: quote 0 & getLiquidity=0 →
                # pool on-chain sudah terkuras (net loss -100%), JANGAN kasih
                # angka palsu dari harga beku Dexscreener.
                eth_real = 0.0
            else:
                eth_real = sold * price_native if price_native > 0 else (
                    pos["eth_spent"] * fraction * (1 + spot_pnl))
        gas_eth = 0.00005
        swap_tx = "simulated_sell"
    else:
        sold, eth_real, gas_eth, swap_tx = await asyncio.to_thread(
            _trader.sell, pos, fraction, price_native)
        if sold <= 0:
            import time as _t
            db.update_position(pos_id, tokens_left=0, status="closed_empty",
                               closed_at=int(_t.time()))
            _cleanup(pos_id)
            return (f"{pos['symbol']}: balance 0 — tidak ada yang bisa dijual, "
                    f"posisi DITUTUP (closed_empty)")

    sim_prefix = "🧪 **[SIMULASI]** " if pos.get("is_simulated") else ""
    db.add_realized(pos_id, eth_real)

    # porsi modal ETH untuk token yang barusan dijual
    cost_eth = pos["eth_spent"] * (sold / pos["tokens_total"]) if pos["tokens_total"] else 0
    real_pnl = (eth_real / cost_eth - 1) if cost_eth > 0 else 0
    real_pnl = max(real_pnl, -1.0)

    thin = spot_pnl > 0 and (spot_pnl - real_pnl) > 0.30
    left = max(pos["tokens_left"] - sold, 0)
    tag = f"{sim_prefix}{pos['symbol']} (`{pos['ca'][:10]}…`)"
    nums = (f"layar {spot_pnl*100:+.1f}% → **realized {real_pnl*100:+.1f}%**\n"
            f"{sold:,.0f} token → **{eth_real:.6f} ETH** masuk · gas {gas_eth:.6f} {_txlink(swap_tx)}")

    # ---------- 🧪 kas virtual: kembalikan pokok + profit/rugi ----------
    if pos.get("is_simulated"):
        try:
            cost_usd = pos["usd_spent"] * (sold / pos["tokens_total"]) if pos["tokens_total"] else 0
            # 🧪 biaya nyata: potong slippage/fee pool DEX (paper_slippage_pct)
            slippage = float(db.get("paper_slippage_pct", float) or 0.02)
            gross_usd = max(cost_usd * (1.0 + real_pnl) * (1.0 - slippage), 0.0)
            # 🧪 gas jual (L2): harga ETH dihitung dari pair saat ini
            gas_eth_sell = float(db.get("paper_gas_eth", float) or 0.0001)
            eth_price_now = (price / price_native) if price_native > 0 else 0.0
            gas_sell_usd = gas_eth_sell * eth_price_now if eth_price_now > 0 else 0.12
            net_returned_usd = max(gross_usd - gas_sell_usd, 0.0)
            new_cash = db.add_paper_cash(net_returned_usd)
            nums += (f"\n💵 Kas balik (net): **+${net_returned_usd:.2f}** "
                     f"(bruto ${gross_usd:.2f} - gas ${gas_sell_usd:.2f} - slip {slippage*100:.0f}%) | "
                     f"Saldo kas: **${new_cash:.2f}**")
        except Exception:
            traceback.print_exc()
    warn = "\n⚠️ _pool tipis: harga layar jauh di atas hasil jual asli (price impact)_" if thin else ""

    import time as _t
    now = int(_t.time())
    # is_close HANYA bila seluruh token benar-benar habis (full exit) atau
    # stop-out total. TP1 dan TP2 parsial (skema exit 3 lapis) mengeposkan
    # posisi tetap OPEN — bukan penutupan — jadi jangan anggap is_close.
    is_close = (reason in ("sl", "rug", "trail")) or (fraction >= 1.0 or left <= 0)
    if is_close and db.get("agent_auto_insting", str) == "1" and config.LLM_API_KEY:
        st = db.stats()
        total_closed = st["tp2"] + st["sl"] + st["manual"] + st["rug"] + st["trail"] + 1
        if total_closed >= 5 and total_closed % 5 == 0:
            asyncio.create_task(_auto_insting())
    if reason in ("tp2", "sl", "rug", "trail") or (reason == "manual" and (fraction >= 1 or left <= 0)):
        db.mark_decision_outcome(pos["ca"], f"{reason} real {real_pnl*100:+.0f}% (layar {spot_pnl*100:+.0f}%)")

    # ---------- 🕵️ skor insider wallet (v16.3) ----------
    # Jika posisi copy-trade dari wallet insider (caller_name INSIDER:*),
    # laporkan hasil exit-nya ke tabel insider_wallets: SL = loss_count
    # (2x berturut → wallet auto-disabled), TP = win_count reset loss.
    # ---------- 🛡️ clone-guard: tandai family gagal (v16.6) ----------
    # Posisi ditutup dengan reason 'rug' ATAU ('sl' dengan pnl <= -0.25)
    # → tandai seluruh keluarga klon/deployer (template fingerprint +
    # operator funder graph) sebagai gagal, agar CA saudara tidak
    # dibeli lagi.
    if is_close and (reason == "rug" or (reason == "sl" and real_pnl <= -0.25)):
        try:
            from . import clone_guard as _cg
            reason_txt = f"{reason} pnl {real_pnl*100:+.0f}%"
            print(f"[clone_guard] 🏷️ tandai keluarga gagal: {pos['symbol']} "
                  f"({pos['ca'][:10]}…) — {reason_txt}")
            _cg.mark_family_failed(pos["ca"], reason_txt)
        except Exception:
            pass

    if is_close:
        c_name = str(pos.get("caller_name") or "")
        c_id = str(pos.get("caller_id") or "")
        if c_name.startswith("INSIDER:") and c_id.startswith("0x"):
            insider_addr = c_id.lower()
            try:
                if reason == "sl" or real_pnl <= -0.15:
                    res = db.record_insider_result(insider_addr, won=False)
                    if res.get("disabled"):
                        print(f"[tracker] ⚠️ Insider {insider_addr} "
                              f"dinonaktifkan otomatis (2x SL berturut-turut)")
                elif reason in ("tp1", "tp2") or real_pnl >= 0.20:
                    db.record_insider_result(insider_addr, won=True)
                    print(f"[tracker] 🎯 Insider {insider_addr} "
                          f"win_count bertambah")
            except Exception:
                traceback.print_exc()

    if reason == "tp1":
        if left <= 0:
            db.update_position(pos_id, tokens_left=0, tp1_done=1, status="closed_tp1", closed_at=now)
            text = f"✅ **TP1 — full exit** {tag}\n{nums}\n(tp1_sell_pct menjual semua) posisi CLOSED{warn}"
        else:
            db.update_position(pos_id, tokens_left=left, tp1_done=1)
            text = f"✅ **TP1** {tag}\n{nums}\nhold moonbag {left:,.0f}{warn}"
    elif reason == "tp2":
        if left <= 0 or fraction >= 1.0:
            db.update_position(pos_id, tokens_left=0, tp2_done=1, status="closed_tp2", closed_at=now)
            text = f"🎯 **TP2 — full exit** {tag}\n{nums}\nposisi CLOSED{warn}"
        else:
            # 🚀 Exit 3 lapis: sisa koin jadi moonbag lapis 3 — dijaga
            # trailing stop moonbag_trail_pct dari puncak, bebas time-stop.
            db.update_position(pos_id, tokens_left=left, tp2_done=1)
            trail_p = float(db.get("moonbag_trail_pct", float) or 0.20) * 100
            try:
                hold_m = max(int(float(db.get("max_hold_sec", str) or 420) / 60), 1)
            except (TypeError, ValueError):
                hold_m = 7
            text = (f"🎯 **TP2** {tag}\n{nums}\n"
                    f"🚀 **HOLD MOONBAG LAPIS 3** ({left:,.0f} tokens)\n"
                    f"• Dijaga Trailing Stop {trail_p:.0f}% dari puncak\n"
                    f"• Bebas dari Time-Stop {hold_m}m{warn}")
    elif reason == "sl":
        db.update_position(pos_id, tokens_left=0, status="closed_sl", closed_at=now)
        text = f"🛑 **STOP LOSS** {tag}\n{nums}\nposisi CLOSED{warn}"
    elif reason == "rug":
        db.update_position(pos_id, tokens_left=0, status="closed_rug", closed_at=now)
        text = f"🚨 **RUG ALERT — liquidity ditarik!** {tag}\nemergency exit sebelum pintu ketutup:\n{nums}\nposisi CLOSED{warn}"
    elif reason == "trail":
        db.update_position(pos_id, tokens_left=0, status="closed_trail", closed_at=now)
        text = f"📈🔒 **TRAILING STOP** {tag}\nprofit diamankan dari puncak:\n{nums}\nposisi CLOSED{warn}"
    elif reason == "timeout":
        db.update_position(pos_id, tokens_left=0, status="closed_timeout", closed_at=now)
        text = (f"⏱️ **TIME STOP (TIMEOUT {int((now - pos['ts'])/60)}m)** {tag}\n"
                f"tidak tembus TP dalam batas waktu:\n{nums}\nposisi CLOSED{warn}")
    elif reason == "inactive":
        db.update_position(pos_id, tokens_left=0, status="closed_inactive", closed_at=now)
        text = (f"⏱️ **EXIT INACTIVE (volume mati {int((now - pos['ts'])/60)}m)** {tag}\n"
                f"0 transaksi / pool kering — moonbag di-harvest:\n{nums}\nposisi CLOSED{warn}")
    else:
        if fraction >= 1 or left <= 0:
            db.update_position(pos_id, tokens_left=0, status="closed_manual", closed_at=now)
            text = f"👆 **Manual sell ALL** {tag}\n{nums}\nposisi CLOSED{warn}"
        else:
            db.update_position(pos_id, tokens_left=left)
            text = f"👆 **Manual sell {fraction*100:.0f}%** {tag}\n{nums}\nsisa {left:,.0f}{warn}"
    return text


async def monitor_loop():
    while True:
        try:
            for pos in db.open_positions():
                await _check(pos)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(_anti_rug_poll_seconds())


def _anti_rug_poll_seconds() -> float:
    """Interval polling loop anti-rug. Bila flag `anti_rug_poll_seconds`
    diset di DB (>0), pakai nilai itu — mode pintas untuk L2 yang butuh
    respon rug dalam detik; bila tidak, pakai `poll_seconds` legacy."""
    try:
        fast = float(db.get("anti_rug_poll_seconds") or 0)
    except (TypeError, ValueError):
        fast = 0.0
    if fast > 0:
        return max(fast, 1.0)
    try:
        return max(float(db.get("poll_seconds") or 20), 1.0)
    except (TypeError, ValueError):
        return 20.0


async def _check(pos):
    # pembersih zombie: open tapi sisa 0 (dari versi lama / dust) → tutup
    if (pos["tokens_left"] or 0) <= 0:
        import time as _t
        db.update_position(pos["id"], status="closed_empty", closed_at=int(_t.time()))
        _cleanup(pos["id"])
        await _notify_exit(f"🧹 **{pos['symbol']}** sisa 0 tapi status masih open — ditutup otomatis (pembersihan)")
        return
    pair = await current_pair(pos)
    if not pair:
        # Dexscreener gagal/down (502/timeout/rate-limit) — jangan langsung
        # biarkan: probing on-chain dulu via _trader. Kalau quote jual
        # on-chain = 0 (pool di-drain / revert) padahal API mati → RUG.
        if _trader and (pos.get("tokens_left") or 0) > 0:
            try:
                token = _trader.erc20(pos["ca"])
                dec = await asyncio.to_thread(token.functions.decimals().call)
                amt_raw = int(pos["tokens_left"] * 10**dec)
                if amt_raw > 0:
                    q_eth = await asyncio.to_thread(
                        _trader.quote_sell_eth, pos["ca"], pos["pool_type"],
                        pos["fee"], amt_raw, pool_id=pos.get("pair_address"))
                    if q_eth <= 0:
                        # Pool di-drain / revert on-chain saat Dexscreener
                        # mati → RUG ALERT
                        _cleanup(pos["id"])
                        await _notify_exit(
                            f"🚨 **{pos['symbol']}** Dexscreener down & "
                            f"quote on-chain 0 ETH — RUG PULL terdeteksi "
                            f"on-chain! EMERGENCY EXIT")
                        await _notify_exit(await execute_sell(pos["id"], 1.0, "rug"))
                        return
            except Exception:
                pass
        return
    price = float(pair.get("priceUsd") or 0)
    if price <= 0:
        return
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)

    # ---------- 0) TIME STOP: max hold 20 menit (1200 dtk, 0 = off) ----------
    # Koin meme cepat mati — jika belum tembus TP dalam batas waktu,
    # lobak posisi biar modal bebas untuk entry berikutnya.
    import time as _t
    status_raw = db.get("max_hold_sec", str)
    try:
        max_hold_sec = int(float(status_raw or 1200))
    except (TypeError, ValueError):
        max_hold_sec = 1200
    # v16.9 (Opsi B): kebijakan khusus CA ...7777 — default no-op
    # (vanity7777_enabled=0), hanya mempersingkat kalau operator menyalakan.
    max_hold_sec = effective_max_hold(pos, max_hold_sec)
    now_ts = int(_t.time())
    pos_age = now_ts - (pos.get("ts") or now_ts)
    # 🚀 Time-Stop Exemption: moonbag lapis 3 (tp2_done=1) sudah 100%
    # house money — bebas dari time-stop biasa. Rug monitor, trailing
    # stop, dan Moonbag Dual-Guard di bawah tetap aktif.
    is_moonbag = bool(pos.get("tp2_done"))  # 1 = moonbag lapis 3
    if max_hold_sec > 0 and pos_age >= max_hold_sec and not is_moonbag:
        _cleanup(pos["id"])
        await _notify_exit(await execute_sell(pos["id"], 1.0, "timeout"))
        return

    # ---------- 🌙 0b) MOONBAG DUAL-GUARD (v16.7) ----------
    # Dua saringan khusus posisi moonbag (tp2_done=1) supaya sisa koin
    # house money tidak menguap begitu saja:
    #   a) Inactivity Guard: volume mati (txns m5 = 0 selama
    #      moonbag_inactivity_sec default 420s) ATAU pool on-chain
    #      terkuras (quote jual = 0 ETH) → EXIT INACTIVE.
    #   b) Hard-Cap Runner: umur posisi >= moonbag_max_hold_sec
    #      (default 2700s / 45m) → HARVEST FULL.
    # Trailing stop 20% tetap aktif mendeteksi penurunan dari puncak.
    if is_moonbag:
        inactivity_raw = db.get("moonbag_inactivity_sec", str)
        try:
            inactivity_sec = int(float(inactivity_raw or 420))
        except (TypeError, ValueError):
            inactivity_sec = 420
        # ---- a) Inactivity Guard ----
        if inactivity_sec > 0 and pos_age >= inactivity_sec:
            txns_m5 = ((pair.get("txns") or {}).get("m5") or {})
            vol_dead = ((txns_m5.get("buys") or 0) == 0
                        and (txns_m5.get("sells") or 0) == 0)
            pool_dry = False
            if _trader:
                try:
                    token = _trader.erc20(pos["ca"])
                    dec = await asyncio.to_thread(token.functions.decimals().call)
                    amt_raw = int(pos["tokens_left"] * 10**dec)
                    if amt_raw > 0:
                        q_eth = await asyncio.to_thread(
                            _trader.quote_sell_eth, pos["ca"], pos["pool_type"],
                            pos["fee"], amt_raw, pool_id=pos.get("pair_address"))
                        pool_dry = (q_eth or 0) <= 0
                except Exception:
                    pass
            if vol_dead or pool_dry:
                _cleanup(pos["id"])
                await _notify_exit(
                    f"⏱ **{pos['symbol']}** (Moonbag) volume mati "
                    f"(0 txns / pool kering) → EXIT INACTIVE!")
                await _notify_exit(await execute_sell(pos["id"], 1.0, "inactive"))
                return
        # ---- b) Hard-Cap Runner ----
        hardcap_raw = db.get("moonbag_max_hold_sec", str)
        try:
            hardcap_sec = int(float(hardcap_raw or 2700))
        except (TypeError, ValueError):
            hardcap_sec = 2700
        if hardcap_sec > 0 and pos_age >= hardcap_sec:
            _cleanup(pos["id"])
            await _notify_exit(
                f"🏁 **{pos['symbol']}** (Moonbag) mencapai batas maksimal "
                f"45m runner → HARVEST FULL!")
            await _notify_exit(await execute_sell(pos["id"], 1.0, "timeout"))
            return

    # ---------- 1) RUG MONITOR: liq drop mendadak → emergency exit ----------
    # Abaikan pembacaan liq 0/None (Dexscreener stale) — jangan update baseline
    # dan jangan trigger, biar satu data buruk nggak bikin false rug.
    last_liq = _liq_last.get(pos["id"])
    if liq > 0:
        _liq_last[pos["id"]] = liq
    if last_liq and last_liq > 0 and liq > 0 and liq < last_liq * (1 - db.get("rug_liq_drop")):
        # Verifikasi dulu apakah pool on-chain memang terkuras, atau cuma
        # glitch/delay API Dexscreener (liq usang). Quote jual on-chain
        # dibandingkan dengan nilai spot yang diharapkan.
        is_real_rug = True
        if _trader and (pos.get("tokens_left") or 0) > 0:
            try:
                token = _trader.erc20(pos["ca"])
                dec = await asyncio.to_thread(token.functions.decimals().call)
                amt_raw = int(pos["tokens_left"] * 10**dec)
                if amt_raw > 0:
                    q_eth = await asyncio.to_thread(
                        _trader.quote_sell_eth, pos["ca"], pos["pool_type"],
                        pos["fee"], amt_raw, pool_id=pos.get("pair_address"))
                    # Jika quote on-chain masih > 50% dari expected spot →
                    # false alarm Dexscreener, pool on-chain masih tebal.
                    expected_eth = pos["tokens_left"] * float(pair.get("priceNative") or 0)
                    if expected_eth > 0 and q_eth >= expected_eth * 0.5:
                        is_real_rug = False
                        print(f"[engine] Rug alert diabaikan untuk "
                              f"{pos['symbol']}: quote on-chain masih sehat "
                              f"({q_eth:.6f} ETH vs exp {expected_eth:.6f} ETH)")
            except Exception:
                pass
        if is_real_rug:
            try:
                _liq_last.pop(pos["id"], None)
                _thin_warned.discard(pos["id"])
                await _notify_exit(
                    f"🚨 **{pos['symbol']}** liq anjlok ${last_liq:,.0f} → ${liq:,.0f} "
                    f"({(liq/last_liq-1)*100:.0f}%) — EMERGENCY EXIT!")
                await _notify_exit(await execute_sell(pos["id"], 1.0, "rug"))
            except Exception as e:
                await _notify_exit(f"⚠️ Emergency exit {pos['symbol']} GAGAL (LP mungkin sudah kosong): {e}")
            return

    # ---------- 💹 PnL utama: quote jual on-chain RIIL ----------
    # Harga Dexscreener bisa basi 20-40 detik — koin runner baru dibeli
    # punya harga on-chain lebih tinggi dari layar, sehingga PnL layar bisa
    # boltak -30%+ palsu di poll pertama dan memicu SL palsu. PnL dihitung
    # dari quoter on-chain asli (nembus depth pool); Dexscreener hanya fallback.
    onchain_pnl = None
    if _trader:
        try:
            token = _trader.erc20(pos["ca"])
            dec = await asyncio.to_thread(token.functions.decimals().call)
            amt_raw = int(pos["tokens_left"] * 10**dec)
            if amt_raw > 0:
                q_eth = await asyncio.to_thread(
                    _trader.quote_sell_eth, pos["ca"], pos["pool_type"],
                    pos["fee"], amt_raw, pool_id=pos.get("pair_address"))
                cost_eth = pos["eth_spent"] * (pos["tokens_left"] / pos["tokens_total"]) \
                    if pos["tokens_total"] else 0
                if cost_eth > 0 and q_eth > 0:
                    onchain_pnl = q_eth / cost_eth - 1.0
                elif pos.get("pool_type") == "v4" and pos.get("pair_address"):
                    from . import v4 as v4mod
                    onchain_l = await asyncio.to_thread(
                        v4mod.liquidity, pos["pair_address"], _trader.w3)
                    if onchain_l is not None and onchain_l <= 0:
                        _cleanup(pos["id"])
                        await _notify_exit(
                            f"🚨 **{pos['symbol']}** LP v4 on-chain = 0 "
                            f"(walau layar Dexscreener masih ${liq:,.0f}) — "
                            f"RUG PULL ON-CHAIN! EMERGENCY EXIT")
                        await _notify_exit(await execute_sell(pos["id"], 1.0, "rug"))
                        return
        except Exception:
            pass  # quoter gagal → fallback ke harga layar di bawah
    if onchain_pnl is not None:
        pnl = onchain_pnl
        # Harga efektif untuk peak & trailing: turunan dari kuoter on-chain,
        # sama basis dengan entry_price_usd. Harga layar Dexscreener tertinggal
        # 20-30s → bandingkan layar vs entry on-chain memicu trail palsu.
        price = pos["entry_price_usd"] * (1.0 + onchain_pnl)
    else:
        pnl = price / pos["entry_price_usd"] - 1
    tp1_v = pos.get("tp1_x") or db.get("tp1")
    tp2_v = pos.get("tp2_x") or db.get("tp2")
    sl_v = pos.get("sl_x") or db.get("sl")

    # ---------- 2) peak tracking (buat trailing) ----------
    peak = pos.get("peak_price") or pos["entry_price_usd"]
    if price > peak:
        peak = price
        db.update_position(pos["id"], peak_price=peak)

    try:
        # ---------- 3) sebelum TP1: SL normal dari entry ----------
        if not pos["tp1_done"] and pnl <= sl_v:
            _cleanup(pos["id"])
            await _notify_exit(await execute_sell(pos["id"], 1.0, "sl"))
            return

        # ---------- 4) setelah TP1: breakeven lock + trailing stop ----------
        # Moonbag lapis 3 (tp2_done=1) pakai jarak trailing sendiri
        # (moonbag_trail_pct, default 20%) — bukan trail_pct biasa (25%).
        if pos["tp1_done"] and db.get("trail_on", str) == "1":
            if pos.get("tp2_done"):
                trail_dist = float(db.get("moonbag_trail_pct", float) or 0.20)
            else:
                trail_dist = db.get("trail_pct")
            stop_level = max(pos["entry_price_usd"], peak * (1 - trail_dist))
            if price <= stop_level:
                _cleanup(pos["id"])
                await _notify_exit(await execute_sell(pos["id"], 1.0, "trail"))
                return
        elif pos["tp1_done"] and pnl <= sl_v:
            _cleanup(pos["id"])
            await _notify_exit(await execute_sell(pos["id"], 1.0, "sl"))
            return

        verify = (db.get("tp_verify", str) == "1") and not pos.get("is_simulated")
        if pnl >= tp2_v and db.get("tp2_on", str) == "1" and not pos.get("tp2_done"):
            frac = float(db.get("tp2_sell_pct", float) or 0.50)
            if verify:
                ep = await asyncio.to_thread(_trader.exec_pnl, pos, frac)
                if ep < tp2_v * 0.7:
                    if pos["id"] not in _thin_warned:
                        _thin_warned.add(pos["id"])
                        await _notify(
                            f"🫧 **{pos['symbol']}** layar {pnl*100:+.0f}% tapi hasil jual asli "
                            f"cuma {ep*100:+.0f}% (pool tipis) — TP2 ditahan, tunggu depth")
                    return
            if frac >= 1.0:
                _cleanup(pos["id"])
            await _notify_exit(await execute_sell(pos["id"], frac, "tp2"))
        elif pnl >= tp1_v and not pos["tp1_done"]:
            frac = db.get("tp1_sell_pct")
            if verify:
                ep = await asyncio.to_thread(_trader.exec_pnl, pos, frac)
                if ep < tp1_v * 0.7:
                    if pos["id"] not in _thin_warned:
                        _thin_warned.add(pos["id"])
                        await _notify(
                            f"🫧 **{pos['symbol']}** layar {pnl*100:+.0f}% tapi hasil jual asli "
                            f"cuma {ep*100:+.0f}% (pool tipis) — TP1 ditahan, tunggu depth")
                    return
            _thin_warned.discard(pos["id"])
            await _notify_exit(await execute_sell(pos["id"], frac, "tp1"))
    except Exception as e:
        await _notify_exit(f"⚠️ Gagal eksekusi sell {pos['symbol']}: {e}")


def _cleanup(pid):
    _thin_warned.discard(pid)
    _liq_last.pop(pid, None)
    # hindari memory leak: buang lock posisi yang sudah tidak dipakai
    # (dilakukan di execute_sell setelah lock dilepas, via _cleanup_marks)
    _cleanup_marks.add(pid)


# set pelacak posisi yang barusan di-cleanup (pembersihan lock, anti-leak)
_cleanup_marks = set()


def _cleanup_done(pid) -> bool:
    pos = db.position(pid)
    return (not pos) or (pos.get("status") != "open")


async def agent_review_loop():
    """Review posisi terbuka oleh agent (role manager). TP/SL deterministik tetap backstop."""
    from . import agent
    while True:
        await asyncio.sleep(max(int(db.get("agent_review_min")), 2) * 60)
        if db.get("agent_manage_on", str) != "1" or not config.LLM_API_KEY:
            continue
        for pos in db.open_positions():
            try:
                pair = await current_pair(pos)
                if not pair:
                    continue
                action, reason = await asyncio.to_thread(agent.review_position, pos, pair)
                if action == "SELL_HALF":
                    result = await execute_sell(pos["id"], 0.5, "manual")
                    await _notify_exit(f"✋ **TANGAN exit 50%** {pos['symbol']}\n_{reason}_\n{result}")
                elif action == "SELL_ALL":
                    result = await execute_sell(pos["id"], 1.0, "manual")
                    await _notify_exit(f"✋ **TANGAN exit ALL** {pos['symbol']}\n_{reason}_\n{result}")
            except Exception as e:
                print(f"[agent-review fail] {pos['symbol']}: {e}")


async def daily_recap_loop():
    """Rekap harian jam recap_hour WIB (UTC+7)."""
    import time as _t
    while True:
        now = _t.time()
        wib = now + 7 * 3600
        target_hour = int(db.get("recap_hour"))
        secs_today = wib % 86400
        target_secs = target_hour * 3600
        wait = (target_secs - secs_today) % 86400
        if wait < 60:
            wait += 86400
        await asyncio.sleep(wait)
        if db.get("recap_on", str) != "1":
            continue
        try:
            await _notify(build_recap(int(_t.time()) - 86400))
        except Exception:
            traceback.print_exc()


def build_recap(since_ts: int) -> str:
    d = db.recap_data(since_ts)
    closed = {c["status"]: c for c in d["closed"]}
    n = lambda k: closed.get(k, {}).get("n", 0)
    eth_in = sum(c["eth_in"] for c in d["closed"])
    eth_out = sum(c["eth_out"] for c in d["closed"])
    net = eth_in - eth_out
    emo = "🟢" if net >= 0 else "🔴"
    skipped = max(d["calls"] - d["buys"], 0)
    lines = [
        "🌅 **REKAP 24 JAM — hoodsniper**\n",
        f"📨 call masuk: **{d['calls']}** · di-skip filter: {skipped}",
        f"🛒 entry baru: **{d['buys']}** (${d['usd_spent']:.2f})",
        f"🎯 TP2: {n('closed_tp2')} · ✅→📈 trail: {n('closed_trail')} · "
        f"🛑 SL: {n('closed_sl')} · 🚨 rug: {n('closed_rug')} · 👆 manual: {n('closed_manual')}",
        f"{emo} PnL realized (posisi closed): **{net:+.6f} ETH**",
        f"📊 masih open: {d['open']} posisi",
    ]
    tmo = n("closed_timeout")
    if tmo:
        lines.insert(4, f"⏱️ timeout (tidak tembus TP): **{tmo}**")
    if d["top_caller"]:
        t = d["top_caller"]
        lines.append(f"🏆 caller terbaik: **{t['caller_name']}** ({t['tp1'] or 0} TP1 dari {t['n']} call)")
    lines.append("\n_semua angka = realized asli dari wallet, bukan estimasi layar_")
    return "\n".join(lines)


async def _auto_insting():
    """Latih insting otomatis tiap 5 posisi closed + usulan tuning parameter (Opsi A)."""
    from . import agent
    from telethon import Button
    try:
        lessons, proposals = await asyncio.to_thread(agent.derive_lessons)
        if not lessons and not proposals:
            return

        lines = []
        if lessons:
            lines.extend(f"• {t}" for t in lessons)

        buttons = []
        if proposals:
            lines.append("\n⚙️ **Usulan Auto-Tuner (Evaluasi Parameter):**")
            for prop in proposals:
                lines.append(f"• **{prop['label']}**: `{prop['cur']}` → `{prop['val']}`\n  _{prop['reason']}_")
                buttons.append([Button.inline(f"✅ Terapkan: {prop['label']} = {prop['val']}", f"applyset:{prop['param']}:{prop['val']}")])
            buttons.append([Button.inline("❌ Abaikan Usulan", b"tune:dismiss")])

        tail = (" (otomatis, dari 5 close terakhir):**\n" + "\n".join(lines)) if lines else " (otomatis):**\n_tidak ada data_"
        if _notify:
            await _notify("💡 **Insting baru" + tail, buttons=buttons if buttons else None)
    except Exception as e:
        print(f"[auto-insting fail] {e}")
