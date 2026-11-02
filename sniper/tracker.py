"""🕵️ INSIDER TRACKER (v16.1/v16.8) — intel insider on-chain, ALERT-ONLY.

Sumber sinyal KETIGA HoodSniper: memantau transaksi blok baru di Robinhood
Chain (chainId 4663), mengecek apakah pengirimnya wallet insider (hasil
screening tools/screen_insiders.py, tersimpan di tabel insider_wallets),
lalu kalau insider itu menerima token hasil BUY (event ERC20 Transfer) →
Kirim NOTIF TELEGRAM RINGKAS (wallet, simbol+CA, mcap/liq, chart) seperti
intel pasar — TANPA masuk pipa listener.

Mode setting `insider_alert_only`:
  "1" (DEFAULT) = ALERT-ONLY — tidak ada posisi baru, tidak ada evaluasi
      buy, tidak print listener. Notif via listener._notify / bind_notify.
  "0" = perilaku LAMA — CA diteruskan ke pipa bot yang sama
      (listener.process_ca, allow_buy=True).

Desain:
- Async loop INDEPENDEN (task background bot.py), on/off via setting tracker_on.
- Deteksi: eth_getLogs batch event ERC20 Transfer ke wallet insider
  (TRANSFER_TOPIC), bukan lagi scan per-blok router selector.
- Anti-lag: satu call getLogs menyapu BATCH_MAX_BLOCKS blok; kalau
  tertinggal > CATCHUP_LIMIT blok → fast-forward.
- Aman diganggu RPC gagal: try/except + sleep; loop tidak mati.
"""
from __future__ import annotations

import asyncio
import time
import threading

from . import config, db, listener, fmt
from . import dexscreener as dx

# ============================ KONSTANTA ============================


# Quote assets Robinhood Chain — dipakai buat membedakan tokenIn vs tokenOut
WETH  = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73".lower()
USDG  = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168".lower()
NATIVE_SENTINEL = "0x0000000000000000000000000000000000000000"
QUOTE_ASSETS = frozenset({WETH, USDG, NATIVE_SENTINEL})

# Buat proteksi slippage: bandingkan harga pasar Dexscreener sebelum vs
# sesudah stagger INSIDER processing — jika harga lari > MAX_SLIP_PCT%
# dalam selang ini, kemungkinan insider buy di puncak momentum.
MAX_SLIP_PCT = 5.0

# Kalau insider membeli lalu harga langsung anjlok, jangan kejar.
# (sudah tertangani listener.process_ca staleness gate + safety filter)

POLL_IDLE = 0.5     # dtk kalau tidak ada blok baru (Robinhood Chain ~10 blok/dtk)

# ============ BATCH LOG SCAN (anti-lag) ============
# Node publik Robinhood Chain menolak WSS (Cloudflare/Envoy HTTP 400) dan
# eth_get_block per-blok lambat (~270ms) → tertinggal 7 blok/dtk. Solusi:
# eth_getLogs batch — 50–300 blok dalam 1 RPC call (~250ms). Filter topic
# ERC20 Transfer(address indexed from, address indexed to) dengan to =
# wallet insider. Event = insider menerima/membeli token asli (BUKAN quote).
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55"
                  "a4df523b3ef")
BATCH_MAX_BLOCKS = 100   # sapu maks 100 blok per siklus batch
CATCHUP_LIMIT = 500      # kalau tertinggal > 500 blok → fast-forward
CATCHUP_KEEP = 50        # ...tapi tetap pindai 50 blok terakhir
POLL_INTERVAL = 2.5      # 2.5s ≈ ~25 blok baru di Robinhood Chain

# ============================ PIPA ============================

def _src() -> dict:
    """Sumber sinyal untuk insider tracker — memakai sumber sinyal scanner
       kalau ada; kalau tidak, buat source sintetis src dict."""
    for s in db.sources(only_enabled=True):
        if (s.get("name") or "").startswith("Scanner"):
            return s
    return {"id": None, "chat_id": 0, "topic_id": 0,
            "name": "INSIDER_TRACKER", "mcap_max": 50000}


