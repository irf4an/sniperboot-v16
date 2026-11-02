#!/usr/bin/env python3
"""hoodsniper v16 GABUNGAN — sniper Robinhood Chain DUA sumber sinyal.

Sumber 1 (listener): mirror sinyal grup/channel via akun Telegram sendiri
(Telethon, QR login) — tanpa add bot ke group.
Sumber 2 (scanner): pindai chain/pasar langsung (factory v3 + Dexscreener).
Keduanya masuk SATU pipa, SATU database. Control panel: bot BotFather.

Pemakaian:
  python bot.py wallet          # bikin wallet EVM baru (atau tampilkan yg ada)
  python bot.py wallet new      # paksa generate wallet baru
  python bot.py wallet import 0x<privatekey>   # import key ke .env
  python bot.py login   # sekali: login akun Telegram via QR (interaktif)
  python bot.py run     # jalankan (dipakai PM2)
"""
import asyncio
import os
import sys
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from sniper import (config, db, engine, listener, control_bot, scanner, tracker,
                    clone_guard, pons_radar)
from sniper.trader import Trader

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def _write_env_key(pk: str):
    """Set/replace PRIVATE_KEY di .env."""
    lines, found = [], False
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            lines = f.read().splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("PRIVATE_KEY="):
            lines[i] = f"PRIVATE_KEY={pk}"
            found = True
            break
    if not found:
        lines.append(f"PRIVATE_KEY={pk}")
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(ENV_PATH, 0o600)  # rahasia: cuma owner yang boleh baca
    except OSError:
        pass


def wallet_cmd(args):
    from eth_account import Account
    sub = args[0] if args else ""

    if sub == "import":
        if len(args) < 2:
            raise SystemExit("Pemakaian: python bot.py wallet import 0x<privatekey>")
        pk = args[1].strip()
        pk = pk if pk.startswith("0x") else "0x" + pk
        try:
            acct = Account.from_key(pk)
        except Exception as e:
            raise SystemExit(f"❌ private key tidak valid: {e}")
        _write_env_key(pk)
        print(f"✅ Key di-import ke .env\n   address: {acct.address}")
        return

    existing = (config.PRIVATE_KEY or "").strip()
    if existing and sub != "new":
        try:
            acct = Account.from_key(existing)
            print(f"👛 Wallet aktif (dari .env):\n   address: {acct.address}\n\n"
                  "Mau bikin baru? `python bot.py wallet new` "
                  "(HATI-HATI: menimpa key lama — pindahin dana dulu).")
        except Exception:
            print("⚠️ PRIVATE_KEY di .env ada tapi tidak valid. "
                  "Generate baru: `python bot.py wallet new`")
        return

    acct = Account.create()
    pk = acct.key.hex()
    pk = pk if pk.startswith("0x") else "0x" + pk
    _write_env_key(pk)
    print("🆕 Wallet EVM baru dibuat & disimpan ke .env\n")
    print(f"   address     : {acct.address}")
    print(f"   private key : {pk}\n")
    print("⚠️  SIMPAN private key ini di tempat aman (backup offline).")
    print("⚠️  Ini HOT WALLET di VPS — isi secukupnya (burner), jangan dana besar.")
    print(f"\nSelanjutnya: transfer ETH (Robinhood Chain) ke {acct.address}, lalu jalankan bot.")


