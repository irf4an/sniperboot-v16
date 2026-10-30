import os
from dotenv import load_dotenv

load_dotenv()

def _i(key, default):
    v = os.getenv(key)
    if v is None or v.strip() == "":
        return int(default)
    return int(v)

# ---- Chain / RPC ----
RPC_URL       = os.getenv("RPC_URL", "https://robinhood-rpc.publicnode.com")
CHAIN_ID      = _i("CHAIN_ID", 4663)
PRIVATE_KEY   = os.getenv("PRIVATE_KEY", "")

# ---- Uniswap di Robinhood Chain (Chain 4663) ----
SWAP_ROUTER02 = os.getenv("SWAP_ROUTER02", "0xcaf681a66d020601342297493863e78c959e5cb2")

# ---- Telegram userbot (Telethon, QR login) ----
TG_API_ID     = _i("TG_API_ID", 0)
TG_API_HASH   = os.getenv("TG_API_HASH", "")
SESSION_NAME  = os.getenv("SESSION_NAME", "hoodsniper")

# ---- Control bot (BotFather) ----
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
ADMIN_ID      = _i("ADMIN_ID", 0)          # user id Telegram Kakak (cek via @userinfobot)

# ---- Log publik (mode publik: orang lain HANYA lihat log filter koin) ----
# PUBLIC_LOG_CHAT_ID: chat_id channel/grup PUBLIK tempat log filter koin
# (SKIP/BUY/SIMULASI dari listener.process_ca) disiarkan TANPA tombol aksi.
# Bot WAJIB jadi admin di channel itu supaya bisa posting.
# 0 / kosong = log publik NONAKTIF (perilaku lama: semua notif cuma ke admin).
# Cara dapat id: forward 1 pesan dari channel ke DM bot ini (perintah /id admin).
PUBLIC_LOG_CHAT_ID = _i("PUBLIC_LOG_CHAT_ID", 0)
# Link publik channel (mis. https://t.me/nama_channel) — ditampilkan ke
# user non-admin saat /start supaya mereka tahu ke mana melihat log.
# Kosong = bot hanya bilang log ada di channel publik, tanpa tombol link.
PUBLIC_LOG_LINK    = os.getenv("PUBLIC_LOG_LINK", "").strip()

# ---- Seed source awal (opsional, dipakai kalau tabel sources masih kosong) ----
SEED_GROUP_ID    = _i("SEED_GROUP_ID", 0)  # 0 = mulai bersih (tanpa auto-seed)
SEED_TOPIC_DEGEN = _i("SEED_TOPIC_DEGEN", 250551)
SEED_TOPIC_MEMBER= _i("SEED_TOPIC_MEMBER", 395868)

# ---- Dexscreener ----
DEX_CHAIN_MATCH = os.getenv("DEX_CHAIN_MATCH", "robinhood")

# ---- Blockscout (safety filter) ----
BLOCKSCOUT_API = os.getenv("BLOCKSCOUT_API", "https://robinhoodchain.blockscout.com/api/v2")
EXPLORER_URL   = os.getenv("EXPLORER_URL", "https://robinhoodchain.blockscout.com")

