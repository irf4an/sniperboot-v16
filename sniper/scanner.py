"""🔭 Scanner (v16 GABUNGAN) — SUMBER SINYAL KEDUA di samping listener grup.

Di versi gabungan ini bot punya DUA telinga dalam SATU proses & SATU database:
listener (menunggu CA di-call di grup/channel Telegram) dan scanner ini, yang
MEMINDAI coin langsung dari chain/pasar: kandidat dari EMPAT sumber event
on-chain — TokenLaunched Pons V2 (launchpad terbesar chain ini), Initialize
PoolManager Uniswap v4, PoolCreated factory Uniswap v3, PairCreated factory
Uniswap v2 — + profil/boost terbaru Dexscreener (pelengkap), saring pakai
filter pasar deterministik,
peringkatkan pakai skor sederhana, lalu serahkan yang terbaik ke pipa yang
sama persis dengan listener (listener.process_ca, allow_buy=True).

⚠️ Eksekusi buy diatur MODE GLOBAL, sama seperti sinyal dari grup:
agent_dry_run=1 (default) → keputusan BUY Mata berhenti jadi notif
🧪 MODE LATIHAN, tidak dieksekusi. agent_dry_run=0 → buy DIEKSEKUSI
beneran, termasuk jalur fallback rule-based bila Mata mati/gagal. Mekanisme
pagar allow_buy=False di process_ca tetap tersedia, tapi scanner gabungan
tidak memakainya (dulu di v14 scanner terkunci keras; itu sudah lewat).
Kedua sumber berbagi dedupe global per CA: siapa datang duluan yang tercatat.
Lihat README_GABUNGAN.md.
"""
import asyncio
import math
import time

from . import config, db, listener
from . import dexscreener as dx
from .hexutil import topic_addr

# --- Robinhood Chain mainnet (chainId 4663) ---
# Alamat kontrak sumber event & quote asset utama di chain ini.
V4_POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
WETH_ADDR = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG_ADDR = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
QUOTE_ASSETS = {WETH_ADDR.lower(), USDG_ADDR.lower()}
# Di Uniswap v4, ETH natif direpresentasikan alamat nol (sentinel) —
# untuk penilaian pasangan ia berkedudukan sama seperti WETH (quote).
NATIVE_SENTINEL = "0x0000000000000000000000000000000000000000"
QUOTE_ASSETS_V4 = QUOTE_ASSETS | {NATIVE_SENTINEL}
V4_INITIALIZE_SIG = "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
MAX_BLOCKS_PER_CYCLE = 2000

# Statistik siklus terakhir (diisi collect_and_rank, dibaca scanner_loop/test)
_last_stats = {"kandidat": 0, "sudah_dilihat": 0, "lolos": 0}

# Retry kandidat antrean pending (in-process): CA → waktu evaluasi terakhir.
# Throttle 20 detik supaya koin yang masih muda tidak memanggil RPC/safety
# tiap siklus 5 detik selama 10 menit penuh.
_retry_last: dict = {}
_RETRY_THROTTLE_SEC = 20.0

# Cap keputusan per jam: stempel waktu keputusan dalam proses ini saja.
# Sengaja in-memory — restart proses = cap mulai dari nol lagi (lihat docs).
_decision_ts = []


# ==================== parser event (fungsi murni) ====================
def parse_v4_initialize_log(log):
    """Dari log Initialize PoolManager Uniswap v4 → CA token non-quote.

    Initialize(bytes32 id, address currency0, address currency1, ...):
    yang ter-indeks adalah id (topics[1]), currency0 (topics[2]),
    currency1 (topics[3]). Pasangan bisa berdenominasi ETH natif
    (currency = alamat nol) — diperlakukan sebagai quote. Aturan sisi
    sama seperti v3: tepat satu sisi non-quote, kalau tidak → None.
    Fungsi murni — bisa di-unit-test tanpa jaringan.
    """
    try:
        topics = log.get("topics") if isinstance(log, dict) else getattr(log, "topics", None)
        if not topics or len(topics) < 4:
            return None
        c0 = topic_addr(topics[2])
        c1 = topic_addr(topics[3])
    except Exception:
        return None
    non_quote = [t for t in (c0, c1) if t not in QUOTE_ASSETS_V4]
    return non_quote[0] if len(non_quote) == 1 else None


