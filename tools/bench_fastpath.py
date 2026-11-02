"""Benchmark read-only: berapa lama fast-path gate sebenarnya.

Tidak menulis DB, tidak mengirim order, tidak menyentuh posisi.
Hanya eth_call / HTTP GET.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper import listener, db, safety, dexscreener as dx
from sniper.trader import Trader


def ms(t0):
    return (time.time() - t0) * 1000


async def main():
    tr = Trader()
    listener._trader = tr
    print(f"[bench] wallet={tr.addr} chain={tr.w3.eth.chain_id}")

    cands = await asyncio.to_thread(dx.profile_candidates)
    print(f"[bench] kandidat dari Dexscreener profiles/boosts: {len(cands)}")

    picked = []
    for ca in cands:
        pair = await asyncio.to_thread(dx.best_pair, ca)
        if not pair:
            continue
        if dx.pool_type(pair) != "v4":
            continue
        if (dx.liq_usd_or_none(pair) or 0) <= 0:
            continue
        picked.append((ca, pair))
        if len(picked) >= 2:
            break

    if not picked:
        print("[bench] tidak ada kandidat v4 berlikuiditas — batal")
        return

    for ca, pair in picked:
        sym = (pair.get("baseToken") or {}).get("symbol", "?")
        print(f"\n=== {sym} {ca} ===")

        t0 = time.time()
        ok, reason = await asyncio.to_thread(safety.check, tr, ca, pair)
        d_safety = ms(t0)
        print(f"  safety.check   : {d_safety:7.0f} ms  ok={ok} {reason[:60]}")

        t0 = time.time()
        ok2, reason2 = await asyncio.to_thread(safety.check, tr, ca, pair)
        print(f"  safety.check#2 : {ms(t0):7.0f} ms  (tanpa cache → biaya diulang)")

        t0 = time.time()
        okf, tag, metr = await listener._fastpath_gate(ca, pair)
        d_gate = ms(t0)
        print(f"  _fastpath_gate : {d_gate:7.0f} ms  lolos={okf} tag={tag}")
        print(f"  metrik: liq={metr.get('liq')} mcap_onchain={metr.get('mcap_onchain')} "
              f"umur={metr.get('pool_age')} ratio={metr.get('ratio')}")
        print(f"  → total dari pair diketahui s/d keputusan: "
              f"{d_safety + d_gate:.0f} ms (safety dihitung 2x oleh pipa)")


asyncio.run(main())
