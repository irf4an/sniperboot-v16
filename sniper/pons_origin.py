"""Deteksi apakah sebuah CA adalah launch dari factory Pons.

Dipakai gate top10 di `safety.check()` dan goldilocks `listener`. Token Pons
lahir lewat bonding curve organik; konsentrasi top10 di awal sering tinggi
tanpa berarti tim menahan supply. Karena itu top10 boleh di-bypass, TAPI
hanya untuk token yang benar-benar berasal dari factory Pons.

Urutan pengecekan (murah → mahal):
  1. cache memori (6 jam)
  2. tabel `pons_origin` di DB (persist antar restart)
  3. `pons_queue` — semua kandidat kurva Pons yang pernah dilewatkan radar
  4. API detail Pons `/api/launches/{ca}` (200 + ada field `address` = Pons)

Fail-safe: kalau API error/timeout, kembalikan False (filter tetap berlaku).
Jangan pernah fail-open di sini — bypass holder filter itu keputusan berisiko.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from . import db

_API = "https://ponsfamily.com/api/launches/{ca}"
_UA = "Mozilla/5.0 (compatible; Hoodsniper/16.10)"
_MEM_TTL = 6 * 3600
_MEM: dict[str, tuple[bool, float]] = {}


def _api_is_pons(ca: str) -> bool | None:
    """True/False dari API Pons, None kalau tidak bisa dipastikan."""
    try:
        req = urllib.request.Request(_API.format(ca=ca), headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status != 200:
                return None
            d = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(d, dict) and d.get("address") and str(d["address"]).lower() == ca:
            return True
        return False
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return None
    except Exception:
        return None


def is_pons_launch(ca: str) -> bool:
    """True kalau `ca` berasal dari factory Pons (dan boleh bypass top10)."""
    ca = (ca or "").strip().lower()
    if not ca:
        return False

    now = time.time()
    hit = _MEM.get(ca)
    if hit and now - hit[1] < _MEM_TTL:
        return hit[0]

    try:
        cached = db.get_pons_origin(ca)
    except Exception:
        cached = None
    if cached is not None:
        _MEM[ca] = (cached, now)
        return cached

    # Semua kandidat kurva Pons tercatat di pons_queue → tidak perlu HTTP.
    try:
        if db.is_pons_queue_member(ca):
            db.set_pons_origin(ca, True)
            _MEM[ca] = (True, now)
            return True
    except Exception:
        pass

    verdict = _api_is_pons(ca)
    if verdict is None:
        # Tidak bisa dipastikan → jangan bypass, jangan cache hasil negatif.
        return False
    try:
        db.set_pons_origin(ca, verdict)
    except Exception:
        pass
    _MEM[ca] = (verdict, now)
    return verdict
