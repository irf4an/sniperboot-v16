import sqlite3
import threading
import time
from . import config

_lock = threading.Lock()
_src_cache = {"ts": 0, "rows": []}

# ---------- 🚫 Alamat yang BUKAN dompet trader ----------
# 0x…dead adalah alamat BURN. Indexer mencatat transfer/event ke alamat ini
# sebagai "beli", sehingga satu alamat palsu bisa mengisi mayoritas tabel
# insider_buys (pernah 18.007 baris = 60,5% dari seluruh tabel, 1.337 CA).
# Alamat ini tidak punya kunci privat → mustahil benar-benar membeli.
_NON_TRADER_BODY = {
    "0" * 40,                                    # zero address
    "0" * 36 + "dead",                           # 0x000…dead (burn)
    "dead" + "0" * 36,                           # 0xdead000… (burn varian)
    "0" * 39 + "1",                              # ecrecover precompile
}


def is_trackable_wallet(address) -> bool:
    """True bila address bisa diperlakukan sebagai dompet trader sungguhan.

    Menolak: format salah, zero address, alamat burn (...dead), dan
    placeholder yang 32 byte pertamanya nol.
    """
    a = str(address or "").strip().lower()
    if len(a) != 42 or not a.startswith("0x"):
        return False
    body = a[2:]
    if body in _NON_TRADER_BODY:
        return False
    if int(body, 16) == 0:
        return False
    # pola burn/precompile: 16 byte pertama seluruhnya nol
    if body[:32] == "0" * 32:
        return False
    return True


