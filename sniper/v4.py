"""Uniswap v4 di Robinhood Chain — support layer untuk Hoodsniper v16.2.

Semua alamat KANONIK sudah diverifikasi on-chain oleh Atlas. JANGAN
mengganti ke alamat lain tanpa verifikasi ulang:

  POOL_MANAGER      0x8366a39CC670B4001A1121B8F6A443A643e40951
                    (emits Initialize(bytes32 id, address c0, address c1, ...))
  UNIVERSAL_ROUTER  0x8876789976decbfcbbbe364623c63652db8c0904
                    (verified, RouterParameters cocok dengan semua di atas;
                     46% dari seluruh swap v4 di chain ini lewat sini)
  STATE_VIEW        0x0284Cb0bcbaa8B87A8AA409D0e41afA7a76355F2
                    (verified, constructor _poolManager = PoolManager kita)
  V4_QUOTER         0xdF8B9bF82CDb73d8f5A84f58233E3591b3a21867
                    (verified, constructor _poolManager = PoolManager kita)
  PERMIT2           0x000000000022D473030F116dDEE9F6B43aC78BA3 — akses token
                    (sell) harus lewat Permit2, lalu token diberi allowance
                    ke PERMIT2 dan router memberi allowOwner.
  WETH9             0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73

Key facts:
  - "pair_address" pool v4 di Dexscreener adalah PoolId (32-byte hex, 66
    karakter), BUKAN alamat kontrak. Satu-satunya cara memulihkan PoolKey
    adalah dari event Initialize PoolManager.
  - StateView.getLiquidity(poolId) = true liquidity on-chain; Dexscreener
    sering null untuk pool v4.
  - Quoter revert custom error bukan data: QuoteSwap(uint256) = SUKSES,
    NotEnoughLiquidity(bytes32) = tolak, UnexpectedRevertBytes = payload.

Semua fungsi menerima objek `w3` yang sudah ada (buat koneksi SEKALI di
caller — Trader/scanner sudah punya). Tidak ada koneksi baru per panggilan.
"""
import time

from hexbytes import HexBytes
from web3 import Web3

from .hexutil import topic_addr

# ===================== Alamat kanonik (verified on-chain, JANGAN diubah) ====
POOL_MANAGER = "0x8366a39CC670B4001A1121B8F6A443A643e40951"
UNIVERSAL_ROUTER = "0x8876789976dEcBfCbBbe364623C63652db8C0904"
STATE_VIEW = "0x0284Cb0bcbaa8B87A8AA409D0e41afA7a76355F2"
V4_QUOTER = "0xdF8B9bF82CDb73d8f5A84f58233E3591b3a21867"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
WETH9 = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"

# ===================== Selector & topic (dihitung & diverifikasi by Atlas) ==
INITIALIZE_TOPIC0 = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"

# Custom error selectors (dari bukti probe langsung ke V4Quoter):
ERR_QUOTE_SWAP = "0xecbd9804"           # QuoteSwap(uint256) = SUKSES
ERR_NOT_ENOUGH_LIQUIDITY = "0x7a5ed734" # NotEnoughLiquidity(bytes32) = tolak
ERR_UNEXPECTED_REVERT = "0x6190b2b0"    # UnexpectedRevertBytes(bytes)

# ===================== PoolKey cache (SQLite) ==============================
# Diperlukan karena PoolId tidak bisa di-decode balik ke PoolKey secara
# matematis — PoolId = keccak(abi.encode(PoolKey)). Kolom disimpan
# huruf-kecil biar bisa lookup langsung pakai pool_id dari pair_address
# Dexscreener.
V4_POOLKEY_TABLE = """CREATE TABLE IF NOT EXISTS v4_pool_keys(
    pool_id       TEXT PRIMARY KEY,
    currency0     TEXT,
    currency1     TEXT,
    fee           INTEGER,
    tick_spacing  INTEGER,
    hooks         TEXT,
    block         INTEGER,
    ts            INTEGER)"""


