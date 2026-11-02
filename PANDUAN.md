# 📖 PANDUAN LENGKAP — hoodsniper

**Auto-entry meme token di Robinhood Chain, dikendalikan penuh dari Telegram, dengan lapisan otak LLM (Otak · Mata · Tangan).**

Dibangun untuk operasi 24/7 di VPS. Mirror sinyal dari akun Telegram sendiri (tanpa nambah bot ke group manapun), semua kontrol lewat bot pribadi gaya trading bot.

---

## Daftar Isi

1. [Konsep singkat](#1-konsep-singkat)
2. [Arsitektur](#2-arsitektur)
3. [Yang perlu disiapkan](#3-yang-perlu-disiapkan)
4. [Instalasi di VPS](#4-instalasi-di-vps)
5. [Isi file .env](#5-isi-file-env)
6. [Login akun Telegram (QR)](#6-login-akun-telegram-qr)
7. [Jalankan dengan PM2](#7-jalankan-dengan-pm2)
8. [Panduan menu bot](#8-panduan-menu-bot)
9. [Otak — lapisan LLM](#9-otak--lapisan-llm)
10. [Skor Caller — ciri khas hoodsniper](#10-skor-caller--ciri-khas-hoodsniper)
11. [Alur kerja lengkap](#11-alur-kerja-lengkap)
12. [Rekomendasi rollout (penting)](#12-rekomendasi-rollout-penting)
13. [Maintenance & troubleshooting](#13-maintenance--troubleshooting)
14. [Keamanan & risiko](#14-keamanan--risiko)

---

## 1. Konsep singkat

hoodsniper mendengarkan room-room call di Telegram lewat **akun Kakak sendiri**, menyaring token yang lewat, membeli otomatis dengan sizing kecil, dan menjual otomatis saat profit atau cut loss — semua tanpa perlu Kakak melek jam 3 pagi.

Di atas mesin dasar itu ada **Otak**: lapisan LLM opsional yang menambahkan pertimbangan (momentum, reputasi caller, pelajaran dari trade lama) sebelum mengambil keputusan. Otak punya dua tangan kerja:

- **👁 Mata** — memutuskan entry: beli atau lewati, seberapa yakin, seberapa besar, target TP/SL berapa.
- **✋ Tangan** — mengelola posisi terbuka: exit lebih awal kalau momentum mati, hold kalau tren masih kuat.

Otak tidak pernah bisa melewati pagar keras (hard filter & clamp). TP/SL deterministik selalu jadi jaring pengaman terakhir.

---

## 2. Arsitektur

```
                 ┌─────────────────────────────────────────────┐
   Room Telegram │  Akun Kakak (Telethon, login QR)             │
   (multi-group) │  → mendengar semua room tempat Kakak member  │
                 └───────────────────┬─────────────────────────┘
                                     │ CA terdeteksi
                                     ▼
                 ┌─────────────────────────────────────────────┐
   HARD FILTER   │  mcap ≤ limit source · liquidity ≥ min ·     │
   (deterministik)│ dev holding ≤ X% · top-10 ≤ Y% · honeypot  │
                 └───────────────────┬─────────────────────────┘
                                     │ lolos
                                     ▼
                 ┌─────────────────────────────────────────────┐
   👁 MATA (LLM) │  Nilai momentum, skor caller, skor room,     │
   opsional      │  insting. Balas: BUY/SKIP + conviction +     │
                 │  size_mult + TP1/TP2/SL. Output DI-CLAMP.    │
                 └───────────────────┬─────────────────────────┘
                                     │ BUY
                                     ▼
                 ┌─────────────────────────────────────────────┐
   EKSEKUSI      │  Swap Uniswap (v2/v3 auto) · simulasi dulu · │
                 │  approve · cek honeypot                      │
                 └───────────────────┬─────────────────────────┘
                                     ▼
                 ┌─────────────────────────────────────────────┐
   MONITOR 20dtk │  TP1 → jual sebagian · TP2 → jual sisa ·     │
   (backstop)    │  SL → cut. Pakai TP/SL per-posisi dari Mata. │
                 └───────────────────┬─────────────────────────┘
                                     │ tiap N menit
                                     ▼
                 ┌─────────────────────────────────────────────┐
   ✋ TANGAN (LLM)│  Review posisi: HOLD / SELL_HALF / SELL_ALL │
   opsional      │  berdasarkan momentum terkini.               │
                 └─────────────────────────────────────────────┘

   Semua notifikasi & kontrol → bot BotFather (panel inline keyboard)
   Semua keputusan Otak → Jejak (decision log) + Insting (lessons)
```

**File utama:**

| File | Peran |
|---|---|
| `bot.py` | Entry point. Dua client: userbot (Telethon) + control bot (BotFather) |
| `sniper/listener.py` | Dengar multi-room, ekstrak CA, jalankan filter + Mata, trigger buy |
| `sniper/trader.py` | Eksekusi swap on-chain (Uniswap v2/v3), honeypot check |
| `sniper/engine.py` | Mesin jual: monitor TP/SL 20 detik + loop review Tangan |
| `sniper/safety.py` | Filter dev holding & konsentrasi top-10 (via Blockscout) |
| `sniper/agent.py` | Lapisan LLM: Mata (entry), Tangan (review), Insting (lessons) |
| `sniper/control_bot.py` | Panel bot: menu, posisi, sources, settings, Otak, stats |
| `sniper/db.py` | SQLite: settings, sources, calls, positions, jejak, insting, skor caller |
| `sniper/dexscreener.py` | Data harga/mcap/liquidity/momentum |
| `sniper/config.py` | Config statis dari .env |

---

## 3. Yang perlu disiapkan

- **VPS** Ubuntu (Node.js untuk ambil alamat Uniswap, Python 3.11+ untuk bot)
- **Wallet agent** — wallet BARU khusus bot, JANGAN wallet utama. Isi ETH secukupnya (0.02–0.05 ETH cukup untuk banyak entry + gas)
- **api_id & api_hash** dari https://my.telegram.org (untuk userbot)
- **Bot token** dari [@BotFather](https://t.me/BotFather) (`/newbot` → copy token)
- **User ID** Kakak dari [@userinfobot](https://t.me/userinfobot) (untuk kunci admin)
- **(Opsional) API key LLM** — Anthropic / DeepSeek / GLM / Kimi / OpenRouter / dll
- Akun Telegram Kakak harus **sudah jadi member** semua room yang mau dipantau

---

## 4. Instalasi di VPS

```bash
# extract project
unzip hoodsniper.zip && cd hoodsniper2

# virtualenv + dependensi Python
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Uniswap v4 sudah pre-konfigurasi untuk Robinhood Chain (4663):
# PoolManager: 0x8366a39cc670b4001a1121b8f6a443a643e40951
# StateView: 0x76251b689254884c59ff10188d3ca346765da7aa
# Universal Router & Permit2 sudah terpasang otomatis di sniper/v4.py.
```

---

## 5. Isi file .env

```bash
cp .env.example .env
nano .env
```

Isi bagian per bagian:

```bash
# ===== Robinhood Chain =====
RPC_URL=https://rpc.mainnet.chain.robinhood.com   # ganti dedicated kalau kena rate limit
CHAIN_ID=4663
PRIVATE_KEY=            # wallet AGENT — bukan wallet utama!

# ===== Uniswap (Opsional - v4 sudah hardcoded) =====
# SWAP_ROUTER02=        # hanya fallback lama
# V2_ROUTER=

# ===== Telegram userbot =====
TG_API_ID=             # my.telegram.org
TG_API_HASH=
SESSION_NAME=hoodsniper

# ===== Control bot (BotFather) =====
BOT_TOKEN=             # dari @BotFather
ADMIN_ID=              # dari @userinfobot — HANYA id ini yang bisa kontrol

# ===== Seed source awal =====
SEED_GROUP_ID=-1002103131992
SEED_TOPIC_DEGEN=250551
SEED_TOPIC_MEMBER=395868

# ===== Agent LLM (opsional) =====
LLM_PROTOCOL=anthropic         # "anthropic" atau "openai"
LLM_API_KEY=
LLM_BASE_URL=                  # kosongkan utk default provider

# ===== Dexscreener / Blockscout =====
DEX_CHAIN_MATCH=robinhood
BLOCKSCOUT_API=https://robinhoodchain.blockscout.com/api/v2
DB_PATH=hoodsniper.db
```

**Contoh setting LLM per provider** (isi `LLM_PROTOCOL`, `LLM_BASE_URL`, lalu ganti nama model dari panel Otak):

| Provider | LLM_PROTOCOL | LLM_BASE_URL | Model |
|---|---|---|---|
| Anthropic | `anthropic` | *(kosong)* | `claude-haiku-4-5` |
| DeepSeek | `openai` | `https://api.deepseek.com/v1` | `deepseek-chat` |
| GLM/Zhipu | `openai` | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.7` |
| Kimi | `openai` | `https://api.moonshot.ai/v1` | `kimi-k2` |
| OpenRouter | `openai` | `https://openrouter.ai/api/v1` | bebas |
| LM Studio (lokal) | `openai` | `http://localhost:1234/v1` | nama model lokal |

Terakhir, kunci izin file:
```bash
chmod 600 .env
```

---

## 6. Login akun Telegram (QR)

Sekali saja, interaktif:

```bash
./venv/bin/python bot.py login
```

QR muncul di terminal. Buka Telegram di HP → **Settings → Devices → Link Desktop Device** → scan. Kalau akun pakai 2FA, bot minta password di terminal. Session tersimpan di `hoodsniper.session`.

---

## 7. Jalankan dengan PM2

```bash
pm2 start ecosystem.config.js
pm2 logs hoodsniper          # pantau log realtime
pm2 save && pm2 startup      # auto-start setelah reboot
```

Lalu buka bot Kakak di Telegram, kirim **`/start`**. Panel muncul.

> ⚠️ Kirim `/start` minimal sekali — bot Telegram tidak bisa memulai chat duluan.

---

## 8. Panduan menu bot

### Menu utama (`/start`)

```
🤖 hoodsniper
status: ▶️ AKTIF
👛 0xA3f9…c21B      ⛽ 0.0485 ETH
📊 open: 3          📡 sources: 2
💵 buy size: $1.00/call

[📊 Posisi]  [📡 Sources]
[⚙️ Settings] [📈 Stats]
[🧠 Otak]    [⏸ Pause]
[🔄 Refresh]
```

### 📊 Posisi
Daftar posisi terbuka dengan **PnL live** (hijau/merah), status TP1, caller & room asal. Tiap posisi punya tombol: `Sell 50%`, `Sell ALL`, dan link chart. Ini override manual — TP/SL otomatis tetap jalan kalau tidak ditekan.

### 📡 Sources — multi-group
Daftar semua room yang dipantau, masing-masing dengan limit mcap sendiri. Tombol per source: on/off dan hapus. Tombol **➕ Tambah Source** — kirim satu baris:

```
https://t.me/c/2103131992/250551 30000 Degen Hood
-1001234567890 15000 Alpha Group
```

Format: `<link_topic_atau_chat_id> <mcap_max> <nama>`. Link topic `t.me/c/...` di-parse otomatis (chat id + topic id). Tanpa topic = semua message di group dipantau. Syarat: akun Kakak sudah member group itu.

### ⚙️ Settings
Semua parameter bisa diedit dari chat tanpa restart. Tap label → kirim angka baru:

| Setting | Default | Arti |
|---|---|---|
| 💵 Buy size (USD) | 1.0 | Nilai entry per call |
| 🎯 TP1 | 0.50 | Take profit pertama (+50%) |
| 🎯 TP2 | 1.00 | Take profit kedua (+100%) |
| 🛑 SL | -0.30 | Stop loss (-30%) |
| 📤 Jual di TP1 | 0.50 | Porsi dijual saat TP1 (50%) |
| 📈/📉 Slippage | 0.30 / 0.35 | Toleransi slippage buy/sell |
| 💧 Min liquidity | 500 | Liquidity minimum (USD) |
| ⏱ Interval cek harga | 20 | Detik antar cek TP/SL |
| 🛡 Safety filter | 1 | On/off filter dev & holder |
| 🕵️ Max dev holding | 5 | % supply dev maksimal |
| 🐋 Max top10 | 40 | % konsentrasi top-10 maksimal |
| 🔒 Fail-closed | 1 | Kalau data holder/dev gagal diverifikasi → SKIP (aman). 0 = lolos (agresif) |
| 🧠 Max size multiplier | 2 | Batas pengali size dari Mata |
| 🧠 Interval review | 10 | Menit antar review Tangan |

### 📈 Stats
Ringkasan: call terdeteksi, total buy, breakdown TP/SL, estimasi PnL ETH, dan **🏆 Skor Caller** (lihat bagian 10).

### 🧠 Otak
Panel lapisan LLM — lihat bagian berikutnya.

### ⏸ Pause / Resume
Hentikan entry sementara tanpa mematikan proses. Posisi terbuka tetap dimonitor.

---

## 9. Otak — lapisan LLM

Panel **🧠 Otak** mengatur seluruh lapisan kecerdasan:

```
🧠 OTAK — lapisan LLM hoodsniper
provider: anthropic
API key: ✅ terpasang
👁 Mata (keputusan entry): ON
✋ Tangan (review posisi): ON (tiap 10 mnt)
mode: 🧪 MODE LATIHAN
model: claude-haiku-4-5
💡 insting tersimpan: 7

• [screener] BUY MICIN (74) → tp2 pnl +112%
  "momentum sehat, holder nyebar, dev 0.8%"
...

[🟢 On Mata]      [🟢 On Tangan]
[🧪 Mode Latihan] [🤖 Ganti model]
[👣 Jejak]        [💡 Latih insting]
```

**👁 Mata** — tiap call yang lolos hard filter dikirim ke LLM dengan konteks: momentum (volume, price change, buys vs sells di 5m & 1h), umur pool, hasil safety check, **skor room**, **skor caller**, keputusan terakhir, dan insting. LLM balas keputusan terstruktur (BUY/SKIP, conviction, size multiplier, TP/SL khusus). Semua output di-clamp dalam pagar.

**✋ Tangan** — tiap N menit review posisi terbuka: momentum mati di +20%? exit. Tren kuat? hold. TP/SL deterministik tetap backstop.

**🧪 Mode Latihan (dry run)** — default ON. Otak memberi keputusan lengkap + alasan **tanpa mengeksekusi apapun**. Wajib dipakai untuk validasi sebelum live.

**👣 Jejak (decision log)** — semua keputusan Otak tercatat dengan alasannya. Outcome (tp2/sl + PnL) ditulis balik ke keputusan asal, jadi kelihatan mana keputusan bagus dan mana zonk.

**💡 Insting (lessons)** — dari ≥5 posisi closed, tombol "Latih insting" meminta LLM menurunkan maksimal 3 pelajaran konkret, yang di-inject ke prompt keputusan berikutnya. Inilah loop belajarnya.

Kalau API error/timeout → **fallback otomatis ke rule-based**, bot tidak pernah macet karena LLM.

---

## 9b. Kekuasaan Otak — fitur yang bikin LLM beneran menentukan

Sejak update ini Otak bukan hiasan — dia punya wewenang nyata (semua tetap
dalam pagar & bisa dimatikan):

**👁 Min Conviction Gate** — keputusan BUY dari Mata dengan conviction di bawah
`agent_min_conviction` (default 55) otomatis di-skip dengan notif "MATA RAGU".
Atur di ⚙️ Settings.

**🚫 Blacklist Caller (bisa ON/OFF)** — Mata boleh mem-blacklist caller yang
track record-nya jelas beracun (≥4 call dibeli, mayoritas rug, nol TP). Caller
yang di-blacklist: semua call berikutnya auto-skip TANPA memanggil LLM (hemat
biaya juga). Aturan main:
- Toggle di panel 🧠 Otak: `🚫 blacklist caller: ON/OFF`. **OFF = daftar tidak
  dipakai DAN Mata tidak bisa menambah caller baru** — tapi daftarnya tetap
  tersimpan; begitu ON lagi, caller lama langsung ke-block lagi.
- Hapus permanen per-caller: panel Blacklist → ♻️ Pulihkan.
- Saat **Mode Latihan**, Mata hanya lapor "MAU mem-blacklist" tanpa eksekusi —
  blacklist beneran cuma terjadi saat LIVE.

**💡 Auto-Insting** — tiap 5 posisi closed, insting dilatih otomatis dari hasil
nyata (`agent_auto_insting`). Tidak perlu tekan tombol lagi.

**🎓 Coach Harian** — setelah rekap pagi, Otak menganalisa 24 jam terakhir dan
mengusulkan maksimal 3 perubahan setting konkret (contoh: `trail_pct 0.25 →
0.35 — runner keluar kecepetan`). Usulan datang dengan tombol ✅ per item —
TIDAK auto-apply; Kakak yang tap untuk menerapkan. Semua usulan dibatasi
whitelist + clamp (Coach tidak bisa menyentuh key di luar daftar aman). Bisa
dipanggil manual: panel Otak → 🎓 Coach sekarang. Toggle: `agent_coach`.

**💬 /tanya** — tanya apa saja ke bot dalam bahasa natural:
`/tanya kenapa kemarin banyak SL?` · `/tanya caller mana paling cuan?`
Jawaban grounded ke data asli (stats, posisi, jejak, skor caller, settings) dan
diinstruksikan jujur bilang kalau datanya tidak ada — bukan mengarang angka.

## 10. Skor Caller — ciri khas hoodsniper

Karena mirror lewat akun sendiri (Telethon), bot tahu **siapa** yang memposting tiap call, bukan cuma di room mana. Dari situ hoodsniper membangun reputasi per orang:

- Tiap posisi mencatat `caller_id` + `caller_name`.
- Bot menghitung per caller: jumlah call dibeli, berapa yang hit TP1, full TP, dan SL.
- Di notif buy, muncul rekam jejak caller-nya:
  `🎯 caller: alpha_degen — 14 call, 9 TP1, 3 SL`
- Di **📈 Stats** ada leaderboard **🏆 Skor Caller** diurut berdasarkan TP1-rate.
- Kalau Otak aktif, skor caller ini masuk ke pertimbangan Mata: caller dengan banyak SL dan sedikit TP → di-size kecil atau di-skip; caller terbukti cuan → boleh lebih yakin.

Ini fitur yang membedakan hoodsniper dari agent LP/DLMM manapun: **bot belajar mengenali caller yang call-nya beneran menghasilkan, lalu menyesuaikan ukuran taruhan ke reputasi orang itu.**

---

## 11. Alur kerja lengkap

Contoh satu call dari awal sampai selesai:

1. Caller `@alpha_degen` posting CA di Robinhood Degen Room jam 03:14 WIB.
2. Userbot menangkap message → ekstrak CA → catat caller.
3. Hard filter: mcap $18k (≤ $30k ✓), liq $6.1k (≥ $500 ✓), dev 0.8% (≤ 5% ✓), top-10 34% (≤ 40% ✓).
4. Mata dipanggil: momentum 5m +40%, buys 41 vs sells 9, caller ini 9/14 TP1 → **BUY, conviction 74, size 1.3x, TP 40%/120%, SL -25%**.
5. (Mode Latihan) → notif "🧪 Mata mau BUY" saja. (Live) → eksekusi swap $1.30.
6. Simulasi sell OK → bukan honeypot. Notif buy masuk dengan tombol sell.
7. Monitor tiap 20 detik. Harga tembus +40% → jual 50% otomatis (TP1).
8. Sisa moonbag naik ke +120% → jual sisa (TP2), posisi close. Outcome ditulis ke Jejak.
9. Setelah 5+ posisi closed, "Latih insting" menurunkan pelajaran baru dari pola menang/kalah.

---

## 12. Rekomendasi rollout (penting)

Jangan langsung live dengan Otak. Urutan yang benar:

1. **Minggu 0 — rule-based murni.** Jalankan tanpa Otak (Mata & Tangan OFF). Pastikan mirror, filter, buy, TP/SL jalan benar dulu. Buy size kecil ($1).
2. **Minggu 1 — Otak MODE LATIHAN.** Nyalakan Mata dalam dry run. Bot tetap trading rule-based, tapi Mata mencatat keputusan bayangannya. Buka 👣 Jejak tiap hari — bandingkan: kalau Mata bilang SKIP tapi rule beli lalu kena SL, Mata benar. Kalau Mata masuk akal konsisten, lanjut.
3. **Minggu 2 — Mata LIVE, size tetap kecil.** Live-kan Mata, tapi 🧠 Max size multiplier = 1 dulu (matikan sizing agresif). Nilai apakah SKIP-nya benar-benar menghindari rug.
4. **Minggu 3+ — buka sizing & Tangan.** Naikkan max multiplier bertahap, nyalakan Tangan. Latih insting rutin.

Kesimpulan yang jujur: Otak membuat keputusan lebih **berkonteks**, bukan menjamin profit. Call room low-cap tetap mayoritas rug — LLM hanya membantu memilih yang lebih kecil kemungkinan buntung. Karena itu sizing kecil tetap disiplin utama.

---

## 12b. PnL Jujur & Anti-Wick (update penting)

Dua perbaikan besar di versi ini:

**1. PnL realized asli (bukan estimasi layar).** Setiap sell sekarang mengukur
ETH yang BENERAN masuk wallet (balance delta), bukan `jumlah token × harga
layar`. Notif menampilkan keduanya:

```
🎯 TP2 MEMESTOCK
layar +486.8% → realized -5.0%
353,099 token → 0.000495 ETH masuk · gas 0.000004
⚠️ pool tipis: harga layar jauh di atas hasil jual asli (price impact)
```

Angka "layar" = harga spot Dexscreener yang men-trigger. Angka "realized" =
uang beneran. Di pool tipis keduanya bisa beda jauh — sekarang bot tidak bisa
bohong lagi. Stats & Insting agent juga belajar dari angka realized.

**2. Verifikasi TP anti-wick (`tp_verify`, default ON).** Sebelum eksekusi
TP1/TP2, bot minta quote asli ke router (nembus depth pool, bukan harga
layar). Kalau hasil jual eksekutabel < 70% dari target TP, eksekusi DITAHAN
dan Kakak dapat notif 🫧 "pool tipis — TP ditahan". Jadi wick kosong tanpa
likuiditas tidak lagi men-trigger jual seluruh posisi di harga khayalan.
SL sengaja TIDAK diverifikasi — keluar cepat selalu prioritas.

Matikan via ⚙️ Settings → 🫧 Verifikasi TP anti-wick = 0 kalau mau perilaku lama.

---

## 12c. Rug Monitor, Trailing Stop & Rekap Harian

**🚨 LP Rug Monitor** — tiap cycle monitor, liquidity pool semua posisi terbuka
dipantau. Kalau liq anjlok mendadak melebihi `rug_liq_drop` (default 40%) dalam
satu interval → **emergency exit detik itu juga**, tanpa nunggu harga kena SL.
Ini pertahanan terhadap kematian paling umum di low cap: dev tarik LP. Kalau
eksekusi gagal (LP sudah benar-benar kosong), Kakak tetap dapat alert.

**📈🔒 Trailing Stop + Breakeven Lock** (`trail_on`, default ON) — setelah TP1
kena, moonbag tidak lagi pakai SL -30% dari entry. Stop level sisa posisi jadi:
`max(harga entry, puncak tertinggi × (1 - trail_pct))` — artinya:
- profit yang sudah di tangan **tidak pernah balik jadi rugi** (lantai = entry),
- runner yang lari terus di-ikutin dari belakang; keluar hanya saat turun
  `trail_pct` (default 25%) dari puncak.
Mau moonbag lari tanpa dipotong di +100%? Set `tp2_on = 0` — TP2 mati, exit
moonbag sepenuhnya lewat trailing. Runner 5x jadi ke-capture.

**🌅 Rekap Harian** (`recap_on`, jam `recap_hour` WIB, default 07:00) — satu
pesan tiap pagi: call masuk & yang di-skip, entry baru, breakdown TP2/trail/
SL/rug/manual, PnL realized 24 jam (angka wallet asli), posisi yang masih
open, dan caller terbaik semalam. Bangun tidur, baca 20 detik, selesai.

---

## 13. Maintenance & troubleshooting

```bash
pm2 logs hoodsniper          # log realtime (termasuk [skip], [safety-skip])
pm2 restart hoodsniper       # setelah ubah .env
pm2 stop hoodsniper          # pause total

# inspeksi database
sqlite3 hoodsniper.db "SELECT symbol,status,realized_eth FROM positions ORDER BY id DESC LIMIT 20;"
sqlite3 hoodsniper.db "SELECT caller_name,COUNT(*),SUM(tp1_done) FROM positions WHERE caller_id NOTNULL GROUP BY caller_id;"
```

| Gejala | Kemungkinan sebab | Solusi |
|---|---|---|
| Bot tidak balas `/start` | ADMIN_ID salah | Cek user id via @userinfobot |
| Semua call `[skip]` | limit mcap terlalu ketat / chain match salah | Cek Settings & `DEX_CHAIN_MATCH` |
| Error 429 di log | RPC/Dexscreener rate limit | RPC dedicated, naikkan interval |
| Mata selalu error | model tidak cocok provider | Cek nama model vs `LLM_PROTOCOL`/`LLM_BASE_URL` |
| Buy sukses token tak masuk | fee-on-transfer aneh | Otomatis ditangani; kalau berulang skip token itu |
| Sell gagal terus | LP ditarik (rug) | Tidak ada yang bisa dijual — dana hangus |

**File yang harus di-backup** kalau pindah VPS: `hoodsniper.session`, `hoodsniper_bot.session`, `hoodsniper.db`. Jangan commit ke git.

---

## 14. Keamanan & risiko

- **Wallet agent terpisah, isi secukupnya.** Private key tersimpan plaintext di `.env` pada VPS. Anggap seluruh saldo wallet itu **dana yang siap hangus**.
- **`chmod 600 .env`** dan jangan pernah commit `.env`, `*.session`, atau `*.db`.
- **Admin lock** — hanya `ADMIN_ID` yang bisa mengontrol bot; user lain di-reject.
- **Call low-cap ~90% rug.** Honeypot check & safety filter membantu, tapi tidak menutup: honeypot delayed, high-tax token, dan LP yang ditarik sebelum SL sempat eksekusi tetap bisa lolos.
- **Bukan financial advice.** Perangkat lunak ini disediakan apa adanya. Menjalankan bot trading otomatis berisiko kehilangan dana. Mulai selalu dari Mode Latihan dan dana kecil.

---

*hoodsniper — dibangun untuk Cupang Ventures. Lo yang tidur, mesin yang kerja.* 🤖