def _conn():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS sources(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER, topic_id INTEGER DEFAULT 0,
            name TEXT, mcap_max REAL, enabled INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS calls(
            ca TEXT PRIMARY KEY, source_id INTEGER, ts INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS positions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ca TEXT, symbol TEXT, pair_address TEXT, chain_slug TEXT,
            pool_type TEXT, fee INTEGER,
            entry_price_usd REAL, tokens_total REAL, tokens_left REAL,
            eth_spent REAL, usd_spent REAL,
            realized_eth REAL DEFAULT 0,
            tp1_done INTEGER DEFAULT 0,
            status TEXT DEFAULT 'open',
            honeypot INTEGER DEFAULT 0,
            source_name TEXT, ts INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ca TEXT, symbol TEXT, role TEXT, action TEXT,
            conviction INTEGER, size_mult REAL,
            reason TEXT, risks TEXT, outcome TEXT, ts INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS lessons(
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, ts INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS wallets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, address TEXT, privkey TEXT,
            enc INTEGER DEFAULT 0, active INTEGER DEFAULT 0, ts INTEGER)""")
        for col in ("tp1_x REAL", "tp2_x REAL", "sl_x REAL",
                    "caller_id INTEGER", "caller_name TEXT", "buy_tx TEXT",
                    "peak_price REAL", "closed_at INTEGER",
                    "is_simulated INTEGER DEFAULT 0",
                    "tp2_done INTEGER DEFAULT 0"):
            try:
                c.execute(f"ALTER TABLE positions ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        for col in ("sender_id INTEGER", "sender_name TEXT"):
            try:
                c.execute(f"ALTER TABLE calls ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        c.execute("""CREATE TABLE IF NOT EXISTS sybil_blacklist(
            address TEXT PRIMARY KEY,
            reason TEXT,
            ca TEXT,
            wallet_count INTEGER,
            span_sec INTEGER,
            detected_at INTEGER)""")
        # ---- 📡 Radar Pons V2 (alert-only, TIDAK pernah masuk pipa beli) ----
        # Satu baris = satu event TokenLaunched dari factory Pons V2.
        # status: new → (enriched) → alerted | rejected | expired
        # Tidak ada kolom yang dipakai listener.process_ca / trader.buy.
        c.execute("""CREATE TABLE IF NOT EXISTS pons_queue(
            ca TEXT PRIMARY KEY,
            deployer TEXT,
            curve TEXT,
            launch_block INTEGER,
            launch_ts INTEGER,
            symbol TEXT,
            name TEXT,
            total_supply REAL,
            status TEXT DEFAULT 'new',
            score INTEGER,
            verdict TEXT,
            reason TEXT,
            llm_model TEXT,
            checked_at INTEGER,
            alerted_at INTEGER,
            block_buy INTEGER DEFAULT 0)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pons_status ON pons_queue(status)")
        # Migrasi aditif: DB yang dibuat sebelum kolom `block_buy` & curve metrics ada.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(pons_queue)")}
        if "block_buy" not in cols:
            c.execute("ALTER TABLE pons_queue ADD COLUMN block_buy INTEGER DEFAULT 0")
        curve_cols = {
            "progress": "REAL DEFAULT 0.0",
            "volume_usd": "REAL DEFAULT 0.0",
            "trade_count": "INTEGER DEFAULT 0",
            "market_cap_usd": "REAL DEFAULT 0.0",
            "raised_eth": "REAL DEFAULT 0.0",
            "creator_tax_bps": "INTEGER DEFAULT 0",
            "socials": "TEXT DEFAULT ''",
            "queued_at": "INTEGER DEFAULT 0",
        }
        for col_name, col_def in curve_cols.items():
            if col_name not in cols:
                c.execute(f"ALTER TABLE pons_queue ADD COLUMN {col_name} {col_def}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pons_launch_ts ON pons_queue(launch_ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pons_progress ON pons_queue(progress)")
        for k, v in config.SETTING_DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        # v16.5 (Solusi B): migrasi DB lama — longgarkan slippage anchor
        # Dexscreener 0.30 → 0.60 + percepat siklus scanner 15 → 5 detik.
        # ON CONFLICT DO UPDATE hanya kalau nilai masih default lama.
        for _k, _old, _new in (("buy_slippage", "0.30", "0.60"),
                               ("scanner_interval_sec", "15", "5")):
            c.execute("UPDATE settings SET value=:new "
                      "WHERE key=:k AND value=:old",
                      {"k": _k, "old": _old, "new": _new})
        # backfill: DB lama dari init pertama sebelum paper_entry_spread_pct ada
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                  ("paper_entry_spread_pct",
                   config.SETTING_DEFAULTS.get("paper_entry_spread_pct", "0.01")))
        # seed source awal kalau tabel kosong
        if not c.execute("SELECT 1 FROM sources LIMIT 1").fetchone() and config.SEED_GROUP_ID:
            c.execute("INSERT INTO sources(chat_id,topic_id,name,mcap_max) VALUES(?,?,?,?)",
                      (config.SEED_GROUP_ID, config.SEED_TOPIC_DEGEN, "Robinhood Degen Room", 30000))
            c.execute("INSERT INTO sources(chat_id,topic_id,name,mcap_max) VALUES(?,?,?,?)",
                      (config.SEED_GROUP_ID, config.SEED_TOPIC_MEMBER, "RH Member Call", 10000))


# ---------- settings ----------
def get(key, cast=float):
    with _lock, _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    v = r["value"] if r else config.SETTING_DEFAULTS.get(key, "0")
    return cast(v)


def set_(key, value):
    with _lock, _conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ---------- dompet kas virtual (paper trading) ----------
def get_paper_cash() -> float:
    """Saldo kas virtual mode latihan (USD)."""
    try:
        return float(get("paper_cash_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def deduct_paper_cash(amount: float) -> bool:
    """Potong kas virtual sebesar amount. False (tanpa mengubah saldo)
    bila kas tidak mencukupi."""
    cash = get_paper_cash()
    if cash < amount:
        return False
    set_("paper_cash_usd", str(cash - amount))
    return True


def add_paper_cash(amount: float) -> float:
    """Masukkan hasil jual (pokok + profit/rugi) ke kas virtual. Balikan
    balance baru."""
    cash = get_paper_cash() + amount
    set_("paper_cash_usd", str(cash))
    return cash


def reset_paper_cash(initial: float = 100.0):
    """Reset dompet kas virtual: modal awal dan saldo keduanya = initial."""
    set_("paper_initial_usd", str(initial))
    set_("paper_cash_usd", str(initial))


def paused() -> bool:
    return get("paused", str) == "1"


# ---------- sources ----------
def _invalidate():
    _src_cache["ts"] = 0


def sources(only_enabled=False):
    now = time.time()
    if now - _src_cache["ts"] > 5:
        with _lock, _conn() as c:
            _src_cache["rows"] = [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id")]
        _src_cache["ts"] = now
    rows = _src_cache["rows"]
    return [s for s in rows if s["enabled"]] if only_enabled else rows


def match_source(chat_id: int, topic_id: int):
    """Source enabled yang cocok dengan chat+topic. topic_id=0 di source = semua topic."""
    for s in sources(only_enabled=True):
        if s["chat_id"] == chat_id and (s["topic_id"] == 0 or s["topic_id"] == topic_id):
            return s
    return None


def add_source(chat_id, topic_id, name, mcap_max):
    with _lock, _conn() as c:
        c.execute("INSERT INTO sources(chat_id,topic_id,name,mcap_max) VALUES(?,?,?,?)",
                  (chat_id, topic_id, name, mcap_max))
    _invalidate()


def toggle_source(sid):
    with _lock, _conn() as c:
        c.execute("UPDATE sources SET enabled = 1 - enabled WHERE id=?", (sid,))
    _invalidate()


def del_source(sid):
    with _lock, _conn() as c:
        c.execute("DELETE FROM sources WHERE id=?", (sid,))
    _invalidate()


# ---------- calls / dedupe ----------
def seen_call(ca):
    with _lock, _conn() as c:
        return c.execute("SELECT 1 FROM calls WHERE ca=?", (ca.lower(),)).fetchone() is not None


def touch_call(ca):
    """Segarkan stempel waktu 'pernah dilihat' tanpa menambah baris baru.

    Dipakai jalur retry antrean pending: CA yang gagal gate boleh dievaluasi
    ulang, tapi dedup jangka panjang tetap berlaku begitu keluar antrean.
    """
    with _lock, _conn() as c:
        c.execute("UPDATE calls SET ts=? WHERE ca=?", (int(time.time()), ca.lower()))


def record_call(ca, source_id, sender_id=None, sender_name=None):
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO calls(ca,source_id,sender_id,sender_name,ts) VALUES(?,?,?,?,?)",
                  (ca.lower(), source_id, sender_id, sender_name, int(time.time())))


# ---------- positions ----------
def open_position(**kw):
    kw.setdefault("caller_id", None)
    kw.setdefault("caller_name", None)
    kw.setdefault("buy_tx", None)
    kw.setdefault("is_simulated", 0)
    with _lock, _conn() as c:
        c.execute("""INSERT INTO positions
            (ca,symbol,pair_address,chain_slug,pool_type,fee,entry_price_usd,
             tokens_total,tokens_left,eth_spent,usd_spent,honeypot,source_name,
             caller_id,caller_name,buy_tx,is_simulated,ts)
            VALUES(:ca,:symbol,:pair_address,:chain_slug,:pool_type,:fee,:entry_price_usd,
                   :tokens_total,:tokens_left,:eth_spent,:usd_spent,:honeypot,:source_name,
                   :caller_id,:caller_name,:buy_tx,:is_simulated,:ts)""",
            {**kw, "ts": int(time.time())})
    # catat entry ke entry_log clone_guard — SETELAH lock dilepas (db._lock
    # non-reentrant: memanggil note_entry di dalam `with _lock` = DEADLOCK).
    # Fail-open: kegagalan logging TIDAK boleh membatalkan posisi.
    try:
        from . import clone_guard as _cg
        _cg.note_entry(kw["ca"], kw.get("symbol", ""), kw.get("name", ""))
    except Exception:
        pass


def open_positions():
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM positions WHERE status='open'")]


def position(pid):
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def update_position(pid, **fields):
    sets = ",".join(f"{k}=?" for k in fields)
    with _lock, _conn() as c:
        c.execute(f"UPDATE positions SET {sets} WHERE id=?", (*fields.values(), pid))


def add_realized(pid, eth_out):
    with _lock, _conn() as c:
        c.execute("UPDATE positions SET realized_eth = realized_eth + ? WHERE id=?", (eth_out, pid))


def reset_stats(pos_id=None, call_rowid=None):
    """Set baseline offset ID untuk perhitungan statistik tanpa menghapus riwayat DB."""
    with _lock, _conn() as c:
        if pos_id is None:
            pos_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM positions").fetchone()[0]
        if call_rowid is None:
            call_rowid = c.execute("SELECT COALESCE(MAX(rowid), 0) FROM calls").fetchone()[0]
        now_ts = str(int(time.time()))
        for k, v in [("stats_baseline_pos_id", str(pos_id)),
                     ("stats_baseline_call_id", str(call_rowid)),
                     ("stats_baseline_ts", now_ts)]:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    return int(pos_id), int(call_rowid)


def stats():
    with _lock, _conn() as c:
        row = lambda q: c.execute(q).fetchone()[0]
        get_val = lambda k: (c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone() or [None])[0]
        try:
            base_pos = int(get_val("stats_baseline_pos_id") or 0)
        except (ValueError, TypeError):
            base_pos = 0
        try:
            base_call = int(get_val("stats_baseline_call_id") or 0)
        except (ValueError, TypeError):
            base_call = 0

        pos_cond = f"WHERE id > {base_pos}"
        pos_and = f"AND id > {base_pos}"
        call_cond = f"WHERE rowid > {base_call}"

        return {
            "calls":   row(f"SELECT COUNT(*) FROM calls {call_cond}"),
            "buys":    row(f"SELECT COUNT(*) FROM positions {pos_cond}"),
            "open":    row(f"SELECT COUNT(*) FROM positions WHERE status='open' {pos_and}"),
            "tp1":     row(f"SELECT COUNT(*) FROM positions WHERE tp1_done = 1 {pos_and}"),
            "tp2":     row(f"SELECT COUNT(*) FROM positions WHERE tp2_done = 1 {pos_and}"),
            "sl":      row(f"SELECT COUNT(*) FROM positions WHERE status='closed_sl' {pos_and}"),
            "timeout": row(f"SELECT COUNT(*) FROM positions WHERE status IN ('closed_timeout','closed_inactive') {pos_and}"),
            "manual":  row(f"SELECT COUNT(*) FROM positions WHERE status='closed_manual' {pos_and}"),
            "rug":     row(f"SELECT COUNT(*) FROM positions WHERE status='closed_rug' {pos_and}"),
            "trail":   row(f"SELECT COUNT(*) FROM positions WHERE status='closed_trail' {pos_and}"),
            "tp1full": row(f"SELECT COUNT(*) FROM positions WHERE status='closed_tp1' {pos_and}"),
            "empty":   row(f"SELECT COUNT(*) FROM positions WHERE status='closed_empty' {pos_and}"),
            "spent":   row(f"SELECT COALESCE(SUM(usd_spent),0) FROM positions {pos_cond}"),
            "eth_out": row(f"SELECT COALESCE(SUM(realized_eth),0) FROM positions {pos_cond}"),
            "eth_in":  row(f"SELECT COALESCE(SUM(eth_spent),0) FROM positions {pos_cond}"),
        }


# ---------- agent: decisions & lessons ----------
def log_decision(**kw):
    with _lock, _conn() as c:
        c.execute("""INSERT INTO decisions(ca,symbol,role,action,conviction,size_mult,reason,risks,ts)
            VALUES(:ca,:symbol,:role,:action,:conviction,:size_mult,:reason,:risks,:ts)""",
            {**kw, "ts": int(time.time())})


def recent_decisions(n=5, role=None):
    q = "SELECT * FROM decisions"
    if role:
        q += f" WHERE role='{role}'"
    q += " ORDER BY id DESC LIMIT ?"
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(q, (n,))]


def mark_decision_outcome(ca, outcome):
    with _lock, _conn() as c:
        c.execute("""UPDATE decisions SET outcome=? WHERE id=(
            SELECT id FROM decisions WHERE ca=? AND role='screener' ORDER BY id DESC LIMIT 1)""",
            (outcome, ca.lower()))


def add_lesson(text):
    with _lock, _conn() as c:
        c.execute("INSERT INTO lessons(text,ts) VALUES(?,?)", (text, int(time.time())))


def recent_lessons(n=5):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (n,))]


