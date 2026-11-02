"""📡 Radar Pons V2 — EARLY ALERT ONLY (tidak pernah membeli).

Membaca event `TokenLaunched` dari factory Pons V2 di Robinhood Chain
(chainId 4663), menyaring sampah, lalu mengirim ALERT AWAL ke DM admin
SEBELUM token graduate ke Uniswap v4.

════════════════════════ BATAS KERAS ════════════════════════
Modul ini TIDAK PERNAH memanggil:
    • listener.process_ca()
    • trader.buy()
    • db.open_position() / tabel positions
Token di bonding curve BELUM punya pool v4 — kalau modul ini ikut pipa
beli, hasilnya membeli koin tanpa likuiditas. Karena itu radar punya
tabel sendiri (`pons_queue`), watermark sendiri (`pons_last_block`),
toggle sendiri (`pons_radar_on`), dan loop sendiri.

Alur satu siklus:
    getLogs TokenLaunched  (watermark pons_last_block)
      → INSERT OR IGNORE ke pons_queue        (dedup CA di PRIMARY KEY)
      → enrich paralel via RPC                (name / symbol / totalSupply)
      → skor heuristik                        (gibberish, klaster nama, dev)
      → tier LLM                              (verdict + skor) bila diaktifkan
      → skor ≥ pons_alert_min_score & cap/jam belum penuh
      → alert Telegram ke DM admin

Publik (opsi C) sengaja TERTUNDA: `digest_loop` hanya mengirim rekap per
jam, dan hanya bila `pons_radar_public=1`. Jendela 2–4 menit sebelum
graduasi tetap eksklusif DM admin.
"""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import agent, config, db

PONS_V2_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
PONS_LAUNCHED_SIG = "TokenLaunched(address,address,address,address,uint256,uint256)"
MAX_BLOCKS_PER_CYCLE = 2000
NAME_CLUSTER_WINDOW_SEC = 3600
_VOWELS = set("aeiou")

_w3_cache = {"w3": None, "ts": 0.0}
_w3_lock = threading.Lock()

_notify_cb = None
_public_notify_cb = None

# ==================== veto jalur beli (0 ms) ====================
# Listener memanggil `is_blacklisted(ca)` di awal fast-path. Set ini
# diperbarui `blacklist_loop` tiap 30 detik, jadi jalur beli TIDAK melakukan
# query DB per kandidat — cukup satu cek keanggotaan set di memori.
_blacklist = set()
_blacklist_ts = 0.0
BLACKLIST_REFRESH_SEC = 30


# ==================== plumbing notifikasi ====================
def bind_notify(notify, public_notify=None):
    """Pasang callback Telegram: `notify` = DM admin (real-time),
    `public_notify` = channel publik (hanya dipakai digest per jam)."""
    global _notify_cb, _public_notify_cb
    if notify is not None:
        _notify_cb = notify
    if public_notify is not None:
        _public_notify_cb = public_notify


# ==================== veto jalur beli ====================
def refresh_blacklist(force=False) -> int:
    """Muat ulang set veto dari DB. Return jumlah CA yang diblokir."""
    global _blacklist, _blacklist_ts
    now = time.time()
    if not force and _blacklist_ts and now - _blacklist_ts < BLACKLIST_REFRESH_SEC:
        return len(_blacklist)
    try:
        _blacklist = db.get_pons_blocked_cas()
        _blacklist_ts = now
    except Exception as e:
        print(f"[pons-radar] refresh blacklist gagal (pakai set lama): {e}")
    return len(_blacklist)


def is_blacklisted(ca) -> bool:
    """O(1). True = koin diveto Radar Pons saat masih di bonding curve.

    Dipanggil listener di awal fast-path SEBELUM gate matematika. Panggilan
    pertama per proses bisa memicu satu query DB (~1 ms) bila loop refresh
    belum sempat jalan; setelah itu murni lookup set.
    """
    if not _blacklist_ts:
        refresh_blacklist()
    return str(ca or "").strip().lower() in _blacklist


async def _send_alert(text, buttons=None):
    cb = _notify_cb
    if cb is None:
        print(f"[pons-radar] notify belum terpasang — alert dibuang:\n{text}")
        return False
    try:
        await cb(text, buttons=buttons)
        return True
    except Exception as e:
        print(f"[pons-radar] gagal kirim alert: {e}")
        return False


