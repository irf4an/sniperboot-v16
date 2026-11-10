"""💸 Panduan Dana — top up & tarik ETH, plus asisten AI grounded.

Modul ini dipakai panel Telegram supaya operator bot (siapa pun yang pegang
VPS) tetap dapat panduan langkah-demi-langkah walau tidak ada Atlas yang
mendampingi. Dua lapis:

1. DETERMINISTIK (default, selalu jalan tanpa LLM): langkah top up & tarik
   yang dirakit dari fakta nyata — alamat wallet aktif, saldo on-chain, gas
   price, dan nama jaringan. Tidak ada angka karangan.
2. LLM (opsional): kalau `LLM_API_KEY` ada, pertanyaan bebas operator
   dijawab dengan konteks langkah di atas. Kalau LLM mati, bot tetap
   menjawab lewat pencocokan kata kunci deterministik.

Prinsip: AI TIDAK PERNAH mengeksekusi transfer. Eksekusi tarik saldo selalu
lewat tombol konfirmasi eksplisit + angka yang ditampilkan dulu.
"""
from __future__ import annotations

import time

CHAIN_NAME = "Robinhood Chain"
CHAIN_ID = 4663
EXPLORER = "https://robinhoodchain.blockscout.com"
BRIDGE_URL = "https://across.to"

# Batas pengaman: satu tarikan tidak boleh lebih dari ini (ETH).
WITHDRAW_MAX_ETH = 5.0


# ---------------------------------------------------------------- panduan
def topup_guide(wallet_addr: str, bal_eth: float) -> str:
    return (
        "📥 **CARA TOP UP SALDO (Bank → Wallet Bot)**\n\n"
        f"Wallet aktif bot:\n`{wallet_addr}`\n"
        f"Saldo sekarang: **{bal_eth:.6f} ETH**\n"
        f"Jaringan: **{CHAIN_NAME}** (chain {CHAIN_ID}) — bukan Ethereum/ERC20.\n\n"
        "**PENTING:** bank & exchange (Tokocrypto/Binance) TIDAK punya jaringan "
        f"{CHAIN_NAME}. Jangan transfer langsung ke alamat di atas dari exchange, "
        "dananya nyangkut di jaringan lain.\n\n"
        "**Alur 4 langkah:**\n"
        "1️⃣ **Bank → Exchange**\n"
        "   Deposit Rupiah (VA BCA / QRIS) di Tokocrypto atau Binance, lalu beli ETH.\n"
        "2️⃣ **Exchange → MetaMask**\n"
        "   Withdraw ETH, pilih jaringan **Arbitrum One** (fee ±Rp3–8rb). "
        "Jangan pilih Ethereum/ERC20 (gas $5–10).\n"
        "3️⃣ **MetaMask → Robinhood Chain**\n"
        f"   Buka {BRIDGE_URL} → Connect Wallet → "
        f"From **Arbitrum**, To **{CHAIN_NAME}**, asset ETH.\n"
        "   Isi kolom *recipient* dengan alamat wallet bot di atas, supaya "
        "dananya langsung mendarat di bot (tidak perlu kirim dua kali).\n"
        "4️⃣ **Verifikasi**\n"
        "   Tunggu 1–2 menit, lalu tap 🔄 Refresh di panel Wallet. "
        "Saldo on-chain akan muncul otomatis.\n\n"
        "_Tips: tes dengan nominal kecil dulu (±$5) sebelum nominal besar._"
    )


def start_guide(wallet_addr: str, bal_eth: float) -> str:
    """Panduan dari nol: bikin key baru → backup → top up → trading → tarik."""
    return (
        "🧭 **MULAI DARI NOL (key baru → dana masuk → tarik lagi)**\n\n"
        "1️⃣ **Buat wallet**\n"
        "   👛 Wallet → ➕ Generate.\n"
        "   Bot kirim **alamat** + **private key** sekali. Backup key offline "
        "(kertas/password manager), lalu hapus pesan key dari chat.\n\n"
        "2️⃣ **Catat alamatnya**\n"
        f"   Wallet aktif sekarang:\n`{wallet_addr}`\n"
        "   Ini alamat yang dipakai bot buat beli. Jangan tukar-tukar.\n\n"
        "3️⃣ **Top up**\n"
        "   Bank → exchange (beli ETH) → withdraw ke **Arbitrum One** → "
        f"bridge di {BRIDGE_URL} tujuan **{CHAIN_NAME}** dengan recipient "
        "alamat di atas. Detail: tombol 📥 Top Up.\n\n"
        "4️⃣ **Biarkan bot trading**\n"
        "   Selama `agent_dry_run=1` bot cuma simulasi. Setelah yakin, matikan "
        "mode latihan supaya order benar-benar on-chain.\n\n"
        "5️⃣ **Tarik hasil**\n"
        "   📤 Tarik → masukkan alamat MetaMask + jumlah → konfirmasi. "
        "Lalu bridge balik ke Arbitrum → exchange → rekening bank.\n"
        "   Detail: tombol 📤 Tarik.\n\n"
        f"Saldo on-chain sekarang: **{bal_eth:.6f} ETH**"
    )