def count_closed():
    """Jumlah posisi yang benar-benar sudah ditutup (semua status closed*).

    Dipakai pemicu Auto-Tuner: tiap kelipatan 15 close. Dihitung langsung dari
    tabel positions supaya tidak bergantung pada penjumlahan stats() yang
    gampang dobel-hitung (tp2_done vs status).
    """
    with _lock, _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM positions WHERE status LIKE 'closed%'").fetchone()[0]


def closed_positions_with_decisions(n=20):
    with _lock, _conn() as c:
        rows = c.execute("""SELECT p.symbol, p.status, p.usd_spent, p.realized_eth,
            p.eth_spent, p.tp1_done, p.source_name,
            (SELECT reason FROM decisions d WHERE d.ca=p.ca AND d.role='screener'
             ORDER BY d.id DESC LIMIT 1) AS entry_reason,
            (SELECT conviction FROM decisions d WHERE d.ca=p.ca AND d.role='screener'
             ORDER BY d.id DESC LIMIT 1) AS conviction
            FROM positions p WHERE p.status LIKE 'closed%'
            ORDER BY p.id DESC LIMIT ?""", (n,)).fetchall()
        return [dict(r) for r in rows]


# ---------- caller scoring ----------
def caller_stats(caller_id):
    if not caller_id:
        return None
    with _lock, _conn() as c:
        r = c.execute("""SELECT COUNT(*) AS calls_bought,
            SUM(status='closed_tp2') AS full_tp,
            SUM(tp1_done) AS hit_tp1,
            SUM(status='closed_sl') AS stopped
            FROM positions WHERE caller_id=?""", (caller_id,)).fetchone()
        return dict(r)


def top_callers(n=10):
    with _lock, _conn() as c:
        rows = c.execute("""SELECT caller_name,
            COUNT(*) AS buys,
            SUM(tp1_done) AS hit_tp1,
            SUM(status='closed_tp2') AS full_tp,
            SUM(status='closed_sl') AS stopped
            FROM positions WHERE caller_id IS NOT NULL
            GROUP BY caller_id
            HAVING buys >= 1
            ORDER BY (CAST(SUM(tp1_done) AS REAL) / COUNT(*)) DESC, buys DESC
            LIMIT ?""", (n,)).fetchall()
        return [dict(r) for r in rows]