def _fetch_event_candidates(w3, label, address, sig, watermark_key, parser) -> list:
    """Mesin generik SATU sumber event on-chain (semua factory/launchpad).

    Pola yang sama untuk tiap sumber: watermark per-sumber disimpan di
    settings (maks MAX_BLOCKS_PER_CYCLE blok per siklus). Pertama kali
    jalan (watermark 0) watermark dilompatkan ke head — TIDAK memindai
    riwayat chain dari genesis. Alamat kontrak selalu dikonversi ke
    checksum sebelum get_logs — web3.py menolak alamat huruf-kecil,
    dan bug itulah yang dulu mematikan sumber v3 (baru ketahuan dari
    log lapangan 2026-10-21). Error satu sumber diisolasi: dicatat
    sebagai "[scanner] <label> gagal (diabaikan)" dan sumber lain
    tetap berjalan. Tidak pernah raise.
    """
    try:
        if w3 is None:
            return []
        from web3 import Web3
        latest = int(w3.eth.block_number)
        last = int(db.get(watermark_key))
        if last <= 0:
            db.set_(watermark_key, str(latest))
            print(f"[scanner] {label}: watermark dimulai dari blok {latest}")
            return []
        from_block = last + 1
        if from_block > latest:
            return []
        to_block = min(latest, from_block + MAX_BLOCKS_PER_CYCLE - 1)
        topic0 = Web3.keccak(text=sig).hex()
        if not topic0.startswith("0x"):
            topic0 = "0x" + topic0
        logs = w3.eth.get_logs({
            "address": Web3.to_checksum_address(address),
            "topics": [topic0],
            "fromBlock": from_block,
            "toBlock": to_block,
        })
        out = []
        for log in logs:
            ca = parser(log)
            if ca and ca not in out:
                out.append(ca)
        db.set_(watermark_key, str(to_block))
        return out
    except Exception as e:
        print(f"[scanner] {label} gagal (diabaikan): {e}")
        return []


# Hanya Uniswap v4 (venue meme utama chain ini, ~96%+).
# Uniswap v3 (sarang jebakan tick range), v2, dan Pons dinonaktifkan.
_EVENT_SOURCES = [
    ("factory-v4", V4_POOL_MANAGER, V4_INITIALIZE_SIG,
     "scanner_last_block_v4", parse_v4_initialize_log),
]


def fetch_multipool_candidates(w3) -> list:
    """Kandidat dari SEMUA sumber event on-chain, digabung unik.

    Sumber: Pons V2 (TokenLaunched), Uniswap v4 (Initialize di
    PoolManager), Uniswap v3 (PoolCreated), Uniswap v2 (PairCreated).
    Kegagalan satu sumber tidak menggugurkan yang lain (tiap sumber
    diisolasi di _fetch_event_candidates). w3 None → []."""
    if w3 is None:
        return []
    out = []
    for label, address, sig, key, parser in _EVENT_SOURCES:
        for ca in _fetch_event_candidates(w3, label, address, sig, key, parser):
            if ca not in out:
                out.append(ca)
    return out


def _make_w3():
    """Web3 read-only dari RPC_URL .env. Gagal → None."""
    try:
        from web3 import Web3
        return Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 15}))
    except Exception as e:
        print(f"[scanner] web3 tidak tersedia: {e}")
        return None


# ==================== FILTER PASAR & SKOR ====================
def _chg(pair, tf):  # helper: perubahan harga per timeframe (%)
    return float(((pair.get("priceChange") or {}).get(tf)) or 0)


