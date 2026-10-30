"""Control panel via bot BotFather — gaya trading bot (inline keyboard).

Menu: Posisi (PnL live + tombol sell), Sources (multi-group add/toggle/hapus),
Settings (edit semua parameter), Stats, Pause/Resume.
"""
import asyncio
import re
import traceback
from telethon import events, Button, utils
from . import config, db, engine, agent, clone_guard

_pending = {}  # admin_id -> action string ("add_source" | "set:key" | "insaddr")
_trader = None
_user = None  # userbot Telethon (buat resolve channel publik via get_entity)

# ======================= PANEL INSIDER (v16.6) =======================
# Submenu di 📡 Sources. Builders = fungsi murni (diuji tanpa Telethon),
# handler = cb._route + dobel via insaddr (input teks, pola "wimport").
PAGE_SIZE = 10
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_rotate_task = None  # Task rotasi background — pengunci anti double-run
_rotate_summary = None   # dict hasil terakhir (untuk header panel)


def _btn_label(btn):
    """Label tombol — menoleransi stub Button tanpa Telethon (tests)."""
    return getattr(btn, "text", "") or ""


def insider_addr_valid(raw):
    """0x + 40 hex, regex ketat. Return address lower() atau None."""
    m = ADDR_RE.match((raw or "").strip())
    return m.group(0).lower() if m else None


def _short_addr(addr, label=""):
    a = (addr or "").lower()
    if label and label.strip():
        return label.strip()
    return f"{a[:6]}…{a[-4:]}"


def _insider_labels(addrs):
    labels = {}
    for row in db.list_insiders():
        addr = (row.get("address") or "").lower()
        label = (row.get("label") or "").strip()
        if addr and label and addr in addrs:
            labels[addr] = label
    return labels


def _last_seen_seen(rows):
    vals = [int(r.get("last_seen") or 0) for r in rows]
    return max(vals) if vals else 0


def insider_screen_page_text():
    rows = db.list_insiders()
    active = [r for r in rows if r["enabled"]]
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [
        "🕵️ **Insider Wallets**",
        f"aktif: **{len(active)} / {len(rows)}** wallet",
    ]
    last = _last_seen_seen(rows)
    if last:
        import time as _t
        lines.append(f"last_seen terbaru: **{last_ago(last, int(_t.time()))}**")
    if _rotate_summary:
        s = _rotate_summary
        lines.append(f"🔄 rotasi terakhir: +{s.get('added', 0)} / "
                     f"prune {s.get('disabled', 0)} · "
                     f"aktif {s.get('total_active', 0)}")
    return "\n".join(lines), pages


def last_ago(ts, now=None):
    if not ts:
        return ""
    import time as _t
    d = (now or int(_t.time())) - ts
    if d < 60:
        return f"{d}d lalu"
    if d < 3600:
        return f"{d // 60}m lalu"
    if d < 86400:
        return f"{d // 3600}j lalu"
    return f"{d // 86400}h lalu"


def insider_rows_text(rows):
    now = int(__import__("time").time())
    lines = []
    for r in rows:
        label = _short_addr(r["address"], r.get("label"))
        seen = int(r.get("last_seen") or 0)
        seen_txt = last_ago(seen, now) if seen else "—"
        lines.append(f"• {label} · "
                     f"w{r['win_count']}/l{r['loss_count']} · {seen_txt}")
    return lines

LINK_RE = re.compile(r"t\.me/c/(\d+)(?:/(\d+))?")


def _admin(event):
    return event.sender_id == config.ADMIN_ID


# ---------- MODE PUBLIK (user non-admin: HANYA lihat log filter koin) ----------
# Panel (Posisi/Sources/Settings/Stats/Otak/Wallet + semua tombolnya)
# KHUSUS admin. User lain yang membuka bot ini hanya diarahkan ke
# channel log publik — tanpa panel, tanpa data wallet/posisi/stats.
# Log disiarkan ke channel oleh listener._notify_log (bot.py:
# public_notify → PUBLIC_LOG_CHAT_ID), BUKAN dari chat ini, jadi user
# publik tidak perlu chat dengan bot untuk mengikuti log.
def public_start_text():
    lines = [
        "🤖 **hoodsniper — Log Publik**",
        "",
        "Bot ini menampilkan **log filter koin** secara publik: setiap "
        "koin yang disaring scanner (SKIP beserta alasannya, maupun yang "
        "lolos → BUY/SIMULASI) disiarkan ke channel log.",
        "",
        "🔒 Panel kontrol bot ini **khusus admin** — posisi, wallet, "
        "settings, dan tombol jual tidak bisa dilihat/dipakai dari sini.",
    ]
    if config.PUBLIC_LOG_LINK:
        lines += ["", "📜 Ikuti log-nya di channel publik lewat tombol di bawah."]
    elif config.PUBLIC_LOG_CHAT_ID:
        lines += ["", "📜 Log disiarkan di channel publik bot ini — "
                      "gabung channel-nya untuk mengikuti."]
    else:
        lines += ["", "📜 Channel log publik belum diaktifkan admin."]
    return "\n".join(lines)


def public_start_buttons():
    """Tombol untuk user publik: HANYA tombol URL ke channel log
    (atau None). Tidak pernah ada tombol inline/panel di sini."""
    if config.PUBLIC_LOG_LINK:
        return [[Button.url("📜 Lihat Log Filter Koin", config.PUBLIC_LOG_LINK)]]
    return None


# ---------- panel builders ----------
def main_menu_text():
    bal = _trader.eth_balance()
    st = "⏸ PAUSED" if db.paused() else "▶️ AKTIF"
    n_open = len(db.open_positions())
    n_src = len(db.sources(only_enabled=True))
    stats = db.stats()
    pub_line = ("\n📣 Log publik: **AKTIF** (filter koin → channel)"
                if config.PUBLIC_LOG_CHAT_ID
                else "\n📣 Log publik: nonaktif")
    if db.get("agent_dry_run", str) == "1":
        return (
            f"🤖 **hoodsniper v2**\n\n"
            f"status: **{st}**\n"
            f"🧪 **MODE LATIHAN (Paper Trading)**\n"
            f"💵 Kas Virtual: **${db.get_paper_cash():.2f}** / "
            f"**${float(db.get('paper_initial_usd')):.2f}**\n"
            f"📊 Posisi Open: **{n_open}** | 📡 sources aktif: **{n_src}**\n"
            f"🎯 TP1: {stats['tp1']} | 🎯 TP2: {stats['tp2']} | "
            f"🛑 SL: {stats['sl']} | ⏱ Timeout: {stats['timeout']}\n"
            f"👛 Burner Live: `{_trader.addr}` ({bal:.5f} ETH)\n"
            f"💵 buy size: **${db.get('buy_usd'):.2f}**/call"
            f"{pub_line}"
        )
    return (
        f"🤖 **hoodsniper v2**\n\n"
        f"status: **{st}**\n"
        f"👛 `{_trader.addr}`\n"
        f"⛽ saldo: **{bal:.6f} ETH**\n"
        f"📊 posisi open: **{n_open}** | 📡 sources aktif: **{n_src}**\n"
        f"🎯 TP1: {stats['tp1']} | 🎯 TP2: {stats['tp2']} | "
        f"🛑 SL: {stats['sl']} | ⏱ Timeout: {stats['timeout']}\n"
        f"💵 buy size: **${db.get('buy_usd'):.2f}**/call"
        f"{pub_line}"
    )