# ---------- rekap harian ----------
def recap_data(since_ts: int):
    with _lock, _conn() as c:
        row = lambda q, a=(): c.execute(q, a).fetchone()
        calls = row("SELECT COUNT(*) FROM calls WHERE ts>=?", (since_ts,))[0]
        buys = row("SELECT COUNT(*), COALESCE(SUM(usd_spent),0), COALESCE(SUM(eth_spent),0) "
                   "FROM positions WHERE ts>=?", (since_ts,))
        closed = [dict(r) for r in c.execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(realized_eth),0) AS eth_in, "
            "COALESCE(SUM(eth_spent),0) AS eth_out FROM positions "
            "WHERE closed_at>=? GROUP BY status", (since_ts,))]
        open_n = row("SELECT COUNT(*) FROM positions WHERE status='open'")[0]
        top = row("""SELECT caller_name, COUNT(*) AS n, SUM(tp1_done) AS tp1
            FROM positions WHERE ts>=? AND caller_id IS NOT NULL
            GROUP BY caller_id ORDER BY tp1 DESC, n DESC LIMIT 1""", (since_ts,))
        return {
            "calls": calls,
            "buys": buys[0], "usd_spent": buys[1], "eth_spent_new": buys[2],
            "closed": closed, "open": open_n,
            "top_caller": dict(top) if top and top["caller_name"] else None,
        }


# ======================= PENDING POOL RETRY QUEUE =======================
# Antrean CA yang terdeteksi on-chain tapi Dexscreener belum mengindeks
# pair-nya (indeks Dexscreener bisa molor 1-3 menit). Tanpa antrean ini CA
# langsung dibuang di collect_and_rank (best_pair -> None) dan hilang
# selamanya. CA ditahan sampai best_pair mulai mengembalikan pair, atau
# kedaluwarsa lewat max_age_sec.
_PENDING_TABLE = """CREATE TABLE IF NOT EXISTS pending_pools(
    ca TEXT PRIMARY KEY,
    first_seen INTEGER,
    retries INTEGER DEFAULT 0)"""


def add_pending_pool(ca: str):
    ca = str(ca).strip().lower()
    if not ca:
        return
    with _lock, _conn() as c:
        c.execute(_PENDING_TABLE)
        c.execute("INSERT OR IGNORE INTO pending_pools(ca, first_seen, retries)"
                  " VALUES(?,?,0)", (ca, int(time.time())))


def get_pending_pools(max_age_sec: int = 600) -> list:
    with _lock, _conn() as c:
        c.execute(_PENDING_TABLE)
        rows = c.execute(
            "SELECT ca, first_seen, retries FROM pending_pools"
            " WHERE (? - first_seen) <= ?", (int(time.time()), int(max_age_sec)),
        ).fetchall()
    return [{"ca": r["ca"], "first_seen": r["first_seen"], "retries": r["retries"]}
            for r in rows]


def remove_pending_pool(ca: str):
    ca = str(ca).strip().lower()
    if not ca:
        return
    with _lock, _conn() as c:
        c.execute(_PENDING_TABLE)
        c.execute("DELETE FROM pending_pools WHERE ca=?", (ca,))


def prune_pending_pools(max_age_sec: int = 600):
    with _lock, _conn() as c:
        c.execute(_PENDING_TABLE)
        c.execute("DELETE FROM pending_pools WHERE (? - first_seen) > ?",
                  (int(time.time()), int(max_age_sec)))


def increment_pending_retry(ca: str):
    ca = str(ca).strip().lower()
    if not ca:
        return
    with _lock, _conn() as c:
        c.execute(_PENDING_TABLE)
        c.execute("UPDATE pending_pools SET retries = retries + 1 WHERE ca=?",
                  (ca,))


# ======================= WALLETS (multi-wallet gaya trading bot) =======================
def _wallet_encrypt(pk_hex: str):
    """Return (stored_str, enc_flag). Enkripsi pakai WALLET_SECRET kalau ada
    (keystore JSON bawaan eth-account, tanpa dependency tambahan)."""
    secret = (config.WALLET_SECRET or "").strip()
    if secret:
        import json as _j
        from eth_account import Account
        ks = Account.encrypt(pk_hex, secret)
        return _j.dumps(ks), 1
    return pk_hex, 0


def wallet_privkey(row) -> str:
    """Ambil private key plaintext dari row wallet (dekripsi kalau perlu)."""
    if row["enc"]:
        secret = (config.WALLET_SECRET or "").strip()
        if not secret:
            raise RuntimeError("wallet terenkripsi tapi WALLET_SECRET di .env kosong")
        import json as _j
        from eth_account import Account
        k = Account.decrypt(_j.loads(row["privkey"]), secret)
        h = k.hex()
        return h if h.startswith("0x") else "0x" + h
    return row["privkey"]


def add_wallet(name, address, pk_hex, make_active=False) -> int:
    stored, enc = _wallet_encrypt(pk_hex)
    with _lock, _conn() as c:
        if make_active:
            c.execute("UPDATE wallets SET active=0")
        cur = c.execute(
            "INSERT INTO wallets(name,address,privkey,enc,active,ts) VALUES(?,?,?,?,?,?)",
            (name, address, stored, enc, 1 if make_active else 0, int(time.time())))
        return cur.lastrowid


def list_wallets():
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM wallets ORDER BY id")]


def get_wallet(wid):
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM wallets WHERE id=?", (wid,)).fetchone()
    return dict(r) if r else None


def active_wallet():
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM wallets WHERE active=1 LIMIT 1").fetchone()
    return dict(r) if r else None


def set_active_wallet(wid):
    with _lock, _conn() as c:
        c.execute("UPDATE wallets SET active=0")
        c.execute("UPDATE wallets SET active=1 WHERE id=?", (wid,))


def del_wallet(wid):
    with _lock, _conn() as c:
        c.execute("DELETE FROM wallets WHERE id=?", (wid,))


def wallet_count() -> int:
    with _lock, _conn() as c:
        return c.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]