def market_ok(pair) -> tuple:
    """Pra-filter deterministik MURAH (sebelum safety & Mata yang mahal).

    Dua pagar anti-pucuk v16.3:
    1. Umur pair: hanya koin segar <= scanner_max_age_min (default 15 menit).
       Koin tua yang sudah pumping (kasus McRib: 40 menit, h1 +803%) TOLAK.
    2. Pagar keras MARKET CAP: mcap > scanner_mcap_max (default $45,000)
       → TOLAK (pucuk_mcap). Persentase pump h1/m5 TIDAK dipakai menolak
       koin baru — koin baru lahir dengan harga mikroskopis, satu beli
       awal $50-$100 bikin % pool loncat +250..+390% secara alami.

    Jalur koin BARU (umur <= 15 menit / created kosong tapi momentum wajar):
    TIDAK wajib vol_h1 >= 1000 atau txns_h1 >= 20 (koin segar otomatis
    kecil vol/tx-nya). Cukup: liq >= scanner_min_liq DAN ada aktivitas
    beli (buys m5 >= 1 atau total txns m5 >= 2).

    Return (lolos, alasan) — alasan diawali kode pendek (liq_rendah/
    terlalu_tua/pucuk_mcap/beli_sepi) supaya
    gampang dihitung per tahap di test.
    """
    liq = dx.liq_usd_or_none(pair)
    if liq is None:
        # Dexscreener likuiditas null (sering terjadi pool v4, pairAddress =
        # PoolId). JANGAN anggap 0. Baca likuiditas ON-CHAIN StateView —
        # tapi fungsi ini murni/cheap tanpa RPC. Solusi: kalau null dan tidak
        # ada RPC, teruskan ke gate likuiditas v4 yang lebih keras di
        # listener.process_ca (yang menolak keras kalau on-chain 0).
        if dx.pool_type(pair) != "v4":
            # Perilaku lama: 0 → dianggap liq_rendah (bukan pool v4).
            if dx.liq_usd(pair) < db.get("scanner_min_liq"):
                return False, f"liq_rendah (${dx.liq_usd(pair):,.0f} < ${db.get('scanner_min_liq'):,.0f})"
        # v4: teruskan dengan alasan liq_onchain_check
    elif liq < db.get("scanner_min_liq"):
        return False, f"liq_rendah (${liq:,.0f} < ${db.get('scanner_min_liq'):,.0f})"

    # ---- Pagar 1: umur ----
    max_age = db.get("scanner_max_age_min")
    created = pair.get("pairCreatedAt")
    age_min = None
    if created:
        try:
            age_min = (time.time() * 1000 - float(created)) / 60000.0
        except (TypeError, ValueError):
            age_min = None
    if max_age > 0 and age_min is not None and age_min > max_age:
        return False, f"terlalu_tua ({age_min:.0f} mnt > {max_age:.0f} mnt)"

    is_new = (age_min is None) or (age_min <= max_age if max_age > 0 else age_min <= 15)

    # ---- Pagar anti-pucuk (v16.3): pagar MARKET CAP, bukan persentase ----
    # Koin baru lahir dengan harga mikroskopis; satu beli awal $50-$100
    # langsung bikin % pool di Dexscreener loncat +250..+390%. Persentase
    # h1/m5 karenanya TIDAK dipakai untuk menolak koin baru. Penggantinya:
    # pagar keras market cap — kalau mcap sudah > scanner_mcap_max
    # (default $45k), itu pucuk sesungguhnya, ditolak.
    mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
    mcap_max = db.get("scanner_mcap_max")
    if mcap > 0 and mcap > mcap_max:
        return False, f"pucuk_mcap (${mcap:,.0f} > ${mcap_max:,.0f})"

    if is_new:
        # ---- Jalur koin baru: jangan pakai syarat vol_h1/txns_h1 ----
        m5 = ((pair.get("txns") or {}).get("m5") or {})
        buys_m5 = int(m5.get("buys") or 0)
        sells_m5 = int(m5.get("sells") or 0)
        if buys_m5 < 1 and (buys_m5 + sells_m5) < 2:
            return False, f"beli_sepi (buys_m5={buys_m5}, txns_m5={buys_m5 + sells_m5})"
        return True, "ok_baru"

    # ---- Jalur koin umum (created kosong & h1 pump < pagar dianggap baru;
    #      jalur ini hanya terpakai kalau max_age=0 yaitu umur diabaikan) ----
    vol = float((pair.get("volume") or {}).get("h1") or 0)
    if vol < db.get("scanner_min_vol_h1"):
        return False, f"vol_rendah (${vol:,.0f} < ${db.get('scanner_min_vol_h1'):,.0f})"
    h1 = (pair.get("txns") or {}).get("h1") or {}
    n_tx = int(h1.get("buys") or 0) + int(h1.get("sells") or 0)
    if n_tx < db.get("scanner_min_txns_h1"):
        return False, f"txns_sepi ({n_tx} < {db.get('scanner_min_txns_h1'):.0f})"
    return True, "ok"