# ---- Agent (LLM multi-provider) ----
# LLM_PROTOCOL: "anthropic" atau "openai" (OpenAI-compatible: OpenAI/DeepSeek/GLM/
# Kimi/Qwen/OpenRouter/9router/LM Studio — tinggal ganti LLM_BASE_URL)
LLM_PROTOCOL = os.getenv("LLM_PROTOCOL", "anthropic").lower()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY  = os.getenv("LLM_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_KEY = LLM_API_KEY  # alias backwards-compat

# ---- Wallet (multi-wallet via bot). Isi WALLET_SECRET utk enkripsi key di DB. ----
WALLET_SECRET = os.getenv("WALLET_SECRET", "")

DB_PATH = os.getenv("DB_PATH", "hoodsniper.db")

# Default settings runtime (masuk DB saat init pertama, selanjutnya diatur via bot)
SETTING_DEFAULTS = {
    "buy_usd":        "5.0",
    # ---- 🧪 Paper trading: biaya transaksi nyata (identik mainnet) ----
    # Gas per swap di Robinhood Chain L2 + fee pool DEX & slippage nyata.
    "paper_gas_eth":       "0.0001",
    "paper_slippage_pct":  "0.02",
    # Spread entry: harga beli didorong naik oleh latency broadcast
    # (300-500ms / 5-8 blok pump +1..3%) + price impact + fee pool DEX.
    # Koin wajib naik +2.5% dulu untuk mencapai titik impas (breakeven).
    "paper_entry_spread_pct": "0.025",
    "tp1":            "0.20",
    "tp2":            "0.45",
    "sl":             "-0.30",
    "tp1_sell_pct":   "0.60",
    # --- TP2 jual 100% sisa (rug LP v4 terukur 302-425s, moonbag hangus). Turunkan <1.0 untuk aktifkan moonbag ---
    "tp2_sell_pct":   "1.0",
    "moonbag_trail_pct": "0.20",
    "tp_verify":      "1",
    "rug_liq_drop":   "0.40",
    "trail_on":       "1",
    "trail_pct":      "0.25",
    "tp2_on":         "1",
    "recap_on":       "1",
    "recap_hour":     "7",
    "buy_slippage":   "0.60",
    "sell_slippage":  "0.35",
    "min_liq_usd":    "500",
    "poll_seconds":   "3",
    "max_hold_sec":   "240",
    # ---- 🌙 Moonbag dual-guard (v16.7) ----
    # inactivity: 7 menit tanpa transaksi (Dexscreener txns.m5 = 0) ATAU
    # quote jual on-chain = 0 → EXIT INACTIVE.
    # hard cap: 45 menit batas maksimal runner moonbag → HARVEST FULL.
    "moonbag_inactivity_sec": "420",
    "moonbag_max_hold_sec":   "2700",
    # ---- 🎭 Aturan khusus CA vanity berakhiran ...7777 (v16.9, Opsi B) ----
    # TEMUAN DATA (107 posisi closed): rug rate ...7777 = 19% vs non-7777
    # = 18%; avg PnL ...7777 +28.6% vs non-7777 +32.2%. Secara statistik
    # TIDAK ADA bedanya — sufiks 7777 bukan prediktor rug. Karena itu
    # default DISABLED supaya tidak memotong winner yang justru keluar
    # lewat TIMEOUT di menit 4 (avg +43%).
    # Kalau operator tetap mau mengetatkan: set vanity7777_enabled=1 dan
    # time-stop khusus 7777 dipakai (min dari max_hold_sec & nilai ini).
    "vanity7777_enabled":      "0",
    "vanity7777_max_hold_sec": "120",
    "paused":         "0",
    "safety_on":      "1",
    "dev_max_pct":    "5",
    "top10_max_pct":  "40",
    "safety_fail_closed": "1",
    # ---- Filter sosmed (jejak X/Web/TG dari Dexscreener info) ----
    "social_filter":      "any",   # off | any | x | tg | x_tg | web | count
    "social_min_count":   "1",     # min jumlah link sosmed (mode 'count')
    "social_fail_closed": "0",     # 1 = kalau info sosmed kosong/gagal → SKIP
    "agent_on":       "0",
    "agent_manage_on": "0",
    "agent_dry_run":  "1",
    "agent_max_drift": "0.50",
    "agent_model":    "claude-haiku-4-5",
    "agent_size_mult_max": "2",
    "agent_review_min": "10",
    "agent_min_conviction": "55",
    "agent_blacklist": "1",
    "agent_auto_insting": "1",
    # ---- 🔭 Scanner (v16 gabungan): sumber sinyal KEDUA di samping listener grup.
    # Semantik mode menyusul v15: eksekusi diatur mode global (agent_dry_run
    # default 1), BUKAN kunci keras seperti prototipe v14. ----
    # Concurrency (v16.4): scanner memproses MAKS scanner_batch_max koin
    # teratas per siklus, dievaluasi PARALEL dengan Semaphore
    #(scanner_concurrency). Di Robinhood Chain (blok 0.1-0.2s), antrean
    # serial 10-20 detik = tertinggal 50-100 blok → koin kedua sudah
    # dipompa orang lain. Paralel 2 menghilangkan delay itu.
    "scanner_batch_max":     "2",
    "scanner_concurrency":   "2",
    "scanner_on":            "1",
    "scanner_interval_sec":  "5",
    "scanner_mcap_max":      "45000",
    "scanner_ignore_v3":     "1",
    "scanner_min_liq":       "2000",
    "scanner_min_vol_h1":    "1000",
    "scanner_min_txns_h1":   "20",
    "scanner_max_age_min":   "5",
    "scanner_max_h1_pump":   "150",
    "scanner_max_per_hour":  "0",
    "scanner_factory_on":    "1",
    "scanner_last_block":    "0",
    # ---- 🕵️ Insider Tracker (v16.1): sumber sinyal KETIGA (copy-trade smart money) ----
    "tracker_on":                    "1",
    "tracker_last_block":    "0",
    # ---- 🔔 Insider alert-only (v16.8, Opsi 1) ----
    # "1" = insider buy CUMA kirim notifikasi Telegram ringkas (tanpa
    #       masuk pipa listener.process_ca — tidak ada posisi baru).
    # "0" = perilaku lama: CA diteruskan ke pipa bot (loud fallback).
    "insider_alert_only":    "1",
    # ---- 🧹 Dedup alert insider (wallet sama + CA sama dalam satu jendela waktu) ----
    # Detik jendela dedup: beli berulang wallet yang sama di token yang sama
    # dalam jendela ini hanya kirim notif mini "(+N beli lagi…)". "0" = mati.
    "insider_alert_dedup_window_sec": "600",
    # ---- 🔄 Rotasi wallet insider (v16.3): prune boncos + rekrut dari runner ----
    "tracker_auto_rotate":   "1",
    # ---- 🧪 Dompet kas virtual (paper trading mode latihan) ----
    "paper_initial_usd": "20.0",
    "paper_cash_usd":    "20.0",
    # ---- ⚡ FAST-PATH Goldilocks Defensive (entry tanpa LLM) ----
    # entry_mode: 'mata' = MATA (LLM memutuskan), 'fast' = evaluasi murni
    # matematika/bytecode (0 detik LLM). Toggle via panel 🧠 Otak.
    "entry_mode":                 "mata",
    "goldilocks_min_age_sec":     "10",
    "goldilocks_max_age_sec":     "300",
    "goldilocks_min_liq":         "4000.0",
    "goldilocks_min_liq_mcap_ratio": "0.35",
    "goldilocks_max_mcap":        "35000.0",
    "goldilocks_min_mcap":        "5000.0",
    "goldilocks_max_dev_pct":     "0.04",
    "goldilocks_max_top10_pct":   "0.30",
    "goldilocks_max_slippage":    "0.25",
    "goldilocks_tp1":             "0.35",
    "goldilocks_tp2":             "0.80",
    # ---- 🤖 Sybil Cluster Guard: anti wash-trading dev bot (v16.9) ----
    # Jika N dompet insider beli koin yang sama dalam jendela waktu W detik:
    # Teridentifikasi sebagai klaster wash-trading dev bot → blokir & buang.
    "sybil_cluster_guard_on":        "1",
    "sybil_cluster_min_wallets":     "7",
    "sybil_cluster_window_sec":      "60",
    # ---- 🛡️ Clone guard / operator-family guard (v16.6) ----
    # Entry rate-limit per keluarga operator: maks N CA DISTINCT per keluarga
    # dalam jendela H jam. Bukan blacklist token — Pons tipe apa pun tetap
    # boleh dibeli selama keluarganya belum kelebihan jatah entry.
    # (Nama key memakai '_per_day' demi kompatibilitas kontraksi tugas, nilai
    #  default window 24 jam.)
    "clone_max_entries_per_day": "3",
    "clone_entry_window_hours":  "24",
    # ---- 📡 Userbot Telegram (alpha groups listener) — 100% OPSIONAL ----
    # Default '0' = NONAKTIF: akun pribadi tidak online di VPS (bebas risiko
    # ban/flag), sinyal 100% dipasok on-chain (factory & insider tracker).
    # '1' = aktif kembali mirror sinyal grup via akun Telegram sendiri.
    # Mode `python bot.py login` selalu connect (untuk QR login apapun).
    "userbot_enabled": "0",
}

SETTING_LABELS = {
    "buy_usd":       "💵 Buy size (USD)",
    "tp1":           "🎯 TP1 (0.5 = +50%)",
    "tp2":           "🎯 TP2 (1.0 = +100%)",
    "sl":            "🛑 SL (-0.3 = -30%)",
    "tp1_sell_pct":  "📤 Jual di TP1 (0.5 = 50%)",
    "tp2_sell_pct":  "🎯 TP2: porsi sisa yang dijual (0.50 = 50%, 1.0 = full close)",
    "moonbag_trail_pct": "🚀 Moonbag: jarak trailing stop dari puncak (0.20 = 20%)",
    "tp_verify":     "🫧 Verifikasi TP anti-wick (1/0)",
    "rug_liq_drop":  "🚨 Rug: liq drop trigger (0.4=40%)",
    "trail_on":      "📈 Trailing stop setelah TP1 (1/0)",
    "trail_pct":     "📈 Trailing % dari puncak (0.25)",
    "tp2_on":        "🎯 TP2 aktif (0 = biarkan runner lari)",
    "recap_on":      "🌅 Rekap harian (1/0)",
    "recap_hour":    "🌅 Jam rekap WIB (0-23)",
    "buy_slippage":  "📈 Slippage buy",
    "sell_slippage": "📉 Slippage sell",
    "min_liq_usd":   "💧 Min liquidity (USD)",
    "poll_seconds":  "⏱ Interval cek harga (dtk)",
    "max_hold_sec":  "⏱ Batas max hold posisi (detik, misal 420 = 7m, 0=off)",
    "moonbag_inactivity_sec": "⏱ Moonbag: batas volume mati (detik, 0 txns m5 → EXIT INACTIVE, 420 = 7m)",
    "vanity7777_enabled":      "🎭 Aturan khusus CA ...7777 aktif (1/0, default 0 — data tidak mendukung)",
    "vanity7777_max_hold_sec": "🎭 Time-stop khusus CA ...7777 (detik, dipakai hanya jika vanity7777_enabled=1)",
    "moonbag_max_hold_sec":   "🏁 Moonbag: batas maksimal hold runner (detik, 2700 = 45m → HARVEST FULL)",
    "safety_on":     "🛡 Safety filter (1=on 0=off)",
    "dev_max_pct":   "🕵️ Max dev holding (%)",
    "top10_max_pct": "🐋 Max top10 holders (%)",
    "safety_fail_closed": "🔒 Fail-closed kalau data gagal (1/0)",
    "social_filter":  "🌐 Filter sosmed",
    "social_min_count":"🌐 Min jumlah sosmed (mode count)",
    "social_fail_closed":"🌐 Skip kalau sosmed kosong (1/0)",
    "agent_size_mult_max": "🧠 Max size multiplier agent",
    "agent_review_min": "🧠 Interval review agent (menit)",
    "agent_min_conviction": "👁 Min conviction buat entry (0-100)",
    "scanner_on":            "🔭 Scanner: analisa semua coin (1=ON) — sumber sinyal kedua",
    "scanner_interval_sec":  "🔭 Scanner: interval siklus (dtk)",
    "scanner_mcap_max":      "🔭 Scanner: maks mcap kandidat (USD)",
    "scanner_ignore_v3":     "🔭 Scanner: hanya pool v4 (1=ON, abaikan v2/v3)",
    "scanner_min_liq":       "🔭 Scanner: min likuiditas (USD)",
    "scanner_min_vol_h1":    "🔭 Scanner: min volume 1 jam (USD)",
    "scanner_min_txns_h1":   "🔭 Scanner: min transaksi 1 jam",
    "scanner_max_age_min":   "🔭 Scanner: maks umur pair (menit, 0=abaikan)",
    "scanner_max_h1_pump":   "🔭 Scanner: pagar anti-pucuk, maks h1 pump (%)",
    "scanner_max_per_hour":  "🔭 Scanner: maks BUY per jam (0 = unlimited)",
    "scanner_factory_on":    "🔭 Scanner: sumber event factory v3 (1=ON)",
    "scanner_batch_max":     "🔭 Scanner: maks koin diambil per siklus (2)",
    "scanner_concurrency":   "🔭 Scanner: maks evaluasi paralel serentak (Semaphore)",
    "tracker_auto_rotate":   "🔄 Tracker: rotasi wallet insider otomatis 24h (1/0)",
    "insider_alert_only":    "🔔 Tracker insider: alert-only tanpa posisi (1) / ikut pakir (0)",
    "insider_alert_dedup_window_sec": "🧹 Dedup alert insider: jendela waktu (detik, 0=mati)",
    "entry_mode":            "⚡ Mode entry (mata/fast)",
    "goldilocks_min_age_sec":     "⚡ Goldilocks: min umur pool (dtk)",
    "goldilocks_max_age_sec":     "⚡ Goldilocks: maks umur pool (dtk)",
    "goldilocks_min_liq":         "⚡ Goldilocks: min likuiditas (USD)",
    "goldilocks_min_liq_mcap_ratio": "⚡ Goldilocks: min rasio liq/mcap",
    "goldilocks_max_mcap":        "⚡ Goldilocks: maks mcap (USD)",
    "goldilocks_min_mcap":        "⚡ Goldilocks: min mcap (USD)",
    "goldilocks_max_dev_pct":     "⚡ Goldilocks: maks dev (rasio 0-1)",
    "goldilocks_max_top10_pct":   "⚡ Goldilocks: maks top10 (rasio 0-1)",
    "goldilocks_max_slippage":    "⚡ Goldilocks: maks slippage quote",
    "goldilocks_tp1":             "⚡ Goldilocks: TP1 (0.35 = +35%)",
    "goldilocks_tp2":             "⚡ Goldilocks: TP2 (0.80 = +80%)",
    "sybil_cluster_guard_on":     "🤖 Sybil Guard: blokir klaster wash-trading dev bot (1/0)",
    "sybil_cluster_min_wallets":  "🤖 Sybil Guard: min dompet serempak klaster (7)",
    "sybil_cluster_window_sec":   "🤖 Sybil Guard: jendela waktu serempak detik (60)",
    "clone_max_entries_per_day": "🛡 Clone guard: maks entry per keluarga operator",
    "clone_entry_window_hours":  "🛡 Clone guard: jendela hitung entry (jam)",
    "userbot_enabled":       "📡 Userbot Telegram alpha groups (1/0, default 0=off)",
}

SETTING_DESCRIPTIONS = SETTING_LABELS


def validate():
    # PRIVATE_KEY tidak wajib di sini: wallet bisa dari DB (generate via bot).
    # Trader yang mastiin ADA wallet (DB aktif atau .env), kalau tidak error jelas.
    missing = [k for k, v in {
        "SWAP_ROUTER02": SWAP_ROUTER02,
        "TG_API_ID": TG_API_ID,
        "TG_API_HASH": TG_API_HASH, "BOT_TOKEN": BOT_TOKEN,
        "ADMIN_ID": ADMIN_ID,
    }.items() if not v]
    if missing:
        raise SystemExit(f"[config] .env belum lengkap: {', '.join(missing)}")