# ======================= INSIDER WALLETS (v16.1) =======================
# Tabel kandidat "smart money": wallet yang terbukti masuk lebih dari satu
# koin pemenang di fase awal (hasil screening tools/screen_insiders.py).
# Tidak menyimpan private key — alamat publik + skor jejaknya saja.
def _init_insider_wallets(c):
    c.execute("""CREATE TABLE IF NOT EXISTS insider_wallets(
        address   TEXT PRIMARY KEY,
        label     TEXT DEFAULT '',
        win_count INTEGER DEFAULT 0,
        enabled   INTEGER DEFAULT 1,
        added_at  INTEGER)""")
    # kolom opsional di data lama — aman dipanggil berulang saat init
    for col in ("win_cas TEXT DEFAULT ''", "notes TEXT DEFAULT ''",
                "loss_count INTEGER DEFAULT 0",
                "last_seen INTEGER DEFAULT 0",
                # v16.9: skema bobot 2 sumber (Blockscout + GeckoTerminal)
                "score REAL DEFAULT 0",
                "sources TEXT DEFAULT ''",
                "last_verified INTEGER DEFAULT 0"):
        try:
            c.execute(f"ALTER TABLE insider_wallets ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass


def record_insider_result(address: str, won: bool) -> dict:
    """Catat hasil trade copy-trade insider.
    - won=True (TP): win_count +1, loss_count direset — wallet sehat.
    - won=False (SL): loss_count +1; mencapai 2x berturut-turut → wallet
      otomatis dinonaktifkan (enabled=0, sindikat burner rotasi).
    """
    address = str(address).strip().lower()
    now = int(time.time())
    with _lock, _conn() as c:
        _init_insider_wallets(c)
        if won:
            c.execute("""UPDATE insider_wallets
                         SET win_count = win_count + 1, loss_count = 0,
                             last_seen = ? WHERE address = ?""", (now, address))
            return {"address": address, "action": "win", "disabled": False}
        row = c.execute("SELECT loss_count FROM insider_wallets WHERE address=?",
                        (address,)).fetchone()
        new_loss = int(row["loss_count"] or 0) + 1
        if new_loss >= 2:
            c.execute("""UPDATE insider_wallets
                         SET loss_count = ?, enabled = 0, last_seen = ?
                         WHERE address = ?""", (new_loss, now, address))
            return {"address": address, "action": "loss", "disabled": True}
        c.execute("""UPDATE insider_wallets
                     SET loss_count = ?, last_seen = ? WHERE address = ?""",
                  (new_loss, now, address))
        return {"address": address, "action": "loss", "disabled": False}


def prune_stale_insiders(max_inactive_days: int = 3) -> int:
    """Nonaktifkan wallet insider yang lama tak terlihat ATAU terbukti
    boncos: last_seen > 0 dan tak aktif >= max_inactive_days hari (default
    3 hari / 72 jam — memcoin exit liquiditas cepat), dengan
    win_count < 2 (skor belum layak). Return jumlah yang dinonaktifkan."""
    cutoff = int(time.time()) - max_inactive_days * 86400
    with _lock, _conn() as c:
        _init_insider_wallets(c)
        cur = c.execute("""UPDATE insider_wallets SET enabled = 0
                           WHERE enabled = 1 AND last_seen > 0
                             AND last_seen < ?
                             AND win_count < 2""", (cutoff,))
        return cur.rowcount


def add_insider_wallet(address, label="", win_count=0, notes="", win_cas="",
                       update_if_exists=False, score=0.0, sources="",
                       last_verified=0, enabled=1, set_wins=False):
    """Catat/naikkan skor wallet insider. update_if_exists=True (dipakai
    screener saat wallet sudah ada tapi menang koin baru) → win_count
    ditambah, bukan ditimpa.

    score/sources/last_verified = hasil skema bobot 2 sumber (v16.9).
    enabled=0 dipakai untuk baris karantina (GeckoTerminal-only, belum
    terverifikasi Blockscout) — masuk tabel tapi tidak dipantau tracker.
    """
    address = str(address).strip().lower()
    # 🚫 Jangan pernah rekrut alamat burn/zero sebagai "insider" — auto_rotate
    # sempat memasukkan 0x…dead dan mencemari 60% tabel insider_buys.
    if not is_trackable_wallet(address) or is_sybil_blacklisted(address):
        return False
    with _lock, _conn() as c:
        _init_insider_wallets(c)
        if update_if_exists:
            # set_wins=True → win_count = fakta ulang (MAX, tidak pernah
            # menyusut); set_wins=False → diakumulasi (auto_rotate).
            wins_expr = ("MAX(win_count, excluded.win_count)" if set_wins
                         else "win_count + excluded.win_count")
            # Skor: scan yang mencakup CA >= sebelumnya = otoritatif (decay
            # berlaku, boleh turun). Scan parsial (CA lebih sedikit) tidak
            # boleh menurunkan skor — pakai yang tertinggi.
            score_expr = ("MAX(score, excluded.score)" if not set_wins else
                          "CASE WHEN excluded.win_count >= win_count "
                          "THEN excluded.score ELSE MAX(score, excluded.score) END")
            c.execute(f"""INSERT INTO insider_wallets
                (address,label,win_count,enabled,added_at,notes,win_cas,
                 score,sources,last_verified)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(address) DO UPDATE SET
                    win_count = {wins_expr},
                    score = {score_expr},
                    sources = CASE WHEN excluded.sources = ''
                                   THEN sources ELSE excluded.sources END,
                    last_verified = MAX(last_verified, excluded.last_verified),
                    win_cas   = CASE WHEN excluded.win_cas = ''
                                     THEN win_cas
                                     ELSE CASE WHEN win_cas = ''
                                               THEN excluded.win_cas
                                               ELSE win_cas || ',' || excluded.win_cas END END""",
                (address, label, int(win_count), int(enabled), int(time.time()),
                 notes, win_cas, float(score), sources, int(last_verified)))
        else:
            c.execute("""INSERT OR IGNORE INTO insider_wallets
                (address,label,win_count,enabled,added_at,notes,win_cas,
                 score,sources,last_verified)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (address, label, int(win_count), int(enabled), int(time.time()),
                 notes, win_cas, float(score), sources, int(last_verified)))


def get_active_insiders() -> list:
    """Daftar address insider yang aktif (huruf kecil, siap dibandingkan).
    Mengecualikan alamat sybil botnet yang ada di sybil_blacklist.
    """
    with _lock, _conn() as c:
        _init_insider_wallets(c)
        rows = c.execute("""
            SELECT address FROM insider_wallets
            WHERE enabled=1
              AND address NOT IN (SELECT address FROM sybil_blacklist)
        """).fetchall()
    return [r["address"].lower() for r in rows]


def toggle_insider(address):
    """Aktif/non-aktifkan wallet insider. Return status baru (int)."""
    address = str(address).strip().lower()
    with _lock, _conn() as c:
        _init_insider_wallets(c)
        c.execute("""UPDATE insider_wallets
                     SET enabled = 1 - enabled WHERE address=?""", (address,))
        r = c.execute("SELECT enabled FROM insider_wallets WHERE address=?",
                      (address,)).fetchone()
    return r["enabled"] if r else None


def list_insiders(only_enabled=False):
    with _lock, _conn() as c:
        _init_insider_wallets(c)
        q = "SELECT * FROM insider_wallets"
        if only_enabled:
            q += " WHERE enabled=1"
        q += " ORDER BY score DESC, win_count DESC, added_at DESC"
        return [dict(r) for r in c.execute(q)]


def insider_labels(addrs) -> dict:
    """Map address (huruf kecil) → label operator, hanya untuk `addrs`.

    Dipakai notifikasi tracker & panel control_bot supaya keduanya
    menampilkan nama wallet yang sama. Gagal DB tidak boleh memutus alert.
    """
    labels = {}
    try:
        for row in list_insiders():
            addr = (row.get("address") or "").lower()
            label = (row.get("label") or "").strip()
            if addr and label and addr in addrs:
                labels[addr] = label
    except Exception:
        pass
    return labels


def _init_insider_buys(c):
    c.execute("""CREATE TABLE IF NOT EXISTS insider_buys(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ca TEXT, wallet TEXT, symbol TEXT DEFAULT '?',
        mcap REAL DEFAULT 0, liq REAL DEFAULT 0,
        pair_address TEXT DEFAULT '', ts INTEGER)""")


def record_insider_buy(ca, wallet="", symbol="?", mcap=0.0, liq=0.0,
                       pair_address="", ts=None):
    # 🚫 Tolak alamat yang bukan dompet trader (burn/zero/precompile/sybil blacklist).
    # Transfer ke 0x…dead bukan pembelian; mencatatnya menggelembungkan
    # hitungan dompet dan membuat log insider tidak bisa dipercaya.
    if wallet:
        w_clean = str(wallet).strip().lower()
        if not is_trackable_wallet(w_clean) or is_sybil_blacklisted(w_clean):
            return
    now = int(ts if ts is not None else time.time())
    with _lock, _conn() as c:
        _init_insider_buys(c)
        c.execute("""INSERT INTO insider_buys(ca,wallet,symbol,mcap,liq,pair_address,ts)
                     VALUES(?,?,?,?,?,?,?)""",
                  ((ca or "").lower(), (wallet or "").lower(),
                   symbol or "?", float(mcap or 0), float(liq or 0),
                   pair_address or "", now))
        c.execute("DELETE FROM insider_buys WHERE ts < ?", (now - 86400,))


def top_insider_buys(window_sec=3600, limit=10, now=None):
    now_i = int(now if now is not None else time.time())
    cutoff = now_i - max(60, int(window_sec or 3600))
    with _lock, _conn() as c:
        _init_insider_buys(c)
        rows = c.execute("""
            SELECT ca,
                   MAX(symbol) AS symbol,
                   COUNT(*) AS buy_count,
                   -- Dompet kosong (wallet="") JANGAN dihitung sebagai
                   -- breadth: itu data tidak dikenal, bukan keroyokan.
                   COUNT(DISTINCT CASE WHEN wallet IS NOT NULL AND wallet <> ''
                                       THEN wallet END) AS wallet_count,
                   MAX(mcap) AS mcap,
                   MAX(liq) AS liq,
                   MAX(pair_address) AS pair_address,
                   MAX(ts) AS last_ts
            FROM insider_buys
            WHERE ts >= ?
              -- 🚫 Sabuk pengaman: jangan pernah tampilkan alamat burn/zero atau sybil blacklist
              -- walau ada baris lama yang lolos sebelum guard ingestion.
              AND (wallet IS NULL OR wallet = ''
                   OR (wallet NOT LIKE '0x00000000000000000000000000000000%'
                       AND wallet <> '0xdead000000000000000042069420694206942069'
                       AND wallet NOT IN (SELECT address FROM sybil_blacklist)))
            GROUP BY ca
            -- v16.9 (Opsi A): peringkat utama = BREADTH (jumlah dompet unik),
            -- baru jumlah transaksi. Koin yang dikeroyok 11 wallet berbeda
            -- tidak boleh tenggelam di bawah 1-2 bot wash-trading yang
            -- menembak CA yang sama puluhan kali.
            ORDER BY wallet_count DESC, buy_count DESC, last_ts DESC
            LIMIT ?""", (cutoff, int(limit))).fetchall()
        return [dict(r) for r in rows]


# ---------- 🤖 Anti Wash-Trading Sybil Cluster (v16.9) ----------

def is_sybil_blacklisted(address: str) -> bool:
    if not address:
        return False
    a = str(address).strip().lower()
    with _lock, _conn() as c:
        return c.execute("SELECT 1 FROM sybil_blacklist WHERE address = ?", (a,)).fetchone() is not None


def list_sybil_blacklist() -> list[dict]:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM sybil_blacklist ORDER BY detected_at DESC").fetchall()]


def detect_and_purge_sybil_clusters(min_wallets=None, window_sec=None, ca=None) -> list[dict]:
    """Deteksi klaster wash-trading dev bot (Matriks Dinamis 3-Tier):
    - Tier 1 (Swarm): >= min_wallets (default 6) dlm <= window_sec (default 45s) di 1 koin.
    - Tier 2 (Squad): 4-5 dompet dlm <= 30s berulang di >= 2 koin berbeda.
    - Tier 3 (Micro Pair): 2-3 dompet dlm <= 25s berulang di >= 3 koin berbeda.
    Semua dompet sybil diblokir, masuk blacklist, dinonaktifkan di insider_wallets,
    dan dibersihkan dari insider_buys.
    """
    if min_wallets is None:
        try:
            min_wallets = int(get("sybil_cluster_min_wallets", str) or 6)
        except Exception:
            min_wallets = 6
    if window_sec is None:
        try:
            window_sec = int(get("sybil_cluster_window_sec", str) or 45)
        except Exception:
            window_sec = 45

    query = """
        SELECT ca, wallet, ts, symbol
        FROM insider_buys
        WHERE wallet IS NOT NULL AND wallet <> ''
    """
    params = []
    if ca:
        query += " AND ca = ?"
        params.append(str(ca).strip().lower())
    query += " ORDER BY ca, ts ASC"

    with _lock, _conn() as c:
        _init_insider_buys(c)
        _init_insider_wallets(c)
        rows = c.execute(query, params).fetchall()
        from collections import defaultdict
        ca_buys = defaultdict(list)
        for r in rows:
            ca_buys[r["ca"]].append((r["ts"], (r["wallet"] or "").strip().lower(), r["symbol"]))

        clusters = []
        all_sybil_wallets = set()
        now_ts = int(time.time())

        # ---- Tier 1: Swarm (Keroyokan di 1 koin) ----
        for c_ca, buys in ca_buys.items():
            sym = buys[0][2] if buys else ""
            n = len(buys)
            for i in range(n):
                t0 = buys[i][0]
                w_set = {buys[i][1]}
                span_sec = 0
                for j in range(i + 1, n):
                    t1 = buys[j][0]
                    if t1 - t0 <= window_sec:
                        w_set.add(buys[j][1])
                        span_sec = t1 - t0
                    else:
                        break
                if len(w_set) >= min_wallets:
                    wallets_list = list(w_set)
                    clusters.append({
                        "tier": "swarm",
                        "ca": c_ca,
                        "symbol": sym,
                        "wallets": wallets_list,
                        "span_sec": span_sec,
                        "detected_at": now_ts,
                    })
                    all_sybil_wallets.update(wallets_list)
                    break

        # ---- Tier 2 & 3: Multi-coin Co-occurrence (Pasangan/Regu berulang) ----
        # Hanya jalan saat scan global (ca=None) atau seluruh DB
        if not ca and len(ca_buys) >= 2:
            pair_cas = defaultdict(set)
            for c_ca, buys in ca_buys.items():
                n = len(buys)
                for i in range(n):
                    ti, wi, _ = buys[i]
                    for j in range(i + 1, n):
                        tj, wj, _ = buys[j]
                        if tj - ti <= 25:
                            if wi != wj:
                                pair = tuple(sorted([wi, wj]))
                                pair_cas[pair].add(c_ca)
                        else:
                            break

            for (w1, w2), cas in pair_cas.items():
                if len(cas) >= 3:
                    clusters.append({
                        "tier": "micro_pair",
                        "ca": ",".join(list(cas)[:3]),
                        "symbol": "MULTI",
                        "wallets": [w1, w2],
                        "span_sec": 25,
                        "detected_at": now_ts,
                    })
                    all_sybil_wallets.add(w1)
                    all_sybil_wallets.add(w2)

        if all_sybil_wallets:
            for cl in clusters:
                tier_name = cl.get("tier", "swarm")
                for w in cl["wallets"]:
                    c.execute("""
                        INSERT OR REPLACE INTO sybil_blacklist
                        (address, reason, ca, wallet_count, span_sec, detected_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (w, f"klaster sybil {tier_name} ({len(cl['wallets'])} wallet dlm {cl['span_sec']}s)",
                          cl["ca"], len(cl["wallets"]), cl["span_sec"], now_ts))
                    c.execute("""
                        UPDATE insider_wallets
                        SET enabled = 0, label = 'sybil_cluster',
                            notes = ?
                        WHERE address = ?
                    """, (f"klaster wash trading {tier_name} ({len(cl['wallets'])} wallet)", w))
                    c.execute("DELETE FROM insider_buys WHERE wallet = ?", (w,))

        return clusters


# ==================== 📡 RADAR PONS V2 (alert-only) ====================
# Lapisan data MURNI: tidak ada RPC, tidak ada Telegram, tidak ada keputusan
# beli. Radar (sniper/pons_radar.py) yang mengisi & membacanya. Tabel ini
# sengaja TERPISAH dari calls/positions supaya token pre-graduasi mustahil
# ikut masuk pipa listener.process_ca / trader.buy.
_PONS_UPDATABLE = {"symbol", "name", "total_supply", "status", "score",
                   "verdict", "reason", "llm_model", "checked_at", "alerted_at",
                   "block_buy", "progress", "volume_usd", "trade_count",
                   "market_cap_usd", "raised_eth", "creator_tax_bps", "socials",
                   "queued_at"}


def upsert_pons_curve_candidate(ca, deployer=None, curve=None, launch_block=0,
                                launch_ts=0, symbol="", name="", total_supply=0.0,
                                progress=0.0, volume_usd=0.0, trade_count=0,
                                market_cap_usd=0.0, raised_eth=0.0,
                                creator_tax_bps=0, socials="") -> bool:
    """Catat atau perbarui kandidat kurva Pons V2 dari API.

    Jika CA belum pernah tercatat: INSERT status='new'.
    Jika CA sudah ada: update data progress, volume, trade, dll.
    Jika status sebelumnya 'expired' (bukan alerted / block_buy=1),
    kembalikan ke 'new' agar dinilai ulang bila progress naik.
    Return True jika baris baru atau status kembali ke 'new'.
    """
    ca = (ca or "").strip().lower()
    if not ca:
        return False
    now = int(time.time())
    with _lock, _conn() as c:
        row = c.execute("SELECT status, block_buy FROM pons_queue WHERE ca=?", (ca,)).fetchone()
        if not row:
            c.execute(
                """INSERT INTO pons_queue
                   (ca, deployer, curve, launch_block, launch_ts, symbol, name,
                    total_supply, progress, volume_usd, trade_count,
                    market_cap_usd, raised_eth, creator_tax_bps, socials, queued_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')""",
                (ca, (deployer or "").strip().lower() or None,
                 (curve or "").strip().lower() or None,
                 int(launch_block or 0), int(launch_ts or 0),
                 symbol or "", name or "", float(total_supply or 0.0),
                 float(progress or 0.0), float(volume_usd or 0.0),
                 int(trade_count or 0), float(market_cap_usd or 0.0),
                 float(raised_eth or 0.0), int(creator_tax_bps or 0),
                 socials or "", now))
            return True
        st = row["status"]
        bb = row["block_buy"]
        new_status = st
        reset = False
        if st in ("expired", "rejected") and bb != 1:
            new_status = "new"
            reset = True
        c.execute(
            """UPDATE pons_queue SET
               progress=?, volume_usd=?, trade_count=?, market_cap_usd=?,
               raised_eth=?, creator_tax_bps=?, socials=?,
               symbol=COALESCE(NULLIF(?, ''), symbol),
               name=COALESCE(NULLIF(?, ''), name),
               status=?,
               queued_at=CASE WHEN ?='new' AND status!='new' THEN ? ELSE queued_at END
               WHERE ca=?""",
            (float(progress or 0.0), float(volume_usd or 0.0),
             int(trade_count or 0), float(market_cap_usd or 0.0),
             float(raised_eth or 0.0), int(creator_tax_bps or 0),
             socials or "", symbol or "", name or "", new_status,
             new_status, now, ca))
        return reset


def update_pons_token(ca, **fields):
    """Update kolom pons_queue yang diizinkan saja (whitelist _PONS_UPDATABLE)."""
    kv = {k: v for k, v in fields.items() if k in _PONS_UPDATABLE}
    if not kv or not ca:
        return
    sets = ", ".join(f"{k}=?" for k in kv)
    with _lock, _conn() as c:
        c.execute(f"UPDATE pons_queue SET {sets} WHERE ca=?",
                  (*kv.values(), str(ca).strip().lower()))


def get_pons_pending(limit=50) -> list:
    """Antrean yang belum diputuskan, prioritas progress kurva tertinggi."""
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM pons_queue WHERE status='new' "
            "ORDER BY progress DESC, launch_ts ASC LIMIT ?", (int(limit),))]


