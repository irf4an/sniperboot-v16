"""Tool penarikan (withdraw) ETH dari dompet aktif bot ke alamat tujuan di Robinhood Chain.

Penggunaan CLI:
  python3 tools/withdraw.py <alamat_tujuan> <jumlah_eth|all>

Contoh:
  python3 tools/withdraw.py 0x1234... 0.05
  python3 tools/withdraw.py 0x1234... all
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web3 import Web3  # noqa: E402
from eth_account import Account  # noqa: E402
from sniper import config, db  # noqa: E402


def withdraw_eth(to_address: str, amount_eth_or_all: str) -> dict:
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        return {"ok": False, "error": "RPC tidak terhubung"}

    # 1. Validasi alamat tujuan
    try:
        to_chk = Web3.to_checksum_address(to_address.strip())
    except Exception as e:
        return {"ok": False, "error": f"Alamat tujuan tidak valid: {e}"}

    # 2. Ambil private key wallet aktif bot
    w = db.active_wallet()
    if not w:
        pk = (config.PRIVATE_KEY or "").strip()
        if not pk:
            return {"ok": False, "error": "Tidak ada wallet aktif di DB atau .env"}
    else:
        pk = db.wallet_privkey(w)

    acct = Account.from_key(pk)
    from_addr = acct.address

    bal_wei = w3.eth.get_balance(from_addr)
    bal_eth = bal_wei / 1e18

    # 3. Hitung estimasi gas fee (transfer ETH standar = 21,000 gas)
    gas_limit = 21000
    gas_price = w3.eth.gas_price
    gas_cost_wei = gas_limit * gas_price

    if amount_eth_or_all.lower() == "all":
        send_wei = bal_wei - gas_cost_wei
        if send_wei <= 0:
            return {
                "ok": False,
                "error": f"Saldo tidak cukup untuk gas fee. Saldo: {bal_eth:.6f} ETH, Gas: {gas_cost_wei/1e18:.6f} ETH"
            }
    else:
        try:
            req_eth = float(amount_eth_or_all)
            send_wei = int(w3.to_wei(req_eth, "ether"))
        except ValueError:
            return {"ok": False, "error": "Format jumlah ETH tidak valid"}

        if send_wei + gas_cost_wei > bal_wei:
            return {
                "ok": False,
                "error": (
                    f"Saldo tidak cukup. Perlu {w3.from_wei(send_wei + gas_cost_wei, 'ether'):.6f} ETH "
                    f"(termasuk gas), saldo tersedia: {bal_eth:.6f} ETH"
                )
            }

    # 4. Buat dan kirim transaksi
    nonce = w3.eth.get_transaction_count(from_addr, "pending")
    tx = {
        "nonce": nonce,
        "to": to_chk,
        "value": send_wei,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "chainId": config.CHAIN_ID,
    }

    signed_tx = acct.sign_transaction(tx)
    raw_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    tx_hash = raw_hash.hex()
    tx_hex = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash

    # 5. Tunggu konfirmasi on-chain
    receipt = w3.eth.wait_for_transaction_receipt(raw_hash, timeout=60)
    status_ok = receipt.get("status") == 1

    return {
        "ok": status_ok,
        "from": from_addr,
        "to": to_chk,
        "amount_eth": float(w3.from_wei(send_wei, "ether")),
        "tx_hash": tx_hex,
        "block": receipt.get("blockNumber"),
        "explorer": f"https://robinhoodchain.blockscout.com/tx/{tx_hex}",
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 tools/withdraw.py <to_address> <amount_eth|all>")
        sys.exit(1)

    dest = sys.argv[1]
    amt = sys.argv[2]
    print(f"Mengirim {amt} ETH ke {dest}...")
    res = withdraw_eth(dest, amt)
    if res["ok"]:
        print(f"✅ Berhasil! Terkirim {res['amount_eth']:.6f} ETH")
        print(f"TX Hash: {res['tx_hash']}")
        print(f"Explorer: {res['explorer']}")
    else:
        print(f"❌ Gagal: {res.get('error')}")
        sys.exit(1)