def main_menu_buttons():
    pause_btn = Button.inline("▶️ Resume", b"pause") if db.paused() else Button.inline("⏸ Pause", b"pause")
    return [
        [Button.inline("📊 Posisi", b"positions"), Button.inline("📡 Sources", b"sources")],
        [Button.inline("⚙️ Settings", b"settings"), Button.inline("📈 Stats", b"stats")],
        [Button.inline("🧠 Otak", b"agent"), pause_btn],
        [Button.inline("👛 Wallet", b"wallet"), Button.inline("🔄 Refresh", b"menu")],
    ]


async def positions_panel():
    rows = db.open_positions()
    if not rows:
        return "📊 **Posisi**\n\nTidak ada posisi terbuka.", [[Button.inline("« Menu", b"menu")]]
    lines, buttons = ["📊 **Posisi Terbuka**\n"], []
    for p in rows:
        pair = await engine.current_pair(p)
        price = float((pair or {}).get("priceUsd") or 0)
        pnl = (price / p["entry_price_usd"] - 1) * 100 if price > 0 else 0
        emo = "🟢" if pnl >= 0 else "🔴"
        if p.get("tp2_done"):
            status_tag = " · TP2✓ (🚀 Moonbag)"
        elif p["tp1_done"]:
            status_tag = " · TP1✓ (⏳ OTW TP2)"
        else:
            status_tag = " (⏳ OTW TP1)"
        lines.append(
            f"{emo} **{p['symbol']}** {pnl:+.1f}%{status_tag} _via {p['source_name']}_\n"
            f"   entry ${p['entry_price_usd']:.10f} | sisa {p['tokens_left']:,.0f}"
        )
        buttons.append([
            Button.inline(f"{p['symbol']} 📉50%", f"sell:{p['id']}:50"),
            Button.inline("🚨 ALL", f"sell:{p['id']}:100"),
            Button.url("📊", f"https://dexscreener.com/{p['chain_slug']}/{p['pair_address']}"),
        ])
    buttons.append([Button.inline("« Menu", b"menu")])
    return "\n".join(lines), buttons


def sources_panel():
    rows = db.sources()
    lines, buttons = ["📡 **Sources**\n"], []
    if db.get("userbot_enabled", str) == "1":
        # Userbot aktif → source mirror grup relevan, tampilkan daftarnya.
        lines.append("📡 **Sinyal Aktif**: On-Chain Scanner (Uniswap v4) + Insider Tracker (55 Wallets)")
        lines.append("📡 Userbot Telegram: **AKTIF** (mirror sinyal grup di bawah)\n")
        for s in rows:
            st = "✅" if s["enabled"] else "🚫"
            tp = f" · topic {s['topic_id']}" if s["topic_id"] else " · semua topic"
            lines.append(f"{st} **{s['name']}** — mcap ≤ ${s['mcap_max']:,.0f}\n   `{s['chat_id']}`{tp}")
            buttons.append([
                Button.inline(f"{'🚫 Off' if s['enabled'] else '✅ On'} {s['name'][:14]}", f"srct:{s['id']}"),
                Button.inline("🗑", f"srcd:{s['id']}"),
            ])
    else:
        # Default v16.6: userbot non-aktif — tanpa daftar source mirror.
        lines.append("📡 **Sinyal Aktif**: On-Chain Scanner (Uniswap v4) + Insider Tracker (55 Wallets)")
        lines.append("🔒 Userbot Telegram: **NON-AKTIF** (Akun personal aman & bebas risiko ban)")
        lines.append("_Tambah/buka source mirror: aktifkan dulu setting 'userbot_enabled'_\n")
    insiders = db.list_insiders(only_enabled=True)
    buttons.append([Button.inline("🕵️ Insider", b"ins:0"),
                    Button.inline("🔥 Log Beli Insider", b"insbuys:3600")])
    buttons.append([Button.inline("« Menu", b"menu")])
    return "\n".join(lines), buttons


def insider_buys_panel(window_sec=3600):
    win = int(window_sec or 3600)
    lbl = "10m" if win <= 600 else ("1j" if win <= 3600 else "24j")
    rows = db.top_insider_buys(window_sec=win, limit=10)
    import time as _t
    now_i = int(_t.time())
    lines = [f"🔥 **Log Beli Insider ({lbl} terakhir)**"]
    if not rows:
        lines.append("\n_Belum ada transaksi beli insider di jendela waktu ini._")
    else:
        lines.append("_Urutan: jumlah dompet unik terbanyak → jumlah beli:_\n")
        for i, r in enumerate(rows, 1):
            ago = last_ago(r["last_ts"], now_i)
            mc = f"${r['mcap']:,.0f}" if r.get("mcap") else "n/a"
            lq = f"${r['liq']:,.0f}" if r.get("liq") else "n/a"
            chart = (f" · [Chart](https://dexscreener.com/robinhood/{r['pair_address']})"
                     if r.get("pair_address") else "")
            lines.append(
                f"**{i}. {r['symbol']}** — 🔥 **{r['buy_count']}x beli** "
                f"({r['wallet_count']} wallet) · {ago}\n"
                f"   📊 mcap {mc} · liq {lq}{chart}\n"
                f"   `{r['ca']}`"
            )
    buttons = [
        [Button.inline("✅ 10m" if win == 600 else "10m", b"insbuys:600"),
         Button.inline("✅ 1 Jam" if win == 3600 else "1 Jam", b"insbuys:3600"),
         Button.inline("✅ 24 Jam" if win == 86400 else "24 Jam", b"insbuys:86400")],
        [Button.inline("🔄 Refresh", f"insbuys:{win}"),
         Button.inline("🕵️ Insider", b"ins:0")],
        [Button.inline("« Sources", b"sources"),
         Button.inline("« Menu", b"menu")],
    ]
    return "\n".join(lines), buttons