def reject_pons(ca, reason, status="rejected", score=0, verdict="BUANG",
                llm_model=None, block_buy=False):
    """Tandai kandidat ditolak (heuristik maupun LLM) + simpan alasannya.

    `block_buy=True` HANYA untuk penolakan yang bersifat penilaian (LLM
    BUANG, skor di bawah ambang). Penolakan operasional — cap alert per jam
    penuh, RPC gagal — TIDAK boleh mem-blacklist koin, karena bukan berarti
    koinnya jelek.
    """
    update_pons_token(ca, status=status, reason=(reason or "")[:400],
                      score=int(score or 0), verdict=verdict,
                      llm_model=llm_model, checked_at=int(time.time()),
                      block_buy=1 if block_buy else 0)


def get_pons_blocked_cas() -> set:
    """Set CA yang diveto Radar Pons (dibaca listener sebagai blacklist 0 ms).

    Hanya baris `block_buy=1`. Dipanggil tiap 30 detik oleh loop refresh,
    BUKAN per kandidat — jalur beli cukup memeriksa set di memori.
    """
    with _lock, _conn() as c:
        return {str(r["ca"]).lower() for r in
                c.execute("SELECT ca FROM pons_queue WHERE block_buy=1")}


def mark_pons_alerted(ca, symbol=None, name=None, score=0, verdict="AMAN",
                      reason="", llm_model=None):
    """Tandai kandidat sudah dikirim sebagai alert Telegram."""
    now = int(time.time())
    update_pons_token(ca, symbol=symbol, name=name, score=int(score or 0),
                      verdict=verdict, reason=(reason or "")[:400],
                      llm_model=llm_model, status="alerted",
                      checked_at=now, alerted_at=now)


