"""Analisis read-only: apakah cap dev 5% membuang koin yang sebenarnya bagus?

Pertanyaan: koin yang di-skip karena dev > 5% — apakah ada yang tetap naik
cukup untuk kena TP2 kita (+80% = 1.8x)?

Metode:
  1. Ambil semua CA yang di-skip dengan alasan dev_terlalu_besar dari log.
  2. Ambil OHLCV pool-nya dari GeckoTerminal (hour + day).
  3. Hitung puncak tertinggi dibanding candle pertama (≈ harga launch).
  4. Bandingkan dengan posisi yang BENAR-BENAR dibeli (dev ≤ 5%) dari DB.

Read-only. Output JSON: /tmp/dev_threshold_analysis.json
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import geckoterminal as gt  # noqa: E402

LOG = ROOT / "logs" / "pm2-out.log"
DB = ROOT / "hoodsniper.db"
OUT = Path("/tmp/dev_threshold_analysis.json")

CA_LINE = re.compile(
    r"\[scanner\] (0x[0-9a-fA-F]{40}) -> skip_fast:dev_terlalu_besar(?:\s*\(([\d.]+)%\))?")
SYM_LINE = re.compile(r"\[fast-skip\] (.+?): dev_terlalu_besar")


def parse_skips(limit: int = 60):
    pending_sym = None
    pending_dev = None
    found = {}
    with LOG.open(errors="ignore") as f:
        for line in f:
            if "dev_terlalu_besar" not in line:
                continue
            m = SYM_LINE.search(line)
            if m:
                pending_sym = m.group(1).strip()
                pct = re.search(r"\(([\d.]+)%\)", line)
                pending_dev = float(pct.group(1)) if pct else None
                continue
            m = CA_LINE.search(line)
            if m:
                ca = m.group(1).lower()
                dev = float(m.group(2)) if m.group(2) else pending_dev
                if ca not in found:
                    found[ca] = {"ca": ca, "symbol": pending_sym or ca[:10],
                                 "dev_pct": dev}
                pending_sym = None
                pending_dev = None
    rows = list(found.values())
    return rows[-limit:][::-1]


def _ohlcv(pool: str, timeframe: str):
    path = (f"/networks/{gt.NETWORK}/pools/{pool}/ohlcv/{timeframe}"
            f"?aggregate=1&limit=1000&currency=usd&token=base")
    data = gt._get(path)
    if not data:
        return []
    lst = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = []
    for c in lst:
        try:
            out.append({"ts": int(c[0]), "high": float(c[2]), "close": float(c[4])})
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda x: x["ts"])
    return out


def measure(ca: str):
    pools = gt.pools_of(ca)
    if not pools:
        return None
    pool = pools[0]
    hour = _ohlcv(pool, "hour")
    day = _ohlcv(pool, "day")
    base = None
    peak = 0.0
    if hour:
        base = hour[0]["close"] or None
        peak = max(c["high"] for c in hour)
    if day:
        if base is None:
            base = day[0]["close"] or None
        peak = max(peak, max(c["high"] for c in day))
    if not base or base <= 0 or peak <= 0:
        return {"pool": pool, "candles": len(hour) + len(day)}
    return {"pool": pool, "candles": len(hour) + len(day),
            "x_peak": round(peak / base, 2)}


def db_benchmark():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT status, peak_price, entry_price_usd FROM positions "
        "WHERE peak_price > 0 AND entry_price_usd > 0").fetchall()
    conn.close()
    if not rows:
        return {}
    mults = [p / e for _, p, e in rows]
    return {"n": len(mults),
            "p50": round(st.median(mults), 2),
            "max": round(max(mults), 2),
            "ge_1_5x": sum(1 for m in mults if m >= 1.5),
            "ge_1_8x": sum(1 for m in mults if m >= 1.8),
            "ge_2x": sum(1 for m in mults if m >= 2.0)}


def main():
    bench = db_benchmark()
    print("=== BENCHMARK: posisi yang DIBELI (dev <= 5%) ===")
    print(json.dumps(bench, indent=2))

    skips = parse_skips(60)
    print(f"\n=== KANDIDAT DI-SKIP karena dev > 5%: {len(skips)} ===")
    results = []
    for i, s in enumerate(skips, 1):
        m = measure(s["ca"])
        rec = {**s, "result": m}
        results.append(rec)
        devtxt = f"{s['dev_pct']:.1f}%" if s["dev_pct"] else ">5%"
        if m and m.get("x_peak"):
            print(f"[{i}/{len(skips)}] {s['symbol'][:16]:16} dev {devtxt:>6} "
                  f"-> peak {m['x_peak']:6.2f}x")
        else:
            print(f"[{i}/{len(skips)}] {s['symbol'][:16]:16} dev {devtxt:>6} "
                  f"-> data tidak tersedia")
        if i % 10 == 0:
            OUT.write_text(json.dumps({"benchmark": bench, "skips": results}, indent=2))

    OUT.write_text(json.dumps({"benchmark": bench, "skips": results}, indent=2))
    ok = [r for r in results if r["result"] and r["result"].get("x_peak")]
    print(f"\n=== RINGKASAN ({len(ok)} koin terukur) ===")
    if ok:
        xs = [r["result"]["x_peak"] for r in ok]
        for th in (1.5, 1.8, 2, 3, 5, 10):
            n = sum(1 for x in xs if x >= th)
            print(f"  >= {th:>4}x : {n:>3} koin ({n/len(xs)*100:5.1f}%)")
        print(f"  median : {st.median(xs):.2f}x")
        print(f"  maks   : {max(xs):.2f}x")
    print(f"\nDetail: {OUT}")


if __name__ == "__main__":
    main()