def score_pair(pair) -> float:
    """Skor peringkat (heuristik sederhana, sengaja transparan):
    log10 volume 1 jam + log10 likuiditas + rasio beli + bonus MOMENTUM M5.

    Bonus momentum (v16.2, early-momentum sniping):
    - chg_m5 di rentang manis +5%..+30% → +0.5 (tren baru mulai, belum pucuk).
    - rasio beli m5 >= 70% → +0.5 (akumulasi segar, bukan distribusi).
    Pagar kerasnya sudah di market_ok; di sini hanya memberi PRIORITAS lebih
    tinggi ke koin yang momentumnya baru mulai.
    """
    vol = float((pair.get("volume") or {}).get("h1") or 0)
    liq = dx.liq_usd_or_none(pair)
    if liq is None:
        liq = dx.liq_usd(pair)  # 0 → log10(1+0)=0, skor netral, bukan NaN
    h1 = (pair.get("txns") or {}).get("h1") or {}
    buys = int(h1.get("buys") or 0)
    sells = int(h1.get("sells") or 0)
    buy_ratio = buys / (buys + sells) if (buys + sells) > 0 else 0.5
    score = math.log10(1 + vol) + math.log10(1 + liq) + buy_ratio

    chg_m5 = _chg(pair, "m5")
    if 5.0 <= chg_m5 <= 30.0:
        score += 0.5  # tren baru mulai — rentang manis early momentum
    m5 = ((pair.get("txns") or {}).get("m5") or {})
    b5 = int(m5.get("buys") or 0)
    s5 = int(m5.get("sells") or 0)
    if (b5 + s5) > 0 and b5 / (b5 + s5) >= 0.7:
        score += 0.5  # akumulasi segar
    return score


