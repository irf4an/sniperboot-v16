"""Eksekusi buy/sell di Uniswap (Robinhood Chain). Sync — panggil via asyncio.to_thread."""
import threading
import time
from web3 import Web3
from eth_account import Account
from . import config, db

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "totalSupply", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]

ROUTER02_ABI = [
    {"name": "WETH9", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenIn", "type": "address"},
         {"name": "tokenOut", "type": "address"},
         {"name": "fee", "type": "uint24"},
         {"name": "recipient", "type": "address"},
         {"name": "amountIn", "type": "uint256"},
         {"name": "amountOutMinimum", "type": "uint256"},
         {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
]

POOL_ABI = [
    {"name": "fee", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint24"}]},
]

WETH_ABI = ERC20_ABI + [
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "wad", "type": "uint256"}], "outputs": []},
]

MAX_UINT = 2**256 - 1


def _txh(rcpt) -> str:
    h = rcpt.transactionHash.hex()
    return h if h.startswith("0x") else "0x" + h


def _active_key():
    """Private key wallet aktif: dari DB (wallet aktif) dulu, fallback ke .env."""
    try:
        w = db.active_wallet()
        if w:
            return db.wallet_privkey(w)
    except Exception:
        pass
    return (config.PRIVATE_KEY or "").strip() or None


