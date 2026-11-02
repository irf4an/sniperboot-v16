"""Listener userbot: pantau SEMUA source di DB (multi-group, dinamis tanpa restart)."""
import asyncio
import re
import time
import traceback
from telethon import events, Button
from . import config, db, safety, agent, fmt, dexscreener as dx, pons_radar


_trader = None
_notify = None
_public_notify = None  # async fn(text, buttons=None) → channel log PUBLIK (opsional)


def pons_veto(ca) -> bool:
    """True bila CA diveto Radar Pons saat masih di bonding curve.

    Dipisah jadi fungsi kecil supaya bisa diuji langsung: satu-satunya
    ketergantungannya adalah set di memori `pons_radar.is_blacklisted`
    (O(1), tanpa query DB di jalur beli).
    """
    try:
        return pons_radar.is_blacklisted(ca)
    except Exception as e:
        # Fail-open: radar rusak TIDAK boleh menghentikan bot membeli.
        print(f"[pons-veto] cek veto error (dianggap lolos): {e}")
        return False


# ---------- LOG PUBLIK (mode publik) ----------
# SEMUA notif yang lahir di process_ca adalah "log filter koin"
# (SKIP mcap/liq/sosmed/safety/fast-path, kartu Mata, SIMULASI BUY, BUY,
# gagal buy). Notif itu difan-out oleh _notify_log:
#   • admin  → teks + tombol LENGKAP (termasuk Sell 50% / Sell ALL)
#   • publik → teks yang sama, tombol DIPANGKAS: hanya tombol URL
#     (Chart / Tx) yang dipertahankan; semua tombol inline (callback
#     aksi) DIBUANG — publik tidak pernah menerima tombol yang bisa
#     memicu aksi apa pun.
# Notif NON-filter (engine TP/SL/recap, tracker insider, panel, wallet)
# TIDAK lewat sini — mereka memanggil notify admin langsung dan tidak
# pernah disiarkan ke publik.
def _is_url_button(btn):
    """True bila tombol ini tombol URL (aman utk publik), BUKAN callback.

    Menoleransi dua bentuk Telethon:
      • lama (≤1.4x awal): KeyboardButtonUrl dengan atribut `.url`
      • baru (1.45): KeyboardInlineButton dengan `.type` =
        InlineButtonTypeUrl (punya `.url`); callback `.type`-nya
        InlineButtonTypeCallback (punya `.data`, tanpa `.url`).
    Duck-typing murni — tidak import tipe Telethon apa pun."""
    if getattr(btn, "url", None):
        return True
    if type(btn).__name__ == "KeyboardButtonUrl":
        return True
    t = getattr(btn, "type", None)
    if t is not None:
        return bool(getattr(t, "url", None)) or type(t).__name__.endswith("Url")
    return False


def public_buttons(buttons):
    """Pangkas tombol untuk publik: sisakan hanya tombol URL.

    Menerima struktur tombol Telethon (list baris, boleh bersarang —
    mis. baris Sell pada kartu SIMULASI adalah list di dalam list).
    Tombol URL dipertahankan; semua tombol callback/aksi (Sell, dsb.)
    DIBUANG. Semua tombol URL yang tersisa digabung jadi SATU baris.
    Return None bila tidak ada tombol URL yang tersisa.
    Fungsi murni — diuji tanpa Telethon asli (duck-typing)."""
    flat = []

    def _walk(node):
        if isinstance(node, (list, tuple)):
            for sub in node:
                _walk(sub)
        elif _is_url_button(node):
            flat.append(node)

    _walk(buttons or [])
    return [flat] if flat else None


async def _notify_log(text, buttons=None):
    """Fan-out SATU log filter koin: admin (lengkap) + publik (aman).

    Kegagalan kirim ke publik TIDAK PERNAH menggagalkan notif admin
    maupun pipa filter — ditelan + dicetak ke console saja."""
    if _notify is not None:
        await _notify(text, buttons=buttons)
    if _public_notify is not None:
        try:
            await _public_notify(text, buttons=public_buttons(buttons))
        except Exception as e:  # channel belum siap dsb. — jangan bunuh pipa
            print(f"[public-log fail] {e}")


def bind_public_notify(fn):
    """Pasang/lepas callback log publik (dipakai bot.py & tests)."""
    global _public_notify
    _public_notify = fn