# ==================== parser event (fungsi murni) ====================
def _topic_addr(topic) -> str:
    """Ambil 20 byte terakhir dari sebuah topic log → alamat huruf kecil."""
    if hasattr(topic, "hex"):
        h = topic.hex()
    elif isinstance(topic, (bytes, bytearray)):
        h = topic.hex()
    else:
        h = str(topic)
    h = h.lower()
    if not h.startswith("0x"):
        h = "0x" + h
    return "0x" + h[-40:]


def parse_launch_log(log) -> dict | None:
    """TokenLaunched(token, curve, deployer, ...) → dict kandidat.

    topics[1]=token, topics[2]=bonding curve, topics[3]=deployer.
    Fungsi murni (tanpa jaringan) supaya bisa di-unit-test.
    """
    try:
        topics = log.get("topics") if isinstance(log, dict) else getattr(log, "topics", None)
        if not topics or len(topics) < 4:
            return None
        token = _topic_addr(topics[1])
        curve = _topic_addr(topics[2])
        deployer = _topic_addr(topics[3])
        if isinstance(log, dict):
            block = int(log.get("blockNumber") or 0)
        else:
            block = int(getattr(log, "blockNumber", 0) or 0)
    except Exception:
        return None
    if not token or token == "0x" + "0" * 40:
        return None
    if token == curve or token == deployer:
        return None
    return {"ca": token, "curve": curve, "deployer": deployer, "block": block}


# ==================== heuristik (fungsi murni) ====================
def _norm_name(name) -> str:
    return " ".join(str(name or "").lower().split())


def symbol_penalty(symbol, name="") -> tuple:
    """Penalti skor untuk simbol/nama yang jelas hasil generator sampah.

    Bukan filter keras — hanya mengurangi skor. Aturan sengaja konservatif:
    biar token sah berjuluk pendek ("WF", "PONKE") tidak ikut terbuang.
    Return (penalti 0-30, alasan pendek).
    """
    sym = str(symbol or "").strip()
    nm = str(name or "").strip()
    if not sym:
        return 30, "symbol kosong"
    low = sym.lower()
    letters = [ch for ch in low if ch.isalpha()]
    if len(letters) >= 4:
        vowels = sum(1 for ch in letters if ch in _VOWELS)
        if vowels / len(letters) < 0.18:
            return 30, "simbol tanpa vokal"
    run = 0
    for ch in low:
        if ch.isalpha() and ch not in _VOWELS:
            run += 1
            if run >= 5:
                return 20, "konsonan acak"
        else:
            run = 0
    if len(sym) > 14:
        return 15, "simbol kepanjangan"
    digits = sum(1 for ch in sym if ch.isdigit())
    if digits >= 4 and digits >= len(sym) * 0.6:
        return 15, "simbol dominan angka"
    if nm and nm.lower() == low and len(nm) <= 3:
        return 10, "nama = simbol"
    return 0, ""


def base_score(symbol, name, deployer_launches=0, name_cluster=0) -> tuple:
    """Skor heuristik 0-100 (sebelum LLM). Return (skor, catatan).

    Skala sengaja dibuat supaya token BERSIH (tanpa satu pun bendera merah)
    duduk di 70 — di atas ambang alert default 60. Satu-dua penalti ringan
    masih lolos; tiga penalti sekaligus jatuh di bawah ambang.
    """
    score = 70
    notes = []
    pen, why = symbol_penalty(symbol, name)
    if pen:
        score -= pen
        notes.append(why)
    if not str(name or "").strip():
        score -= 10
        notes.append("nama kosong")
    if name_cluster >= 3:
        score -= 20
        notes.append(f"nama sama {name_cluster}x/jam")
    elif name_cluster == 2:
        score -= 12
        notes.append("nama sama 2x/jam")
    elif name_cluster == 1:
        score -= 5
        notes.append("nama sama 1x/jam")
    if deployer_launches >= 6:
        score -= 15
        notes.append(f"deployer {deployer_launches} launch/jam")
    elif deployer_launches >= 3:
        score -= 8
        notes.append(f"deployer {deployer_launches} launch/jam")
    return max(0, min(100, score)), "; ".join(notes)