# ---------- panel insider (submenu Sources) ----------
def insider_screen_panel(page=0, rows=None):
    """Builder layar Insider — murni (diuji tanpa Telethon).

    page 0-based; tiap halaman PAGE_SIZE wallet, urutan sama dengan
    db.list_insiders() (win_count DESC, added_at DESC)."""
    rows = rows if rows is not None else db.list_insiders()
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    active_n = sum(1 for r in rows if r["enabled"])
    seg = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    text = _insider_header(rows) + "\n\n" + "\n".join(insider_rows_text(seg))
    buttons = [[Button.inline("✅" if r["enabled"] else "🚫",
                              f"instoggle:{page}:{r['address']}")]
               for r in seg]
    # nav halaman — ◀ page-1 / "i/total" (label, nonaktif) / ▶ page+1
    nav = []
    if page > 0:
        nav.append(Button.inline("◀", f"inspage:{page - 1}"))
    nav.append(Button.inline(f"{page + 1}/{pages}", b"inspagecur"))
    if page < pages - 1:
        nav.append(Button.inline("▶", f"inspage:{page + 1}"))
    buttons.append(nav)
    # aksi massal + scan
    buttons.append([Button.inline("🔥 Log Beli Insider", b"insbuys:3600")])
    buttons.append([Button.inline("🔍 Scan Wallet", b"insaddr"),
                    Button.inline("🔄 Rotate Sekarang", b"insrotate")])
    buttons.append([Button.inline("🗑 Reset (Nonaktifkan semua)", b"insreset"),
                    Button.inline("✅ Aktifkan semua", b"insenableall")])
    buttons.append([Button.inline("« Sources", b"sources"),
                    Button.inline("« Menu", b"menu")])
    return text, buttons


def _insider_header(rows):
    active_n = sum(1 for r in rows if r["enabled"])
    last = _last_seen_seen(rows)
    lines = [f"🕵️ **Insider Wallets** — aktif **{active_n}/{len(rows)}**"]
    try:
        sybil_n = len(db.list_sybil_blacklist())
        if sybil_n:
            lines.append(f"🤖 sybil guard: **{sybil_n}** bot wash-trading diblokir")
    except Exception:
        pass
    if last:
        import time as _t
        lines.append(f"🕒 last_seen terbaru: **{last_ago(last, int(_t.time()))}**")
    if _rotate_summary:
        s = _rotate_summary
        lines.append(f"🔄 rotasi terakhir: +{s.get('added', 0)} baru · "
                     f"prune {s.get('disabled', 0)} · "
                     f"aktif {s.get('total_active', 0)}")
    return "\n".join(lines)


def _insider_confirm_reset_text(rows):
    texts = [
        "🗑 **Nonaktifkan SEMUA wallet insider?**",
        "",
        "Semua wallet akan di-set `enabled = 0` (tracker berhenti",
        "pantau sementara). ⚠️ TIDAK ada yang dihapus — data skor",
        "tetap aman, dan bisa diaktifkan ulang lewat ✅ Aktifkan semua.",
    ]
    buttons = [[
        Button.inline("🔥 Ya, nonaktifkan semua", b"insreset:yes"),
        Button.inline("✋ Batal", b"insreset:no"),
    ]]
    return "\n".join(texts), buttons


MAIN_SETTING_KEYS = ["buy_usd", "tp1", "tp2", "sl"]


def settings_panel():
    tp1_val = float(db.get('tp1', float) or 0.50) * 100
    tp2_val = float(db.get('tp2', float) or 1.00) * 100
    sl_val = float(db.get('sl', float) or -0.30) * 100
    buy_val = db.get('buy_usd', str)
    tp2_on = str(db.get('tp2_on', str)) in ("1", "1.0")
    trail_on = str(db.get('trail_on', str)) in ("1", "1.0")
    trail_pct = float(db.get('trail_pct', float) or 0.25) * 100
    if tp2_on:
        exit_line = f"🎯 TP2 (sapu bersih sisa): **+{tp2_val:.0f}%**"
    elif trail_on:
        exit_line = f"📈 Moonbag runner: **trailing {trail_pct:.0f}% dari ATH** (TP2 mati)"
    else:
        exit_line = "📈 Moonbag runner: **dibiarkan terbuka** (TP2 & trailing mati)"
    lines = [
        "⚙️ **Settings Utama** (tap untuk edit)\n",
        f"💵 Buy size: **${buy_val}** / call",
        f"🎯 TP1 (ambil modal 50%): **+{tp1_val:.0f}%**",
        exit_line,
        f"🛑 SL (stop loss): **{sl_val:.0f}%**\n",
        "_Filter keamanan & radar aktif (otomatis):_",
        f"• Min Liquidity: **${db.get('scanner_min_liq', str)}**",
        f"• Max Market Cap: **${db.get('scanner_mcap_max', str)}**",
        f"• Dev Max: **{db.get('dev_max_pct', str)}%** · Top 10 Max: **{db.get('top10_max_pct', str)}%**",
        f"• Gas L2: **-{db.get('paper_gas_eth', float)*2400:.2f}** · Spread entry (breakeven): **2.5%** · Slippage: **2%**",
    ]
    buttons = [
        [Button.inline(f"💵 Buy (${buy_val})", b"set:buy_usd"),
         Button.inline(f"🎯 TP1 (+{tp1_val:.0f}%)", b"set:tp1")],
        [Button.inline(f"🎯 TP2 (+{tp2_val:.0f}%) {'ON' if tp2_on else 'OFF'}", b"set:tp2"),
         Button.inline(f"🛑 SL ({sl_val:.0f}%)", b"set:sl")],
        [Button.inline("⚙️ Setelan Lanjutan (Advanced)", b"settings_adv")],
        [Button.inline("« Menu", b"menu")],
    ]
    return "\n".join(lines), buttons