class Trader:
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 30}))
        key = _active_key()
        if not key:
            raise SystemExit(
                "[trader] Belum ada wallet. Generate lewat bot: 👛 Wallet → ➕ Generate, "
                "atau CLI `python bot.py wallet new`.")
        self.acct = Account.from_key(key)
        self.addr = self.acct.address
        self.router3 = self.w3.eth.contract(
            Web3.to_checksum_address(config.SWAP_ROUTER02), abi=ROUTER02_ABI)
        self.router2 = None
        cid = self.w3.eth.chain_id
        if cid != config.CHAIN_ID:
            raise SystemExit(f"[trader] RPC nyambung ke chain {cid}, bukan {config.CHAIN_ID} — cek RPC_URL")
        _routers = [("SWAP_ROUTER02", self.router3.address)]
        for name, addr in _routers:
            if len(self.w3.eth.get_code(addr)) <= 2:
                raise SystemExit(
                    f"[trader] {name}={addr} TIDAK ADA kontraknya di chain {cid} — "
                    f"alamat salah (mungkin alamat chain lain). Ambil yang benar via "
                    f"'node tools/fetch_addresses.mjs' lalu verifikasi di Blockscout.")
        self.weth_addr = self.router3.functions.WETH9().call()
        self.weth = self.w3.eth.contract(self.weth_addr, abi=WETH_ABI)
        self._nonce_lock = threading.Lock()

    # ---------- helpers ----------
    def erc20(self, addr):
        return self.w3.eth.contract(Web3.to_checksum_address(addr), abi=ERC20_ABI)

    def eth_balance(self):
        return self.w3.eth.get_balance(self.addr) / 1e18

    def use_wallet(self, privkey: str):
        """Ganti wallet aktif saat runtime (dipanggil control bot setelah user pilih)."""
        self.acct = Account.from_key(privkey)
        self.addr = self.acct.address
        return self.addr

    def pool_fee(self, pair_address) -> int:
        try:
            return self.w3.eth.contract(
                Web3.to_checksum_address(pair_address), abi=POOL_ABI
            ).functions.fee().call()
        except Exception:
            return 3000

    def _send(self, fn, value=0, gas_mult=1.3):
        with self._nonce_lock:
            base = {"from": self.addr, "value": value, "chainId": config.CHAIN_ID}
            fn.call(base)  # simulasi dulu
            gas = int(fn.estimate_gas(base) * gas_mult)
            tx = fn.build_transaction({
                **base,
                "gas": gas,
                "gasPrice": int(self.w3.eth.gas_price * 1.2),
                "nonce": self.w3.eth.get_transaction_count(self.addr, "pending"),
            })
            signed = self.acct.sign_transaction(tx)
            h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            rcpt = self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
            if rcpt.status != 1:
                raise RuntimeError(f"tx revert: {h.hex()}")
            return rcpt

    def _ensure_approval(self, token_addr, spender):
        # Spender bisa datang dari konstanta modul yang ditulis lowercase
        # (mis. UNIVERSAL_ROUTER) — web3 menolak alamat non-checksum saat
        # membangun call `allowance()`. Normalisasi di sini supaya bug kelas
        # ini tidak bisa lagi menggagalkan buy SETELAH swap on-chain sukses.
        spender = Web3.to_checksum_address(spender)
        t = self.erc20(token_addr)
        if t.functions.allowance(self.addr, spender).call() < MAX_UINT // 2:
            self._send(t.functions.approve(spender, MAX_UINT))

    # ---------- buy ----------
    def buy(self, ca, pair, source_name="", usd_override=None):
        from . import dexscreener as dx
        ca = Web3.to_checksum_address(ca)
        ptype = dx.pool_type(pair)
        if ptype == "v2":
            raise RuntimeError("pool v2 dinonaktifkan (mode v4-only)")
        eth_price = dx.eth_usd(pair)
        if eth_price <= 0:
            raise RuntimeError("gagal derive harga ETH dari pair")

        buy_usd = usd_override if usd_override else db.get("buy_usd")
        slippage = db.get("buy_slippage")
        eth_in = buy_usd / eth_price
        wei_in = self.w3.to_wei(eth_in, "ether")

        bal = self.w3.eth.get_balance(self.addr)
        if bal < wei_in * 2:
            raise RuntimeError(f"saldo ETH kurang: {bal/1e18:.6f} ETH")

        token = self.erc20(ca)
        dec = token.functions.decimals().call()
        try:
            sym = token.functions.symbol().call()
        except Exception:
            sym = pair.get("baseToken", {}).get("symbol", "?")

        price_native = float(pair.get("priceNative") or 0)
        expected = (eth_in / price_native) if price_native else 0
        min_out = int(expected * (1 - slippage) * 10**dec) if expected else 0

        # ---------- ON-CHAIN SPOT PRICE ANCHOR (v16.6) ----------
        # Harga layar Dexscreener cached 20-30 detik. Pada koin runner, harga
        # on-chain sudah lari ke depan sementara layar masih menampilkan
        # harga pool saat creation — memicu revert TIDAK WAJAR karena min_out
        # mengacu ke harga layar basi. Probe quote riil on-chain dulu
        # (min_out=0 = spot probe murni); bila sukses → min_out dari quote
        # aktual dgn toleransi slippage sebagai proteksi drift antar-blok
        # terhadap HARGA RIIL on-chain, bukan harga layar.
        try:
            ptype_probe = ptype
            fee_probe = 3000
            pool_id_probe = None
            if ptype_probe == "v3":
                try:
                    fee_probe = int(self.pool_fee(pair["pairAddress"]))
                except Exception:
                    fee_probe = 3000
            if ptype_probe == "v4":
                pool_id_probe = pair.get("pairAddress")
            ok_q, amt_q, _qerr = self.quote_buy_tokens(
                ca, ptype_probe, fee_probe, wei_in, min_out=0,
                pool_id=pool_id_probe)
            if ok_q and amt_q > 0:
                min_out = int(amt_q * (1.0 - slippage))
        except Exception as e:
            print(f"[buy anchor] {sym} quoter spot probe gagal "
                  f"({e}) — fallback rumus priceNative")

        before = token.functions.balanceOf(self.addr).call()
        eth_before = self.w3.eth.get_balance(self.addr)

        if ptype == "v4":
            # ---------- Uniswap v4: pairAddress = PoolId, bukan kontrak ----------
            from . import v4 as v4mod
            pool_id = pair["pairAddress"]
            liq = v4mod.liquidity(pool_id, self.w3)
            if not liq:  # 0 atau None → pool kosong / data tak terbaca
                raise RuntimeError(f"[skip] {sym} v4 liquidity 0 (pool kosong)")
            pk = v4mod.pool_key(pool_id, self.w3)
            if pk is None:
                raise RuntimeError(f"[skip] {sym} v4_poolkey_notfound (event "
                                   f"Initialize tidak ketemu dalam 200k blok)")
            fee = pk.fee
            self._last_v4_pool_id = pool_id  # disimpan buat posisi/quote sell
            commands, inputs, value = v4mod.encode_buy(pk, wei_in, min_out)
            fn = self.w3.eth.contract(
                Web3.to_checksum_address(v4mod.UNIVERSAL_ROUTER),
                abi=v4mod.UNIVERSAL_ROUTER_ABI
            ).functions.execute(commands, inputs, int(time.time()) + 120)
            rcpt = self._send(fn, value=value)
            router_used = v4mod.UNIVERSAL_ROUTER
        else:
            fee = self.pool_fee(pair["pairAddress"])
            fn = self.router3.functions.exactInputSingle((
                self.weth_addr, ca, fee, self.addr, wei_in, min_out, 0))
            rcpt = self._send(fn, value=wei_in)
            router_used = self.router3.address
        buy_tx = _txh(rcpt)

        got_raw = token.functions.balanceOf(self.addr).call() - before
        if got_raw <= 0:
            raise RuntimeError("swap sukses tapi token tidak masuk")
        got = got_raw / 10**dec
        eth_out_real = (eth_before - self.w3.eth.get_balance(self.addr)) / 1e18  # termasuk gas

        # Approval pasca-swap TIDAK boleh mematikan pencatatan posisi.
        # Kalau langkah ini gagal (RPC, allowlist, dll.) token SUDAH ada di
        # wallet; sell v4 tetap melakukan approval sendiri lewat
        # v4.ensure_v4_sell_approval(). Sebelumnya exception di sini membuat
        # posisi tidak pernah tercatat → tidak ada TP/SL otomatis.
        try:
            self._ensure_approval(ca, router_used)
        except Exception as _e:
            print(f"[trader] approval pasca-buy gagal (posisi tetap dicatat): {_e}")
        honeypot = not self._can_sell(ca, ptype, fee, got_raw)

        return {
            "ca": ca.lower(), "symbol": sym,
            # `name` WAJIB ikut: clone_guard.note_entry() memakai symbol+name
            # sebagai family key. Tanpa ini entry_log menulis "sym:marvin|"
            # sementara cek memakai "sym:marvin|marvin the martian" →
            # rate-limit tidak pernah cocok (bug terverifikasi 2026-10-25).
            "name": (pair.get("baseToken") or {}).get("name", ""),
            "pair_address": pair["pairAddress"], "chain_slug": pair["chainId"],
            "pool_type": ptype, "fee": fee,
            "entry_price_usd": buy_usd / got,
            "tokens_total": got, "tokens_left": got,
            "eth_spent": eth_out_real if eth_out_real > 0 else eth_in,
            "usd_spent": buy_usd,
            "honeypot": 1 if honeypot else 0,
            "source_name": source_name,
            "buy_tx": buy_tx,
        }

    def _can_sell(self, ca, ptype, fee, amount_raw):
        try:
            if ptype == "v4":
                return True
            fn = self.router3.functions.exactInputSingle((
                Web3.to_checksum_address(ca), self.weth_addr, fee,
                self.addr, amount_raw, 0, 0))
            fn.call({"from": self.addr})
            return True
        except Exception:
            return False

    # ---------- quote BUY on-chain (paper-trading entry akurat) ----------
    def quote_buy_tokens(self, ca, ptype, fee, wei_in, min_out=0, pool_id=None):
        """Simulasi swap beli on-chain SEKARANG (eth_call / Quoter, TANPA tx).

        Return (ok, amount_out_tokens_raw, err_reason):
          ok=True  → amount_out = jumlah token RAW (wei/micro-unit) yang
                     BAKAL didapat di chain asli pada detik ini (masuk
                     kedalaman pool / price impact riil).
          ok=False → err_reason alasan revert on-chain yang jelas
                     (pool kosong, slippage, quoter error dst.).

        min_out = ambang keuntungan dari harga layar (expected_raw *
        (1 - buy_slippage)) — bila quote on-chain di bawahnya, transaksi
        BUY nyata PASTI revert (amountOutMinimum) → ok=False
        ("slippage_exceeded_onchain").

        Dipakai mode latihan (paper trading): harga masuk dari QUOTE
        aktual, bukan harga layar Dexscreener yang cached 30-60s.
        """
        ca = Web3.to_checksum_address(ca)
        min_out = int(min_out or 0)
        if ptype == "v4":
            if not pool_id:
                return False, 0, "v4_quoter_error:pool_id kosong"
            from . import v4 as v4mod
            liq = v4mod.liquidity(pool_id, self.w3)
            if liq is not None and liq <= 0:
                return False, 0, "v4_quoter_not_enough_liquidity:pool kosong"
            pk = v4mod.pool_key(pool_id, self.w3)
            if pk is None:
                return False, 0, "v4_quoter_error:poolkey_notfound"
            try:
                res = v4mod.quote_buy(pk, int(wei_in), self.w3)
            except Exception as e:
                return False, 0, f"v4_quoter_error:{e}"
            if not res.ok:
                return False, 0, f"v4_quoter_{res.kind}:{res.error}"
            # slippage check perlu harga anchor — loop balik ke pemanggil
            # lewat min_out buat v4: quoter v4 gak punya "expected" layar;
            # guard slippage justru di jalur NotEnoughLiquidity/revert di
            # atas. Kalau amount_out 0, tetap tolak.
            if res.amount_out <= 0:
                return False, res.amount_out, "v4_quoter_zero_output"
            return (False, res.amount_out, "slippage_exceeded_onchain") \
                if (min_out and res.amount_out < int(min_out)) \
                else (True, res.amount_out, "")
        elif ptype == "v2":
            return False, 0, "v2_pool_disabled"
        else:  # v3
            try:
                out = self.router3.functions.exactInputSingle((
                    self.weth_addr, ca, int(fee), self.addr,
                    int(wei_in), 1, 0)).call({"from": self.addr})
            except Exception as e:
                msg = str(e)
                if "NotEnoughLiquidity" in msg or "liquidity" in msg.lower():
                    return False, 0, f"v3_quoter_not_enough_liquidity:{msg}"
                return False, 0, f"v3_quoter_error:{msg}"
            if out <= 0:
                return False, int(out), "v3_quoter_zero_output"
            return (False, int(out), "slippage_exceeded_onchain") \
                if (min_out and out < int(min_out)) else (True, int(out), "")

    # ---------- quote (verifikasi TP anti-wick) ----------
    def quote_sell_eth(self, ca, ptype, fee, amount_raw, pool_id=None) -> float:
        """Berapa ETH BENERAN yang bakal didapat kalau jual amount_raw sekarang.
        v3: static-call exactInputSingle (butuh allowance — sudah di-approve saat buy).
        v2: getAmountsOut (pure view).
        v4: V4Quoter.quoteExactInputSingle (arah sell: token→ETH).
        Ini quote nembus depth pool, bukan harga spot layar."""
        ca = Web3.to_checksum_address(ca)
        if ptype == "v2":
            raise RuntimeError("quote v2 dinonaktifkan (mode v4-only)")
        if ptype == "v4":
            from . import v4 as v4mod
            if not pool_id:
                return 0.0
            liq = v4mod.liquidity(pool_id, self.w3)
            if liq is not None and liq <= 0:
                return 0.0
            pk = v4mod.pool_key(pool_id, self.w3)
            if pk is None:
                return 0.0
            res = v4mod.quote_sell(pk, int(amount_raw), self.w3)
            if res.ok and res.amount_out > 0:
                return int(res.amount_out) / 1e18
            return 0.0
        out = self.router3.functions.exactInputSingle((
            ca, self.weth_addr, fee, self.addr, amount_raw, 0, 0)).call({"from": self.addr})
        return out / 1e18

    def exec_pnl(self, pos, fraction) -> float:
        """PnL EKSEKUTABEL: quote hasil jual fraction posisi vs porsi modal ETH-nya.
        Dipakai monitor buat verifikasi TP — biar wick tanpa depth nggak
        men-trigger 'profit' palsu."""
        token = self.erc20(pos["ca"])
        dec = token.functions.decimals().call()
        amount_raw = int(pos["tokens_left"] * fraction * 10**dec)
        if amount_raw <= 0:
            return -1.0
        eth_quote = self.quote_sell_eth(pos["ca"], pos["pool_type"], pos["fee"],
                                        amount_raw, pool_id=pos.get("pair_address"))
        cost = pos["eth_spent"] * ((pos["tokens_left"] * fraction) / pos["tokens_total"]) \
            if pos["tokens_total"] else 0
        return (eth_quote / cost - 1) if cost > 0 else -1.0

    # ---------- sell ----------
    def sell(self, pos, fraction, cur_price_native):
        """Jual `fraction` dari tokens_left.

        Return (tokens_sold, eth_realized, gas_eth):
        eth_realized = ETH ASLI yang masuk wallet (balance delta, bukan estimasi
        dari harga spot) — supaya PnL yang dilaporkan jujur walau price impact
        besar di pool tipis. gas_eth = total gas yang kepakai selama proses sell.
        """
        ca = Web3.to_checksum_address(pos["ca"])
        token = self.erc20(ca)
        dec = token.functions.decimals().call()
        bal_raw = token.functions.balanceOf(self.addr).call()
        want_raw = int(pos["tokens_left"] * fraction * 10**dec)
        amount_raw = min(bal_raw, want_raw)
        if amount_raw <= 0:
            return 0.0, 0.0, 0.0

        slippage = db.get("sell_slippage")
        expected_eth = (amount_raw / 10**dec) * cur_price_native
        min_out = int(self.w3.to_wei(expected_eth * (1 - slippage), "ether"))

        eth_before = self.w3.eth.get_balance(self.addr)
        weth_before = self.weth.functions.balanceOf(self.addr).call()
        gas_wei = 0
        swap_tx = None

        def send(fn, value=0, is_swap=False):
            nonlocal gas_wei, swap_tx
            rcpt = self._send(fn, value=value)
            gas_wei += rcpt.gasUsed * rcpt.effectiveGasPrice
            if is_swap:
                swap_tx = _txh(rcpt)
            return rcpt

        if pos["pool_type"] == "v2":
            raise RuntimeError("sell posisi v2 dinonaktifkan (mode v4-only)")
        if pos["pool_type"] == "v4":
            # ---------- v4 sell: jalur UniversalRouter ----------
            # BUTUH PERMIT2 dua lapis (lihat sniper.v4.ensure_v4_sell_approval):
            #   1. token.approve(PERMIT2, amount)   — ERC20 approve biasa
            #   2. Permit2.approve(token, UNIVERSAL_ROUTER, amount, expiration)
            # Dua-duanya WAJIB; kalau hanya langkah 1, swap revert.
            from . import v4 as v4mod
            pk = v4mod.pool_key(pos["pair_address"], self.w3)
            if pk is None:
                raise RuntimeError("v4_poolkey_notfound saat sell")
            for _tx in v4mod.ensure_v4_sell_approval(
                    ca, self.w3, amount_raw, int(time.time()) + 180):
                self._send(_tx["fn"])
            def mk_v4(mo):
                commands, inputs, value = v4mod.encode_sell(pk, amount_raw, mo)
                return self.w3.eth.contract(
                    Web3.to_checksum_address(v4mod.UNIVERSAL_ROUTER),
                    abi=v4mod.UNIVERSAL_ROUTER_ABI
                ).functions.execute(commands, inputs, int(time.time()) + 120)
            try:
                send(mk_v4(min_out), is_swap=True)
            except Exception:
                send(mk_v4(1), is_swap=True)  # panic exit
        else:
            self._ensure_approval(ca, self.router3.address)
            def mk(mo):
                return self.router3.functions.exactInputSingle((
                    ca, self.weth_addr, pos["fee"], self.addr, amount_raw, mo, 0))
            try:
                send(mk(min_out), is_swap=True)
            except Exception:
                send(mk(1), is_swap=True)
            wbal = self.weth.functions.balanceOf(self.addr).call()
            if wbal > weth_before:
                send(self.weth.functions.withdraw(wbal - weth_before))

        eth_after = self.w3.eth.get_balance(self.addr)
        # realized = delta saldo + gas yang kepotong selama sell (gas dilaporkan terpisah)
        eth_realized = max((eth_after - eth_before + gas_wei) / 1e18, 0.0)
        return amount_raw / 10**dec, eth_realized, gas_wei / 1e18, swap_tx