def withdraw_guide(wallet_addr: str, bal_eth: float, gas_gwei: float | None = None) -> str:
    gas_line = (f"Gas saat ini: **{gas_gwei:.2f} gwei** "
                f"(±{21000 * (gas_gwei or 0) * 1e-9:.6f} ETH untuk transfer).\n"
                if gas_gwei else "")
    return (
        "📤 **CARA TARIK SALDO (Wallet Bot → Bank)**\n\n"
        f"Wallet aktif bot:\n`{wallet_addr}`\n"
        f"Saldo on-chain: **{bal_eth:.6f} ETH**\n"
        f"{gas_line}\n"
        "**Alur 4 langkah:**\n"
        "1️⃣ **Bot → MetaMask**\n"
        "   Tap tombol **📤 Tarik** di bawah, masukkan alamat MetaMask pribadi + "
        "jumlah ETH. Bot akan menampilkan rincian (jumlah, gas, sisa saldo) "
        "sebelum minta konfirmasi. Tidak ada transfer tanpa konfirmasi.\n"
        "2️⃣ **Robinhood Chain → Arbitrum**\n"
        f"   Buka {BRIDGE_URL} → Connect Wallet MetaMask → "
        f"From **{CHAIN_NAME}**, To **Arbitrum**, asset ETH. "
        "Recipient bisa alamat MetaMask atau langsung alamat deposit ETH exchange.\n"
        "3️⃣ **Arbitrum → Exchange**\n"
        "   Kalau tadi dikirim ke MetaMask: withdraw ETH dari MetaMask ke exchange "
        "via jaringan **Arbitrum One** (fee murah).\n"
        "4️⃣ **Exchange → Bank**\n"
        "   Jual ETH → IDR, lalu Withdraw IDR ke rekening BCA. Cair beberapa menit.\n\n"
        "⚠️ **Jangan** kirim token/koin langsung dari bot ke exchange tanpa "
        "menyeberangkan lewat bridge dulu — exchange tidak mengenal "
        f"{CHAIN_NAME}."
    )


# --------------------------------------------------- eksekusi (butuh trader)
def _fmt_eth(wei: int) -> str:
    return f"{wei / 1e18:.6f}"


def withdraw_preview(trader, dest: str, amount_raw: str) -> dict:
    """Hitung rencana penarikan TANPA mengirim transaksi."""
    from web3 import Web3

    w3 = trader.w3
    try:
        to_chk = Web3.to_checksum_address(str(dest).strip())
    except Exception:
        return {"ok": False, "error": "Alamat tujuan tidak valid (harus 0x + 40 hex)."}

    acct = getattr(trader, "acct", None)
    if acct is None:
        return {"ok": False, "error": "Wallet aktif tidak punya private key di memori."}

    bal_wei = w3.eth.get_balance(acct.address)
    gas_price = w3.eth.gas_price
    gas_cost = 21000 * gas_price

    amt = str(amount_raw).strip().lower()
    if amt in ("all", "semua", "max"):
        send_wei = bal_wei - gas_cost
        if send_wei <= 0:
            return {"ok": False,
                    "error": f"Saldo {_fmt_eth(bal_wei)} ETH tidak cukup untuk gas "
                             f"{_fmt_eth(gas_cost)} ETH."}
    else:
        try:
            send_wei = int(w3.to_wei(float(amt), "ether"))
        except Exception:
            return {"ok": False, "error": "Jumlah ETH tidak valid (contoh: 0.02 atau all)."}
        if send_wei <= 0:
            return {"ok": False, "error": "Jumlah harus lebih dari 0."}
        if send_wei > int(WITHDRAW_MAX_ETH * 1e18):
            return {"ok": False, "error": f"Melebihi batas pengaman {WITHDRAW_MAX_ETH} ETH per tarikan."}
        if send_wei + gas_cost > bal_wei:
            return {"ok": False,
                    "error": f"Saldo kurang. Butuh {_fmt_eth(send_wei + gas_cost)} ETH "
                             f"(termasuk gas), tersedia {_fmt_eth(bal_wei)} ETH."}

    return {
        "ok": True,
        "from": acct.address,
        "to": to_chk,
        "amount_wei": send_wei,
        "amount_eth": send_wei / 1e18,
        "gas_eth": gas_cost / 1e18,
        "left_eth": (bal_wei - send_wei - gas_cost) / 1e18,
        "gwei": gas_price / 1e9,
    }