def expire_pons_queue(max_age_sec=3600) -> int:
    """Kandidat 'new' yang sudah kelewat tua dianggap tidak relevan."""
    cutoff = int(time.time()) - int(max_age_sec)
    with _lock, _conn() as c:
        cur = c.execute(
            """UPDATE pons_queue SET status='expired'
               WHERE status='new' AND (
                   (queued_at > 0 AND queued_at < ?) OR
                   (queued_at = 0 AND launch_ts > 0 AND launch_ts < ?)
               )""", (cutoff, cutoff))
        return cur.rowcount


def pons_alert_count_since(ts) -> int:
    """Jumlah alert terkirim sejak `ts` — dipakai cap per jam."""
    with _lock, _conn() as c:
        return int(c.execute(
            "SELECT COUNT(*) FROM pons_queue WHERE status='alerted' AND alerted_at >= ?",
            (int(ts),)).fetchone()[0])


def pons_stats(hours=24) -> dict:
    """Ringkasan radar: hitungan per status + alert dalam jendela `hours`."""
    cutoff = int(time.time()) - int(hours) * 3600
    out = {"total": 0, "new": 0, "alerted": 0, "rejected": 0,
           "expired": 0, "alerts_24h": 0}
    with _lock, _conn() as c:
        for r in c.execute("SELECT status, COUNT(*) n FROM pons_queue GROUP BY status"):
            out[r["status"]] = int(r["n"])
            out["total"] += int(r["n"])
        out["alerts_24h"] = int(c.execute(
            "SELECT COUNT(*) FROM pons_queue WHERE status='alerted' AND alerted_at >= ?",
            (cutoff,)).fetchone()[0])
    return out