def compose_alert(rec: dict) -> str:
    """Susun teks alert Telegram dari satu record kandidat.

    Fungsi murni — dipakai test. Tidak ada angka yang dikarang: semua field
    berasal dari record DB/RPC; yang tidak ada ditampilkan sebagai '?'.
    """
    sym = rec.get("symbol") or "?"
    name = rec.get("name") or ""
    ca = rec.get("ca") or ""
    score = rec.get("score")
    verdict = rec.get("verdict") or "?"
    reason = (rec.get("reason") or "").strip()
    dep = rec.get("deployer") or ""
    dev_launches = rec.get("deployer_launches")
    age = rec.get("age_sec")
    block = rec.get("launch_block") or 0

    head = f"📡 **RADAR PONS — SINYAL AWAL**\n**{sym}**"
    if name and name.strip().lower() != str(sym).lower():
        head += f" · {name}"
    lines = [head, f"`{ca}`", ""]
    if score is not None:
        lines.append(f"🧠 skor **{int(score)}** · verdict **{verdict}**")
    if reason:
        lines.append(f"_{reason}_")
    if dep:
        short = dep[:6] + "…" + dep[-4:]
        dev_line = f"👤 dev `{short}`"
        if dev_launches is not None:
            dev_line += f" · {int(dev_launches)} launch/jam"
        lines.append(dev_line)
    meta = []
    if age is not None and age >= 0:
        meta.append(f"umur {int(age) // 60}m {int(age) % 60}s")
    if block:
        meta.append(f"blok {int(block)}")
    if meta:
        lines.append("🕒 " + " · ".join(meta))
    lines.append("📈 pool v4 belum ada — pantau graduasi")
    lines.append("_alert-only: bot tidak membeli otomatis_")
    return "\n".join(lines)


def compose_digest(rows) -> str | None:
    """Rekap publik per jam (opsi C). None bila tidak ada alert."""
    rows = list(rows or [])
    if not rows:
        return None
    lines = [f"📡 **Radar Pons — {len(rows)} sinyal (1 jam terakhir)**", ""]
    for r in rows[:10]:
        lines.append(f"• **{r.get('symbol') or '?'}** · skor {r.get('score') or '?'}"
                     f" · {r.get('verdict') or '?'}")
    if len(rows) > 10:
        lines.append(f"… dan {len(rows) - 10} lainnya")
    lines.append("")
    lines.append("_Rekap tertunda — bukan sinyal real-time._")
    return "\n".join(lines)


def _explorer_buttons(ca):
    try:
        from telethon import Button
        return [[Button.url("🔍 Explorer", f"{config.EXPLORER_URL}/token/{ca}")]]
    except Exception:
        return None


# ==================== I/O: RPC ====================
def _w3():
    """Web3 read-only, di-cache 5 menit. Gagal → None (tidak raise)."""
    now = time.time()
    with _w3_lock:
        cached = _w3_cache.get("w3")
        if cached is not None and now - _w3_cache.get("ts", 0) < 300:
            return cached
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(config.RPC_URL,
                                        request_kwargs={"timeout": 20}))
            _w3_cache["w3"] = w3
            _w3_cache["ts"] = now
            return w3
        except Exception as e:
            print(f"[pons-radar] web3 tidak tersedia: {e}")
            return None