def _dbconn():
    """Koneksi SQLite role-only untuk cache PoolKey (dipisah dari db.py supaya
    modul ini bisa di-test dengan DB_PATH apapun tanpa menarik semua tabel bot)."""
    try:
        from . import config
        path = config.DB_PATH
    except Exception:
        path = "hoodsniper.db"
    import sqlite3
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(V4_POOLKEY_TABLE)
    return con


def _cache_put(w3, pool_id: str, pk: "PoolKey", block: int):
    with _dbconn() as c:
        c.execute(
            "INSERT OR IGNORE INTO v4_pool_keys"
            "(pool_id,currency0,currency1,fee,tick_spacing,hooks,block,ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (str(pool_id).lower(),
             str(pk.currency0).lower(),
             str(pk.currency1).lower(),
             int(pk.fee), int(pk.tickSpacing),
             str(pk.hooks).lower(), int(block),
             int(time.time())))


def _cache_get(pool_id: str) -> "PoolKey | None":
    try:
        pool_id = str(pool_id).lower()
    except Exception:
        return None
    if not pool_id:
        return None
    try:
        with _dbconn() as c:
            row = c.execute(
                "SELECT * FROM v4_pool_keys WHERE pool_id=?", (pool_id,)
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return PoolKey(
        currency0=row["currency0"], currency1=row["currency1"],
        fee=row["fee"], tickSpacing=row["tick_spacing"], hooks=row["hooks"])


ZERO_ADDRESS_F = "0x0000000000000000000000000000000000000000"


class PoolKey:
    """PoolKey Uniswap v4 (serasi dengan struct Solidity PoolKey).
    currency0/currency1: ETH native direpresentasikan 0x0 (20 nol).
    """
    __slots__ = ("currency0", "currency1", "fee", "tickSpacing", "hooks")

    def __init__(self, currency0, currency1, fee, tickSpacing, hooks):
        # D7: fallback HARUS alamat 20-byte penuh — "0x00" bukan alamat
        # valid dan membuat to_checksum_address raise.
        self.currency0 = Web3.to_checksum_address(
            currency0 or ZERO_ADDRESS_F)
        self.currency1 = Web3.to_checksum_address(
            currency1 or ZERO_ADDRESS_F)
        self.fee = int(fee)
        self.tickSpacing = int(tickSpacing)
        self.hooks = Web3.to_checksum_address(hooks or ZERO_ADDRESS_F)

    def __repr__(self):
        return (f"PoolKey(c0={self.currency0}, c1={self.currency1}, "
                f"fee={self.fee}, ts={self.tickSpacing}, hooks={self.hooks})")

    # alias gampang dibaca di log
    @property
    def as_tuple(self):
        return (self.currency0, self.currency1, self.fee, self.tickSpacing, self.hooks)


# ===================== pool_key: dari event Initialize =====================
SEARCH_STEP_BLOCKS = 5000
SEARCH_MAX_STEPS = 40       # total mundur max 200.000 blok


def _is_pool_id(s): 
    """True bila `s` terlihat seperti PoolId v4 (32-byte hex), bukan alamat kontrak."""
    s = str(s).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    return len(s) == 64 and all(ch in "0123456789abcdef" for ch in s)


def _to_params(log):
    """Dari log Initialize di PoolManager → PoolKey tuple
    (currency0, currency1, fee, tickSpacing, hooks, block, ts).
    Event ABI:
      Initialize(bytes32 indexed id, address indexed currency0,
                 address indexed currency1, uint24 fee, int24 tickSpacing,
                 address hooks, uint160 sqrtPriceX96, int24 tick)
    Nothing indexed selain id/c0/c1 → sisanya dibongkar dari `data`.
    """
    topics = log.get("topics")
    if not topics or len(topics) < 4:
        return None
    # kandidat alamat tidak valid untuk v4
    for t in topics[1:]:
        try:
            h = t.hex() if hasattr(t, "hex") else str(t)
        except Exception:
            return None
    c0 = topic_addr(topics[2])
    c1 = topic_addr(topics[3])
    data = log.get("data") or "0x" + "0" * 64 * 5
    try:
        from eth_abi import decode
        raw = data if not hasattr(data, "hex") else "0x" + data.hex().removeprefix("0x")
        # data = fee(uint24 padding ke uint256 dulu? tidak — layout yang
        # benar di v4: fee(uint24), tickSpacing(int24), hooks(address),
        # sqrtPriceX96(uint160), tick(int24) — tapi pad ke 32 byte masing2)
        (fee, tickSpacing, hooks, _sqrt, _tick) = decode(
            ["uint24", "int24", "address", "uint160", "int24"], HexBytes(raw))
    except Exception:
        return None
    return _PoolKeyWithBlock(c0, c1, fee, tickSpacing, hooks,
                             int(log.get("blockNumber") or 0))


class _PoolKeyWithBlock:
    __slots__ = ("currency0", "currency1", "fee", "tickSpacing", "hooks", "block")

    def __init__(self, currency0, currency1, fee, tickSpacing, hooks, block):
        self.currency0 = currency0
        self.currency1 = currency1
        self.fee = fee
        self.tickSpacing = tickSpacing
        self.hooks = hooks
        self.block = block


def pool_key(pool_id, w3):
    """Temukan PoolKey sebuah PoolId v4, dari event Initialize PoolManager.

    pools di chain ini memang banyak; pencarian mundur berlapis dan
    hasilnya di-cache ke SQLite `v4_pool_keys` supaya siklus selanjutnya
    tidak jaringan lagi.

    - pool_id BUKAN PoolId (bukan 64-hex) → None (pemanggil wajib skip).
    - Ketemu → PoolKey (kenti ter-cache).
    - Tidak ketemu dalam SEARCH_MAX_STEPS langkah (200k blok) → None.
    - w3 None atau error jaringan → None (TIDAK crash).
    """
    if w3 is None or pool_id is None:
        return None
    pool_id = str(pool_id).strip().lower()
    if not _is_pool_id(pool_id):
        return None

    cached = _cache_get(pool_id)
    if cached is not None:
        return cached

    try:
        latest = int(w3.eth.block_number)
    except Exception:
        return None

    topic = "0x" + pool_id.lower().removeprefix("0x")
    pm = Web3.to_checksum_address(POOL_MANAGER)
    from_block = latest
    for step in range(SEARCH_MAX_STEPS):
        end = from_block
        from_block = max(0, end - SEARCH_STEP_BLOCKS + 1)
        try:
            logs = w3.eth.get_logs({
                "address": pm,
                "topics": [INITIALIZE_TOPIC0, "0x" + topic.removeprefix("0x").zfill(64)],
                "fromBlock": from_block,
                "toBlock": end,
            })
        except Exception:
            return None
        for log in logs:
            try:
                pk = _to_params(log)
            except Exception:
                pk = None
            if pk is None:
                continue
            out = PoolKey(pk.currency0, pk.currency1, pk.fee, pk.tickSpacing, pk.hooks)
            try:
                _cache_put(w3, pool_id, out, pk.block)
            except Exception:
                pass  # cache gagal → kembalikan saja nilainya
            return out
        if from_block == 0:
            return None  # sudah sampai genesis
    return None


# ===================== State read helpers ==================================
STATE_VIEW_ABI = [
    {"name": "getLiquidity", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "poolId", "type": "bytes32"}],
     "outputs": [{"name": "liquidity", "type": "uint128"}]},
    {"name": "getSlot0", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "poolId", "type": "bytes32"}],
     "outputs": [{"name": "sqrtPriceX96", "type": "uint160"},
                 {"name": "tick", "type": "int24"},
                 {"name": "protocolFee", "type": "uint24"},
                 {"name": "lpFee", "type": "uint24"}]},
]