def _insider_alert_text(pair, ca, wallet_label: str,
                        extra_buys: int = 0, dedup_window_sec: int = 0) -> str:
    """Teks notif alert insider — maks 6 baris, tanpa emoji jualan.

    extra_buys > 0 → baris wallet diberi penghitung dedup:
    "(+N beli lagi dalam 10m)" — notif mini gabungan, bukan alert penuh.
    """
    sym = (pair.get("baseToken") or {}).get("symbol", "?") if pair else "?"
    mcap = dx.mcap_of(pair) if pair else 0
    liq = dx.liq_usd_or_none(pair) if pair else None
    pair_addr = (pair or {}).get("pairAddress", "")
    chain = (pair or {}).get("chainId") or "robinhood"
    chart = f"https://dexscreener.com/{chain}/{pair_addr}" if pair_addr else ""
    wallet_line = f"wallet `{wallet_label}`"
    if extra_buys > 0:
        window_txt = "?"
        if dedup_window_sec:
            if dedup_window_sec % 3600 == 0:
                window_txt = f"{dedup_window_sec // 3600}h"
            elif dedup_window_sec % 60 == 0:
                window_txt = f"{dedup_window_sec // 60}m"
            else:
                window_txt = f"{dedup_window_sec}s"
        wallet_line = f"wallet `{wallet_label}` (+{extra_buys} beli lagi dalam {window_txt})"
    lines = [
        "🕵️ **INSIDER BUY — {}**".format(sym),
        wallet_line,
        f"`{ca}`",
        f"📊 mcap {fmt.usd_compact(mcap, '?')} · liq {fmt.usd_compact(liq, '?') if liq is not None else 'n/a'}",
    ]
    if chart:
        lines.append(f"📈 {chart}")
    return "\n".join(lines)


def _deliver_alert(text: str):
    """Kirim alert via listener._notify bila ada; fallback → callback global
    tracker (bind_notify dari bot.py). Detached task — tracker loop tidak
    menunggu notif selesai. Tidak boleh raise."""
    cb = getattr(listener, "_notify", None) or _notify_cb
    if cb is None:
        print(f"[insider] (alert tak terkirim — notify belum terikat)\n{text}")
        return
    try:
        asyncio.create_task(cb(text))
    except Exception as e:
        print(f"[insider] notify fail: {e}")


_INSIDER_RETRY_DELAY = 30.0  # detik — retry best_pair sebelum alert


# ==================== DEDUP ALERT (wallet, CA) ====================
# State process-level: (wallet_lower, ca_lower) -> (last_alert_ts, extra_count).
# Dedup dihitung hanya untuk alert yang BENAR-BENAR dikirim ke Telegram
# (bukan yang dibiyangkan best_pair None).(dict plain aman: tracker jalan
# satu loop asyncio; lock hanya untuk task background/retry.)
_alert_dedup: dict = {}
_alert_dedup_lock = threading.Lock()


def _dedup_window_sec() -> int:
    """Jendela dedup (detik) dari setting db. 0/negatif/berisik → 0 (mati)."""
    try:
        v = int(db.get("insider_alert_dedup_window_sec", str) or "600")
    except Exception:
        v = 600
    return v if v > 0 else 0


def _dedup_check_send(wallet: str, ca: str, pair, now=None):
    """Dedup per-CA koin (1 koin = maks 1 notif chat per jendela) + catat ke DB
    insider_buys agar frekuensi beli tiap koin bisa dipantau lewat tombol Log Beli.
    """
    try:
        win = _dedup_window_sec()
        ts = (now if now is not None else time.time())
        try:
            sym = ((pair or {}).get("baseToken") or {}).get("symbol") or "?"
            db.record_insider_buy(
                ca, wallet, symbol=sym,
                mcap=dx.mcap_of(pair) if pair else 0,
                liq=dx.liq_usd_or_none(pair) or 0 if pair else 0,
                pair_address=(pair or {}).get("pairAddress") or "",
                ts=int(ts))
            # ---- 🤖 Sybil Guard: cek apakah transaksi ini memicu klaster wash-trading ----
            if (db.get("sybil_cluster_guard_on", str) or "1") == "1":
                clusters = db.detect_and_purge_sybil_clusters(ca=ca)
                if clusters:
                    cl = clusters[0]
                    print(f"[sybil-guard] 🚨 Klaster wash-trading terdeteksi di {cl['symbol']} ({ca[:10]}…): "
                          f"{len(cl['wallets'])} dompet dalam {cl['span_sec']}s diblokir & dibuang!")
                    return None
        except Exception:
            pass
        key = (ca or "").lower()
        extra = 0
        with _alert_dedup_lock:
            state = _alert_dedup.get(key)
            if win and state and ts - state[0] < win:
                _alert_dedup[key] = (state[0], state[1] + 1)
                return None
            if state:
                extra = state[1]
            if len(_alert_dedup) > 500:  # ponytail: prune murah, tanpa thread
                for k in [k for k, v in _alert_dedup.items()
                          if ts - v[0] >= max(win, 1)]:
                    del _alert_dedup[k]
            _alert_dedup[key] = (ts, 0)
        labels = db.insider_labels({wallet.lower()} if wallet else set())
        return _insider_alert_text(
            pair, ca, fmt.short_addr(wallet, labels),
            extra_buys=extra if win else 0, dedup_window_sec=win)
    except Exception as e:
        print(f"[insider] dedup check fail: {e}")
        labels = db.insider_labels({(wallet or "").lower()} if wallet else set())
        return _insider_alert_text(pair, ca, fmt.short_addr(wallet, labels))