def scan_new_launches() -> int:
    """Baca TokenLaunched baru sejak watermark → pons_queue.

    Watermark pertama kali (0) dilompatkan ke head: radar tidak memindai
    riwayat chain dari genesis. Return jumlah baris BARU.
    """
    w3 = _w3()
    if w3 is None:
        return 0
    try:
        from web3 import Web3
        latest = int(w3.eth.block_number)
        last = int(db.get("pons_last_block", int))
        if last <= 0:
            db.set_("pons_last_block", str(latest))
            print(f"[pons-radar] watermark dimulai dari blok {latest}")
            return 0
        from_block = last + 1
        if from_block > latest:
            return 0
        to_block = min(latest, from_block + MAX_BLOCKS_PER_CYCLE - 1)
        topic0 = Web3.keccak(text=PONS_LAUNCHED_SIG).hex()
        if not topic0.startswith("0x"):
            topic0 = "0x" + topic0
        logs = w3.eth.get_logs({
            "address": Web3.to_checksum_address(PONS_V2_FACTORY),
            "topics": [topic0],
            "fromBlock": from_block,
            "toBlock": to_block,
        })
    except Exception as e:
        print(f"[pons-radar] getLogs gagal (diabaikan): {e}")
        return 0

    head_ts = None
    new_rows = 0
    for log in logs:
        rec = parse_launch_log(log)
        if not rec:
            continue
        ts = 0
        try:
            ts = int(w3.eth.get_block(rec["block"])["timestamp"])
        except Exception:
            if head_ts is None:
                try:
                    head_ts = int(w3.eth.get_block(to_block)["timestamp"])
                except Exception:
                    head_ts = int(time.time())
            ts = head_ts
        if db.add_pons_token(rec["ca"], deployer=rec["deployer"],
                             curve=rec["curve"], launch_block=rec["block"],
                             launch_ts=ts):
            new_rows += 1
    db.set_("pons_last_block", str(to_block))
    if new_rows:
        print(f"[pons-radar] {new_rows} launch baru (blok {from_block}-{to_block})")
    return new_rows


_ERC20_ABI = [
    {"name": "name", "outputs": [{"type": "string"}], "inputs": [],
     "stateMutability": "view", "type": "function"},
    {"name": "symbol", "outputs": [{"type": "string"}], "inputs": [],
     "stateMutability": "view", "type": "function"},
    {"name": "totalSupply", "outputs": [{"type": "uint256"}], "inputs": [],
     "stateMutability": "view", "type": "function"},
]


def enrich_token(ca: str) -> dict | None:
    """Baca name/symbol/totalSupply on-chain. None bila kontrak tidak menjawab."""
    w3 = _w3()
    if w3 is None:
        return None
    try:
        from web3 import Web3
        c = w3.eth.contract(address=Web3.to_checksum_address(ca), abi=_ERC20_ABI)
    except Exception:
        return None
    out = {"symbol": "", "name": "", "total_supply": 0.0}
    ok = 0
    for field, fn in (("symbol", "symbol"), ("name", "name")):
        try:
            out[field] = str(getattr(c.functions, fn)().call())[:64]
            ok += 1
        except Exception:
            pass
    try:
        out["total_supply"] = float(c.functions.totalSupply().call())
        ok += 1
    except Exception:
        pass
    return out if ok else None


# ==================== tier LLM ====================
_LLM_SYSTEM = (
    "Kamu penyaring launchpad meme coin on-chain. Tugasmu menilai apakah "
    "token yang baru lahir di bonding curve layak dikirim sebagai ALERT AWAL "
    "ke trader. Tidak ada data harga/pool — nilai dari nama, simbol, rekam "
    "jejak deployer, dan sinyal spam.\n"
    "ATURAN KERAS: balas HANYA satu objek JSON. Jangan menulis analisis, "
    "jangan pakai markdown, karakter pertama WAJIB '{'.\n"
    'Skema: {"verdict":"AMAN|WASPADA|BUANG","score":0-100,"reason":"<=18 kata"}\n'
    "Tulis `reason` dalam Bahasa Indonesia."
)


def _llm_parse(raw):
    """Parse JSON ketat. Model 'chatty' (dump penalaran) gagal di sini."""
    d = agent._parse_json(raw)
    if not isinstance(d, dict):
        return None
    return d