def _pool_id_bytes32(pool_id):
    """Diterima: '0x...' 64-hex, juga tanpa 0x. Return HexBytes 32-byte.
    Bukan PoolId → raise → pemanggil harus tahu. dipakai internal only."""
    s = str(pool_id).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 64:
        raise ValueError(f"bukan PoolId v4 (harus 32-byte hex): {pool_id!r}")
    return HexBytes(bytes.fromhex(s))


def liquidity(pool_id, w3):
    """StateView.getLiquidity(poolId) → int (uint128), atau None kalau
    w3 hilang / pool_id salah / RPC error. 0 = pool kosong (kasus grok)."""
    if w3 is None or pool_id is None:
        return None
    try:
        pv = w3.eth.contract(
            Web3.to_checksum_address(STATE_VIEW), abi=STATE_VIEW_ABI)
        return int(pv.functions.getLiquidity(
            _pool_id_bytes32(pool_id)).call())
    except Exception:
        return None


# ===================== V4Quoter quote ======================================
V4_QUOTER_ABI = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "poolKey", "type": "tuple", "components": [
             {"name": "currency0", "type": "address"},
             {"name": "currency1", "type": "address"},
             {"name": "fee", "type": "uint24"},
             {"name": "tickSpacing", "type": "int24"},
             {"name": "hooks", "type": "address"}]},
         {"name": "zeroForOne", "type": "bool"},
         {"name": "exactAmount", "type": "uint128"},
         {"name": "hookData", "type": "bytes"}]}],
     "outputs": [{"name": "amountOut", "type": "uint256"},
                 {"name": "gasEstimate", "type": "int256"}]},
]