def withdraw_execute(trader, dest: str, amount_raw: str, preview: dict | None = None) -> dict:
    """Kirim ETH dari wallet aktif ke `dest`. Wajib dipanggil SETELAH preview."""
    from web3 import Web3

    w3 = trader.w3
    acct = getattr(trader, "acct", None)
    if acct is None:
        return {"ok": False, "error": "Wallet aktif tidak punya private key di memori."}

    plan = preview or withdraw_preview(trader, dest, amount_raw)
    if not plan.get("ok"):
        return plan

    to_chk = Web3.to_checksum_address(plan["to"])
    send_wei = int(plan["amount_wei"])
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    tx = {
        "nonce": nonce,
        "to": to_chk,
        "value": send_wei,
        "gas": 21000,
        "gasPrice": w3.eth.gas_price,
        "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    h = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=90)
    tx_hex = h.hex()
    tx_hex = tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex
    ok = receipt.get("status") == 1
    return {
        "ok": ok,
        "amount_eth": send_wei / 1e18,
        "to": to_chk,
        "tx_hash": tx_hex,
        "block": receipt.get("blockNumber"),
        "explorer": f"{EXPLORER}/tx/{tx_hex}",
        "error": None if ok else "transaksi revert/failed",
    }


# --------------------------------------------------------------- asisten AI
_SYSTEM = """Kamu asisten keuangan internal bot sniper Hoodsniper (Robinhood Chain,
chainId 4663). Tugasmu memandu operator manusia soal TOP UP dan TARIK SALDO.

Aturan keras:
- Jawab bahasa Indonesia santai, ringkas (maks 120 kata), langkah bernomor.
- Jawab HANYA berdasarkan data JSON yang diberikan. Jangan mengarang alamat,
  angka saldo, nama exchange, atau biaya.
- Kamu TIDAK BISA memindahkan dana. Kalau operator minta "tarikkan", arahkan
  ke tombol 📤 Tarik / 📤 Withdraw di panel — jangan bilang kamu sudah
  mengirim.
- Jangan pernah meminta/menampilkan private key atau seed phrase.
- Kalau pertanyaan di luar topik dana, bilang bahwa kamu khusus panduan
  top up & tarik saldo."""


def _keyword_answer(q: str, wallet_addr: str, bal_eth: float) -> str | None:
    """Jawaban deterministik saat LLM mati. None kalau tidak yakin."""
    t = (q or "").lower()
    if not t:
        return None
    if any(k in t for k in ("top up", "topup", "isi saldo", "deposit", "fund")):
        return topup_guide(wallet_addr, bal_eth)
    if any(k in t for k in ("tarik", "withdraw", "cair", "wd")):
        return withdraw_guide(wallet_addr, bal_eth)
    if any(k in t for k in ("bridge", "jembatan", "across")):
        return (
            f"🌉 **Bridge ke {CHAIN_NAME}**\n\n"
            f"1. Buka {BRIDGE_URL}\n"
            "2. Connect Wallet MetaMask\n"
            "3. From: jaringan asal (Arbitrum/Base), To: "
            f"{CHAIN_NAME}\n"
            f"4. Recipient: `{wallet_addr}`\n"
            "5. Confirm, tunggu 1–2 menit.\n\n"
            "Biaya bridge kecil (±$0.1–0.5), jauh lebih murah daripada withdraw "
            "ERC20 langsung."
        )
    if any(k in t for k in ("gas", "fee", "biaya")):
        return (
            "⛽ **Biaya yang perlu disiapkan**\n\n"
            f"• Transfer ETH di {CHAIN_NAME}: 21.000 gas, sangat murah (sen).\n"
            "• Bridge Across: ±$0.1–0.5 per penyeberangan.\n"
            "• Withdraw ETH dari exchange ke Arbitrum: Rp3–8rb.\n"
            "• Jual ETH → IDR di exchange: sesuai fee trading.\n\n"
            f"Saldo bot sekarang: **{bal_eth:.6f} ETH**."
        )
    if any(k in t for k in ("saldo", "balance", "berapa")):
        return (
            f"💰 Saldo on-chain wallet bot: **{bal_eth:.6f} ETH**\n"
            f"`{wallet_addr}`\n"
            f"Jaringan: {CHAIN_NAME} ({CHAIN_ID})"
        )
    return None


def ask(question: str, wallet_addr: str, bal_eth: float,
        gas_gwei: float | None = None, llm_call=None) -> str:
    """Jawab pertanyaan operator. Deterministik dulu, LLM sebagai pelengkap."""
    local = _keyword_answer(question, wallet_addr, bal_eth)
    if local:
        return local
    if not llm_call:
        return ("Aku bisa bantu soal **top up**, **tarik saldo**, **bridge**, "
                "**gas/fee**, dan **saldo**. Contoh: \"cara tarik ke BCA\".")
    ctx = {
        "wallet": wallet_addr,
        "saldo_eth": round(bal_eth, 6),
        "gas_gwei": gas_gwei,
        "chain": {"nama": CHAIN_NAME, "id": CHAIN_ID, "explorer": EXPLORER},
        "bridge": BRIDGE_URL,
        "batas_tarik_eth": WITHDRAW_MAX_ETH,
        "langkah_topup": topup_guide(wallet_addr, bal_eth),
        "langkah_tarik": withdraw_guide(wallet_addr, bal_eth, gas_gwei),
    }
    import json as _json
    try:
        return llm_call(_SYSTEM, _json.dumps({"question": question, "data": ctx}), 700)
    except Exception as e:
        return (f"⚠️ AI sedang tidak bisa dihubungi ({e.__class__.__name__}). "
                "Pakai tombol 📥 Top Up atau 📤 Tarik di atas.")