def llm_verdict(rec: dict) -> dict:
    """Minta verdict ke LLM. Gagal → None (pemanggil pakai skor heuristik).

    Dua percobaan: normal, lalu retry dengan instruksi lebih keras. Model
    reasoning (deepseek-v4-flash, glm-5.3) sering membuang token untuk
    penalaran lalu terpotong — karena itu max_tokens 400 dan retry dipakai,
    bukan menaikkan timeout.
    """
    if db.get("pons_llm_on", str) != "1" or not config.LLM_API_KEY:
        return None
    model = (db.get("pons_llm_model", str) or "").strip() or db.get("agent_model", str)
    payload = {
        "symbol": rec.get("symbol"),
        "name": rec.get("name"),
        "total_supply": rec.get("total_supply"),
        "deployer": rec.get("deployer"),
        "deployer_launches_1h": rec.get("deployer_launches"),
        "name_used_1h": rec.get("name_cluster"),
        "heuristic_score": rec.get("heur_score"),
        "heuristic_notes": rec.get("heur_notes"),
    }
    body = json.dumps(payload)
    d = None
    for attempt in (1, 2):
        try:
            if attempt == 2:
                body = body + '\n\nBalas HANYA JSON: {"verdict":"...","score":0,"reason":"..."}'
            raw = agent._call(_LLM_SYSTEM, body, 400, model=model)
            d = _llm_parse(raw)
            if d:
                break
        except Exception as e:
            last = e
        else:
            last = None
        if attempt == 2:
            print(f"[pons-radar] LLM gagal untuk {rec.get('ca', '')[:12]}: {last or 'bukan JSON'}")
            return None
    if not d:
        return None
    try:
        verdict = str(d.get("verdict") or "").strip().upper()
        if verdict not in ("AMAN", "WASPADA", "BUANG"):
            verdict = "WASPADA"
        try:
            score = int(round(float(d.get("score"))))
        except (TypeError, ValueError):
            score = int(rec.get("heur_score") or 50)
        return {
            "verdict": verdict,
            "score": max(0, min(100, score)),
            "reason": agent.sanitize_text(d.get("reason") or "", 140),
            "model": model,
        }
    except Exception as e:
        print(f"[pons-radar] verdict LLM tidak valid: {e}")
        return None


# ==================== satu putaran pemrosesan ====================
def _enrich_many(batch, concurrency=5) -> dict:
    out = {}
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as ex:
        futs = {ex.submit(enrich_token, r["ca"]): r["ca"] for r in batch}
        for f in as_completed(futs):
            ca = futs[f]
            try:
                out[ca] = f.result()
            except Exception:
                out[ca] = None
    return out


def process_candidates(batch) -> list:
    """Nilai kandidat 'new' → tolak (DB) atau kembalikan list alert siap kirim.

    Sinkron (RPC + LLM blocking) — panggil lewat asyncio.to_thread.
    """
    if not batch:
        return []
    enriched = _enrich_many(batch, db.get("pons_enrich_concurrency", int))
    min_score = int(db.get("pons_alert_min_score", int))
    cap = int(db.get("pons_alert_max_per_hour", int))
    cap_left = None
    if cap > 0:
        cap_left = cap - db.pons_alert_count_since(int(time.time()) - 3600)
    cluster_since = int(time.time()) - NAME_CLUSTER_WINDOW_SEC
    now = int(time.time())
    alerts = []

    # PASS 1 — simpan nama/simbol lebih dulu. Tanpa ini, klaster nama tidak
    # pernah terdeteksi di dalam satu batch: token pertama melihat 0, token
    # kedua melihat 1, dst., padahal 4 koin bisa muncul di batch yang sama.
    for rec in batch:
        data = enriched.get(rec["ca"])
        if data:
            db.update_pons_token(rec["ca"], symbol=data.get("symbol") or "",
                                 name=data.get("name") or "",
                                 total_supply=data.get("total_supply"))

    # PASS 2 — nilai tiap kandidat.
    for rec in batch:
        ca = rec["ca"]
        data = enriched.get(ca)
        if not data:
            db.reject_pons(ca, "rpc_gagal: kontrak tidak menjawab name/symbol",
                           score=0)
            continue
        symbol, name = data.get("symbol") or "", data.get("name") or ""
        cluster = db.pons_name_cluster(_norm_name(name), cluster_since) if name else 0
        cluster = max(0, cluster - 1)  # jangan hitung token ini sendiri
        hist = db.pons_deployer_history(rec.get("deployer"))
        heur, notes = base_score(symbol, name,
                                 deployer_launches=hist.get("launches", 0),
                                 name_cluster=cluster)
        work = dict(rec)
        work.update({
            "symbol": symbol, "name": name,
            "total_supply": data.get("total_supply"),
            "deployer_launches": hist.get("launches", 0),
            "name_cluster": cluster,
            "heur_score": heur, "heur_notes": notes,
            "age_sec": (now - int(rec.get("launch_ts") or now)),
        })

        # --- tier LLM (hakim akhir) ---
        llm = llm_verdict(work)
        if llm:
            final = int(round((heur + llm["score"]) / 2))
            work.update({"score": final, "verdict": llm["verdict"],
                         "reason": llm["reason"] or notes,
                         "llm_model": llm["model"]})
            if llm["verdict"] == "BUANG":
                db.reject_pons(ca, f"LLM BUANG: {llm['reason'] or '-'}",
                               score=final, verdict="BUANG",
                               llm_model=llm["model"], block_buy=True)
                continue
        else:
            work.update({"score": heur, "verdict": "WASPADA",
                         "reason": notes or "heuristik saja (LLM tidak aktif/gagal)",
                         "llm_model": None})

        if work["score"] < min_score:
            db.reject_pons(ca, f"skor {work['score']} < {min_score}",
                           score=work["score"], verdict=work["verdict"],
                           block_buy=True)
            continue
        if cap_left is not None and cap_left <= 0:
            db.reject_pons(ca, "cap alert per jam penuh", score=work["score"],
                           verdict=work["verdict"])
            continue
        # Simpan hasil penilaian walau pengiriman Telegram nanti gagal —
        # baris tetap status 'new' supaya dicoba lagi siklus berikutnya, tapi
        # panel/analisis sudah bisa melihat skor & alasan.
        db.update_pons_token(ca, score=work["score"], verdict=work["verdict"],
                             reason=(work.get("reason") or "")[:400],
                             llm_model=work.get("llm_model"), checked_at=now)
        alerts.append(work)
        if cap_left is not None:
            cap_left -= 1
    return alerts