class QuoteResult:
    __slots__ = ("ok", "amount_out", "error", "kind")
    """kind: "ok" | "not_enough_liquidity" | "bad_revert" | "error" """

    def __init__(self, ok, amount_out=0, error="", kind="error"):
        self.ok = ok
        self.amount_out = amount_out
        self.error = error
        self.kind = kind


def _decode_error(revert_bytes):
    """Revert V4Quoter → (kind, detail). Pemanggil QuoteSwap(ok) di-decode
    terpisah."""
    sel = revert_bytes[:4].hex()
    if sel == ERR_NOT_ENOUGH_LIQUIDITY[2:]:
        return "not_enough_liquidity", "NotEnoughLiquidity(bytes32)"
    if sel == ERR_QUOTE_SWAP[2:]:
        return "quote_swap", "QuoteSwap(uint256)"
    if sel == ERR_UNEXPECTED_REVERT[2:]:
        # UnexpectedRevertBytes(bytes) — bongkar payload asli di dalamnya
        inner = ""
        try:
            from eth_abi import decode as abidecode
            (payload,) = abidecode(["bytes"], HexBytes(revert_bytes[4:]))
            if hasattr(payload, "hex"):
                inner = payload.hex()
            else:
                inner = str(payload)
        except Exception:
            inner = revert_bytes[4:].hex()
        inner_clean = inner.removeprefix("0x").lower()
        if inner_clean.startswith(ERR_NOT_ENOUGH_LIQUIDITY[2:].lower()):
            return "not_enough_liquidity", "NotEnoughLiquidity(bytes32)"
        return "bad_revert", inner or "UnexpectedRevertBytes"
    return "bad_revert", revert_bytes[:4].hex()