def recent_pons_alerts(hours=24, limit=20) -> list:
    cutoff = int(time.time()) - int(hours) * 3600
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM pons_queue WHERE status='alerted' AND alerted_at >= ? "
            "ORDER BY alerted_at DESC LIMIT ?", (cutoff, int(limit)))]


def pons_name_cluster(norm_name, since_ts) -> int:
    """Berapa kali nama token (lowercase, sudah dirapikan) muncul sejak since_ts.

    Dipakai mendeteksi sindikat spam: dev yang meluncurkan koin bernama sama
    berkali-kali dalam satu jam (mis. 'Wander Forest' 3x dengan simbol beda).
    """
    name = " ".join((norm_name or "").lower().split())
    if not name:
        return 0
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT name FROM pons_queue WHERE launch_ts >= ? "
            "AND name IS NOT NULL AND name <> ''", (int(since_ts),)).fetchall()
    return sum(1 for r in rows
               if " ".join(str(r["name"] or "").lower().split()) == name)


def pons_deployer_history(deployer, window_sec=3600) -> dict:
    """Rekam jejak deployer DI RADAR INI saja (bukan registry global).

    Dihitung dalam jendela `window_sec` (default 1 jam) supaya angka yang
    ditampilkan ke Telegram benar-benar berarti "launch/jam", bukan total
    sepanjang tabel hidup.
    """
    d = (deployer or "").strip().lower()
    if not d:
        return {"launches": 0, "alerted": 0}
    cutoff = int(time.time()) - int(window_sec)
    with _lock, _conn() as c:
        launches = c.execute(
            "SELECT COUNT(*) FROM pons_queue WHERE deployer=? AND launch_ts >= ?",
            (d, cutoff)).fetchone()[0]
        alerted = c.execute(
            "SELECT COUNT(*) FROM pons_queue WHERE deployer=? AND launch_ts >= ? "
            "AND status='alerted'", (d, cutoff)).fetchone()[0]
    return {"launches": int(launches), "alerted": int(alerted)}




