"""🛡️ sniper/clone_guard.py — Clone / operator-family guard (v16.6).

Latar: `bye` punya 6 CA kloningan; `Marvin` ≥3 CA kloningan (semuanya
berakhiran 47777). Bot pernah membeli 2 Marvin berbeda tanpa sadar.

Deteksi dua lapis (bukti on-chain 2026-10-22..25):
  1. TEMPLATE FINGERPRINT — sha256(deployed_bytecode[:1000]): kloningan
     dari template yang sama punya fingerprint identik (Marvin #1..#3:
     02123179bd13c28a…).
  2. OPERATOR FUNDER GRAPH — deployer burner yang didanai funder yang
     sama = satu operator. Satu funder (0x22820FDa…) mendanai dua
     deployer Marvin berbeda.

Semua Blockscout call fail-OPEN: error/timeout TIDAK boleh memblokir
pembelian (guard ini pelengkap, bukan pengganti fail-closed holder check).
"""
import hashlib
import threading
import time

try:
    from curl_cffi import requests as _requests
    _S = _requests.Session(impersonate="chrome120")
except ImportError:
    import requests as _requests
    _S = _requests.Session()
    _S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})

from . import config, db  # noqa: E402  (noqa lama)

BS = config.BLOCKSCOUT_API.rstrip("/")