def _quote_single(pool_key_tuple, zero_for_one: bool, amount_in: int, w3):
    """Inti V4Quoter.quoteExactInputSingle untuk buy (zero_for_one=True)
    maupun sell (zero_for_one=False)."""
    if w3 is None:
        return QuoteResult(False, error="w3 None", kind="error")
    try:
        pk = pool_key_tuple if isinstance(pool_key_tuple, PoolKey) \
            else PoolKey(*(_tuple5(pool_key_tuple)))
    except Exception as e:
        return QuoteResult(False, error=f"pool_key invalid: {e}", kind="error")
    try:
        q = w3.eth.contract(Web3.to_checksum_address(V4_QUOTER), abi=V4_QUOTER_ABI)
        params = (
            (pk.currency0, pk.currency1, pk.fee, pk.tickSpacing, pk.hooks),
            bool(zero_for_one), int(amount_in), b"")
        out = q.functions.quoteExactInputSingle(params).call()
        return QuoteResult(True, int(out[0]), kind="ok")
    except Exception as e:
        # web3.py: ContractLogicError punya .data (revert payload)
        data = getattr(e, "data", None)
        rb = None
        if isinstance(data, str):
            rb = HexBytes(data)
        elif isinstance(data, dict):
            inner = data.get("data") or data.get("message")
            if isinstance(inner, str):
                if inner.startswith("0x"):
                    rb = HexBytes(inner)
                else:
                    # beberapa node mengembalikan pesan "+ hex"
                    import re as _re
                    m = _re.search(r"0x[0-9a-fA-F]+", str(inner))
                    rb = HexBytes(m.group(0)) if m else None
        elif isinstance(data, (bytes, bytearray)):
            rb = HexBytes(data)
        if rb is None:
            sel = getattr(e, "message", "") or str(e)
            import re as _re
            m = _re.search(r"0x[0-9a-fA-F]{8,}", str(sel))
            rb = HexBytes(m.group(0)) if m else None
        if rb is not None and len(rb) >= 4:
            kind, detail = _decode_error(rb)
            if kind == "quote_swap":
                # QuoteSwap(uint256) = SUKSES — baca amountOut
                try:
                    from eth_abi import decode as abidecode
                    (amount_out,) = abidecode(["uint256"], rb[4:36])
                    return QuoteResult(True, int(amount_out), kind="ok")
                except Exception:
                    return QuoteResult(False, error="QuoteSwap undecodable",
                                       kind="bad_revert")
            return QuoteResult(False,
                               error=detail if kind != "bad_revert" else detail,
                               kind=kind)
        return QuoteResult(False, error=str(e), kind="error")


def quote_buy(pool_key_tuple, amount_in_wei, w3):
    """Quoter V4Quoter.quoteExactInputSingle beli ETH→token (zeroForOne=True,
    currency0 = sentinel alamat nol = ETH native)."""
    return _quote_single(pool_key_tuple, True, amount_in_wei, w3)


def quote_sell(pool_key_tuple, amount_in_tokens_raw, w3):
    """Quoter V4Quoter.quoteExactInputSingle jual token→ETH (zeroForOne=False,
    currency1 = token → currency0 = ETH native)."""
    return _quote_single(pool_key_tuple, False, amount_in_tokens_raw, w3)


def _tuple5(t):
    if len(t) != 5:
        raise ValueError("PoolKey tuple harus 5 elemen")
    return t


# ===================== UniversalRouter encode ==============================
# command byte V4_SWAP = 0x10
CMD_V4_SWAP = b"\x10"
# action opcode (VERIFIED on-chain Robinhood Chain: tx
# 0xf9b2e4e8b978c24b0c0a9738dd5e2b5692f5901ba4a33f434279674992f100e6 —
# commands=0x10, inputs[0] decode(["bytes","bytes[]"]) → actions=0x060c0f)
ACT_SWAP_EXACT_IN_SINGLE = 0x06
ACT_SETTLE_ALL = 0x0c
ACT_TAKE_ALL = 0x0f

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _as_poolkey(pk):
    if isinstance(pk, PoolKey):
        return pk
    return PoolKey(*_tuple5(pk))


def _swap_direction_for_buy(pk):
    """D5: arah swap ditentukan dari posisi token di PoolKey, bukan hardcoded.
    Buy (ETH → token) hanya bisa langsung bila token = currency1 dan
    currency0 = ETH native (0x0). Return True (zeroForOne) atau None."""
    if pk.currency0 == ZERO_ADDRESS:
        return True   # ETH(0) → token: zeroForOne=True
    return None       # token di currency0 → butuh wrap WETH, tak didukung