def settings_advanced_panel():
    lines, buttons = ["⚙️ **Setelan Lanjutan (Advanced)**\n_Parameter teknis mesin & filter scanner:_\n"], []
    row = []
    for key, label in config.SETTING_LABELS.items():
        if key in MAIN_SETTING_KEYS:
            continue
        lines.append(f"{label}: **{db.get(key, str)}**")
        row.append(Button.inline(label.split(" ")[0] + " " + key, f"set:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([Button.inline("« Kembali ke Settings Utama", b"settings"), Button.inline("« Menu", b"menu")])
    return "\n".join(lines), buttons


def wallet_panel():
    ws = db.list_wallets()
    lines = ["👛 **Wallet** — multi-wallet gaya trading bot\n"]
    if not ws:
        lines.append("_belum ada wallet._\nTap **➕ Generate** buat bikin baru, atau **📥 Import** key lama.")
    else:
        for w in ws:
            star = "⭐ " if w["active"] else ""
            extra = ""
            if w["active"]:
                try:
                    extra = f" · ⛽ {_trader.eth_balance():.5f} ETH"
                except Exception:
                    pass
            lock = " 🔒" if w["enc"] else ""
            lines.append(f"{star}**{w['name']}**{lock}{extra}\n`{w['address']}`")
    buttons = []
    for w in ws:
        row = []
        if not w["active"]:
            row.append(Button.inline(f"⭐ Pakai {w['name'][:10]}", f"wact:{w['id']}"))
        row.append(Button.inline("🔑 Key", f"wkey:{w['id']}"))
        row.append(Button.inline("🗑", f"wdel:{w['id']}"))
        buttons.append(row)
    buttons.append([Button.inline("➕ Generate", b"wgen"), Button.inline("📥 Import", b"wimport")])
    buttons.append([Button.inline("🔄 Reset Saldo", b"paper_reset"),
                    Button.inline("➕ Tambah $50", b"paper_add_50")])
    buttons.append([Button.inline("« Menu", b"menu")])
    wallet_lines = [
        "🧪 **Dompet Virtual (Mode Latihan)**",
        f"Kas Tersedia: **${db.get_paper_cash():.2f}** "
        f"(Modal Awal: ${float(db.get('paper_initial_usd')):.2f})",
        "",
    ]
    lines = ["\n".join(wallet_lines), "\n".join(lines)]
    return "\n".join(lines), buttons


def stats_panel():
    s = db.stats()
    pnl_eth = s["eth_out"] - s["eth_in"]
    lines = [
        "📈 **Stats**\n",
        f"📨 call terdeteksi: **{s['calls']}**",
        f"🟢 total buy: **{s['buys']}** (${s['spent']:.2f})",
        f"📊 open: **{s['open']}** | 🎯 TP1: **{s['tp1']}** | 🎯 TP2: **{s['tp2']}** | "
        f"🛑 SL: **{s['sl']}** | ⏱ Timeout: **{s['timeout']}**",
        f"📈 trail: **{s['trail']}** | 🚨 rug: **{s['rug']}** | 👆 manual: **{s['manual']}** | 🧹 empty: **{s['empty']}**",
        f"⛽ ETH keluar (incl. gas buy): {s['eth_in']:.6f} | ETH balik (real): {s['eth_out']:.6f}",
        f"💰 PnL realized: **{pnl_eth:+.6f} ETH**",
    ]
    top = db.top_callers(5)
    if top:
        lines.append("\n🏆 **Skor Caller** (TP1-rate)")
        for i, cl in enumerate(top, 1):
            rate = (cl["hit_tp1"] or 0) / cl["buys"] * 100 if cl["buys"] else 0
            lines.append(f"{i}. **{cl['caller_name'] or '?'}** — {cl['buys']} call · "
                         f"{rate:.0f}% TP1 · {cl['full_tp'] or 0} full TP · {cl['stopped'] or 0} SL")
    buttons = [
        [Button.inline("🔄 Reset Stats ke 0", b"stats_reset"), Button.inline("« Menu", b"menu")]
    ]
    return "\n".join(lines), buttons


def agent_panel():
    on = db.get("agent_on", str) == "1"
    manage = db.get("agent_manage_on", str) == "1"
    dry = db.get("agent_dry_run", str) == "1"
    key_ok = bool(config.LLM_API_KEY)
    n_lessons = len(db.recent_lessons(100))
    mode = db.get("entry_mode", str) or "mata"
    mode_line = ("👁 **MATA (LLM)** — keputusan entry oleh LLM"
                 if mode == "mata"
                 else "⚡ **FAST-PATH (Math)** — evaluasi murni matematika (0 detik LLM)")
    lines = [
        "🧠 **OTAK — lapisan LLM hoodsniper**\n",
        f"⚡ saklar entry: {mode_line}",
        f"provider: **{config.LLM_PROTOCOL}**" + (f" · `{config.LLM_BASE_URL}`" if config.LLM_BASE_URL else ""),
        f"API key: {'✅ terpasang' if key_ok else '❌ belum ada LLM_API_KEY di .env'}",
        f"👁 Mata (keputusan entry): **{'ON' if on else 'OFF'}**",
        f"✋ Tangan (review posisi): **{'ON' if manage else 'OFF'}** (tiap {db.get('agent_review_min'):.0f} mnt)",
        f"mode: **{'🧪 MODE LATIHAN' if dry else '🔴 LIVE'}**",
        f"model: `{db.get('agent_model', str)}`",
        f"💡 insting tersimpan: **{n_lessons}**\n",
    ]
    for d in db.recent_decisions(3):
        oc = f" → {d['outcome']}" if d.get("outcome") else ""
        lines.append(f"• [{d['role']}] **{d['action']} {d['symbol']}** ({d['conviction']}){oc}\n  _{d['reason'][:80]}_")
    buttons = [
        [Button.inline(
            ("🔘 Mode: 👁 MATA (LLM)" if mode == "mata"
             else "🔘 Mode: ⚡ FAST-PATH (Math)"),
            b"toggle_entry_mode")],
        [Button.inline(f"{'🔴 Off' if on else '🟢 On'} Mata", b"agt:on"),
         Button.inline(f"{'🔴 Off' if manage else '🟢 On'} Tangan", b"agt:manage")],
        [Button.inline(f"{'🔴 Live-kan' if dry else '🧪 Mode Latihan'}", b"agt:dry"),
         Button.inline("🤖 Ganti model", b"agt:model")],
        [Button.inline("👣 Jejak", b"agt:log"),
         Button.inline("💡 Latih insting", b"agt:lessons")],
        [Button.inline("« Menu", b"menu")],
    ]
    return "\n".join(lines), buttons


# ---------- register handlers ----------
def register(bot, trader, user_client=None):
    global _trader, _user
    _trader = trader
    _user = user_client

    @bot.on(events.NewMessage(pattern=r"^/start|^/menu"))
    async def start(event):
        if not _admin(event):
            # PUBLIK: tanpa panel, tanpa data apa pun — hanya arahan
            # ke channel log filter koin.
            return await event.respond(public_start_text(),
                                       buttons=public_start_buttons())
        _pending.pop(event.sender_id, None)
        await event.respond(main_menu_text(), buttons=main_menu_buttons())

    @bot.on(events.NewMessage(pattern=r"^/log"))
    async def log_help(event):
        """Semua user: arahkan ke log publik. Admin dapat status teknisnya."""
        if _admin(event):
            if config.PUBLIC_LOG_CHAT_ID:
                status = (f"✅ **AKTIF** → `{config.PUBLIC_LOG_CHAT_ID}`"
                          + (f"\n🔗 {config.PUBLIC_LOG_LINK}"
                             if config.PUBLIC_LOG_LINK else ""))
            else:
                status = ("❌ **NONAKTIF** — isi `PUBLIC_LOG_CHAT_ID` di `.env` "
                          "lalu restart bot.")
            return await event.respond(
                "📜 **Log Publik — Filter Koin**\n\n"
                f"Status: {status}\n\n"
                "_Yang disiarkan: SKIP/BUY/SIMULASI dari filter koin "
                "(tanpa tombol aksi). Panel, posisi, TP/SL, rekap, dan "
                "alert insider tetap khusus chat admin ini._",
                buttons=public_start_buttons())
        return await event.respond(public_start_text(),
                                   buttons=public_start_buttons())

    @bot.on(events.NewMessage(pattern=r"^/id"))
    async def id_help(event):
        if not _admin(event):
            return
        await event.respond(
            "🆔 **Cari chat_id channel/grup**\n\n"
            "Forward 1 pesan dari channel-nya ke chat ini — gw balas chat_id-nya.\n\n"
            "_Kalau channel-nya restrict forward, pakai **Copy Message Link** "
            "(tahan pesan → Copy Message Link) terus paste langsung ke ➕ Tambah Source._"
        )

    @bot.on(events.NewMessage(func=lambda e: e.is_private and e.message.fwd_from is not None))
    async def id_from_forward(event):
        if not _admin(event):
            return
        fwd = event.message.fwd_from
        peer = getattr(fwd, "from_id", None)
        if peer is None:
            return await event.respond(
                "⚠️ Pesan ini menyembunyikan sumbernya (privacy forward), jadi id-nya gak kebaca.\n"
                "Pakai **Copy Message Link** dari channel-nya, lalu paste ke ➕ Tambah Source."
            )
        try:
            cid = utils.get_peer_id(peer)
        except Exception as e:
            return await event.respond(f"⚠️ gagal baca id: {e}")
        name = getattr(fwd, "from_name", None) or ""
        tail = f" — {name}" if name else ""
        await event.respond(
            f"🆔 chat_id: `{cid}`{tail}\n\n"
            f"Tambah jadi source, kirim (ganti mcap & nama):\n"
            f"`{cid} 30000 Nama Channel`\n\n"
            f"_pastikan akun userbot sudah join channel ini dulu._"
        )

    @bot.on(events.NewMessage(pattern=r"^/tanya\s+(.+)", func=lambda e: e.is_private))
    async def tanya_cmd(event):
        if not _admin(event):
            return
        if not config.LLM_API_KEY:
            return await event.respond("LLM_API_KEY belum diisi — /tanya butuh Otak nyala.")
        q = event.pattern_match.group(1)
        await event.respond("🧠 mikir…")
        from . import agent as _agent, engine as _engine
        import time as _t
        ctx = {
            "stats": db.stats(),
            "open_positions": [{k: p.get(k) for k in
                ("symbol", "entry_price_usd", "tokens_left", "tp1_done", "source_name", "caller_name")}
                for p in db.open_positions()],
            "recent_decisions": db.recent_decisions(8),
            "top_callers": db.top_callers(5),
            "blacklist": db.list_blacklist(),
            "settings": {k: db.get(k, str) for k in config.SETTING_DEFAULTS},
            "recap_24h": _engine.build_recap(int(_t.time()) - 86400),
        }
        try:
            ans = await __import__("asyncio").to_thread(_agent.tanya, q, ctx)
            await event.respond(ans or "…kosong")
        except Exception as e:
            await event.respond(f"⚠️ gagal: {e}")

    @bot.on(events.CallbackQuery())
    async def cb(event):
        if not _admin(event):
            return await event.answer(
                "🔒 Panel khusus admin. Log filter koin ada di channel publik.",
                alert=True)
        data = event.data.decode()
        try:
            await _route(event, data)
        except Exception as e:
            traceback.print_exc()
            await event.answer(f"error: {e}"[:190], alert=True)

    @bot.on(events.NewMessage())
    async def text_input(event):
        if not _admin(event) or event.raw_text.startswith("/"):
            return
        if event.message.fwd_from is not None:
            return  # pesan forward → ditangani id_from_forward, bukan input source
        action = _pending.pop(event.sender_id, None)
        if not action:
            return
        if action == "add_source":
            await _do_add_source(event)
        elif action == "insaddr":
            # input teks alamat wallet untuk 🕵️ Insider → 🔍 Scan Wallet
            addr = insider_addr_valid(event.raw_text)
            if not addr:
                await event.respond(
                    "❌ Format alamat EVM salah.\n"
                    "Kirim **satu alamat** polos: `0x` + 40 karakter hex "
                    "(0-9, a-f).\nContoh: `0x1234abcd…` (40 hex)"
                )
                _pending[event.sender_id] = "insaddr"
                return
            await event.respond(f"🔎 Screening wallet `{addr}` di background…")
            await _scan_wallet_done(event, addr)
        elif action == "import_wallet":
            from eth_account import Account
            pk = event.raw_text.strip()
            pk = pk if pk.startswith("0x") else "0x" + pk
            try:
                acct = Account.from_key(pk)
            except Exception as e:
                return await event.respond(f"❌ private key tidak valid: {e}")
            first = db.wallet_count() == 0
            name = f"Wallet {db.wallet_count() + 1}"
            db.add_wallet(name, acct.address, pk, make_active=first)
            if first:
                try:
                    _trader.use_wallet(pk)
                except Exception:
                    pass
            await event.respond(
                f"✅ **{name}** di-import\n`{acct.address}`"
                + ("\n✅ diset jadi wallet aktif." if first else "")
                + "\n\n_hapus pesan key-mu di atas biar aman._")
            t, b = wallet_panel()
            await event.respond(t, buttons=b)
        elif action == "agentset:agent_model":
            db.set_("agent_model", event.raw_text.strip())
            await event.respond(f"✅ model → `{event.raw_text.strip()}`")
            t, b = agent_panel()
            await event.respond(t, buttons=b)
        elif action == "paper_reset":
            raw = event.raw_text.strip().lstrip("$").replace(",", "")
            try:
                amt = float(raw)
                if amt <= 0:
                    raise ValueError("harus > 0")
            except ValueError:
                _pending[event.sender_id] = "paper_reset"
                return await event.respond("❌ Masukkan angka > 0 (contoh: `20` atau `100`).")
            db.reset_paper_cash(amt)
            await event.respond(f"✅ Saldo virtual direset ke **${amt:.2f}**")
            t, b = wallet_panel()
            await event.respond(t, buttons=b)
        elif action.startswith("set:"):
            key = action[4:]
            val = event.raw_text.strip()
            try:
                float(val)
                db.set_(key, val)
                await event.respond(f"✅ **{config.SETTING_LABELS[key]}** → `{val}`")
            except ValueError:
                await event.respond("❌ Harus angka. Ulangi dari ⚙️ Settings.")
            t, b = (settings_panel() if key in MAIN_SETTING_KEYS
                    else settings_advanced_panel())
            await event.respond(t, buttons=b)


async def _route(event, data):
    if data == "menu":
        await event.edit(main_menu_text(), buttons=main_menu_buttons())
    elif data == "positions":
        await event.answer("loading harga…")
        t, b = await positions_panel()
        await event.edit(t, buttons=b)
    elif data == "sources":
        t, b = sources_panel()
        await event.edit(t, buttons=b)
    elif data == "settings":
        t, b = settings_panel()
        await event.edit(t, buttons=b)
    elif data == "settings_adv":
        t, b = settings_advanced_panel()
        await event.edit(t, buttons=b)
    elif data == "stats":
        t, b = stats_panel()
        await event.edit(t, buttons=b)
    elif data == "stats_reset":
        pos_id, call_id = db.reset_stats()
        await event.answer(f"✅ Hitungan stats direset ke 0 (baseline pos #{pos_id})", alert=True)
        t, b = stats_panel()
        await event.edit(t, buttons=b)
    elif data == "agent":
        t, b = agent_panel()
        await event.edit(t, buttons=b)
    elif data == "toggle_entry_mode":
        new_mode = "fast" if (db.get("entry_mode", str) or "mata") == "mata" else "mata"
        db.set_("entry_mode", new_mode)
        toast = (f"⚡ Mode entry diubah ke: {new_mode.upper()} (0 detik LLM)"
                 if new_mode == "fast"
                 else f"👁 Mode entry diubah ke: MATA (keputusan via LLM)")
        await event.answer(toast[:190])
        t, b = agent_panel()
        try:
            await event.edit(t, buttons=b)
        except Exception:
            pass  # MessageNotModified / konten sama — abaikan
    elif data == "agt:on":
        db.set_("agent_on", "0" if db.get("agent_on", str) == "1" else "1")
        t, b = agent_panel(); await event.edit(t, buttons=b)
    elif data == "agt:manage":
        db.set_("agent_manage_on", "0" if db.get("agent_manage_on", str) == "1" else "1")
        t, b = agent_panel(); await event.edit(t, buttons=b)
    elif data == "agt:dry":
        db.set_("agent_dry_run", "0" if db.get("agent_dry_run", str) == "1" else "1")
        if db.get("agent_dry_run", str) == "0":
            await event.answer("🔴 LIVE — Otak sekarang eksekusi dana beneran", alert=True)
        t, b = agent_panel(); await event.edit(t, buttons=b)
    elif data == "agt:model":
        _pending[event.sender_id] = "agentset:agent_model"
        await event.respond(f"🤖 Model sekarang: `{db.get('agent_model', str)}`\nKirim nama model baru:")
    elif data == "agt:log":
        rows = db.recent_decisions(8)
        if not rows:
            await event.answer("belum ada keputusan", alert=True)
        else:
            lines = ["👣 **JEJAK — riwayat keputusan Otak**\n"]
            for d in rows:
                oc = f" → {d['outcome']}" if d.get("outcome") else ""
                lines.append(f"• [{d['role']}] **{d['action']} {d['symbol']}** ({d['conviction']}){oc}\n  _{d['reason'][:100]}_")
            await event.respond("\n".join(lines))
    elif data == "agt:lessons":
        await event.answer("melatih insting dari posisi closed…")
        try:
            out = await __import__("asyncio").to_thread(agent.derive_lessons)
            if out:
                await event.respond("💡 **Insting baru terbentuk:**\n" + "\n".join(f"• {t}" for t in out))
            else:
                await event.respond("Belum cukup pengalaman — butuh ≥5 posisi closed buat melatih insting.")
        except Exception as e:
            await event.respond(f"⚠️ gagal melatih insting: {e}")
    elif data.startswith("applyset:"):
        _, key, val = data.split(":", 2)
        if key not in config.SETTING_DEFAULTS:
            return await event.answer("⛔ key tidak diizinkan", alert=True)
        db.set_(key, val)
        await event.answer(f"✅ {key} → {val}")
        await event.respond(f"🎓 Diterapkan: **{key} = {val}**")
    elif data == "wallet":
        t, b = wallet_panel()
        await event.edit(t, buttons=b)
    elif data in ("paper_reset", "paper_reset_100"):
        _pending[event.sender_id] = "paper_reset"
        await event.respond(
            f"🔄 **Reset Saldo Virtual**\n"
            f"Kas sekarang: **${db.get_paper_cash():.2f}** "
            f"(Modal awal: ${float(db.get('paper_initial_usd') or 0):.2f})\n\n"
            f"Kirim nominal saldo baru (USD, contoh: `20` atau `100`):"
        )
    elif data == "paper_add_50":
        new_cash = db.add_paper_cash(50.0)
        await event.answer(f"Ditambah $50 ke kas virtual "
                           f"(saldo ${new_cash:.2f})")
        t, b = wallet_panel()
        await event.edit(t, buttons=b)
    elif data == "wgen":
        from eth_account import Account
        acct = Account.create()
        pk = acct.key.hex()
        pk = pk if pk.startswith("0x") else "0x" + pk
        first = db.wallet_count() == 0
        name = f"Wallet {db.wallet_count() + 1}"
        db.add_wallet(name, acct.address, pk, make_active=first)
        if first:
            try:
                _trader.use_wallet(pk)
            except Exception:
                pass
        enc_note = "\n_(key disimpan terenkripsi di server)_" if config.WALLET_SECRET else ""
        await event.respond(
            f"🆕 **{name} dibuat**\n\n"
            f"📬 Address (buat kirim dana):\n`{acct.address}`\n\n"
            f"🔑 **PRIVATE KEY — backup SEKARANG, lalu hapus pesan ini:**\n`{pk}`\n\n"
            f"⚠️ Siapa pun yang pegang key ini bisa nguras wallet. Simpan offline, "
            f"jangan share. Ini hot wallet di server — isi secukupnya (burner).{enc_note}"
            + ("\n\n✅ Diset jadi **wallet aktif** (dipakai buat beli)." if first else ""))
        t, b = wallet_panel()
        await event.respond(t, buttons=b)
    elif data == "wimport":
        _pending[event.sender_id] = "import_wallet"
        await event.respond(
            "📥 Kirim **private key EVM** (0x…) yang mau di-import.\n"
            "_Setelah masuk, hapus pesan key-mu biar gak nyangkut di history._")
    elif data.startswith("wact:"):
        w = db.get_wallet(int(data[5:]))
        if not w:
            return await event.answer("wallet tidak ada", alert=True)
        try:
            _trader.use_wallet(db.wallet_privkey(w))
            db.set_active_wallet(w["id"])
            await event.answer(f"⭐ aktif: {w['name']}")
        except Exception as e:
            return await event.answer(f"gagal: {e}"[:190], alert=True)
        t, b = wallet_panel()
        try:
            await event.edit(t, buttons=b)
        except Exception:
            pass
    elif data.startswith("wkey:"):
        w = db.get_wallet(int(data[5:]))
        if not w:
            return await event.answer("wallet tidak ada", alert=True)
        try:
            pk = db.wallet_privkey(w)
        except Exception as e:
            return await event.answer(f"gagal baca key: {e}"[:190], alert=True)
        await event.respond(
            f"🔑 **{w['name']}** — private key:\n`{pk}`\n\n"
            f"📬 `{w['address']}`\n\n⚠️ hapus pesan ini setelah backup. Jangan share ke siapa pun.")
    elif data.startswith("wdel:"):
        w = db.get_wallet(int(data[5:]))
        if w:
            db.del_wallet(w["id"])
            await event.answer("🗑 wallet dihapus dari bot")
        t, b = wallet_panel()
        try:
            await event.edit(t, buttons=b)
        except Exception:
            pass
    elif data == "pause":
        db.set_("paused", "0" if db.paused() else "1")
        await event.answer("⏸ paused" if db.paused() else "▶️ resumed")
        await event.edit(main_menu_text(), buttons=main_menu_buttons())
    elif data.startswith("sell:"):
        _, pid, pct = data.split(":")
        await event.answer("eksekusi sell…")
        result = await engine.execute_sell(int(pid), int(pct) / 100, "manual")
        await event.respond(result)
    elif data.startswith("srct:"):
        db.toggle_source(int(data[5:]))
        t, b = sources_panel()
        await event.edit(t, buttons=b)
    elif data.startswith("srcd:"):
        db.del_source(int(data[5:]))
        await event.answer("🗑 dihapus")
        t, b = sources_panel()
        await event.edit(t, buttons=b)
    elif data == "srcadd":
        _pending[event.sender_id] = "add_source"
        await event.respond(
            "➕ **Tambah source** — kirim 1 baris:\n\n"
            "`<target> <mcap_max> <nama>`\n\n"
            "**Channel PUBLIK** (auto-resolve):\n"
            "`@degencalls 30000 Degen Public`\n"
            "`https://t.me/degencalls 30000 Degen Public`\n\n"
            "**Channel/Grup PRIVATE** (harus sudah join):\n"
            "`-1001234567890 15000 Alpha Private` _(chat_id)_\n"
            "`https://t.me/c/2103131992/250551 30000 Degen Hood` _(link + topic)_\n\n"
            "⚠️ Akun Telegram Kakak WAJIB sudah join channel-nya dulu "
            "(publik maupun private), baru bisa dipantau.\n"
            "💡 Gak tau chat_id private-nya? Ketik /id lalu forward 1 pesan dari channel itu."
        )
    elif data.startswith("ins:"):
        # 🕵️ Insider — buka layar yang ada di dalam 📡 Sources
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        t, b = insider_screen_panel(page)
        await event.edit(t, buttons=b)
    elif data.startswith("insbuys"):
        parts = data.split(":", 1)
        win = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 3600
        t, b = insider_buys_panel(win)
        try:
            await event.edit(t, buttons=b)
        except Exception:
            pass
    elif data.startswith("inspage:"):
        page = data.split(":", 1)[1]
        t, b = insider_screen_panel(int(page)) if page.isdigit() \
            else insider_screen_panel(0)
        try:
            await event.edit(t, buttons=b)
        except Exception:
            pass
    elif data == "inspagecur":
        await event.answer()  # indikator halaman tidak bisa diklik
    elif data.startswith("instoggle:"):
        # instoggle:<page>:<addr> → toggle_insider + refresh page yang sama
        _, page_s, addr = data.split(":", 2)
        new_state = db.toggle_insider(addr)
        if new_state is None:
            return await event.answer("wallet tidak ada", alert=True)
        await event.answer("✅ diaktifkan" if new_state else "🚫 dinonaktifkan")
        t, b = insider_screen_panel(int(page_s or 0))
        await event.edit(t, buttons=b)
    elif data == "insaddr":
        _pending[event.sender_id] = "insaddr"
        await event.respond(
            "🔍 **Scan Wallet** — kirim alamat wallet EVM polos untuk "
            "di-screening.\n"
            "Format: `0x` + 40 hex.\n"
            "_Kirim satu alamat per pesan._")
    elif data == "insrotate":
        global _rotate_task, _rotate_summary
        if _rotate_task is not None and not _rotate_task.done():
            return await event.answer("🔄 rotasi sedang berjalan…", alert=True)
        _rotate_summary = None
        _rotate_task = asyncio.create_task(_rotate_now(event))
    elif data == "insreset":
        rows = db.list_insiders()
        if not rows:
            return await event.answer("belum ada wallet", alert=True)
        t, b = _insider_confirm_reset_text(rows)
        await event.edit(t, buttons=b)
    elif data == "insreset:no":
        t, b = insider_screen_panel(0)
        await event.edit(t, buttons=b)
    elif data == "insreset:yes":
        rows = db.list_insiders()
        for r in rows:
            if r["enabled"]:
                db.toggle_insider(r["address"])
        db.set_("auto_rotate_lock", "0")
        await event.answer(f"🚫 {len(rows)} wallet dinonaktifkan"
                           " (tidak ada yang dihapus)")
        t, b = insider_screen_panel(0)
        await event.edit(t, buttons=b)
    elif data == "insenableall":
        rows = db.list_insiders()
        for r in rows:
            if not r["enabled"]:
                db.toggle_insider(r["address"])
        await event.answer(f"✅ {len(rows)} wallet diaktifkan")
        t, b = insider_screen_panel(0)
        await event.edit(t, buttons=b)
    elif data.startswith("insconf:"):
        # Tambah wallet hasil screening ke insider — via add_insider_wallet
        _, addr, ca = data.split(":", 2)
        if not insider_addr_valid(addr):
            return await event.answer("❌ alamat invalid", alert=True)
        db.add_insider_wallet(addr, label="manual_panel", win_count=1,
                              notes=f"scan: {ca}", win_cas=ca,
                              update_if_exists=True)
        was_before = any(r["address"] == addr.lower()          # prior exist
                         for r in db.list_insiders())
        await event.answer("✅ +1 win (sudah ada)" if was_before
                           else "✅ ditambah ke insider")
        await event.respond(
            f"✅ **{addr}** masuk daftar insider.\n"
            f"`{addr}` · win +1 · label `manual_panel`\n"
            f"_dedupe: kalau wallet sudah ada, win_count-nya naik (bukan duplikat)._")
    elif data.startswith("setval:"):
        _, key, val = data.split(":", 2)
        db.set_(key, val)
        await event.answer(f"✅ {key} → {val}")
        if key in MAIN_SETTING_KEYS:
            t, b = settings_panel()
        else:
            t, b = settings_advanced_panel()
        try:
            await event.edit(t, buttons=b)
        except Exception:
            pass  # MessageNotModified / pesan sama — abaikan
    elif data.startswith("set:"):
        key = data[4:]
        _pending[event.sender_id] = data
        await event.respond(
            f"⚙️ **{config.SETTING_LABELS[key]}**\n"
            f"nilai sekarang: `{db.get(key, str)}`\n\nKirim nilai baru (angka):"
        )


async def _resolve_target(target: str):
    """Ubah input jadi (chat_id, topic). Support publik & private.

    - `-100xxxxxxxxxx`                    → private/any (sudah join), topic 0
    - `t.me/c/<internal>[/<topic>]`       → private (link internal)
    - `@username` / `t.me/username[/<t>]` → publik, resolve via userbot get_entity
    """
    target = target.strip()

    # 1) chat_id numerik langsung
    try:
        return int(target), 0
    except ValueError:
        pass

    # 2) link private t.me/c/<internal>/<topic>
    m = LINK_RE.search(target)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(2) or 0)

    # 3) publik: @username atau t.me/username
    #    NB: angka di belakang link publik (t.me/username/123) itu ID PESAN,
    #    bukan topic — jadi channel broadcast selalu topic 0 (pantau semua pesan).
    #    Forum privat pakai cabang t.me/c/<internal>/<topic> di atas.
    t = target
    for pre in ("https://", "http://"):
        if t.startswith(pre):
            t = t[len(pre):]
    if t.startswith("t.me/"):
        t = t[len("t.me/"):]
    t = t.lstrip("@")
    username = (t.split("/")[0] or "").strip()
    if not username:
        raise ValueError("target kosong")
    if _user is None:
        raise ValueError("userbot belum siap — pakai chat_id (-100...) buat sekarang")
    ent = await _user.get_entity(username)          # butuh sudah join utk baca pesannya
    return utils.get_peer_id(ent), 0


async def _do_add_source(event):
    parts = event.raw_text.strip().split(maxsplit=2)
    if len(parts) < 3:
        return await event.respond("❌ Format: `<target> <mcap_max> <nama>`")
    target, mcap_s, name = parts
    try:
        mcap = float(mcap_s)
    except ValueError:
        return await event.respond("❌ mcap_max harus angka.")
    try:
        chat_id, topic = await _resolve_target(target)
    except ValueError as e:
        return await event.respond(f"❌ Target tidak valid: {e}")
    except Exception as e:
        return await event.respond(
            f"❌ Gagal resolve `{target}`: {e}\n"
            "Pastikan akun sudah join channel-nya. "
            "Kalau private, pakai chat_id `-100...` langsung."
        )
    db.add_source(chat_id, topic, name, mcap)
    await event.respond(f"✅ Source **{name}** ditambahkan — `{chat_id}`"
                        f"{f' topic {topic}' if topic else ' (semua topic)'} | mcap ≤ ${mcap:,.0f}\n"
                        f"_pastikan akun userbot sudah join channel ini, kalau belum sinyalnya gak kebaca._")
    t, b = sources_panel()
    await event.respond(t, buttons=b)


# ---------- insider: background helpers (screening & rotasi) ----------

async def _rotate_now(event):
    """Jalankan rotasi pakai tools/rotate_insiders.rotate() yang sudah ada —
    di background thread. Ringkasan ke chat; klik ganda diblokir _rotate_task."""
    global _rotate_summary
    import time as _t
    await event.respond("🔄 Rotasi wallet insider dimulai: kumpulkan runner "
                        "on-chain, prune, rekrut…")
    started = int(_t.time())
    try:
        summary = await asyncio.to_thread(_invoke_rotate)
    except Exception as e:
        traceback.print_exc()
        await event.respond(f"⚠️ rotasi gagal: {e}")
        return
    if summary is None:
        await event.respond("⚠️ modul tools/rotate_insiders.load failed — "
                            "lihat log console bot.")
        return
    _rotate_summary = summary
    await event.respond(_rotate_report(summary, started))


def _invoke_rotate():
    """Impor dynamic rotate dari tools/rotate_insiders (optional module)."""
    import importlib
    mod = importlib.import_module("tools.rotate_insiders")
    if not hasattr(mod, "rotate"):
        raise AttributeError("tools.rotate_insiders.rotate tidak ada")
    return mod.rotate()


def _rotate_report(s, started=None):
    if s.get("dry"):
        return (f"🔄 Rotasi selesai ('{'dry-run'}': tidak ada perubahan DB):\n"
                f"runner: {s.get('runners', 0)} · kandidat baru: "
                f"{s.get('new_candidates', 0)} · prune: {s.get('disabled', 0)}\n"
                f"Dry-run: pakai CLI --dry-run di layar untuk ini.")
    from datetime import datetime
    took = None
    if started:
        took = datetime.fromtimestamp(started).strftime("%H:%M")
    return (
        "🔄 **Rotasi insider selesai**"
        + (f" (mulai {took})" if took else "") + "\n"
        f"🧹 prune (nonaktif): **{s.get('disabled', 0)}**\n"
        f"➕ recruit baru: **+{s.get('added', 0)}**\n"
        f"📊 total aktif sekarang: **{s.get('total_active', 0)}**\n"
        f"⛽ dari koin runner: {s.get('runners', 0)}"
            + (f" (kandidat {s.get('new_candidates', 0)})"
               if s.get("new_candidates") else "")
    )


async def _scan_wallet_done(event, addr):
    """Screening satu alamat wallet — background, pakai file yang sama
    (tools.screen_insiders). Hasil kandidat → respond tombol Tambah."""
    global _rotate_summary
    import time as _t
    started = int(_t.time())
    try:
        import tools.screen_insiders as _si
    except ImportError as e:
        await event.respond(f"⚠️ tools/screen_insiders.load failed: {e}")
        return
    try:
        rows = await asyncio.to_thread(
            _screen_one_wallet, _si, addr)
    except Exception as e:
        traceback.print_exc()
        await event.respond(f"⚠️ screening gagal: {e}")
        return
    if not rows:
        # hasil kosong: bukan error — wallet ini gak masuk cross-check
        await event.respond(
            f"🔎 Screening wallet `{addr}`: **TIDAK lolos** krisis sabar.\n"
            f"Alasan: wallet ini tidak muncul di &gt;2 koin pemenang fase awal "
            f"(coba lagi saat koin runner baru muncul).")
        return
    for _addr, cnt, cas in rows[:1]:
        btn = [[Button.inline("✅ Tambah ke insider",
                              f"insconf:{_addr}:{(cas or ['0x0'])[0][:42]}")],
               [Button.inline("« Sources", b"sources"),
                Button.inline("« Menu", b"menu")]]
        await event.respond(
            f"🔎 **Hasil screening wallet**\n"
            f"`{_addr}`\n"
            f"poin: **{cnt}** (di {len(cas)} koin pemenang)\n"
            f"detect: {(int(_t.time()) - started)}s\n"
            f"➕ Layak masuk daftar insider — tap tombol untuk tambah:\n"
            f"(TIDAK otomatis)",
            buttons=btn)


def _screen_one_wallet(si, addr):
    """Screening 1 alamat (CA token pemenang atau wallet):
    pakai fetch_early_buyers + cross_check yang sudah ada."""
    addr = (addr or "").lower()
    try:
        si.fetch_early_buyers(addr)
    except Exception:
        pass
    try:
        rows = si.cross_check([addr]) or []
    except Exception:
        rows = []
    def _match(row):
        a = row.get("address") if isinstance(row, dict) else row[0]
        return str(a).lower() == addr

    if rows:
        return [r for r in rows if _match(r)] or rows[:1]
    try:
        cas_all = si.fetch_gainers(10) or []
    except Exception:
        return []
    if not cas_all:
        return []
    try:
        rows = si.cross_check(cas_all) or []
    except Exception:
        return []
    return [r for r in rows if _match(r)]