async def qr_login(client):
    import qrcode
    qr = await client.qr_login()
    print("\nScan QR dari HP: Settings → Devices → Link Desktop Device\n")
    while not await client.is_user_authorized():
        q = qrcode.QRCode()
        q.add_data(qr.url)
        q.print_ascii(invert=True)
        print(f"(atau buka URL: {qr.url})")
        try:
            await qr.wait(30)
        except SessionPasswordNeededError:
            import getpass
            pw = getpass.getpass("Akun pakai 2FA. Password: ")
            await client.sign_in(password=pw)
        except asyncio.TimeoutError:
            qr = await client.qr_login()
    me = await client.get_me()
    print(f"\n✅ Login sukses: {me.first_name} (@{me.username})")


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"

    # ---- wallet: generate / import / cek (tanpa perlu login Telegram) ----
    if mode == "wallet":
        wallet_cmd(sys.argv[2:])
        return

    # ---- v16.6 DECOUPLING USERBOT: userbot 100% OPSIONAL ----
    # Userbot (akun Telegram pribadi) hanya connect bila:
    #   * setting 'userbot_enabled' = '1' di DB, ATAU
    #   * mode 'login' (QR login eksplisit dari operator).
    # Default ('0'): akun personal TIDAK online → bebas risiko ban/flag dari
    # VPS data-center IP. Sinyal 100% dipasok on-chain (scanner factory
    # Uniswap v2/v3/v4 + insider tracker + pending retry queue).
    # Bot kontrol resmi (BotFather, hoodsniper_bot) SELALU jalan — tidak
    # terpengaruh sama sekali.
    db.init()
    config.validate()
    trader = Trader()

    user = None
    userbot_enabled = (db.get("userbot_enabled", str) == "1") or mode == "login"
    if userbot_enabled:
        user = TelegramClient(config.SESSION_NAME, config.TG_API_ID, config.TG_API_HASH)
        await user.connect()

        if mode == "login":
            if await user.is_user_authorized():
                me = await user.get_me()
                print(f"Sudah login sebagai {me.first_name}. Session OK.")
            else:
                await qr_login(user)
            await user.disconnect()
            return

        if not await user.is_user_authorized():
            print("[warn] Userbot belum login atau sesi expired — lanjut tanpa userbot.")
            user = None
    else:
        print("[userbot] NON-AKTIF (setting userbot_enabled=0) — akun Telegram "
              "personal aman. Sinyal 100% on-chain: scanner factory + insider tracker.")

    bot = TelegramClient("hoodsniper_bot", config.TG_API_ID, config.TG_API_HASH)
    await bot.start(bot_token=config.BOT_TOKEN)

    async def notify(text, buttons=None):
        try:
            await bot.send_message(config.ADMIN_ID, text, buttons=buttons, link_preview=False)
        except Exception as e:
            print(f"[notify fail] {e}\n{text}")

    async def public_notify(text, buttons=None):
        """Siaran LOG PUBLIK: hanya log filter koin (dari listener) yang
        sampai ke fungsi ini — teks sama seperti ke admin, tapi tombol
        SUDAH dipangkas listener (hanya URL Chart/Tx, tanpa tombol aksi).
        Panel/posisi/TP-SL/rekap/insider TIDAK pernah lewat sini.
        Nonaktif total bila PUBLIC_LOG_CHAT_ID belum diisi di .env."""
        if not config.PUBLIC_LOG_CHAT_ID:
            return
        try:
            await bot.send_message(config.PUBLIC_LOG_CHAT_ID, text,
                                   buttons=buttons, link_preview=False)
        except Exception as e:
            print(f"[public notify fail] {e}\n{text}")

    if config.PUBLIC_LOG_CHAT_ID:
        print(f"[public-log] AKTIF → chat {config.PUBLIC_LOG_CHAT_ID} "
              "(log filter koin disiarkan tanpa tombol aksi; panel tetap admin)")
    else:
        print("[public-log] nonaktif (PUBLIC_LOG_CHAT_ID kosong di .env)")

    engine.bind(trader, notify, public_notify=public_notify)
    listener.register(user, trader, notify, public_notify=public_notify)
    control_bot.register(bot, trader, user)
    asyncio.create_task(engine.monitor_loop())
    asyncio.create_task(engine.agent_review_loop())
    asyncio.create_task(engine.daily_recap_loop())
    asyncio.create_task(scanner.scanner_loop())  # v16: scanner = sumber sinyal kedua (gabungan)
    asyncio.create_task(tracker.tracker_loop())  # v16.1: tracker insider copy-trade (sumber sinyal ketiga)
    asyncio.create_task(tracker.auto_rotation_loop(notify=notify))  # v16.3: rotasi insider otomatis 24j + scan awal instalasi baru
    # v17: Radar Pons V2 — ALERT AWAL pre-graduasi. Alert-only: tidak pernah
    # masuk pipa beli. Real-time ke DM admin; digest publik per jam (opsi C)
    # hanya bila pons_radar_public=1.
    pons_radar.bind_notify(notify, public_notify=public_notify)
    asyncio.create_task(pons_radar.blacklist_loop())
    asyncio.create_task(pons_radar.radar_loop())
    asyncio.create_task(pons_radar.digest_loop())

    await notify(control_bot.main_menu_text(), buttons=control_bot.main_menu_buttons())
    runners = [bot.run_until_disconnected()]   # bot BotFather SELALU runner inti
    if user is not None:
        runners.append(user.run_until_disconnected())
    label = "listener grup + scanner chain/pasar" if user is not None else "scanner on-chain murni (userbot off)"
    print(f"[hoodsniper v16 GABUNGAN] running: {label}. wallet={trader.addr}")
    await asyncio.gather(*runners)


if __name__ == "__main__":
    asyncio.run(main())