def collect_and_rank() -> list:
    """Satu putaran pengumpulan: kandidat → buang yang sudah pernah dilihat →
    ambil pair → filter pasar → peringkat skor menurun.

    Return list of {"ca", "pair", "score"}. Statistik ringkas siklus disimpan
    di _last_stats. Fungsi sinkron (requests) — panggil via asyncio.to_thread.
    """
    cands_profiles = dx.profile_candidates()
    # antrean pending: CA yang terdeteksi on-chain tapi Dexscreener belum
    # selesai mengindeks pair-nya (indeks bisa molor 1-3 menit). Tanpa
    # antrean ini CA langsung dibuang saat best_pair -> None dan hilang
    # selamanya. Tahan di antrean sampai best_pair mengembalikan pair, atau
    # kedaluwarsa (> 600 detik) lalu dibersihkan.
    #
    # v16.9: antrean ini juga menampung koin yang SUDAH terindeks tapi masih
    # gagal pra-filter (liq kecil karena baru lahir, dsb). Dulu CA-nya
    # dikeluarkan sebelum filter → hilang permanen dan tidak pernah dicek
    # lagi saat likuiditasnya naik.
    pending_now: dict = {}
    if db.get("scanner_factory_on", str) == "1":
        new_onchain = fetch_multipool_candidates(_make_w3())
        for ca in new_onchain:
            if not db.seen_call(ca):
                db.add_pending_pool(ca)
    # bersihkan antrean yang kedaluwarsa (> 600 detik / 10 menit)
    db.prune_pending_pools(max_age_sec=600)
    for p in db.get_pending_pools(max_age_sec=600):
        pending_now[p["ca"]] = p
    cands = list(dict.fromkeys(cands_profiles + list(pending_now)))
    _last_stats["kandidat"] = len(cands)
    _last_stats["sudah_dilihat"] = 0
    _last_stats["ditahan"] = 0
    out = []
    now = time.time()
    for ca in cands:
        seen = db.seen_call(ca)
        in_pending = ca in pending_now
        if seen and not in_pending:
            _last_stats["sudah_dilihat"] += 1
            continue
        if in_pending and now - _retry_last.get(ca, 0.0) < _RETRY_THROTTLE_SEC:
            # Koin di antrean (termasuk yang sudah pernah masuk listener)
            # tidak perlu dicek tiap siklus 5 detik: cukup tiap 20 detik.
            # Hemat kuota Dexscreener + RPC, tanpa mengubah peluang entry
            # (gate umur minimum 10 detik, jadi jeda 20 detik tetap aman).
            continue
        try:
            pair = dx.best_pair(ca)
            _retry_last[ca] = now
        except Exception as e:
            _retry_last[ca] = now
            print(f"[scanner] best_pair gagal {ca}: {e}")
            continue
        if not pair:
            # Dexscreener belum selesai indeks. Biarkan CA di antrean pending
            # supaya dicek lagi siklus berikutnya — catat retry-nya.
            db.increment_pending_retry(ca)
            continue
        ok, reason = market_ok(pair)
        if not ok:
            # Belum layak TAPI belum tentu mati (koin baru: liq masih naik).
            # Simpan di antrean sampai kedaluwarsa, jangan buang diam-diam.
            if not in_pending and dx.pool_type(pair) == "v4":
                db.add_pending_pool(ca)
            _last_stats.setdefault("ditahan", 0)
            _last_stats["ditahan"] += 1
            continue
        # lolos pra-filter: keluarkan dari antrean HANYA kalau gate akhir
        # (listener) juga tidak lagi bisa berubah — lihat scanner_loop.
        _retry_last[ca] = now
        out.append({"ca": ca, "pair": pair, "score": score_pair(pair),
                    "retry": seen, "in_pending": in_pending})
    out.sort(key=lambda r: r["score"], reverse=True)
    _last_stats["lolos"] = len(out)
    return out


# ==================== CAP PER JAM ====================
def cap_left(now=None) -> int:
    """Sisa kuota BUY scanner dalam 1 jam terakhir (in-process).

    Setting `scanner_max_per_hour <= 0` (default "0") = UNLIMITED:
    evaluasi/skip scanner tidak pernah dibatasi, hanya cap eksekusi BUY.
    """
    max_h = int(float(db.get("scanner_max_per_hour") or 0))
    if max_h <= 0:
        return 999999  # 0 atau negatif = UNLIMITED (batasan non-aktif)
    now = now if now is not None else time.time()
    cutoff = now - 3600
    while _decision_ts and _decision_ts[0] < cutoff:
        _decision_ts.pop(0)
    return max(0, max_h - len(_decision_ts))


def _note_decision(now=None):
    _decision_ts.append(now if now is not None else time.time())


# ==================== SOURCE & LOOP ====================
def get_scanner_source() -> dict:
    """Baris source sintetis "SCANNER" (dibuat kalau belum ada).

    chat_id=0 tidak akan pernah cocok dengan grup Telegram sungguhan, jadi
    source ini murni label asal-usul buat calls/stats — bukan target listener.
    mcap_max selalu dibaca segar dari setting scanner_mcap_max.
    """
    mcap_max = float(db.get("scanner_mcap_max"))
    for s in db.sources():
        if (s.get("name") or "").upper() == "SCANNER":
            return {"id": s["id"], "name": s["name"], "mcap_max": mcap_max}
    db.add_source(0, 0, "SCANNER", mcap_max)
    for s in db.sources():
        if (s.get("name") or "").upper() == "SCANNER":
            return {"id": s["id"], "name": s["name"], "mcap_max": mcap_max}
    return {"id": 0, "name": "SCANNER", "mcap_max": mcap_max}