def encode_buy(pool_key, amount_in, min_out, deadline=0, hook_data=b""):
    """Encode perintah UniversalRouter 'buy exact ETH → token' tanpa transaksi.

    FORMAT (diverifikasi dari tx v4 nyata on-chain):
      commands = b"\\x10" (V4_SWAP)
      inputs[0] = abi.encode(["bytes","bytes[]"], [actions, params])
        actions = 3 byte OPSDI SAJA: 0x06 0x0c 0x0f
        params  = [abi.encode(ExactInputSingleParams),
                   abi.encode(SETTLE_ALL=(currency,uint256)),
                   abi.encode(TAKE_ALL=(currency,uint256))]
      value = amount_in (buy pakai ETH native)

    Return (commands, inputs, value). Value selalu int (wei).
    """
    pk = _as_poolkey(pool_key)
    zf = _swap_direction_for_buy(pk)
    if zf is None:
        # D5: token di sisi currency0 — buy ETH native langsung tidak bisa
        # (ETH harus di currency0). Jangan diam-diam hasilkan tx salah arah.
        raise ValueError(
            'kind="unsupported_native_side": token di currency0 '
            f"({pk.currency0}); buy ETH native butuh token di currency1 "
            "(currency0 = 0x0)")
    exact_in = (
        (pk.currency0, pk.currency1, pk.fee, pk.tickSpacing, pk.hooks),
        True,                     # zeroForOne: ETH(0) → token
        int(amount_in),
        int(min_out),
        bytes(hook_data),
    )
    actions = bytes([ACT_SWAP_EXACT_IN_SINGLE, ACT_SETTLE_ALL, ACT_TAKE_ALL])
    params = [
        eth_abi_encode_exact_in(exact_in),
        eth_abi_encode((ZERO_ADDRESS, int(amount_in))),                   # SETTLE_ALL: ETH maxAmount
        eth_abi_encode((pk.currency1, int(min_out))),                    # TAKE_ALL: token minAmount
    ]
    inputs = [_encode_v4_input(actions, params)]
    return CMD_V4_SWAP, inputs, int(amount_in)


def encode_sell(pool_key, amount_in, min_out, deadline=0, hook_data=b""):
    """Encode perintah UniversalRouter 'sell exact token → ETH' tanpa transaksi.

    Arah zeroForOne=False (token → ETH): SETTLE_ALL memasukkan token,
    TAKE_ALL menerima ETH. Aset token harus di-approve dua lapis:
      1. token.approve(PERMIT2, amount) — ERC20 approve biasa
      2. Permit2.approve(token, UNIVERSAL_ROUTER, amount, expiration)
    Lihat ensure_v4_sell_approval(). DOKUMENTASI saja — JANGAN dieksekusi
    dalam test / mode latihan.
    """
    pk = _as_poolkey(pool_key)
    if pk.currency1 == ZERO_ADDRESS:
        raise ValueError("encode_sell mengasumsikan token berada di currency1; "
                         f"didapat currency1={pk.currency1}")
    if pk.currency0 != ZERO_ADDRESS:
        # D6: guard — sell ke ETH native hanya valid bila ETH = currency0.
        raise ValueError("encode_sell menolak pool yang ETH tidak di currency0; "
                         f"didapat currency0={pk.currency0}")
    exact_in = (
        (pk.currency0, pk.currency1, pk.fee, pk.tickSpacing, pk.hooks),
        False,                    # zeroForOne=False: token → ETH
        int(amount_in),
        int(min_out),
        bytes(hook_data),
    )
    actions = bytes([ACT_SWAP_EXACT_IN_SINGLE, ACT_SETTLE_ALL, ACT_TAKE_ALL])
    params = [
        eth_abi_encode_exact_in(exact_in),
        eth_abi_encode((pk.currency1, int(amount_in))),                 # SETTLE_ALL: token masuk
        eth_abi_encode((ZERO_ADDRESS, int(min_out))),                   # TAKE_ALL: ETH keluar
    ]
    inputs = [_encode_v4_input(actions, params)]
    return CMD_V4_SWAP, inputs, 0


