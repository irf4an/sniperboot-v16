#!/usr/bin/env python3
"""🕵️ tools/screen_insiders.py — Screening wallet insider (v16.1).

Cari "smart money" di Robinhood Chain: ambil koin yang sudah terbang
(gainers di Dexscreener atau dari daftar CA manual), baca pembeli-pembeli
awal mereka via Blockscout (robinhoodchain.blockscout.com), lalu cari
WALLET YANG TERBENTUR DI ≥2 KOIN PEMENANG — dia disimpan ke tabel
insider_wallets (db.sniper) untuk dipantau tracker on-chain.

Pemakaian:
  python tools/screen_insiders.py                     # auto: gainers Dexscreener
  python tools/screen_insiders.py 0xCa1 0xCa2 ...     # daftar CA manual
  python tools/screen_insiders.py --limit 3 --min-buy-eth 0.1
  python tools/screen_insiders.py --dry-run           # tampil saja, simpan tak

Ide: fase awal koin pemenang biasanya diisi wallet-wallet yang sama.
Wallet yang membeli 2+ koin pemenang di pembukaan kemungkinan besar
 insider yang benar — layak di-copy-trade.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from curl_cffi import requests as crequests  # noqa: E402
from sniper import config, db, geckoterminal as gt  # noqa: E402
from sniper import safety  # noqa: E402
from sniper import dexscreener as dx  # noqa: E402
from sniper.tracker import QUOTE_ASSETS  # noqa: E402

# ---------- HTTP ----------
BROWSER_IMPERSONATE = "chrome120"     # identitas TLS yang cukup fresh
BLOCKSCOUT = config.BLOCKSCOUT_API.rstrip("/")
DEX_TRENDS = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/{ca}"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=robinhood"

_session = crequests.Session(impersonate=BROWSER_IMPERSONATE)


def _get(url, *a, **kw):
    return _session.get(url, timeout=20, *a, **kw)


# ---------- 1. AMBIL KOIN YANG TERBANG ----------

DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_SEARCH_MULTI = "https://api.dexscreener.com/latest/dex/search?q={q}"


def _rh_cas_from_pairs(pairs: list) -> list[tuple[float, float, str]]:
    """CA Robinhood Chain dari daftar pair Dexscreener.
       Return list (volume24, priceChange24, ca)."""
    out = []
    for p in pairs or []:
        if (p.get("chainId") or "").lower() != config.DEX_CHAIN_MATCH.lower():
            continue
        ca = ((p.get("baseToken") or {}).get("address") or "").lower()
        if not ca.startswith("0x"):
            continue
        try:
            vol = float(((p.get("volume") or {}).get("h24")) or 0)
        except (TypeError, ValueError):
            vol = 0.0
        try:
            chg = float(((p.get("priceChange") or {}).get("h24")) or 0)
        except (TypeError, ValueError):
            chg = 0.0
        out.append((vol, chg, ca))
    return out


def _dexscreener_profiles_cas() -> list[str]:
    """CA dari token-profiles & token-boosts (filter chainId robinhood)."""
    cas = []
    for url in (DEX_PROFILES, DEX_BOOSTS):
        try:
            r = _get(url)
            items = r.json() or []
        except Exception as e:
            print(f"⚠️ Dexscreener {url.rsplit('/', 1)[-1]} gagal: {e}")
            continue
        for it in items if isinstance(items, list) else []:
            if (it.get("chainId") or "").lower() != config.DEX_CHAIN_MATCH.lower():
                continue
            ca = (it.get("tokenAddress") or "").lower()
            if ca.startswith("0x"):
                cas.append(ca)
    return cas


GT_EXCLUDED_CAS = {
    "0x0000000000000000000000000000000000000000",
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
}


def _geckoterminal_rh_cas() -> list[tuple[float, float, str]]:
    """CA Robinhood Chain dari trending pools & top pools GeckoTerminal.
    Return list (volume24, priceChange24, ca)."""
    out = []
    seen = set()
    pools_data = []
    try:
        pools_data.extend(gt.trending_pools() or [])
    except Exception as e:
        print(f"⚠️ GeckoTerminal trending_pools gagal: {e}")
    try:
        pools_data.extend(gt.pools(page=1) or [])
    except Exception as e:
        print(f"⚠️ GeckoTerminal pools gagal: {e}")

    for p in pools_data:
        b_id = (p.get("relationships", {}).get("base_token", {}).get("data", {}).get("id") or "").replace("robinhood_", "").lower()
        q_id = (p.get("relationships", {}).get("quote_token", {}).get("data", {}).get("id") or "").replace("robinhood_", "").lower()
        ca = None
        if b_id.startswith("0x") and b_id not in GT_EXCLUDED_CAS:
            ca = b_id
        elif q_id.startswith("0x") and q_id not in GT_EXCLUDED_CAS:
            ca = q_id
        if not ca or ca in seen:
            continue
        seen.add(ca)

        attr = p.get("attributes") or {}
        try:
            vol = float(((attr.get("volume_usd") or {}).get("h24")) or 0)
        except (TypeError, ValueError):
            vol = 0.0
        try:
            chg = float(((attr.get("price_change_percentage") or {}).get("h24")) or 0)
        except (TypeError, ValueError):
            chg = 0.0
        out.append((vol, chg, ca))
    return out


def fetch_gainers(limit=10) -> list[str]:
    """CA koin Robinhood Chain aktif di pasar luas (GeckoTerminal + Dexscreener):

      a. GeckoTerminal trending_pools + pools (khusus jaringan Robinhood)
      b. Dexscreener token-profiles + token-boosts (chainId=robinhood)
      c. Dexscreener search multi-query: v4, robinhood, ETH, YMB (chainId=robinhood)
      d. Dedup CA + urutkan berdasarkan volume/priceChange h24 menurun
    """
    cad: dict[str, tuple[float, float]] = {}   # ca → (vol24, chg24)

    # (a) GeckoTerminal native Robinhood pools (trending + top pools)
    for vol, chg, ca in _geckoterminal_rh_cas():
        vol0, chg0 = cad.get(ca, (0.0, 0.0))
        cad[ca] = (max(vol, vol0), max(chg, chg0))

    # (b) profiles + boosts — endpoint token-list, tanpa metric harga
    for ca in _dexscreener_profiles_cas():
        if ca not in cad:
            cad[ca] = (0.0, 0.0)           # metric akan diisi dari query (b)

    # (c) search multi-query — metric volume/priceChange nyata
    for q in ("v4", "robinhood", "ETH", "YMB"):
        try:
            r = _get(DEX_SEARCH_MULTI.format(q=q))
            pairs = (r.json() or {}).get("pairs") or []
        except Exception as e:
            print(f"⚠️ Dexscreener search '{q}' gagal: {e}")
            continue
        for vol, chg, ca in _rh_cas_from_pairs(pairs):
            vol0, chg0 = cad.get(ca, (0.0, 0.0))
            cad[ca] = (max(vol, vol0), max(chg, chg0))

    # (d) skor = volume24 + priceChange24 — dedup di dict di atas.
    scored = sorted(cad.items(), key=lambda kv: kv[1][0] + kv[1][1],
                    reverse=True)
    return [ca for ca, _ in scored[:limit]]


def fetch_early_buyers_ex(ca: str, max_buyers=50, retries=2) -> tuple[str, list[dict]]:
    """Pembeli EARLY sebuah koin via Blockscout + status eksplisit.

    Return (status, transfers) dengan status 'ok' | 'error'.

    🔴 Ini penting: dulu fungsi ini menelan semua kegagalan dan return [] —
    outage Blockscout jadi tidak bisa dibedakan dari "tidak ada pembeli".
    Di skema bobot 2 sumber, [] palsu = SEMUA wallet jatuh ke GT-only dan
    masuk karantina massal. Jadi kegagalan harus jujur: 'error', bukan [].

    Blockscout kadang membalas body berupa STRING JSON (rate-limit), bukan
    objek — dulu itu memicu AttributeError 'str' object has no attribute get.
    """
    url = f"{BLOCKSCOUT}/tokens/{ca}/transfers"
    last_err = None
    items = None
    for attempt in range(retries + 1):
        try:
            # Lewat rantai egress safety: direct → Worker → jina.ai.
            # Direct selalu 403 (Cloudflare managed challenge di IP VPS).
            data = safety._get(f"/tokens/{ca}/transfers")
            if not isinstance(data, dict):
                raise TypeError(f"respons bukan objek JSON ({type(data).__name__})")
            items = data.get("items") or []
            last_err = None
            break
        except Exception as e:                 # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
    if last_err is not None or items is None:
        print(f"⚠️ Blockscout transfers {ca[:10]}… gagal: {last_err}")
        return "error", []

    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            tok = it.get("token")
            toks = ((tok.get("address") if isinstance(tok, dict) else "") or "").lower()
            if toks and toks != ca.lower():
                continue  # transfer token lain dalam batch yang sama
            to_obj, from_obj = it.get("to"), it.get("from")
            if not isinstance(to_obj, dict) or not isinstance(from_obj, dict):
                continue
            to = to_obj.get("hash") or ""
            frm = from_obj.get("hash") or ""
            if not to or not frm:
                continue
            val = 0.0
            try:
                total = it.get("total")
                val = float((total or {}).get("value") or 0.0) if isinstance(total, dict) else 0.0
            except (TypeError, ValueError):
                val = 0.0
            ts = it.get("timestamp")
            stamp = 0
            if isinstance(ts, dict):
                stamp = int(ts.get("timestamp") or 0)
            elif isinstance(ts, (int, float)):
                stamp = int(ts)
            out.append({"from": frm.lower(), "to": to.lower(), "value": val,
                        "ts": stamp,
                        "is_contract": bool(to_obj.get("is_contract", False))})
        except Exception:                      # satu baris rusak ≠ gagal 1 CA
            continue
        if len(out) >= max_buyers * 3:         # buffer buat filter lagi
            break
    return "ok", out


def fetch_early_buyers(ca: str, max_buyers=50) -> list[dict]:
    """Wrapper lama (list saja) — dipakai kode/tools yang tidak butuh status."""
    return fetch_early_buyers_ex(ca, max_buyers)[1]


def fetch_gecko_buyers(ca: str, max_pools: int = 3) -> list[dict]:
    """Korborasi pembeli awal via GeckoTerminal (sumber kedua, v16.6).

    Blockscout TIDAK bisa dibantu Etherscan (403 Cloudflare di robin.
    etherscan.io; API key gratis tidak dukung chainid=4663) → pakai
    GeckoTerminal network `robinhood` (gratis, tanpa API key).
    Return list dict {wallet, usd, price_usd, ts} untuk trade buy.
    Semua kegagalan → [] (jangan raise).
    """
    try:
        from sniper import geckoterminal as gt
    except ImportError:                       # pragma: no cover
        return []
    out = []
    try:
        pools = gt.pools_of(ca)[:max_pools]
    except Exception:
        return []
    for pool in pools:
        try:
            trades = gt.trades(pool)
        except Exception:
            trades = []
        for t in trades:
            if t.get("side") != "buy":
                continue
            w = (t.get("wallet") or "").lower()
            if not w.startswith("0x") or w in QUOTE_ASSETS:
                continue
            out.append({"wallet": w, "usd": float(t.get("usd") or 0),
                        "price_usd": float(t.get("price_usd") or 0),
                        "ts": int(t.get("ts") or 0)})
    return out


def pick_buyers(transfers: list) -> list[str]:
    """Ambil alamat penerima (to) yang BUKAN kontrak router/launchpad.
       Biasanya trader EOA yang membeli (dari router → trader)."""
    buyers = []
    for t in transfers:
        if isinstance(t, str):
            to = t
            is_contract = False
        elif isinstance(t, dict):
            to = t.get("to") or ""
            is_contract = bool(t.get("is_contract", False))
        else:
            continue
        to = to.lower()
        if is_contract:
            continue
        # transfer dari quote/router ke EOA biasa = BUY
        if to and not to.endswith("cb2") and to not in QUOTE_ASSETS:
            buyers.append(to)
        if len(buyers) >= 50:
            break
    return buyers[:50]


# ---------- 2. SKEMA BOBOT 2 SUMBER ----------
# Blockscout dan GeckoTerminal BUKAN saksi independen: GT menurunkan datanya
# dari chain yang sama. Jadi "muncul di dua-duanya" = satu kejadian yang lolos
# kurasi agregator, bukan bukti ganda. Bobot di bawah mencerminkan itu:
#   BS = kebenaran mentah (recall tinggi, noise tinggi: router/pool/burn/airdrop)
#   GT = kurasi (hanya pool ter-index & trade besar) → bukti prominence
W_BOTH = 2.0          # BS + GT
W_BS_STRONG = 1.0     # BS saja, ≥2 beli di koin itu (akumulasi)
W_BS_WEAK = 0.5       # BS saja, 1 beli (numpang lewat)
W_GT_ONLY = 0.25      # GT saja → karantina, wajib verifikasi ulang
CAP_PER_CA = 2.0      # satu koin maksimal menyumbang 2 poin
PROMOTE_SCORE = 2.0
PROMOTE_MIN_VERIFIED_CA = 2


def _decay(ca_ts, now=None) -> float:
    """Koin yang terbang 3 minggu lalu bukan bukti perilaku hari ini."""
    if not ca_ts:
        return 1.0
    age = max(0.0, (now or time.time()) - float(ca_ts))
    if age <= 72 * 3600:
        return 1.0
    if age <= 7 * 86400:
        return 0.7
    return 0.4


def pick_buyer_stats(transfers: list) -> dict:
    """Agregasi per alamat penerima: jumlah beli, status kontrak, nilai token."""
    out: dict[str, dict] = {}
    for t in transfers or []:
        if not isinstance(t, dict):
            continue
        to = (t.get("to") or "").lower()
        if not to:
            continue
        if t.get("is_contract"):
            out.setdefault(to, {"buys": 0, "contract": True, "value": 0.0})
            out[to]["contract"] = True
            continue
        if to.endswith("cb2") or to in QUOTE_ASSETS:
            continue
        e = out.setdefault(to, {"buys": 0, "contract": False, "value": 0.0})
        e["buys"] += 1
        try:
            e["value"] += float(t.get("value") or 0.0)
        except (TypeError, ValueError):
            pass
    return out


def fetch_gecko_buyers_ex(ca: str, max_pools: int = 3) -> tuple[str, set[str]]:
    """GeckoTerminal dengan status eksplisit: 'ok' | 'no_pool' | 'error'.

    PENTING: 'error' (API mati/timeout) TIDAK boleh diperlakukan sama dengan
    'no_pool' (GT benar-benar tidak melihat wallet itu). Dulu keduanya jatuh
    ke bobot 1 secara diam-diam — satu timeout bisa menurunkan semua wallet.
    """
    try:
        from sniper import geckoterminal as gt
    except ImportError:                        # pragma: no cover
        return "error", set()
    try:
        pools = gt.pools_of(ca)[:max_pools]
    except Exception:
        return "error", set()
    if not pools:
        return "no_pool", set()
    wallets: set[str] = set()
    ok_pools = 0
    for pool in pools:
        try:
            trades = gt.trades(pool)
        except Exception:
            continue
        ok_pools += 1
        for t in trades or []:
            if t.get("side") != "buy":
                continue
            w = (t.get("wallet") or "").lower()
            if not w.startswith("0x") or w in QUOTE_ASSETS:
                continue
            wallets.add(w)
    if ok_pools == 0:
        return "error", set()
    return "ok", wallets


def cross_check(winners_cas: list, gecko_bonus: bool = True,
                ca_ts: dict | None = None) -> list[dict]:
    """Skor keyakinan wallet dari 2 sumber (v16.9).

    Per koin per wallet:
      BS + GT            → 2,0
      BS saja, ≥2 beli   → 1,0   (akumulasi, bukan numpang lewat)
      BS saja, 1 beli    → 0,5
      GT saja            → 0,25  (karantina — tidak boleh auto-promote)
      kontrak/burn/zero  → 0     (dibuang)
    Kalikan decay waktu, cap 2,0 per koin, lalu jumlahkan.

    Return list dict: address, score, n_ca, verified_cas, gt_only_cas,
    sources, qualifies, needs_retry.
    """
    ca_ts = ca_ts or {}
    now = time.time()
    per_wallet: dict[str, list[tuple[str, float, str]]] = defaultdict(list)

    for ca in winners_cas:
        ca = (ca or "").lower()
        if not ca.startswith("0x"):
            continue
        bs_state, bs_items = fetch_early_buyers_ex(ca)
        if bs_state != "ok":
            # 🔴 Sumber primer mati → JANGAN skor CA ini sama sekali.
            # Kalau tetap diproses dengan bs kosong, semua wallet jadi
            # "GT-only" dan masuk karantina massal karena outage API.
            continue
        bs = pick_buyer_stats(bs_items)
        if gecko_bonus:
            gt_state, gt_wallets = fetch_gecko_buyers_ex(ca)
        else:
            gt_state, gt_wallets = "off", set()

        for addr in set(bs) | set(gt_wallets):
            addr = addr.lower()
            # 🚫 alamat burn/zero/precompile bukan dompet trader
            if not db.is_trackable_wallet(addr):
                continue
            info = bs.get(addr)
            if info and info["contract"]:
                continue                       # router/pool/launchpad, bukan trader
            in_bs = info is not None
            in_gt = addr in gt_wallets

            if in_bs and in_gt:
                pts, tag = W_BOTH, "bs+gt"
            elif in_bs:
                pts = W_BS_STRONG if info["buys"] >= 2 else W_BS_WEAK
                tag = "bs"
                if gt_state == "error":
                    tag = "bs?"                # GT tidak terjawab → jangan klaim
            else:
                pts, tag = W_GT_ONLY, "gt"     # GT-only → karantina

            pts *= _decay(ca_ts.get(ca), now)
            per_wallet[addr].append((ca, min(pts, CAP_PER_CA), tag))

    rows = []
    for addr, items in per_wallet.items():
        score = sum(p for _, p, _ in items)
        verified = list(dict.fromkeys(ca for ca, _, tag in items
                                      if tag.startswith("bs")))
        gt_only = list(dict.fromkeys(ca for ca, _, tag in items if tag == "gt"))
        sources = ",".join(f"{ca}|{tag}" for ca, _, tag in items)
        rows.append({
            "address": addr,
            "score": round(score, 2),
            "n_ca": len(dict.fromkeys(ca for ca, _, _ in items)),
            "verified_cas": verified,
            "gt_only_cas": gt_only,
            "sources": sources,
            "qualifies": (score >= PROMOTE_SCORE
                          and len(verified) >= PROMOTE_MIN_VERIFIED_CA),
            # perlu scan ulang: GT error ATAU ada CA yang cuma terlihat di GT
            "needs_retry": any(tag == "bs?" for _, _, tag in items)
                           or bool(gt_only),
        })
    rows.sort(key=lambda r: (-r["score"], r["address"]))
    return rows


# ---------- 3. SIMPAN KE DB ----------

def persist(winner_rows: list, dry=False):
    """Simpan hasil skor. Tiga nasib:

      - qualifies      → enabled=1, label screen_insiders (dipantau tracker)
      - GT-only        → enabled=0, label quarantine_gt (masuk tabel, tidak
                         dipantau sampai Blockscout mengonfirmasi)
      - sisanya        → dilewati (skor di bawah ambang / 1 CA)
    """
    stats = {"promoted": 0, "quarantine": 0, "skipped": 0}
    for r in winner_rows:
        addr = r["address"]
        score = float(r["score"])
        verified = r.get("verified_cas") or []
        gt_only = r.get("gt_only_cas") or []
        if r.get("qualifies"):
            enabled, label = 1, "screen_insiders"
            notes = "auto: bobot 2 sumber"
            stats["promoted"] += 1
        elif gt_only and not verified and r["n_ca"] >= 2:
            # Karantina hanya untuk kandidat yang layak diverifikasi ulang:
            # pernah terlihat di ≥2 koin. GT-only 1 koin = noise harian.
            enabled, label = 0, "quarantine_gt"
            notes = "GT-only: belum terverifikasi Blockscout"
            stats["quarantine"] += 1
        else:
            stats["skipped"] += 1
            continue

        win_cas = ",".join(verified or gt_only)
        verified_at = 0 if r.get("needs_retry") else int(time.time())
        if dry:
            print(f"  (dry) {addr[:14]}… skor={score:.2f} "
                  f"label={label} enabled={enabled} CA={win_cas[:50]}")
            continue
        db.add_insider_wallet(
            addr, label=label, win_count=r["n_ca"], notes=notes,
            win_cas=win_cas, update_if_exists=True, score=score,
            sources=r.get("sources", ""), last_verified=verified_at,
            enabled=enabled, set_wins=True)
        print(f"✅ {addr[:14]}… skor={score:.2f} {label} "
              f"({r['n_ca']} CA: {win_cas[:50]}…)")
    print(f"📦 promoted={stats['promoted']} "
          f"quarantine={stats['quarantine']} skipped={stats['skipped']}")


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Screen koin terbang → insider wallets")
    ap.add_argument("cas", nargs="*", help="CA koin manual (opsional)")
    ap.add_argument("--limit", type=int, default=5,
                    help="max koin dikoreksikan (default 5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="tampil saja, tidak menulis DB")
    args = ap.parse_args()

    cas = [c for c in args.cas if c.startswith("0x")]
    if not cas:
        print("🔍 Mencari koin gainers Robinhood di Dexscreener…")
        cas = fetch_gainers(args.limit)
        print(f"   {len(cas)} koin momentum didapat.")
    if not cas:
        print("Tidak ada koin kandidat. Coba berikan CA manual.")
        return

    db.init()
    rows = cross_check(cas)
    print(f"\n📊 Hasil intersection dari {len(cas)} koin:")
    if not rows:
        print("(belum ada wallet terbentur ≥2 koin — coba lagi nanti / CA lain)")
        return
    for r in rows[:20]:
        if r["qualifies"]:
            flag = "✅"
        elif r["gt_only_cas"] and not r["verified_cas"]:
            flag = "🧊"
        else:
            flag = "·"
        print(f"  {flag} {r['address']}  skor={r['score']:.2f}  "
              f"CA={r['n_ca']}  {r['sources'][:80]}")
    n_pass = sum(1 for r in rows if r["qualifies"])
    print(f"\n📦 {n_pass} lolos gate (skor ≥{PROMOTE_SCORE} "
          f"dan ≥{PROMOTE_MIN_VERIFIED_CA} CA terverifikasi Blockscout)")
    persist(rows, dry=args.dry_run)


if __name__ == "__main__":
    main()