def _bs_get(path: str, timeout: float = 8):
    """GET Blockscout → dict, atau None (tanpa raise)."""
    try:
        r = _S.get(f"{BS}{path}", timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# ---------- 1. template fingerprint ----------

def template_fp(ca: str, sc: dict | None = None) -> str | None:
    """sha256(deployed_bytecode[:1000]) dari /smart-contracts/{ca}.
    `sc` opsional bila respons sudah diambil pemanggil (hindari request ganda).
    Endpoint gagal → None."""
    if sc is None:
        sc = _bs_get(f"/smart-contracts/{ca}")
    if not sc:
        return None
    code = sc.get("deployed_bytecode") or ""
    if not code:
        return None
    return hashlib.sha256(code[:1000].encode() if isinstance(code, str) else bytes(code[:1000])).hexdigest()


# ---------- 2. funder graph ----------

def funders_of(deployer: str, limit: int = 30) -> list | None:
    """EOA yang pernah kirim ETH MASUK ke `deployer`.

    Return None saat Blockscout gagal/timeout (bedakan dari list kosong
    yang berarti memang tidak ada funder).

    Catatan API: `?count=` TIDAK didukung Blockscout v2 (HTTP 422) — hanya
    pagination `next_page_params`. Jangan tambahkan param itu lagi.
    """
    data = _bs_get(f"/addresses/{deployer}/transactions", timeout=12)
    if data is None:
        return None
    out = []
    dep = (deployer or "").lower()
    for tx in data.get("items") or []:
        frm = ((tx.get("from") or {}).get("hash") or "").lower()
        to = ((tx.get("to") or {}).get("hash") or "").lower()
        try:
            value = int(float(tx.get("value") or 0))
        except (TypeError, ValueError):
            value = 0
        if to == dep and frm and frm != dep and value > 0 and frm.startswith("0x"):
            if frm not in out:
                out.append(frm)
        if len(out) >= limit:
            break
    return out



# ---------- 0. entry rate-limit per operator-family (v16.6 ratelimit) ----------

def _norm(s: str) -> str:
    """Normalisasi teks keluarga: strip + lower + collapse spasi.
    Konsisten untuk simpan & baca (family_keys / note_entry / too_many_entries)."""
    return " ".join((s or "").strip().lower().split())


def family_keys(ca: str, symbol: str = "", name: str = "") -> list[str]:
    """SEMUA kunci keluarga untuk sebuah CA, urut dari paling kuat:
      1. dep:<deployer>                 (deployer_registry.deployer terisi)
      2. funder:<addr>                  (1 kunci per funder dari operator_funders
                                         untuk deployer tsb, maks 10 funder pertama)
      3. fp:<template_fp>|<symbol>|<name>  (template_fp terisi)
      4. sym:<symbol>|<name>            (fallback, symbol & name dinormalisasi
                                         strip+lower+collapse spasi; HANYA bila
                                         len(symbol) >= 3)
    DB-only, fail-open: error → [].
    """
    ca = (ca or "").strip().lower()
    if not ca.startswith("0x"):
        return []
    keys: list[str] = []
    sym_row = nm_row = ""
    try:
        with db._lock, db._conn() as c:
            _init_tables(c)
            row = c.execute(
                "SELECT deployer, template_fp, symbol, name FROM deployer_registry WHERE ca=?",
                (ca,)).fetchone()
            if row:
                sym = _norm(symbol) or _norm(row["symbol"])
                nm = _norm(name) or _norm(row["name"])
                sym_row, nm_row = sym, nm
                dep = row["deployer"]
                if dep:
                    keys.append(f"dep:{dep.lower()}")
                    frows = c.execute(
                        "SELECT funder FROM operator_funders WHERE deployer=? LIMIT 10",
                        (dep,)).fetchall()
                    keys.extend(f"funder:{f[0].lower()}" for f in frows)
                if row["template_fp"]:
                    keys.append(f"fp:{row['template_fp']}|{sym}|{nm}")
    except Exception:
        pass  # fail-open
    sym_f = _norm(symbol) or sym_row
    nm_f = _norm(name) or nm_row
    if len(sym_f) >= 3:
        keys.append(f"sym:{sym_f}|{nm_f}")
        # Kunci symbol-saja: jaring pengaman kalau `name` tidak ikut terkirim
        # di salah satu jalur (mis. posisi dari trader tanpa field name).
        # Tanpa ini note_entry menulis "sym:marvin|" sementara cek memakai
        # "sym:marvin|marvin the martian" → hitungan selalu 0.
        keys.append(f"symonly:{sym_f}")
    # dedupe, pertahankan urutan kekuatan
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def note_entry(ca: str, symbol: str = "", name: str = "") -> None:
    """CATAT entry posisi yang benar-benar dibuka ke entry_log (per key keluarga).

    Dipanggil SEKALI oleh db.open_position SETELAH INSERT positions berhasil.
    Fail-open total: error apa pun tidak boleh menggagalkan posisi.
    """
    try:
        ca_key = (ca or "").strip().lower()
        if not ca_key.startswith("0x"):
            return
        # permudah pengisian registry (deployer/funder) di background —
        # dipanggil walau register_async sudah pernah dipanggil (efeknya
        # no-op aman, dan nanti register() melengkapi data dari jaringan).
        register_async(ca_key, symbol=symbol, name=name)
        keys = family_keys(ca_key, symbol=symbol, name=name)
        if not keys:
            # registry masih kosong — minimal catat fallback metadata agar
            # rate-limit tetap bekerja measurable untuk keluarganya.
            if len(_norm(symbol)) >= 3:
                keys = [f"sym:{_norm(symbol)}|{_norm(name)}"]
        if not keys:
            return
        now = int(time.time())
        with db._lock, db._conn() as c:
            _init_tables(c)
            for k in keys:
                c.execute("INSERT OR IGNORE INTO entry_log(key,ca,ts) VALUES(?,?,?)",
                          (k, ca_key, now))
    except Exception:
        pass  # guard pelengkap — tidak boleh pernah raise


def backfill_entry_keys(ca: str) -> None:
    """Lengkapi entry_log untuk CA yang SUDAH pernah dibeli, memakai kunci
    keluarga terbaru (dep:/funder:/fp:) tanpa mengubah timestamp asli.

    Dipanggil worker background setelah `register()` selesai. Tidak pernah
    mengubah jumlah entry untuk kunci `sym:` yang sudah ada (INSERT OR IGNORE).
    Fail-open.
    """
    try:
        ca = (ca or "").strip().lower()
        if not ca.startswith("0x"):
            return
        keys = family_keys(ca)
        if not keys:
            return
        with db._lock, db._conn() as c:
            _init_tables(c)
            row = c.execute(
                "SELECT ts FROM entry_log WHERE ca=? ORDER BY ts ASC LIMIT 1",
                (ca,)).fetchone()
            if not row:
                return
            ts = row["ts"]
            for k in keys:
                c.execute("INSERT OR IGNORE INTO entry_log(key,ca,ts) VALUES(?,?,?)",
                          (k, ca, ts))
    except Exception:
        pass  # guard pelengkap — tidak boleh pernah raise


def too_many_entries(ca: str, symbol: str = "", name: str = "",
                     limit: int = 3, hours: int = 24) -> tuple[bool, str]:
    """Rate-limit ENTRY per operator-family: keluarga yang sama ≥ `limit` CA
    DISTINCT dalam `hours` jam terakhir → (True, reason).

    100% DB-ONLY (NOL panggilan jaringan). Wajib selesai < 20 ms.
    Hitung COUNT(DISTINCT ca) untuk SETIAP kunci keluarga; ambil yang TERBESAR.
    Fail-open: DB error → (False, ""), jangan blokir entry.
    """
    try:
        ca_key = (ca or "").strip().lower()
        if not ca_key.startswith("0x"):
            return False, ""
        keys = family_keys(ca_key, symbol=symbol, name=name)
        sym_f = _norm(symbol)
        if len(sym_f) >= 3:
            fb = f"sym:{sym_f}|{_norm(name)}"
            if fb not in keys:
                keys = keys + [fb]
        if not keys:
            return False, ""
        since = int(time.time()) - hours * 3600
        worst = 0
        with db._lock, db._conn() as c:
            _init_tables(c)
            for k in keys:
                n = c.execute(
                    "SELECT COUNT(DISTINCT ca) FROM entry_log WHERE key=? AND ts>=?",
                    (k, since)).fetchone()[0]
                if n > worst:
                    worst = n
        if worst >= limit:
            return True, f"operator-family sudah {worst} entry/{hours}j (maks {limit})"
        return False, ""
    except Exception:
        return False, ""


# ---------- 3. registry DB ----------

def _init_tables(c):
    c.execute("""CREATE TABLE IF NOT EXISTS deployer_registry(
        ca TEXT PRIMARY KEY,
        deployer TEXT,
        template_fp TEXT,
        symbol TEXT,
        name TEXT,
        total_supply TEXT,
        funders TEXT,
        status TEXT DEFAULT 'ok',
        reason TEXT,
        ts INTEGER
    )""")
    # operator_funders: PRIMARY KEY(funder, deployer) — BUKAN funder saja.
    # Satu funder bisa mendanai banyak deployer burner; kunci tautan harus
    # pasangan (funder,deployer) supaya riwayat 'failed' satu deployer tidak
    # tertimpa oleh deployer lain yang didanai funder yang sama.
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='operator_funders'").fetchone()
    need_create = row is None
    if row is not None:
        compact = (row[0] or "").replace(" ", "").upper()
        if "PRIMARYKEY(FUNDER,DEPLOYER)" not in compact:
            # migrasi dari skema lama (funder sebagai PK tunggal)
            c.execute("ALTER TABLE operator_funders RENAME TO operator_funders_old")
            c.execute("""CREATE TABLE operator_funders(
                funder TEXT,
                deployer TEXT,
                ca TEXT,
                status TEXT DEFAULT 'ok',
                ts INTEGER,
                PRIMARY KEY (funder, deployer)
            )""")
            c.execute("""INSERT OR IGNORE INTO operator_funders(funder,deployer,ca,status,ts)
                SELECT funder,deployer,ca,status,ts FROM operator_funders_old""")
            c.execute("DROP TABLE operator_funders_old")
            need_create = False
    if need_create:
        c.execute("""CREATE TABLE IF NOT EXISTS operator_funders(
            funder TEXT,
            deployer TEXT,
            ca TEXT,
            status TEXT DEFAULT 'ok',
            ts INTEGER,
            PRIMARY KEY (funder, deployer)
        )""")

    # entry_log: catat entry per key keluarga operator (rate-limit jumlah
    # entry, bukan blacklist token). PRIMARY KEY(key,ca) → sama CA di-note
    # dua kali tidak menambah count.
    c.execute("""CREATE TABLE IF NOT EXISTS entry_log(
        key TEXT,
        ca TEXT,
        ts INTEGER,
        PRIMARY KEY(key, ca)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_entry_log_key_ts ON entry_log(key, ts)")


def register(ca: str, symbol: str = "", name: str = "",
             total_supply: str = "") -> dict | None:
    """Isi deployer_registry + operator_funders untuk sebuah CA.
    Gagal jaringan → baris status='failed' tetap ditulis (reason jelas)."""
    ca = (ca or "").strip().lower()
    if not ca.startswith("0x"):
        return None
    now = int(time.time())

    # hitung data on-chain (satu kali per panggilan manual)
    d = _bs_get(f"/addresses/{ca}")
    creation_tx = None
    if d:
        creation_tx = d.get("creation_transaction_hash")
    # metadata token: symbol/name/total_supply dari /tokens/{ca}
    # (endpoint /smart-contracts TIDAK mengembalikan symbol)
    meta = _bs_get(f"/tokens/{ca}") or {}
    symbol = symbol or (meta.get("symbol") or "")
    name = name or (meta.get("name") or "")
    total_supply = total_supply or str(meta.get("total_supply") or "")
    fp = template_fp(ca)

    deployer = None
    if creation_tx:
        tx = _bs_get(f"/transactions/{creation_tx}", timeout=10)
        if tx:
            deployer = ((tx.get("from") or {}).get("hash") or "").lower()

    with db._lock, db._conn() as c:
        _init_tables(c)
        c.execute("""INSERT INTO deployer_registry
            (ca,deployer,template_fp,symbol,name,total_supply,funders,status,reason,ts)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ca) DO UPDATE SET
                deployer  = COALESCE(excluded.deployer, deployer),
                template_fp = COALESCE(excluded.template_fp, template_fp),
                symbol    = CASE WHEN excluded.symbol != '' THEN excluded.symbol ELSE symbol END,
                name      = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END,
                total_supply = CASE WHEN excluded.total_supply != '' THEN excluded.total_supply ELSE total_supply END,
                status    = CASE WHEN deployer_registry.status = 'failed'
                                 THEN 'failed' ELSE excluded.status END,
                reason    = CASE WHEN deployer_registry.status = 'failed'
                                 THEN reason ELSE excluded.reason END,
                ts        = excluded.ts""",
            (ca, deployer, fp, symbol, name, str(total_supply), "",
             "ok" if (deployer and fp) else "unknown",
             "" if (deployer and fp) else
             ("blockscout gagal: deployer/fp tidak terverifikasi"), now))

    # funders → operator_funders (fail-open: gagal → tidak ditulis)
    if deployer:
        funders = funders_of(deployer)
        if funders is not None:
            with db._lock, db._conn() as c:
                _init_tables(c)
                for f in funders:
                    c.execute("""INSERT OR IGNORE INTO operator_funders
                        (funder,deployer,ca,status,ts) VALUES(?,?,?, 'ok', ?)""",
                        (f, deployer, ca, now))
    row = get_registry(ca)
    return row


def get_registry(ca: str) -> dict | None:
    ca = (ca or "").strip().lower()
    with db._lock, db._conn() as c:
        _init_tables(c)
        r = c.execute("SELECT * FROM deployer_registry WHERE ca=?", (ca,)).fetchone()
    return dict(r) if r else None


# ---------- 3b. registrasi BACKGROUND (nol latency di jalur entry) ----------

_bg_lock = threading.Lock()
_bg_pending: set[str] = set()
_bg_queue: "queue.Queue" = None
_bg_started = False
_BG_WORKERS = 2


def _bg_worker():
    while True:
        try:
            item = _bg_queue.get()
        except Exception:
            return
        if item is None:
            return
        ca, symbol, name, total_supply = item
        try:
            row = get_registry(ca)
            if not (row and row.get("template_fp") and row.get("deployer")):
                register(ca, symbol=symbol, name=name, total_supply=total_supply)
            # Setelah registry lengkap, lengkapi entry_log dengan kunci
            # operator (dep:/funder:) yang belum ada saat posisi dibuka —
            # registrasi butuh ~20s, sedangkan buy terjadi beberapa detik
            # setelah safety.check. Tanpa backfill, rate-limit operator
            # hanya efektif lewat kunci sym.
            backfill_entry_keys(ca)
        except Exception:
            pass
        finally:
            with _bg_lock:
                _bg_pending.discard(ca)
            try:
                _bg_queue.task_done()
            except Exception:
                pass


def register_async(ca: str, symbol: str = "", name: str = "",
                   total_supply: str = "") -> None:
    """Antrikan pengisian registry ke worker background. SELALU < 1 ms.

    Dipanggil dari jalur entry supaya guard tidak pernah menahan pembelian.
    Duplikat CA di-dedupe lewat `_bg_pending`; antrean dibatasi 500 item
    (kelebihan dibuang — guard ini pelengkap, bukan blocker).
    """
    global _bg_queue, _bg_started
    ca = (ca or "").strip().lower()
    if not ca.startswith("0x"):
        return
    with _bg_lock:
        if ca in _bg_pending:
            return
        if _bg_queue is not None and _bg_queue.qsize() > 500:
            return
        _bg_pending.add(ca)
        try:
            if _bg_queue is None:
                import queue as _q
                _bg_queue = _q.Queue()
            if not _bg_started:
                for i in range(_BG_WORKERS):
                    threading.Thread(target=_bg_worker, name=f"clone-bg-{i}",
                                     daemon=True).start()
                _bg_started = True
            _bg_queue.put((ca, symbol, name, total_supply))
        except Exception:
            _bg_pending.discard(ca)


# ---------- 4. risiko klon ----------
def is_clone_risk(ca: str, symbol: str = "", name: str = "",
                  fresh: bool = False) -> tuple[bool, str]:
    """(True, reason) jika CA ini punya risiko kloningan/keluarga operator.

    ⚠️ FUNGSI INI 100% DB-ONLY — NOL NETWORK CALL.
    Guard ini dipanggil di jalur entry (sebelum beli); kalau ia menembak
    Blockscout, entry bisa tertunda 18-32 detik (terukur) dan koin sudah
    kabur. Pengisian data on-chain dilakukan TERPISAH oleh
    `register_async()` di background thread.

    Alasan TOLAK:
      0. CA ini sendiri sudah berstatus 'failed'.
      1. Baris lain punya template_fp + symbol + name sama & 'failed'.
      1b.Fallback bila fingerprint belum ada: symbol + name sama & 'failed'.
      2. Funder operator yang mendanai deployer ini berstatus 'failed'.
    Fail-open: data tidak cukup → (False, "… tidak terverifikasi").

    `symbol`/`name` opsional dari pair Dexscreener — dipakai untuk cek 1b.
    `fresh` dipertahankan demi kompatibilitas pemanggil lama; DIABAIKAN.
    """
    ca = (ca or "").strip().lower()
    if not ca.startswith("0x"):
        return False, "ca kosong"
    try:
        row = get_registry(ca) or {}
    except Exception:
        return False, "clone_guard: db error (fail-open)"
    fp = row.get("template_fp")
    deployer = row.get("deployer")
    sym = (symbol or row.get("symbol") or "").strip()
    nm = (name or row.get("name") or "").strip()
    if not fp and not sym:
        return False, "clone_guard: belum terdaftar (fail-open)"

    reasons = []
    with db._lock, db._conn() as c:
        _init_tables(c)
        # ---- cek 0: CA ini sendiri sudah pernah ditandai gagal ----
        own = c.execute("SELECT status, reason FROM deployer_registry WHERE ca=?",
                        (ca,)).fetchone()
        if own and own["status"] == "failed":
            reasons.append(f"CA sudah ditandai gagal ({own['reason'] or 'failed'})")
        # ---- cek 1: saudara template yang GAGAL ----
        if fp and sym and nm:
            sibs = c.execute("""SELECT ca, reason FROM deployer_registry
                WHERE template_fp=? AND symbol=? AND name=?
                  AND status='failed' AND ca != ?""",
                (fp, sym, nm, ca)).fetchall()
            if sibs:
                reasons.append(f"template clone dari {sibs[0]['ca'][:10]}… "
                               f"({sibs[0]['reason'] or 'failed'})")
        # ---- cek 1b: fallback symbol+name (fingerprint belum terisi) ----
        # Hanya untuk nama yang cukup spesifik; simbol >=3 char agar tidak
        # memblokir nama umum pendek (mis. "AI", "GM").
        if not (fp and sym and nm) and sym and nm and len(sym) >= 3:
            sibs = c.execute("""SELECT ca, reason FROM deployer_registry
                WHERE symbol=? AND name=? AND status='failed' AND ca != ?""",
                (sym, nm, ca)).fetchall()
            if sibs:
                reasons.append(f"klon symbol/nama dari {sibs[0]['ca'][:10]}… "
                               f"({sibs[0]['reason'] or 'failed'})")
        # ---- cek 2: funder yang sudah gagal (mark_family_failed) ----
        row2 = c.execute("SELECT deployer FROM deployer_registry WHERE ca=?",
                         (ca,)).fetchone()
        dep = (row2["deployer"] if row2 else None) or deployer
        # 2a. funder langsung ditautkan ke deployer ini dengan status failed
        frows = c.execute("""SELECT status, funder FROM operator_funders
            WHERE deployer=? AND status='failed'""", (dep,)).fetchall()
        # 2b. operator family: funder yang sama juga mendanai deployer LAIN
        #     yang statusnya 'failed' — satu funder, banyak burner = satu
        #     operator (bukti Marvin: 0x22820FDa… mendanai 2 deployer).
        if not frows and dep:
            frows = c.execute("""SELECT f.status, f.funder FROM operator_funders f
                WHERE f.funder IN (SELECT funder FROM operator_funders
                                   WHERE deployer=?)
                  AND f.status='failed' AND f.deployer != ?""",
                (dep, dep)).fetchall()
        if frows:
            reasons.append(f"funder {frows[0]['funder'][:10]}… punya riwayat gagal "
                           "(operator family)")
    if reasons:
        return True, "; ".join(reasons)
    return False, "clone_guard: bersih"


# ---------- 5. mark family gagal ----------

def mark_family_failed(ca: str, reason: str):
    """Set status='failed' pada:
      - baris ca ini,
      - semua CA dengan template_fp + symbol + name sama,
      - seluruh funder operator terkait (operator_funders).

    PENTING: fungsi ini dipanggil dari jalur jual (hot path) dan dari
    dalam blok yang mungkin sudah memegang db._lock. Karena itu:
      * JANGAN memanggil register() di sini (register melakukan I/O
        jaringan → bisa menahan lock berdetik-detik),
      * JANGAN memanggil fungsi DB lain saat lock sedang dipegang
        (db._lock adalah threading.Lock non-reentrant → deadlock).
    """
    ca = (ca or "").strip().lower()
    if not ca.startswith("0x"):
        return
    now = int(time.time())
    try:
        with db._lock, db._conn() as c:
            _init_tables(c)
            row = c.execute("SELECT * FROM deployer_registry WHERE ca=?",
                            (ca,)).fetchone()
            if not row:
                # daftar minimal TANPA jaringan — cukup untuk mem-blacklist CA
                # ini; fingerprint/deployer bisa dilengkapi register() nanti.
                c.execute("""INSERT OR REPLACE INTO deployer_registry
                    (ca,deployer,template_fp,symbol,name,total_supply,funders,
                     status,reason,ts)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (ca, None, None, "", "", "", "", "failed", reason, now))
                return
            fp = row["template_fp"]
            sym = row["symbol"]
            nm = row["name"]
            dep = row["deployer"]
            c.execute("UPDATE deployer_registry SET status='failed', reason=?, ts=? "
                      "WHERE ca=?", (reason, now, ca))
            if fp and sym and nm:
                c.execute("""UPDATE deployer_registry SET status='failed', reason=?, ts=?
                    WHERE template_fp=? AND symbol=? AND name=? AND status='ok'""",
                    (f"family rug ({reason}) dari {ca[:10]}…", now, fp, sym, nm))
            if dep:
                c.execute("""UPDATE operator_funders SET status='failed', ts=?
                    WHERE deployer=? AND status='ok'""", (now, dep))
                c.execute("""UPDATE operator_funders SET status='failed', ts=?
                    WHERE funder IN (SELECT funder FROM operator_funders
                                     WHERE deployer=?) AND status='ok'""",
                    (now, dep))
    except Exception:
        pass  # guard pelengkap, jangan pernah raise