def _encode_v4_input(actions: bytes, params: list) -> bytes:
    """inputs[0] = abi.encode(["bytes","bytes[]"], [actions, params]).
    VERIFIED on-chain (bukan dokumentasi): 864 byte utk buy 0.0108 ETH.
    """
    from eth_abi import encode
    return encode(["bytes", "bytes[]"], [bytes(actions), [bytes(p) for p in params]])


def eth_abi_encode(vals, types=None):
    """Shorthand encode dengan tipe implisit dari standar v4."""
    from eth_abi import encode
    if types is None:
        types = []
        for v in vals:
            if isinstance(v, str):
                types.append("address")
            elif isinstance(v, bool):
                types.append("bool")
            elif isinstance(v, bytes):
                types.append("bytes")
            else:
                types.append("uint256")
    return encode(types, vals)


def eth_abi_encode_exact_in(exact_in):
    """ExactInputSingleParams = (PoolKey, bool zeroForOne, uint128 amountIn,
    uint128 minOut, bytes hookData)."""
    pk = exact_in[0]
    from eth_abi import encode as ae
    return ae(["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
              [((
        pk[0], pk[1], pk[2], pk[3], pk[4]
    ), bool(exact_in[1]), int(exact_in[2]), int(exact_in[3]), exact_in[4])])


# universal router contract ABI (buat trader.py)
UNIVERSAL_ROUTER_ABI = [
    {"name": "execute", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "commands", "type": "bytes"},
                {"name": "inputs", "type": "bytes[]"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": []},
]

# Permit2.approve(token, spender, amount(uint160), expiration(uint48))
# selector 0x87517c45 (D3: allowOwner TIDAK ADA di Permit2)
PERMIT2_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "token", "type": "address"},
                {"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint160"},
                {"name": "expiration", "type": "uint48"}], "outputs": []},
]


def ensure_v4_sell_approval(token_addr, w3, amount_raw, deadline):
    """D4: daftar transaksi (urut) untuk approval sell v4 — TIDAK dikirim di
    sini (pemanggil/trader yang kirim). UniversalRouter menarik token lewat
    Permit2, jadi dua langkah ini WAJIB berdua:
      1. token.approve(PERMIT2, amount_raw)          — ERC20 approve biasa
      2. Permit2.approve(token, UNIVERSAL_ROUTER, amount, expiration)
    Kalau hanya langkah 1, swap revert (router tarik via Permit2).
    Return list of call-descriptor dict; cukup dipakai dgn w3.eth.contract.
    """
    amount_raw = int(amount_raw)
    expiration = int(deadline) if deadline and int(deadline) > 0 else 0
    token_addr = Web3.to_checksum_address(token_addr)
    router = Web3.to_checksum_address(UNIVERSAL_ROUTER)
    permit2 = Web3.to_checksum_address(PERMIT2)
    token = w3.eth.contract(token_addr, abi=[
        {"name": "approve", "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "spender", "type": "address"},
                    {"name": "amount", "type": "uint256"}],
         "outputs": [{"name": "", "type": "bool"}]}])
    p2 = w3.eth.contract(permit2, abi=PERMIT2_ABI)
    return [
        {"step": 1, "desc": "token.approve(PERMIT2, amount)",
         "kind": "erc20_approve", "token": token_addr, "spender": permit2,
         "fn": token.functions.approve(permit2, amount_raw), "value": 0},
        {"step": 2,
         "desc": "Permit2.approve(token, UNIVERSAL_ROUTER, amount, expiration)",
         "kind": "permit2_approve", "token": token_addr, "spender": router,
         "fn": p2.functions.approve(token_addr, router, amount_raw, expiration),
         "value": 0},
    ]