# ---------- format angka notifikasi ----------
def _fmt_usd(v) -> str:
    """$48.200 — pemisah ribuan gaya Indonesia, tanpa desimal."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "$?"
    return "$" + f"{v:,.0f}".replace(",", ".")


def _fmt_pct(v) -> str:
    """+184,0% / -15,6% — desimal koma gaya Indonesia, selalu bertanda."""
    if v is None:
        return "?"
    try:
        return f"{float(v):+.1f}%".replace(".", ",")
    except (TypeError, ValueError):
        return "?"


def _fmt_age(pair) -> str:
    """Umur pair: '6 mnt' / '2,5 jam' / '3,1 hari' / '?' bila tak diketahui."""
    ts = pair.get("pairCreatedAt")
    if not ts:
        return "?"
    try:
        m = max(0, int((time.time() * 1000 - float(ts)) / 60000))
    except (TypeError, ValueError):
        return "?"
    if m < 60:
        return f"{m} mnt"
    if m < 1440:
        return f"{m / 60:.1f} jam".replace(".", ",")
    return f"{m / 1440:.1f} hari".replace(".", ",")


def _fmt_gate_metrics(m) -> str:
    """1 baris ringkas metrik gate untuk notif. TIDAK boleh raise.

    Output persis (dipisah " · "):
      "📊 umur 74s · liq $3.1k · mcap $52k · rasio 6% · dev — · top10 —"
    Semua nilai tak tersedia (None/{} /m=None) → kembalikan "".
    """
    try:
        if m is None:
            return ""
        m = dict(m)
    except (TypeError, ValueError):
        return ""

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # Nilai "tersedia" = SETIDAKNYA SATU dari 6 key STORAGE berisi angka
    # yang bisa dikonversi. Kalau tidak ada yang bisa dikonversi:
    #   - m kosong ({} / tanpa storage keys) → string kosong (spec Bagian 2).
    #   - ada storage keys tapi masing-masing kosong → tetap dirender
    #     sebagai skeleton "… dev — …" (notif regresi non-error).
    KEYS = ("pool_age", "liq", "mcap", "ratio", "dev_pct", "top10_pct")
    # "Semua nilai None" (spec Bagian 2) = semua 6 key ada DAN semuanya None.
    all_keys_set = all(k in m for k in KEYS)
    all_none = all(m.get(k) is None for k in KEYS)
    if not all_keys_set and not any(k in m for k in KEYS):
        return ""
    if all_keys_set and all_none:
        return ""
    if m is None:
        return ""

    age_raw = _num(m.get("pool_age"))
    if age_raw is not None:
        umur = f"{age_raw:.0f}s" if age_raw < 120 else f"{age_raw / 60:.0f}m"
    else:
        umur = "—"
    liq = fmt.usd_compact(m.get("liq"))
    mcap = fmt.usd_compact(m.get("mcap"))
    ratio_raw = _num(m.get("ratio"))
    rasio = f"{ratio_raw * 100:.0f}%" if ratio_raw is not None else "—"
    dev_raw = _num(m.get("dev_pct"))
    dev = f"{dev_raw:.1f}%" if dev_raw is not None else "—"
    top_raw = _num(m.get("top10_pct"))
    top10 = f"{top_raw:.1f}%" if top_raw is not None else "—"

    parts = [umur, liq, mcap, rasio, dev, top10]
    return "📊 umur {} · liq {} · mcap {} · rasio {} · dev {} · top10 {}".format(*parts)


def _n(x) -> str:
    return str(int(x)) if x is not None else "?"


def _agent_card(pair, ca, src_name, decision) -> str:
    """Kartu posisi SIMULASI (mode latihan) dengan tata letak tetap.

    Angka metrik dihitung dari pair, BUKAN dari teks LLM — jadi selalu
    hadir dan selalu akurat. Fungsi murni (tanpa I/O), bisa di-unit-test.
    """
    sym = (pair.get("baseToken") or {}).get("symbol", "?")
    mcap_screen = dx.mcap_of(pair)
    mcap_oc = ((decision or {}).get("_gate_metrics") or {}).get("mcap_onchain")
    mcap = float(mcap_oc) if mcap_oc else mcap_screen
    sym = pair.get("baseToken", {}).get("symbol", "?")

    liq = dx.liq_usd_or_none(pair)
    liq_line = fmt.liq(liq)
    liq_pct = (liq / mcap * 100) if (liq is not None and mcap > 0) else 0.0
    chg = pair.get("priceChange") or {}
    txns = pair.get("txns") or {}
    tx5 = txns.get("m5") or {}
    tx1 = txns.get("h1") or {}
    conv = decision.get("conviction", "?")

    head = f"🧪 **MODE LATIHAN — FAST-PATH BUY {sym}** _via {src_name}_"

    lines = [head, f"`{ca}`"]
    lines.append(
        f"📊 Mcap {_fmt_usd(mcap)} · {fmt.liq(liq)}"
        + (f" ({liq_pct:.0f}% mcap)" if liq is not None and mcap > 0 else "")
        + f" · Umur {_fmt_age(pair)}")
    lines.append(
        f"📈 Harga m5 {_fmt_pct(chg.get('m5'))} · h1 {_fmt_pct(chg.get('h1'))} · "
        f"B/S m5 {_n(tx5.get('buys'))}/{_n(tx5.get('sells'))} · "
        f"h1 {_n(tx1.get('buys'))}/{_n(tx1.get('sells'))}")
    lines.append(f"🧭 **Analisa** · conviction {conv}")
    clauses = [c.strip() for c in str(decision.get("reason") or "").split(";")]
    clauses = [c for c in clauses if c]
    if clauses:
        lines += [f"• {c}" for c in clauses]
    else:
        lines.append("• (tanpa alasan tertulis)")
    risks = str(decision.get("risks") or "").strip()
    if risks:
        lines.append(f"⚠️ **Risiko:** {risks}")
    size = f"{float(decision.get('size_mult') or 1.0):.2f}".replace(".", ",")
    lines.append(
        f"🎯 **Rencana:** size {size}x · "
        f"TP1 +{float(decision.get('tp1') or 0) * 100:.0f}% · "
        f"TP2 +{float(decision.get('tp2') or 0) * 100:.0f}% · "
        f"SL {float(decision.get('sl') or 0) * 100:.0f}%")
    lines.append("_(tidak dieksekusi — mode latihan/dry-run)_")
    return "\n".join(lines)


def topic_id(msg) -> int:
    r = msg.reply_to
    if r is not None and getattr(r, "forum_topic", False):
        return getattr(r, "reply_to_top_id", None) or r.reply_to_msg_id or 1
    return 1


def register(user_client=None, trader=None, notify=None, public_notify=None):
    """Pasang handler userbot. user_client OPSIONAL:

    - user_client is None → userbot non-aktif (default v16.6): tidak
      memasang listener apa pun, sinyal 100% dipasok scanner factory
      on-chain & tracker insider. Tidak boleh raise.
    - user_client is not None → pasang handler sinyal grup seperti biasa.

    public_notify (opsional): callback siaran log filter koin ke channel
    publik. Boleh dipasang juga belakangan via bind_public_notify().
    """
    global _trader, _notify
    _trader, _notify = trader, notify
    if public_notify is not None:
        bind_public_notify(public_notify)
    if user_client is None:
        print("[listener] userbot non-aktif; sinyal 100% dipasok dari "
              "scanner factory & tracker.")
        return

    @user_client.on(events.NewMessage())
    async def handler(event):
        try:
            src = db.match_source(event.chat_id, topic_id(event.message))
            if src is None:
                return
            await _handle(event, src)
        except Exception:
            traceback.print_exc()


async def _handle(event, src):
    if db.paused():
        return
    caller_id = event.sender_id
    caller_name = None
    try:
        sender = await event.get_sender()
        if sender:
            caller_name = getattr(sender, "username", None) or getattr(sender, "first_name", None)
    except Exception:
        pass
    text = event.raw_text or ""
    for ca in _extract_ca_strict(text):
        await process_ca(ca, src, caller_id, caller_name)


# ---------- ekstraksi CA dari teks grup ----------
# Teks bebas dari grup tidak pernah dipakai sebagai instruksi; yang diambil
# hanya alamat kontrak valid, sisanya dibuang. Dipisah agar bisa di-unit-test.
def _extract_ca_strict(text) -> list:
    """Ekstrak contract address dengan validasi ketat:
    batas kata di kedua sisi (tidak ikut potongan alamat yang lebih panjang),
    dedupe mempertahankan urutan kemunculan pertama."""
    if not text:
        return []
    found = re.findall(r"(?<![0-9a-fA-Fx])(0x[a-fA-F0-9]{40})(?![0-9a-fA-F])", str(text))
    return list(dict.fromkeys(found))


# =====================================================================
#  ⚡ FAST-PATH GOLDILOCKS DEFENSIVE — evaluasi entry MURNI math/bytecode
#  (0 detik LLM). Tidak ada mode LLM di jalur entry — lihat pons_veto().
#  Fungsi evaluasi murni (tanpa I/O) dipisah agar bisa di-unit-test;
#  benda I/O (safety, quoter on-chain, notif) ada di _fastpath_gate.
# =====================================================================
def goldilocks_eval(pair) -> tuple[bool, str, dict]:
    """Filter statistik Goldilocks dari data Dexscreener (murni math).

    Return (lolos, skip_tag, metrik). skip_tag dipakai untuk status
    'skip_fast:<tag>' (umur/liq/mcap/ratio). Safety dev/top10 & slippage
    quoter diverifikasi di _fastpath_gate (butuh I/O on-chain).
    """
    now_ms = time.time() * 1000
    min_age = float(db.get("goldilocks_min_age_sec", float) or 10)
    max_age = float(db.get("goldilocks_max_age_sec", float) or 300)
    min_liq = float(db.get("goldilocks_min_liq", float) or 4000.0)
    min_ratio = float(db.get("goldilocks_min_liq_mcap_ratio", float) or 0.35)
    max_mcap = float(db.get("goldilocks_max_mcap", float) or 35000.0)
    min_mcap = float(db.get("goldilocks_min_mcap", float) or 5000.0)

    # ---- a. umur pool (pairCreatedAt milidetik) ----
    created = pair.get("pairCreatedAt")
    try:
        pool_age = max(0.0, (now_ms - float(created)) / 1000.0) if created else None
    except (TypeError, ValueError):
        pool_age = None
    # Metrik dasar SELALU dihitung (murni math, tanpa I/O) — supaya notif
    # dan tools/gate_metrics.py tetap bisa menampilkan liq/mcap/rasio walau
    # token gagal di tahap umur. Urutan tag keputusan TIDAK berubah.
    liq = dx.liq_usd_or_none(pair)
    mcap = dx.mcap_of(pair)
    metr = {"pool_age": pool_age, "liq": liq, "mcap": mcap,
            "ratio": (liq / mcap) if (liq and mcap > 0) else 0.0}

    if pool_age is None:
        return False, "age_unknown", metr
    if pool_age < min_age:
        return False, f"age_{pool_age:.0f}s", metr
    if pool_age > max_age:
        return False, f"age_{pool_age:.0f}s", metr

    # ---- b. likuiditas & mcap ----
    if liq is None or liq < min_liq:
        return False, "liq_rendah", metr
    if mcap <= 0 or mcap < min_mcap or mcap > max_mcap:
        return False, "mcap_out_of_range", metr
    # rasio likuiditas/mcap: liku harus tebal dibanding kapitalisasi
    if metr["ratio"] < min_ratio:
        return False, "liq_ratio_rendah", metr
    return True, "", metr


# Penolakan yang sudah pasti & tidak bisa ditindaklanjuti -> jangan spam.
# v16.8: "age_" TIDAK lagi silent — user minta lihat notif SKIP umur juga.
# Anti-spam naif: rate-limit global di _notify (min jeda antar notif).
SILENT_FAST_SKIP_PREFIXES = ()


def should_notify_fast_skip(tag: str) -> bool:
    return not (tag or "").startswith(SILENT_FAST_SKIP_PREFIXES)


async def _fastpath_gate(ca, pair, safety_result=None) -> tuple[bool, str, dict]:
    """Gate lengkap fast-path (bagian I/O): safety dev/top10/honeypot +
    quote on-chain mcap guard. Return (lolos, skip_tag, metrik goldilocks).

    skip_tag:
      'dev_terlalu_besar'  — safety menolak (dev/top10/honeypot/fail-closed)
      'quote_gagal:<err>'  — quoter on-chain revert/gagal (spot probe min_out=0)
      'mcap_onchain_pucuk' — mcap hasil quote on-chain > goldilocks_max_mcap
                             / scanner_mcap_max
    """
    # ---- metrik dasar dipanggil SEKALI di depan (murni math, tanpa I/O) ----
    # Urutan keputusan tetap: safety -> dev -> top10 -> goldilocks -> quoter.
    ok_g, tag_g, base = goldilocks_eval(pair)
    base = base or {}

    # ---- c. safety guard (bytecode honeypot + dev + top10) ----
    if safety_result is not None:
        ok, reason = safety_result
    else:
        ok, reason = await asyncio.to_thread(safety.check, _trader, ca, pair)

    # Ekstrak dev_pct / top10_pct dari reason (lolos maupun gagal)
    m_dev = re.search(r"dev\s+(?:pegang\s+)?([\d\.]+)%", reason)
    if m_dev:
        base["dev_pct"] = float(m_dev.group(1))
    m_top = re.search(r"top10\s+(?:holders\s+pegang\s+)?([\d\.]+)%", reason)
    if m_top:
        base["top10_pct"] = float(m_top.group(1))

    if not ok:
        print(f"[fast-safety] {ca[:10]}: {reason}")
        # Jika dev_pct belum terisi, coba ambil best effort on-chain
        if base.get("dev_pct") is None:
            try:
                dp = await asyncio.to_thread(safety.dev_pct, _trader, ca)
                if dp is not None and dp >= 0:
                    base["dev_pct"] = dp
            except Exception:
                pass

        dp_val = base.get("dev_pct")
        tp_val = base.get("top10_pct")
        dp_str = f" ({dp_val:.1f}%)" if dp_val is not None else ""
        tp_str = f" ({tp_val:.1f}%)" if tp_val is not None else ""

        if "clone guard" in reason:
            cg_sub = reason.split("clone guard:", 1)[1].strip()
            cg_clean = cg_sub.split("(")[0].strip() or cg_sub
            tag = f"clone_guard: {cg_clean}"
        elif "audit kontrak" in reason or "honeypot" in reason or "signature berbahaya" in reason:
            sub = reason.split("gagal:", 1)[-1].strip() if "gagal:" in reason else reason
            sub_clean = sub.split("(")[0].strip() or sub
            tag = f"audit_kontrak: {sub_clean}"
        elif "family limit" in reason:
            tag = "family_limit"
        elif "top10" in reason:
            tag = f"top10_terlalu_besar{tp_str}"
        elif "tidak bisa diverifikasi" in reason:
            tag = "safety_unverified"
        else:
            tag = f"dev_terlalu_besar{dp_str}"
        return False, tag, base

    # batas dev/top10 ketat versi Goldilocks (rasio, bukan persen)
    max_dev = float(db.get("goldilocks_max_dev_pct", float) or 0.04)
    max_top = float(db.get("goldilocks_max_top10_pct", float) or 0.30)
    dev = base.get("dev_pct")
    if dev is None:
        dev = await asyncio.to_thread(safety.dev_pct, _trader, ca)          # persen (0-100)
        base["dev_pct"] = dev                      # persen — simpan SEBELUM cek
    if dev and dev / 100.0 > max_dev:
        return False, f"dev_terlalu_besar ({dev:.1f}% > {max_dev*100:.0f}%)", base
    exclude = {pair.get("pairAddress", ""), ca}
    top = base.get("top10_pct")
    if top is None:
        top = await asyncio.to_thread(safety.top_holders_pct, _trader, ca, exclude)  # persen (0-100)
        base["top10_pct"] = top                    # persen — simpan SEBELUM cek
    if top and top / 100.0 > max_top:
        return False, f"top10_terlalu_besar ({top:.1f}% > {max_top*100:.0f}%)", base

    metr = {}
    ok_m, tag, metr = ok_g, tag_g, base
    if not ok_m:  # data berubah sejak tahap pertama — pakai tag terakhir
        return False, tag, metr
    liq, mcap, pool_age = metr["liq"], metr["mcap"], metr["pool_age"]

    # ---- d. zero-gas on-chain quoter (SPOT PROBE → mcap on-chain) ----
    # Anchor harga = quote on-chain AKTUAL (min_out=0), BUKAN priceNative
    # layar Dexscreener yang cached 20-30s. Runner harga cepat ($8k →
    # $15k-$20k) tidak lagi salah di-reject sebagai 'slippage_besar';
    # pagar nyata hanyalah MARKET CAP on-chain (anti-pucuk).
    buy_usd = db.get("buy_usd")
    eth_price = dx.eth_usd(pair)
    eth_spent = buy_usd / eth_price if eth_price > 0 else 0
    ptype = dx.pool_type(pair)
    fee = 3000
    if ptype == "v3":
        try:
            fee = int(_trader.pool_fee(pair["pairAddress"]))
        except Exception:
            fee = 3000
    wei_in = _trader.w3.to_wei(eth_spent, "ether") if eth_spent > 0 else 0
    dec = 18
    try:
        dec = int(_trader.erc20(ca).functions.decimals().call())
    except Exception:
        dec = 18
    try:
        ok_q, amt_raw, err_reason = await asyncio.to_thread(
            _trader.quote_buy_tokens, ca, ptype, fee, wei_in,
            0, pool_id=pair["pairAddress"])
    except Exception as e:
        ok_q, amt_raw, err_reason = False, 0, f"quoter_exception:{e}"
    if not ok_q or amt_raw <= 0:
        return False, f"quote_gagal:{err_reason or 'quoter_gagal'}", metr

    tokens_total = amt_raw / (10 ** dec)
    entry_price_usd = (db.get("buy_usd") / tokens_total) if tokens_total > 0 else 0
    total_supply = None
    circulating_supply = None
    try:
        token_contract = _trader.erc20(ca)
        supply_raw = await asyncio.to_thread(
            token_contract.functions.totalSupply().call)
        total_supply = supply_raw / (10 ** dec)

        # Kurangi burned tokens (dead & zero address) → circulating supply.
        # Token meme di Robinhood Chain sering membakar ~77% supply at
        # deploy; totalSupply mentah membuat mcap on-chain membengkak ~4.4x
        # dan koin bagus salah di-tolak sebagai 'mcap_onchain_pucuk'.
        burned_raw = 0
        for dead_addr in (
            "0x000000000000000000000000000000000000dEaD",
            "0x0000000000000000000000000000000000000000",
        ):
            try:
                bal = await asyncio.to_thread(
                    token_contract.functions.balanceOf(dead_addr).call)
                burned_raw += int(bal or 0)
            except Exception:
                pass
        circ_raw = max(0, supply_raw - burned_raw)
        if circ_raw > 0:
            circulating_supply = circ_raw / (10 ** dec)
    except Exception:
        total_supply = None
        circulating_supply = None
    if not circulating_supply or circulating_supply <= 0:
        # Fallback estimasi dari supply beredar Dexscreener (mcap pada
        # Dexscreener memakai circulating supply, bukan FDV).
        pair_mcap = float(pair.get("marketCap") or 0)
        pair_price = float(pair.get("priceUsd") or 0)
        circulating_supply = (pair_mcap / pair_price) \
            if (pair_price > 0 and pair_mcap > 0) else 1_000_000_000
    mcap_onchain = entry_price_usd * circulating_supply
    # ---- GUARD NYATA ADALAH MARKET CAP (anti-pucuk) ----
    # Harga on-chain berada di rentang aman? Perbedaan harga layar
    # Dexscreener vs on-chain BUKAN slippage — koin runner di jendela
    # harga masuk. Bila menyentuh pucuk → tolak.
    scanner_mcap_max = float(db.get("scanner_mcap_max", float) or 45000)
    gold_max_mcap = float(db.get("goldilocks_max_mcap", float) or 35000)
    if mcap_onchain > scanner_mcap_max or mcap_onchain > gold_max_mcap:
        metr["mcap_onchain"] = mcap_onchain   # angka pucuk ikut ke notif
        return False, "mcap_onchain_pucuk", metr
    metr.update({"tokens_total": tokens_total,
                 "entry_price_usd": entry_price_usd,
                 "mcap": mcap_onchain,
                 "mcap_onchain": mcap_onchain,
                 "ratio": (liq / mcap_onchain) if (liq and mcap_onchain > 0) else metr.get("ratio")})
    return True, "", metr


async def process_ca(ca, src, caller_id, caller_name, allow_buy=True,
                     retry=False):
    """Proses SATU kandidat CA lewat pipa: filter → sosmed → safety → veto radar → fast-path → buy.

    Dipakai listener (dari pesan grup) maupun scanner (sniper/scanner.py).
    allow_buy=False = PAGAR KERAS: setelah lolos semua tahap, buy TIDAK
    dieksekusi lewat jalur mana pun (termasuk fallback rule-based bila
    mati/gagal) — fungsi berhenti di status "no_buy_scanner". Mekanisme ini
    TETAP TERSEDIA, tapi di v16 gabungan scanner TIDAK memakainya: scanner
    memanggil allow_buy=True dan eksekusi diatur mode global (agent_dry_run
    dst.), persis seperti sinyal dari grup.

    Return string status singkat:
      "dup" | "paused" | "filtered:<alasan>" | "skip_agent" | "dry_run" |
      "no_buy_scanner" | "bought" | "buy_failed:<err>"
    """
    if db.paused():
        return "paused"
    ca_lc = str(ca).lower()
    if retry:
        # Jalur retry antrean pending (scanner): CA sudah pernah dievaluasi
        # tapi masih muda / likuiditasnya belum naik. Boleh dicek ulang,
        # TAPI jangan pernah buka posisi kedua di CA yang sama.
        if any(str(p.get("ca", "")).lower() == ca_lc for p in db.open_positions()):
            return "dup_position"
        db.touch_call(ca)
    else:
        if db.seen_call(ca):
            return "dup"
        db.record_call(ca, src["id"], caller_id, caller_name)

    pair = await asyncio.to_thread(dx.best_pair, ca)
    if not pair:
        print(f"[skip] {ca} tidak ketemu di chain '{config.DEX_CHAIN_MATCH}'")
        return "filtered:no_pair"

    mcap = dx.mcap_of(pair)
    liq = dx.liq_usd_or_none(pair)
    sym = pair.get("baseToken", {}).get("symbol", "?")
    ptype_dx = dx.pool_type(pair)

    # ---------- v4-Only Guard: Tolak pool selain v4 (v2/v3 disabled) ----------
    if db.get("scanner_ignore_v3", str) == "1" and src.get("name") == "SCANNER" and ptype_dx != "v4":
        return f"filtered:{ptype_dx}_pool_disabled"

    # ---------- v4: likuiditas Dexscreener sering null ----------------
    # Jangan sampai filter min-liq salah menolak (atau rug monitor buta).
    # Bila null → baca likuiditas ON-CHAIN StateView.getLiquidity; kalau
    # on-chain 0 → tolak (pool kosong, kasus grok).
    if liq is None:
        onchain_liq = None
        if ptype_dx == "v4":
            try:
                from . import v4 as v4mod
                _w3 = _trader.w3 if _trader is not None else None
                onchain_liq = v4mod.liquidity(pair["pairAddress"], _w3)
            except Exception:
                onchain_liq = None
        if onchain_liq is None:
            print(f"[liq] {sym} liq Dexscreener null dan on-chain tak terbaca "
                  f"— pakai ambang 0 saja saat ini")  # fallthrough ke filter bawah
        elif onchain_liq <= 0:
            print(f"[skip] {sym} v4 liquidity 0 (pool kosong)")
            return "filtered:v4_pool_kosong"

    if mcap <= 0 or mcap > src["mcap_max"]:
        print(f"[skip] {sym} mcap ${mcap:,.0f} > limit ${src['mcap_max']:,.0f} ({src['name']})")
        await _notify_log(
            f"⚡ **SKIP {sym}** _via {src['name']}_\n`{ca}`\n"
            f"reason: mcap_terlalu_besar\n"
            f"📊 mcap ${mcap:,.0f} (limit ${src['mcap_max']:,.0f})"
            + (f" · liq ${liq:,.0f}" if liq else ""))
        return "filtered:mcap"
    if liq is not None and liq < db.get("min_liq_usd"):
        # Dexscreener tahu likuiditasnya dan rendah → tolak
        print(f"[skip] {sym} liq ${liq:,.0f} < min")
        await _notify_log(
            f"⚡ **SKIP {sym}** _via {src['name']}_\n`{ca}`\n"
            f"reason: liq_rendah\n"
            f"📊 liq ${liq:,.0f} (min ${db.get('min_liq_usd'):,.0f})"
            + (f" · mcap ${mcap:,.0f}" if mcap > 0 else ""))
        return "filtered:liq"
    if liq is None:
        print(f"[filter] {sym} liq Dexscreener null — dipakai on-chain (v4) / diteruskan")
        # liq sudah terverifikasi via StateView di atas (bila v4); kalau
        # ptype bukan v4 dan null, anggap 0 saja untuk filter (perilaku lama).
        if ptype_dx != "v4" and dx.liq_usd(pair) < db.get("min_liq_usd"):
            print(f"[skip] {sym} liq ${dx.liq_usd(pair):,.0f} < min")
            return "filtered:liq"

    # ---------- 🌐 FILTER SOSMED (X / Web / TG / dll) ----------
    soc_ok, soc_reason = dx.social_ok(pair)
    if not soc_ok:
        print(f"[social-skip] {sym}: {soc_reason}")
        await _notify_log(f"🌐 **SKIP {sym}** _via {src['name']}_\n`{ca}`\n{soc_reason}")
        return "filtered:social"

    # ---------- 🚫 VETO RADAR PONS (0 ms, pra-graduasi) ----------
    # Radar menilai koin saat masih di bonding curve. Koin yang ditolak
    # (LLM BUANG / skor di bawah ambang) masuk set veto di memori. Dicek
    # SEBELUM safety & gate matematika: kalau koinnya sudah dinilai buruk, tidak
    # perlu panggil RPC/Blockscout sama sekali.
    if pons_veto(ca):
        print(f"[pons-veto] {sym}: ditolak Radar Pons saat pra-graduasi")
        await _notify_log(
            f"🚫 **VETO RADAR PONS {sym}** _via {src['name']}_\n"
            f"`{ca}`\n"
            f"_alasan: ditolak LLM/skor saat masih di bonding curve_\n"
            f"_(fast-path tidak dijalankan)_")
        return "skip_pons_veto"

    ok_safety, reason_safety = await asyncio.to_thread(safety.check, _trader, ca, pair)
    if not ok_safety:
        print(f"[safety-skip] {sym}: {reason_safety}")
        await _notify_log(f"🛡 **SKIP {sym}** _via {src['name']}_\n`{ca}`\n{reason_safety}")
        return "filtered:safety"

    # ---------- ⚡ FAST-PATH GOLDILOCKS (0 detik LLM, satu-satunya jalur) ----
    # Evaluasi murni matematika/bytecode: umur pool → liq/mcap → safety →
    # quote on-chain (slippage ≤ 15%, mcap on-chain ≤ scanner_mcap_max).
    # Lolos semua gate → keputusan sintetis conviction 90 → eksekusi di
    # alur bawah (dry_run_simulated ATAU live buy) seperti biasa.
    decision = None
    ok_f, tag_f, metr_f = await _fastpath_gate(ca, pair, safety_result=(ok_safety, reason_safety))
    if not ok_f:
        print(f"[fast-skip] {sym}: {tag_f}")
        if should_notify_fast_skip(tag_f):
            gl = _fmt_gate_metrics(metr_f)
            await _notify_log(
                f"⚡ **FAST-PATH SKIP {sym}** _via {src['name']}_\n"
                f"`{ca}`\nreason: {tag_f}"
                + (f"\n{gl}" if gl else ""))
        return f"skip_fast:{tag_f}"
    liq_f = metr_f.get("liq") or 0.0
    mcap_f = metr_f.get("mcap") or 0.0
    mcap_oc_f = metr_f.get("mcap_onchain") or mcap_f
    ratio_f = (liq_f / mcap_oc_f) if mcap_oc_f > 0 else (metr_f.get("ratio") or 0.0)
    age_f = metr_f.get("pool_age") or 0.0
    tp1_f = float(db.get("goldilocks_tp1", float) or 0.35)
    tp2_f = float(db.get("goldilocks_tp2", float) or 0.80)
    decision = {
        "action": "BUY",
        "conviction": 90,
        "size_mult": 1.0,
        "tp1_x": tp1_f,
        "tp2_x": tp2_f,
        "sl_x": -0.30,
        # alias kompatibilitas pipa eksekusi lama (blok dry-run/live
        # membaca key tp1/tp2/sl)
        "tp1": tp1_f, "tp2": tp2_f, "sl": -0.30,
        "risks": "",
        "reason": (f"⚡ FAST-PATH Goldilocks: umur {age_f:.0f}s, "
                   f"liq ${liq_f:,.0f}, mcap ${mcap_oc_f:,.0f}, "
                   f"ratio {ratio_f * 100:.1f}%, quote OK"),
        # plumbing metrik gate → notif BUY (2 baris di bawah)
        "_gate_metrics": dict(metr_f or {}),
    }
    db.log_decision(ca=ca, symbol=sym, role="fastpath", action="BUY",
                    conviction=90, size_mult=1.0,
                    reason=decision["reason"], risks="goldilocks_defensive")
    await _notify_log(
        f"⚡ **FAST-PATH BUY {sym}** _via {src['name']}_\n"
        f"`{ca}`\n📊 mcap ${mcap_oc_f:,.0f} | liq ${liq_f:,.0f} "
        f"({ratio_f * 100:.1f}%) | umur pool {age_f:.0f}s\n"
        f"🎯 TP1 +{tp1_f * 100:.0f}% · TP2 +{tp2_f * 100:.0f}% · "
        f"SL -30% (conviction 90)\n_{decision['reason']}_")
    # ---------- 📉 STALENESS GATE: harga sudah lari dari harga evaluasi? ----------
    # Gate ini juga berlaku untuk fast-path: quote on-chain diambil ratusan
    # ms setelah snapshot harga, dan pool baru bisa bergerak cepat. Bila harga
    # sudah melonjak melewati ambang drift (agent_max_drift, default 50%) →
    # BUY dibatalkan (beli di pucuk / mengincar liquidity yang sedang ditarik).
    if decision and decision["action"] == "BUY":
        initial_price = float(((pair.get("priceUsd") or 0)))
        try:
            fresh = await asyncio.to_thread(dx.best_pair, ca)
            fresh_price = float((fresh or {}).get("priceUsd") or 0) if fresh else 0.0
        except Exception:
            fresh_price = 0.0
        max_drift = float(db.get("agent_max_drift") or 0.50)
        if fresh_price > 0 and agent.check_staleness(initial_price, fresh_price, max_drift):
            await _notify_log(
                f"⚡ **FAST-PATH BUY {sym} DIBATALKAN — HARGA STALE** _via {src['name']}_\n"
                f"`{ca}`\n"
                f"harga saat evaluasi ${initial_price:.10f} → saat eksekusi ${fresh_price:.10f} "
                f"(drift {((fresh_price/initial_price-1)*100) if initial_price > 0 else 0:.1f}% "
                f"> {max_drift*100:.0f}%)\n"
                f"alasan: price_stale_drift")
            print(f"[staleness-skip] {sym}: harga sudah drift > {max_drift*100:.0f}% "
                  f"sejak evaluasi FAST-PATH — buy dibatalkan")
            return "skip_agent:price_stale_drift"
    if decision and db.get("agent_dry_run", str) == "1":
        # ---------- 🧪 VIRTUAL PAPER TRADING (mode LATIHAN) ----------
        # Tidak lagi berhenti di "dry_run": FAKTA SINYAL TETAP
        # PILIH KOIN INI — jadi buka posisi SIMULASI, agar TP1/TP2/SL/trail
        # engine monitor ikut menguji rencana trading (harga on-chain
        # & saldo wallet KETIDAKTERLIBATAN sama sekali).
        await _notify_log(_agent_card(pair, ca, src["name"], decision))
        eth_price = dx.eth_usd(pair)
        buy_usd = (db.get("buy_usd") * decision["size_mult"]) if decision else db.get("buy_usd")
        eth_spent = buy_usd / eth_price if eth_price > 0 else 0.0003
        # ---------- 📣 ON-CHAIN QUOTE (paper entry akurat, v16.4) ----------
        # Harga layar Dexscreener cached 30-60s + spread statis TIDAK
        # mencerminkan chain: di on-chain harga pool sudah bergeser / liku
        # habis (revert). Hitung entry dari simulasi swap aktual (eth_call
        # Quoter) pada detik eksekusi. Bila quote revert → BATALKAN entry,
        # jangan catat profit palsu.
        ptype_dx = dx.pool_type(pair)
        fee_dx = 3000
        if ptype_dx == "v3":
            try:
                fee_dx = int(_trader.pool_fee(pair["pairAddress"]))
            except Exception:
                fee_dx = 3000
        wei_in = _trader.w3.to_wei(eth_spent, "ether") if eth_spent > 0 else 0
        # expected (harga layar BASI 20-30s) TIDAK dipakai sebagai anchor —
        # runner cepat ($8k → $15k-$20k) membuat min_out layar memicu revert
        # palsu. Min_out=0 → probe spot on-chain murni; pagar nyata = mcap.
        dec = 18
        try:
            dec = int(_trader.erc20(ca).functions.decimals().call())
        except Exception:
            dec = 18
        try:
            ok_q, amt_raw, err_reason = await asyncio.to_thread(
                _trader.quote_buy_tokens, ca, ptype_dx, fee_dx, wei_in,
                0, pool_id=pair["pairAddress"])
        except Exception as e:
            ok_q, amt_raw, err_reason = False, 0, f"quoter_exception:{e}"
        if not ok_q or amt_raw <= 0:
            reason_line = err_reason or "quoter_gagal"
            await _notify_log(
                f"❌ **SIMULASI BUY GAGAL (On-Chain Revert)** via {src['name']}\n"
                f"{ca}\n"
                f"Alasan on-chain: {reason_line}\n"
                f"_(koin tidak dimasukkan ke posisi karena di chain asli "
                f"transaksi ini pasti revert/gagal)_")
            return "buy_failed:" + reason_line
        # entry efektif RIIL: hasil quote on-chain (termasuk price impact)
        tokens_total = amt_raw / (10 ** dec)
        entry_price_usd = (buy_usd / tokens_total) if tokens_total > 0 else 0
        # Friksi realistis: spread entry (latency broadcast antrean sequencer
        # + fee pool DEX) — koin wajib naik +2.5% dulu untuk breakeven.
        spread = float(db.get("paper_entry_spread_pct", float) or 0.025)
        entry_price_usd = entry_price_usd * (1.0 + spread)
        if tokens_total <= 0 or entry_price_usd <= 0:
            await _notify_log(
                f"❌ **SIMULASI BUY GAGAL (On-Chain Revert)** via {src['name']}\n"
                f"{ca}\n"
                f"Alasan on-chain: quoter_output_kosong (token 0 dihitung)\n"
                f"_(koin tidak dimasukkan ke posisi karena di chain asli "
                f"transaksi ini pasti revert/gagal)_")
            return "buy_failed:quoter_output_kosong"
        # ---------- 🛡 MCAP GUARD REAL-TIME ON-CHAIN (Solusi B, v16.5) ----
        # Mcap Dexscreener cached 30-60s terlambat. Hitung mcap dari harga
        # entry quote on-chain × total supply on-chain; tolak bila sudah
        # melewati batas keras (dipompa sniper lain).
        total_supply_tokens = None
        circulating_supply_tokens = None
        try:
            # pakai kontrak ERC20 yang sudah dibuat untuk decimals di atas
            _erc20 = _trader.erc20(ca)
            supply_raw = await asyncio.to_thread(
                _erc20.functions.totalSupply().call)
            total_supply_tokens = supply_raw / (10 ** dec)

            # Kurangi tokens terbakar (dead & zero address) — supply
            # beredar riil, sama seperti perhitungan Dexscreener.
            burned_raw = 0
            for dead_addr in (
                "0x000000000000000000000000000000000000dEaD",
                "0x0000000000000000000000000000000000000000",
            ):
                try:
                    _bal = await asyncio.to_thread(
                        _erc20.functions.balanceOf(dead_addr).call)
                    burned_raw += int(_bal or 0)
                except Exception:
                    pass
            circ_raw = max(0, supply_raw - burned_raw)
            if circ_raw > 0:
                circulating_supply_tokens = circ_raw / (10 ** dec)
        except Exception:
            total_supply_tokens = None
            circulating_supply_tokens = None
        if not circulating_supply_tokens or circulating_supply_tokens <= 0:
            # fallback estimasi circulating supply dari pair Dexscreener
            pair_mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
            pair_price = float(pair.get("priceUsd") or 0)
            circulating_supply_tokens = (pair_mcap / pair_price) \
                if (pair_price > 0 and pair_mcap > 0) else 1_000_000_000
        mcap_onchain = entry_price_usd * circulating_supply_tokens
        mcap_max = float(db.get("scanner_mcap_max", float)
                         or 0) or float(src.get("mcap_max") or 45000)
        if mcap_onchain > mcap_max:
            await _notify_log(
                f"❌ **SIMULASI BUY GAGAL (Mcap On-Chain Pucuk)** "
                f"via {src['name']}\n"
                f"{ca}\n"
                f"Alasan on-chain: mcap_onchain_pucuk "
                f"($ {mcap_onchain:,.0f} > batas $ {mcap_max:,.0f})\n"
                f"_(koin tidak dimasukkan ke posisi karena sudah dipompa "
                f"melebihi batas aman $45k)_")
            return "buy_failed:mcap_onchain_pucuk"
        pos = {"ca": ca.lower(), "symbol": sym,
               # lihat catatan di sniper/trader.py: `name` dipakai sebagai
               # family key oleh clone_guard.note_entry().
               "name": (pair.get("baseToken") or {}).get("name", ""),
               "pair_address": pair["pairAddress"], "chain_slug": pair["chainId"],
               "pool_type": dx.pool_type(pair), "fee": 3000,
               "entry_price_usd": entry_price_usd,
               "tokens_total": tokens_total, "tokens_left": tokens_total,
               "eth_spent": eth_spent, "usd_spent": buy_usd,
               "honeypot": 0, "source_name": src["name"],
               "caller_id": caller_id, "caller_name": caller_name,
               "buy_tx": "simulated_buy", "is_simulated": 1}
        buy_usd_final = buy_usd
        # Biaya nyata: gas L2 per swap + modal koin. Gas dipotong kas
        # virtual di samping modal, dicatat terpisah di notifikasi.
        gas_eth_buy = float(db.get("paper_gas_eth", float) or 0.0001)
        gas_usd = gas_eth_buy * eth_price if eth_price > 0 else 0.12
        total_buy_cost = buy_usd_final + gas_usd
        if not db.deduct_paper_cash(total_buy_cost):
            # kas virtual kurang → posisi simulasi TIDAK dibuka
            await _notify_log(
                f"🧪 **MODE LATIHAN — BUY {sym} DIBATALKAN** _via {src['name']}_\n"
                f"`{ca}`\n"
                f"saldo_virtual_kurang (kas ${db.get_paper_cash():.2f} < "
                f"${total_buy_cost:.2f} = modal ${buy_usd_final:.2f} + gas ${gas_usd:.2f}) "
                f"— kas virtual tidak cukup.")
            return (False, f"saldo_virtual_kurang (kas "
                           f"${db.get_paper_cash():.2f} < ${total_buy_cost:.2f})")
        db.open_position(**pos)
        try:
            pid = max(p["id"] for p in db.open_positions() if p["ca"] == pos["ca"] and p["is_simulated"])
            db.update_position(
                pid,
                tp1_x=float(decision.get("tp1") or db.get("tp1")),
                tp2_x=float(decision.get("tp2") or db.get("tp2")),
                sl_x=float(decision.get("sl") or db.get("sl")),
                peak_price=entry_price_usd)
        except (ValueError, KeyError):
            pid = None
        db.log_decision(**{"ca": ca, "symbol": sym, "role": "screener",
                           "action": "BUY_SIMULATED",
                           "conviction": decision.get("conviction", 0),
                           "size_mult": decision.get("size_mult", 1.0),
                           "reason": str(decision.get("reason") or ""),
                           "risks": str(decision.get("risks") or "")})
        chart = f"https://dexscreener.com/{pos['chain_slug']}/{pos['pair_address']}"
        btn_sell = ([Button.inline("📉 Sell 50%", f"sell:{pid}:50"),
                     Button.inline("🚨 Sell ALL", f"sell:{pid}:100")] if pid else [])
        sell_line = ""
        if pid:
            sell_line = (f"\n🎯 TP1 +{float(decision.get('tp1') or db.get('tp1'))*100:.0f}% · "
                         f"TP2 +{float(decision.get('tp2') or db.get('tp2'))*100:.0f}% · "
                         f"SL {float(decision.get('sl') or db.get('sl'))*100:.0f}% (SIMULASI aktif)")
        await _notify_log(
            f"🧪 **[SIMULASI] BUY {sym}**  _via {src['name']}_\n"
            f"`{pos['ca']}`\n"
            f"💰 mcap ${mcap_onchain:,.0f} | {fmt.liq(liq)} | {pos['pool_type']}\n"
            f"paper masuk **${pos['usd_spent']:.2f}** ({pos['eth_spent']:.6f} ETH) → "
            f"{pos['tokens_total']:,.0f} {sym}\n"
            f"entry ${pos['entry_price_usd']:.10f} (on-chain quote){sell_line}"
            f"\n💵 Modal: **${buy_usd_final:.2f}** + Gas: **${gas_usd:.2f}**"
            f" (Total: **${total_buy_cost:.2f}**) | Sisa Kas: **${db.get_paper_cash():.2f}**"
            f"\n_(posisi virtual — engine monitor TP/SL aktif, TANPA swap on-chain)_",
            buttons=[btn_sell,
                     [Button.url("📊 Chart", chart)]] if btn_sell else
                     [[Button.url("📊 Chart", chart)]],
        )
        return "dry_run_simulated"

    # ---------- 🔭 PAGAR KERAS (mekanisme allow_buy=False) ----------
    # Titik ini adalah SATU-SATUNYA jalan menuju trader.buy di bawah. Kalau
    # allow_buy=False, fungsi berhenti di sini. Di v14 jalur ini mengunci
    # scanner secara permanen; di v16 gabungan scanner lewat dengan
    # allow_buy=True dan titik ini hanya tercapai bila ada pemanggil lain
    # yang sengaja meminta pagar ditutup.
    if not allow_buy:
        ai_line = ""
        if decision:
            ai_line = (f"\n⚡ Fast-path: **{decision['action']}** "
                       f"(conviction {decision['conviction']})"
                       f"\n_{decision['reason']}_")
        if _notify or _public_notify:
            await _notify_log(
                f"🔭 **SCANNER (dry): {sym}** lolos semua filter{ai_line}\n"
                f"`{ca}`\n💰 mcap ${mcap:,.0f} | {fmt.liq(liq)}\n"
                f"— buy TIDAK dieksekusi (allow_buy=False)")
        return "no_buy_scanner"

    # ---------- 🧪 PAGAR KERAS MODE LATIHAN (jaring pengaman terakhir) ----
    # Paper trading menahan keputusan FAST-PATH → posisi simulasi. Blok ini
    # hanya tercapai bila tidak ada keputusan entry sama sekali (mis. gate
    # melempar exception sebelum decision diisi). Selama agent_dry_run ON,
    # trader.buy TIDAK BOLEH tercapai lewat jalur mana pun.
    if db.get("agent_dry_run", str) == "1":
        await _notify_log(
            f"🧪 **MODE LATIHAN — buy LIVE ditahan untuk {sym}** _via {src['name']}_\n"
            f"`{ca}`\nTidak ada keputusan entry yang valid — selama mode latihan "
            f"buy betulan tidak dieksekusi dan saldo ETH tidak dicek.")
        return "dry_run"

    usd = db.get("buy_usd") * decision["size_mult"]
    try:
        pos = await asyncio.to_thread(_trader.buy, ca, pair, src["name"], usd)
    except Exception as e:
        await _notify_log(f"❌ Gagal buy **{sym}** dari _{src['name']}_: {e}")
        return f"buy_failed:{e}"

    db.open_position(**pos)
    pid = max(p["id"] for p in db.open_positions() if p["ca"] == pos["ca"])
    agent_line = ""
    if decision:
        db.update_position(pid, tp1_x=decision["tp1"], tp2_x=decision["tp2"], sl_x=decision["sl"])
        agent_line = (f"\n👁 conviction {decision['conviction']} · size {decision['size_mult']:.2f}x · "
                      f"TP {decision['tp1']*100:.0f}/{decision['tp2']*100:.0f} · SL {decision['sl']*100:.0f}\n"
                      f"_{decision['reason']}_"
                      + (f"\n⚠️ risks: {decision['risks']}" if decision.get("risks") else ""))
    if caller_id:
        db.update_position(pid, caller_id=caller_id, caller_name=caller_name)
    cs = db.caller_stats(caller_id)
    caller_line = ""
    if caller_name and cs and cs["calls_bought"] > 1:
        caller_line = (f"\n🎯 caller: {caller_name} — {cs['calls_bought']} call, "
                       f"{cs['hit_tp1'] or 0} TP1, {cs['stopped'] or 0} SL")
    elif caller_name:
        caller_line = f"\n🎯 caller: {caller_name} (baru)"
    hp = "\n⚠️ **SIMULASI SELL GAGAL — kemungkinan HONEYPOT!**" if pos["honeypot"] else ""
    _soc = dx.socials_of(pair)
    soc_line = _soc["label"] if _soc["count"] else "tanpa jejak sosmed"
    chart = f"https://dexscreener.com/{pos['chain_slug']}/{pos['pair_address']}"
    txurl = f"{config.EXPLORER_URL}/tx/{pos['buy_tx']}" if pos.get("buy_tx") else None
    # ---------- 2 baris angka gate (dari _fastpath_gate, BUKAN hitungan baru) ----
    gate = (decision or {}).get("_gate_metrics") or {}
    gate_lines = ""

    def _dash(v):
        return "—" if v is None else v

    if gate:
        _age = gate.get("pool_age")
        _age_s = f"{_age:.0f}s" if isinstance(_age, (int, float)) and _age < 120 else (
            f"{_age / 60:.0f}m" if isinstance(_age, (int, float)) else "—")
        _liq = gate.get("liq")
        _mcap = gate.get("mcap")
        _ratio = gate.get("ratio")
        min_ratio = float(db.get("goldilocks_min_liq_mcap_ratio", float) or 0.35)
        max_dev = float(db.get("goldilocks_max_dev_pct", float) or 0.04)      # rasio
        max_top = float(db.get("goldilocks_max_top10_pct", float) or 0.30)    # rasio
        max_oc = float(db.get("goldilocks_max_mcap", float) or 35000)
        _ratio_s = f"{_ratio * 100:.0f}%" if _ratio is not None else "—"
        _dev = f"{gate.get('dev_pct'):.1f}%" if gate.get("dev_pct") is not None else "—"
        _top = f"{gate.get('top10_pct'):.1f}%" if gate.get("top10_pct") is not None else "—"
        _oc = (f"${gate.get('mcap_onchain'):,.0f}"
               if gate.get("mcap_onchain") is not None else "—")
        gate_lines = (
            f"\n⚙️ gate: umur {_dash(_age_s)} · liq/mcap {_dash(_ratio_s)}"
            + (f" (min {min_ratio * 100:.0f}%)" if _ratio is not None else "")
            + f"\n👤 dev {_dash(_dev)} (maks {max_dev * 100:.0f}%)"
            + f" · top10 {_dash(_top)} (maks {max_top * 100:.0f}%)"
            + f" · mcap on-chain {_dash(_oc)} (maks ${max_oc:,.0f})")
    _mcap_live = gate.get("mcap_onchain") or mcap
    await _notify_log(
        f"🟢 **BUY {pos['symbol']}**  _via {src['name']}_\n"
        f"`{pos['ca']}`{gate_lines}\n"
        f"💰 mcap ${_mcap_live:,.0f} | {fmt.liq(liq)} | {pos['pool_type']}\n"
        f"🌐 sosmed: {soc_line}\n"
        f"masuk **${pos['usd_spent']:.2f}** ({pos['eth_spent']:.6f} ETH) → {pos['tokens_total']:,.0f} {pos['symbol']}\n"
        f"entry ${pos['entry_price_usd']:.10f}{caller_line}{hp}{agent_line}",
        buttons=[
            [Button.inline("📉 Sell 50%", f"sell:{pid}:50"),
             Button.inline("🚨 Sell ALL", f"sell:{pid}:100")],
            [Button.url("📊 Chart", chart)] + ([Button.url("🔍 Tx", txurl)] if txurl else []),
        ],
    )
    return "bought"