async def _alert_after_retry(token_ca: str, insider_addr: str,
                             delay: float = 0):
    """Alert-only: ulangi dx.best_pair 1x setelah `delay` detik.

    Dijalankan Di TASK BACKGROUND — tracker loop tidak pernah memblok.
    Kalau pair ketemu setelah retry → alert normal dikirim ke Telegram.
    Kalau masih None → diem saja (log forensik). Tidak boleh raise.
    """
    try:
        await asyncio.sleep(_INSIDER_RETRY_DELAY if not delay else delay)
        try:
            pair = await asyncio.to_thread(dx.best_pair, token_ca)
        except Exception:
            pair = None
        if not pair:
            print(f"[insider] retry best_pair masih None: {token_ca} "
                  f"tetap tak ter-index Dexscreener — alert dibatalkan "
                  f"(wallet {insider_addr or '?'}, hanya di log).")
            return
        labels = db.insider_labels(
            {insider_addr.lower()} if insider_addr else set())
        wallet_label = fmt.short_addr(insider_addr, labels)
        text = _dedup_check_send(insider_addr, token_ca, pair)
        if text is None:
            print(f"[insider] dedup: {token_ca[:10]}… ditahan (retry)")
            return
        print(f"[insider] ALERT (alert-only, retry {delay:.0f}s): "
              f"{wallet_label} beli {token_ca[:10]}… — dikirim ke "
              f"Telegram, TIDAK masuk pipa.")
        _deliver_alert(text)
    except asyncio.CancelledError:
        pass  # shutdown — biarkan
    except Exception as e:
        print(f"[insider] retry alert fail: {e}")


async def process_insider_buy(token_ca: str, insider_addr: str = "",
                              notify=None, pair=None):
    """Insider membeli token (dideteksi _inspect_logs).

    Mode insider_alert_only=1 (DEFAULT): kirim notif Telegram RINGKAS
    (wallet, simbol+CA, mcap/liq, chart) — TANPA masuk pipa listener
    (0 evaluasi buy, 0 posisi baru, 0 RPC tambahan: `pair` dioper dari
    pemanggil yang sudah mengambilnya).

    Mode insider_alert_only=0: perilaku LAMA — CA diteruskan ke
    listener.process_ca (allow_buy=True, siklus penuh sinyal scanner).

    `pair` opsional: respons dx.best_pair dari caller (hemat 1 fetch).
    """
    alert_only = (db.get("insider_alert_only", str) or "1") == "1"
    if not alert_only:
        # ---------- Perilaku lama (fallback aman) ----------
        src = _src()
        print(f"[insider] CA to pipeline: {token_ca}")
        caller_id = insider_addr.lower() if insider_addr else None
        caller_name = (f"INSIDER:{insider_addr[:10]}"
                       if insider_addr else "INSIDER_TRACKER")
        # Pipa listener.process_ca async — panggil via task supaya tracker
        # tidak memblokir loop pemantauan blok.
        asyncio.create_task(listener.process_ca(
            token_ca, src, caller_id=caller_id, caller_name=caller_name,
            allow_buy=True))
        return
    # ---------- Alert-only (v16.8, KORI 1) ----------
    if pair is None:
        try:
            pair = await asyncio.to_thread(dx.best_pair, token_ca)
        except Exception:
            pair = None
    if pair is None:
        # Token tak ter-index Dexscreener (launch baru di v4 pool, transfer
        # langsung, dsb.) → alert akan berisik ('?', mcap $0, tanpa chart).
        # Jangan kirim — log forensik saja, lalu RETRY SEKALI dalam ~30s
        # di task background supaya loop tracker tidak terblok.
        print(f"[insider] best_pair None utk {token_ca} "
              f"(wallet {insider_addr or '?'}) — alert dibiyangkan; "
              f"retry 1x dalam {_INSIDER_RETRY_DELAY:.0f}s.")
        try:
            asyncio.create_task(
                _alert_after_retry(token_ca, insider_addr,
                                   _INSIDER_RETRY_DELAY))
        except RuntimeError as e:
            # Ev-lok luar loop utama — tanpa retry supaya aman.
            print(f"[insider] retry fail (no loop): {e} — "
                  f"CA {token_ca} wallet {insider_addr or '?'} tak teralert.")
        return
    labels = db.insider_labels({insider_addr.lower()} if insider_addr else set())
    wallet_label = fmt.short_addr(insider_addr, labels)
    text = _dedup_check_send(insider_addr, token_ca, pair)
    if text is None:
        print(f"[insider] dedup: {wallet_label} beli {token_ca[:10]}… "
              f"ditahan (sudah dialert dalam jendela)")
        return
    print(f"[insider] ALERT (alert-only): {wallet_label} beli "
          f"{token_ca[:10]}… — dikirim ke Telegram, TIDAK masuk pipa.")
    _deliver_alert(text)



