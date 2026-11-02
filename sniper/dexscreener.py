"""Dexscreener API helper (sync, panggil via asyncio.to_thread)."""
import requests
from . import config

BASE = "https://api.dexscreener.com/latest/dex"
PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
_S = requests.Session()
_S.headers.update({"User-Agent": "hoodsniper/1.0"})


def profile_candidates() -> list:
    """CA token Robinhood Chain dari profil & boost terbaru Dexscreener.

    Ini SAMPEL pasar (token yang baru update profil / lagi di-boost), bukan
    firehose semua coin. Error jaringan apa pun → kembalikan yang sempat
    terkumpul, tidak raise.
    """
    chain = (config.DEX_CHAIN_MATCH or "robinhood").lower()
    out = []
    for url in (PROFILES_URL, BOOSTS_URL):
        try:
            r = _S.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[dexscreener] gagal ambil {url.split('/')[3]}: {e}")
            continue
        for row in (data if isinstance(data, list) else []):
            if not isinstance(row, dict):
                continue
            if (row.get("chainId") or "").lower() != chain:
                continue
            ca = (row.get("tokenAddress") or "").lower()
            if ca and ca not in out:
                out.append(ca)
    return out



def token_pairs(ca: str):
    """Semua pair untuk sebuah token CA (lintas chain)."""
    r = _S.get(f"{BASE}/tokens/{ca}", timeout=10)
    r.raise_for_status()
    return (r.json() or {}).get("pairs") or []


def best_pair(ca: str):
    """Pair v4 terbaik di Robinhood Chain: liquidity USD tertinggi (hanya Uniswap v4)."""
    pairs = [
        p for p in token_pairs(ca)
        if config.DEX_CHAIN_MATCH in (p.get("chainId") or "").lower()
        and (p.get("baseToken", {}).get("address", "").lower() == ca.lower())
        and pool_type(p) == "v4"
    ]
    if not pairs:
        return None
    pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
    return pairs[0]


def get_pair(chain_slug: str, pair_address: str):
    r = _S.get(f"{BASE}/pairs/{chain_slug}/{pair_address}", timeout=10)
    r.raise_for_status()
    d = r.json() or {}
    pairs = d.get("pairs") or ([d["pair"]] if d.get("pair") else [])
    return pairs[0] if pairs else None


def mcap_of(pair) -> float:
    return float(pair.get("marketCap") or pair.get("fdv") or 0)


def liq_usd(pair) -> float:
    return float((pair.get("liquidity") or {}).get("usd") or 0)


def eth_usd(pair) -> float:
    """Harga ETH (native) dalam USD, diturunkan dari priceUsd / priceNative."""
    pu = float(pair.get("priceUsd") or 0)
    pn = float(pair.get("priceNative") or 0)
    return pu / pn if pu and pn else 0.0


def pool_type(pair) -> str:
    """'v4' / 'v2' / 'v3' berdasarkan labels Dexscreener. Default v3.

    Urutan cek: v4 → v2 → v3. v4 dicek DULUAN karena banyak pool v4
    (pairAddress = PoolId 32-byte) di Dexscreener juga membawa label
    lain; sebelum v16.2 mereka salah dilabeli "v3" dan jalur eksekusi
    SwapRouter02-nya revert (70% sinyal).
    """
    labels = [l.lower() for l in (pair.get("labels") or [])]
    if "v4" in labels:
        return "v4"
    if "v2" in labels:
        return "v2"
    return "v3"


def liq_usd_or_none(pair):
    """Liquidity USD dari Dexscreener, atau None bila data liquidity null/hilang.

    Dexscreener mengembalikan liquidity:null untuk sebagian pool v4
    (pairAddress = PoolId, bukan alamat kontrak). liq_usd() lama meng-konversi
    itu jadi 0 — bikin rug monitor buta dan filter min-liq salah menolak.
    Pemanggil yang butuh tiga-keadaan (angka / benar-benar nol / data n/a)
    pakai yang ini; untuk pool v4 dengan None, likuiditas sebenarnya bisa
    dibaca on-chain lewat sniper.v4.liquidity(pool_id, w3) (StateView.getLiquidity).
    Perilaku liq_usd() TIDAK diubah.
    """
    liq = (pair.get("liquidity") or {}).get("usd")
    if liq is None:
        return None
    try:
        return float(liq)
    except (TypeError, ValueError):
        return None


# ======================= FILTER SOSMED =======================
# Data jejak sosmed diambil dari Dexscreener pair["info"]:
#   info.websites = [{"label","url"}]
#   info.socials  = [{"type","url"}]   type: twitter/x, telegram, discord, ...


def socials_of(pair) -> dict:
    """Ringkas jejak sosmed sebuah pair.

    Return dict: has_web, has_x, has_tg, has_discord, types(set), count(int),
    label(str ringkas buat notif).
    """
    info = pair.get("info") or {}
    webs = [w for w in (info.get("websites") or []) if w.get("url")]
    socs = [s for s in (info.get("socials") or []) if s.get("url")]
    types = {(s.get("type") or "").strip().lower() for s in socs}
    has_x = bool(types & {"twitter", "x"})
    has_tg = "telegram" in types
    has_discord = "discord" in types
    has_web = bool(webs) or ("website" in types)
    count = len(webs) + len(socs)
    parts = []
    if has_web: parts.append("web")
    if has_x: parts.append("X")
    if has_tg: parts.append("tg")
    if has_discord: parts.append("dc")
    return {
        "has_web": has_web, "has_x": has_x, "has_tg": has_tg,
        "has_discord": has_discord, "types": types, "count": count,
        "label": "·".join(parts) if parts else "kosong",
    }


def social_ok(pair) -> tuple[bool, str]:
    """Return (lolos, alasan). Dipanggil sebelum safety.check."""
    from . import db
    mode = db.get("social_filter", str)
    if mode == "off":
        return True, "social off"
    s = socials_of(pair)

    # info belum ke-index (token menit-0) → ikut kebijakan fail_closed
    if s["count"] == 0:
        if db.get("social_fail_closed", str) == "1":
            return False, "belum ada jejak sosmed (fail-closed)"
        return True, "⚠️ info sosmed kosong, lolos (fail-open)"

    if mode == "any":
        return True, f"sosmed: {s['label']}"
    if mode == "x":
        return (s["has_x"], f"punya X ({s['label']})" if s["has_x"] else "gak ada X/Twitter")
    if mode == "tg":
        return (s["has_tg"], f"punya TG ({s['label']})" if s["has_tg"] else "gak ada Telegram")
    if mode == "x_tg":
        ok = s["has_x"] and s["has_tg"]
        return (ok, f"X+TG ({s['label']})" if ok else f"kurang, butuh X & TG (ada: {s['label']})")
    if mode == "web":
        return (s["has_web"], f"punya web ({s['label']})" if s["has_web"] else "gak ada website")
    if mode == "count":
        need = int(db.get("social_min_count"))
        ok = s["count"] >= need
        return (ok, f"{s['count']} link sosmed (min {need}) — {s['label']}")
    return True, "mode tak dikenal → lolos"