# Status penolakan yang masih bisa berubah kalau koin dibiarkan matang:
# umur (<10s), likuiditas kecil yang sedang naik, rasio liq/mcap, dan
# quoter yang gagal sesaat. Selain ini dianggap final.
_RETRYABLE_PREFIXES = ("skip_fast:age_", "skip_fast:liq_", "skip_fast:quote_gagal",
                       "filtered:liq")


def _retryable_status(status: str) -> bool:
    return str(status or "").startswith(_RETRYABLE_PREFIXES)


async def scanner_loop():
    """Loop utama scanner (gabungan). Jalan keluar ke pipa:
    listener.process_ca(..., allow_buy=True) — eksekusi diatur mode global
    (agent_dry_run dst.), persis seperti sinyal dari grup Telegram."""
    print("[scanner] loop mulai — GABUNGAN: scanner = sumber sinyal kedua "
          "di samping listener grup (eksekusi mengikuti mode global; "
          "agent_dry_run default ON)")
    while True:
        try:
            if db.get("scanner_on", str) != "1":
                await asyncio.sleep(30)
                continue
            interval = max(3, int(db.get("scanner_interval_sec")))
            if listener._notify is None or listener._trader is None:
                # listener.register belum dipanggil → tidak ada pipa buat
                # memproses. Lewati siklus ini.
                print("[scanner] pipa belum ter-register — siklus dilewati")
                await asyncio.sleep(interval)
                continue
            if db.get("agent_dry_run", str) != "1":
                print("[scanner] ⚠️ agent_dry_run OFF — mode LIVE: keputusan BUY "
                      "dari scanner akan DIEKSEKUSI beneran")
            src_row = get_scanner_source()
            ranked = await asyncio.to_thread(collect_and_rank)
            # ---------- 🧵 PARALEL SEMAPHORE (v16.4) ----------
            # Maks scanner_batch_max koin TERATAS per siklus, dievaluasi
            # bersamaan dengan maks scanner_concurrency task serentak.
            # Tanpa ini koin nomor 2-3 antre 10-20 detik di belakang koin
            # nomor 1 (= 50-100 blok di Robinhood Chain — sudah dipompa
            # orang saat giliran tiba).
            batch = ranked[:min(int(db.get("scanner_batch_max") or 2),
                                cap_left())]
            sem = asyncio.Semaphore(int(db.get("scanner_concurrency") or 2))

            async def _eval(item):
                async with sem:
                    status = await listener.process_ca(
                        item["ca"], src_row, None, "SCANNER", allow_buy=True,
                        retry=bool(item.get("retry")))
                    if status in ("bought", "dry_run_simulated"):
                        _note_decision()
                    # Keluarkan dari antrean pending hanya kalau hasilnya
                    # final. Penolakan "masih terlalu dini" (umur/likuiditas)
                    # dibiarkan sampai kedaluwarsa supaya ada kesempatan
                    # dievaluasi lagi saat liq naik.
                    if item.get("in_pending") and not _retryable_status(status):
                        db.remove_pending_pool(item["ca"])
                    return item["ca"], status

            results = await asyncio.gather(*[_eval(it) for it in batch])
            processed = 0
            for ca_addr, st in results:
                if st != "dup":
                    processed += 1
                print(f"[scanner] {ca_addr} -> {st}")
            print(f"[scanner] kandidat={_last_stats['kandidat']} "
                  f"ditahan={_last_stats.get('ditahan', 0)} "
                  f"lolos={len(ranked)} diproses={processed}")
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[scanner] siklus error (loop lanjut): {e}")
            await asyncio.sleep(30)