# ============================ BATCH LOG SCAN ============================

def _insider_to_topic2(insiders: set) -> list:
    """Susun padded address 32-byte utk topic2 (address indexed `to`)."""
    return ["0x000000000000000000000000" + a.lower().replace("0x", "")
            for a in insiders]



async def _inspect_logs(w3, from_block: int, to_block: int) -> int:
    """Pindai [from_block..to_block] dalam SATU eth_getLogs call:
    cari event ERC20 Transfer ke wallet insider → pipa process_insider_buy.

    Return jumlah sinyal yang dikirim. Dedup per-call: satu token (CA)
    hanya dikirim sekali per siklus batch meski muncul di banyak log,
    dan event kecil token transfer (bukan swap router) tetap menuju pipa.
    """
    insiders = set(db.get_active_insiders())
    if not insiders:
        return 0

    padded = _insider_to_topic2(insiders)
    filt = {
        "fromBlock": from_block,
        "toBlock": to_block,
        "topics": [TRANSFER_TOPIC, None, padded],
    }
    try:
        logs = await asyncio.to_thread(w3.eth.get_logs, filt)
    except Exception as e:
        print(f"[tracker] get_logs [{from_block}..{to_block}] fail: {e}")
        return 0

    sent = 0
    seen_tokens = set()
    for log in (logs or []):
        token_ca = (log.get("address") or "").lower()
        if not token_ca or token_ca in QUOTE_ASSETS:
            continue          # WETH/USDG transfer = SELL insider / unwrap
        if token_ca in seen_tokens:
            continue          # dedup batch — cukup sinyal sekali per CA
        to_hex = log["topics"][2].hex() if hasattr(log["topics"][2], "hex") \
            else str(log["topics"][2])
        insider_addr = "0x" + to_hex[-40:].lower()
        if insider_addr not in insiders:
            continue
        # 🔒 slippage guard: harga sudah lari >MAX_SLIP_PCT% → skip
        pair = None
        try:
            pair = await asyncio.to_thread(dx.best_pair, token_ca)
            pc = (((pair or {}).get("priceChange") or {}).get("m5")) or 0.0
            if float(pc) > MAX_SLIP_PCT:
                print(f"[tracker] beta pusing: 'insider buy "
                      f"{token_ca[:10]}… skip — harga sudah +{pc:.1f}% "
                      f"dalam 5m (slippage guard)")
                continue
        except Exception:
            pass  # data harga gagal → jangan block-trade
        try:
            await process_insider_buy(token_ca, insider_addr=insider_addr,
                                      pair=pair)
            seen_tokens.add(token_ca)
            sent += 1
        except Exception as e:
            print(f"[tracker] process_insider_buy fail: {e}")
    return sent


# ============================ LOOP UTAMA ============================