# ==================== loop ====================
async def blacklist_loop():
    """Warm-up + refresh set veto tiap 30 s.

    Jalan TANPA peduli `pons_radar_on`: kalau radar dimatikan, veto yang
    sudah ada tetap berlaku di jalur beli.
    """
    await asyncio.sleep(5)
    while True:
        try:
            n = await asyncio.to_thread(refresh_blacklist, True)
            if n:
                print(f"[pons-radar] veto aktif: {n} CA diblokir")
            await asyncio.sleep(BLACKLIST_REFRESH_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[pons-radar] blacklist loop error (lanjut): {e}")
            await asyncio.sleep(BLACKLIST_REFRESH_SEC)


async def radar_loop():
    """Loop radar. Default MATI (`pons_radar_on=0`) — nyalakan dari settings."""
    print("[pons-radar] loop siap — alert-only, TIDAK masuk pipa beli")
    await asyncio.sleep(10)
    while True:
        try:
            if db.get("pons_radar_on", str) != "1":
                await asyncio.sleep(30)
                continue
            if _notify_cb is None:
                await asyncio.sleep(15)
                continue
            await asyncio.to_thread(scan_new_launches)
            db.expire_pons_queue(int(db.get("pons_queue_ttl_sec", int)))
            batch = db.get_pons_pending(int(db.get("pons_max_per_cycle", int)))
            if batch:
                alerts = await asyncio.to_thread(process_candidates, batch)
                for rec in alerts:
                    sent = await _send_alert(compose_alert(rec),
                                             buttons=_explorer_buttons(rec["ca"]))
                    if sent:
                        db.mark_pons_alerted(
                            rec["ca"], symbol=rec.get("symbol"),
                            name=rec.get("name"), score=rec.get("score"),
                            verdict=rec.get("verdict"),
                            reason=rec.get("reason"),
                            llm_model=rec.get("llm_model"))
                        print(f"[pons-radar] ALERT {rec.get('symbol')} "
                              f"({rec['ca'][:12]}…) skor {rec.get('score')}")
                print(f"[pons-radar] diproses={len(batch)} alert={len(alerts)}")
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[pons-radar] siklus error (lanjut): {e}")
            await asyncio.sleep(30)


async def digest_loop():
    """Opsi C — rekap publik per jam. Mati kecuali `pons_radar_public=1`.

    Real-time tetap eksklusif DM admin; channel publik hanya menerima rekap
    tertunda supaya jendela 2–4 menit sebelum graduasi tidak bocor.
    """
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.sleep(3600)
            if db.get("pons_radar_public", str) != "1":
                continue
            if _public_notify_cb is None:
                continue
            text = compose_digest(db.recent_pons_alerts(hours=1, limit=50))
            if not text:
                continue
            try:
                await _public_notify_cb(text)
            except Exception as e:
                print(f"[pons-radar] digest publik gagal: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[pons-radar] digest error (lanjut): {e}")