async def tracker_loop():
    """Loop pemantauan blok — task background bot.py. Matikan pakai setting
       tracker_on=0 di DB; aktifkan dengan tracker_on=1. Restart-safe: last
       inspected block disimpan di setting tracker_last_block."""
    print("[tracker] INSIDER TRACKER loop dimulai")
    from web3 import Web3
    from web3.providers import HTTPProvider

    # Probe chain berulang — sampai provider siap (RPC bisa belum siap saat
    # bot.py baru jalan).
    await asyncio.sleep(1.0)
    w3 = None
    try:
        w3 = Web3(HTTPProvider(config.RPC_URL))
        chain_id = await asyncio.to_thread(lambda: w3.eth.chain_id)
        if chain_id != config.CHAIN_ID:
            print(f"[tracker] ⚠️ chain_id RPC = {chain_id}, "
                  f"harusnya {config.CHAIN_ID} (Robinhood Chain)")
    except Exception as e:
        print(f"[tracker] RPC belum siap: {e}")

    last_num = int(db.get("tracker_last_block") or 0)

    while True:
        try:
            if db.get("tracker_on", str) != "1":
                await asyncio.sleep(POLL_IDLE)
                continue
            if w3 is None:
                w3 = Web3(HTTPProvider(config.RPC_URL))
            latest = await asyncio.to_thread(lambda: w3.eth.block_number)
            if latest <= last_num:
                await asyncio.sleep(POLL_IDLE)
                continue

            # Proteksi kalau tertinggal terlalu jauh (baru start / bot mati
            # lama): fast-forward, tapi tetap pindai CATCHUP_KEEP blok akhir.
            if latest - last_num > CATCHUP_LIMIT:
                print(f"[tracker] tertinggal {latest - last_num} blok — "
                      f"fast-forward ke {latest - CATCHUP_KEEP}")
                last_num = latest - CATCHUP_KEEP

            # Batch scan: satu eth_getLogs mengecek hingga BATCH_MAX_BLOCKS
            # blok (anti-lag; benchmark riil 50–300 blok ≈ 250ms 1 RPC call).
            from_block = last_num + 1
            to_block = min(latest, from_block + BATCH_MAX_BLOCKS - 1)
            try:
                await _inspect_logs(w3, from_block, to_block)
            except Exception as e:
                print(f"[tracker] inspect_logs "
                      f"[{from_block}..{to_block}] fail: {e}")
            db.set_("tracker_last_block", to_block)
            last_num = to_block
            # 2.5s ≈ ~25 blok baru di Robinhood Chain (~10.6 blk/dtk)
            await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[tracker] loop error: {e}")
            w3 = None
            await asyncio.sleep(3.0)


# ---------------------------------------------------------------
# 5. AUTO-SCAN AWAL DI KONDISI INSTALASI BARU (tabel insider kosong)
# ---------------------------------------------------------------

ROTATE_INTERVAL_SEC = 21600  # 6 jam — percepat rotasi insider: memcoin exit liquiditas cepat


def bind_notify(notify):
    """Ikat callback Telegram (async fn(text)) dari bot.py supaya scan awal
       instalasi baru bisa melaporkan ringkasannya ke admin via Telegram."""
    global _notify_cb
    _notify_cb = notify


_notify_cb = None


async def _notify_async(text: str):
    cb = _notify_cb
    if cb is None:
        return
    try:
        await cb(text)
    except Exception as e:
        print(f"[tracker] notify fail: {e}")


async def _initial_scan_if_empty():
    """Panggil sekali di awal loop: kalau tabel insider kosong (instalasi
       baru / git clone pertama), langsung scan koin runner on-chain dan
       recruit insider awal — tracker tidak jalan upgrade sendirian."""
    active_count = len(db.get_active_insiders())
    if active_count > 0:
        return
    print("[tracker] 🕵️ Instalasi baru terdeteksi (0 insider aktif) — "
          "memicu scan awal runner on-chain sekarang...")
    await _notify_async(
        "🕵️ Instalasi baru terdeteksi (0 insider aktif) — "
        "scan awal runner on-chain dimulai...")
    try:
        from tools.rotate_insiders import rotate
        summary = await asyncio.to_thread(rotate, 15)
    except Exception as e:
        print(f"[tracker] scan awal gagal: {e}")
        await _notify_async(f"[tracker] scan awal gagal: {e}")
        return
    added = summary.get("added", 0)
    total = summary.get("total_active", 0)
    print(f"[tracker] 🔄 Scan awal selesai: +{added} wallet baru dijaring "
          f"(total aktif: {total})")
    await _notify_async(
        f"🔄 Scan awal selesai: +{added} wallet baru dijaring "
        f"(total aktif: {total})")


async def auto_rotation_loop(notify=None):
    """Loop background rotasi wallet insider tiap 6 jam (non-blocking):
    prune wallet boncos/lunglai + daftar insider baru dari koin runner
    terbaru. On/off via setting tracker_auto_rotate (default "1").

    Semua kerjaannya sync/network → asyncio.to_thread supaya loop blok
    tracker tidak sempat tertahan.
    """
    if notify is not None:
        bind_notify(notify)
    print("[tracker] 🔄 auto rotation loop dimulai (interval 6h)")
    try:
        await _initial_scan_if_empty()
    except Exception as e:
        print(f"[tracker] initial scan error: {e}")
    while True:
        try:
            if db.get("tracker_auto_rotate", str) == "1":
                from tools.rotate_insiders import rotate
                summary = await asyncio.to_thread(rotate)
                print(f"[tracker] 🔄 rotasi insider: {summary}")
        except Exception as e:
            print(f"[tracker] rotation error: {e}")
        await asyncio.sleep(ROTATE_INTERVAL_SEC)
