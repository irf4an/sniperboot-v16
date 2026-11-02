"""Agent layer — Claude API sebagai otak pertimbangan entry & manajemen posisi.

Prinsip: LLM memutuskan DI DALAM pagar deterministik, tidak pernah melewatinya.
- Hard filter (mcap/liq/safety) jalan duluan; yang gagal tidak pernah sampai ke LLM.
- Semua output LLM di-clamp: size multiplier, TP/SL override, dsb.
- TP/SL deterministik tetap backstop terakhir di monitor loop.
- Setiap keputusan dicatat di decision log (bisa ditanya "kenapa?" via bot).
- Lessons diturunkan dari posisi closed dan di-inject ke prompt berikutnya.

Sync (requests) — panggil via asyncio.to_thread.
"""
import json
import time
import requests
from . import config, db, dexscreener as dx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"

# ---- pagar keras: rentang parameter yang diizinkan LLM manajer ----
MANAGER_SYSTEM = """You are the position-management module of hoodsniper. Deterministic TP/SL rules already run every cycle as a backstop; you add judgment between those thresholds (e.g. momentum died at +20%, exit early; or strong trend at +40%, hold for TP).

Respond with ONLY a JSON object:
{"action":"HOLD"|"SELL_HALF"|"SELL_ALL","reason":"<=200 chars, bahasa Indonesia santai"}

Be conservative: SELL actions execute real trades. Only sell when the data clearly says momentum is gone or risk spiked. In "reason", cite the actual numbers behind the call (pnl, price change, buy/sell flow) as 1-2 short clauses separated by '; ' — no number, no claim."""

LESSONS_SYSTEM = """You analyze closed trades of a meme token sniper and extract at most 3 short, concrete, actionable lessons (bahasa Indonesia). Respond ONLY with a valid JSON array of strings, e.g. ["pelajaran 1", "pelajaran 2"]. Do not output thinking or prose."""



def _openai_text(r):
    """Ambil teks jawaban dari respons OpenAI-compatible.

    Normalnya JSON utuh. Tapi sebagian gateway (mis. 9Router + provider
    streaming seperti Kiro) membalas SSE 'data: {...}' beruntun - r.json()
    langsung meledak JSONDecodeError. Di sini chunk SSE dirangkai ulang
    dari delta.content tiap chunk.

    Model reasoning (mis. DeepSeek via gateway publik) punya jebakan
    lain, terbukti di lapangan 2026-10-22: respons JSON valid sampai,
    tetapi message.content KOSONG dan isinya berada di
    message.reasoning_content (anggaran token habis untuk berpikir,
    jawaban akhir tidak sempat ditulis ke content). Pembaca lama
    menyimpulkan "tidak bisa dibaca" dan keputusan gugur — maka
    reasoning_content kini dibaca sebagai jalan keluar terakhir
    (di JSON utuh maupun di delta SSE); _parse_json cukup toleran
    untuk memetik objek keputusan dari teks berpikir itu.
    """
    try:
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        if msg.get("content"):
            return msg["content"]
        if msg.get("reasoning_content"):
            return msg["reasoning_content"]
    except ValueError:
        pass
    parts = []
    rparts = []
    for line in (r.text or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
            if delta.get("reasoning_content"):
                rparts.append(delta["reasoning_content"])
            msgc = (ch.get("message") or {}).get("content")
            if msgc:
                parts.append(msgc)
    text = "".join(parts) or "".join(rparts)
    if not text:
        raise ValueError(
            f"respons LLM tidak bisa dibaca (bukan JSON/SSE): {(r.text or '')[:200]!r}")
    return text

# ---------- LLM call (multi-provider) ----------
def _call(system, user, max_tokens=600, model=None):
    model = model or db.get("agent_model", str)
    if config.LLM_PROTOCOL == "anthropic":
        url = config.LLM_BASE_URL or ANTHROPIC_URL
        r = requests.post(url, timeout=45, headers={
            "x-api-key": config.LLM_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        r.raise_for_status()
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    # OpenAI-compatible: OpenAI / DeepSeek / GLM / Kimi / Qwen / OpenRouter / 9router / LM Studio
    base = (config.LLM_BASE_URL or OPENAI_DEFAULT_BASE).rstrip("/")
    r = requests.post(f"{base}/chat/completions", timeout=45, headers={
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }, json={
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "stream": False,
    })
    r.raise_for_status()
    return _openai_text(r)


def _parse_json(text):
    """Toleran: markdown fence, teks pembungkus, 'Extra data', DAN output kepotong
    (truncated) karena max_tokens kekecilan — sering di provider verbose via 9router."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip()
    # mulai dari kurung pembuka terluar yang muncul duluan
    a_obj, a_arr = t.find("{"), t.find("[")
    if a_arr != -1 and (a_obj == -1 or a_arr < a_obj):
        start = a_arr
    elif a_obj != -1:
        start = a_obj
    else:
        raise ValueError("tidak ada JSON di respons LLM")
    frag = t[start:]
    dec = json.JSONDecoder()
    # raw_decode: ambil objek/array valid PERTAMA, abaikan sisa ("Extra data")
    try:
        obj, _ = dec.raw_decode(frag)
        return obj
    except json.JSONDecodeError:
        pass
    # kemungkinan truncated → tutup string/kurung yang menggantung lalu retry
    repaired = _repair_truncated(frag)
    obj, _ = dec.raw_decode(repaired)
    return obj


def _repair_truncated(s):
    """Tambal JSON yang kepotong: tutup string terbuka + kurung yang belum ketutup.
    Untuk array of objects, buang elemen terakhir yang belum lengkap."""
    # buang trailing yang jelas nggantung
    s = s.rstrip()
    # hitung kurung di luar string
    depth_c = depth_s = 0
    in_str = esc = False
    last_complete = 0  # posisi setelah elemen array top-level terakhir yang komplit
    for i, ch in enumerate(s):
        if esc:
            esc = False; continue
        if ch == "\\" and in_str:
            esc = True; continue
        if ch == '"':
            in_str = not in_str; continue
        if in_str:
            continue
        if ch == "{": depth_c += 1
        elif ch == "}": depth_c -= 1
        elif ch == "[": depth_s += 1
        elif ch == "]": depth_s -= 1
        if ch == "," and depth_s == 1 and depth_c == 0:
            last_complete = i  # koma pemisah antar elemen array top-level
    if in_str:
        s += '"'
    # kalau array & masih ada elemen nggantung, potong sampai elemen komplit terakhir
    if s[:1] == "[" and (depth_c > 0 or depth_s > 1) and last_complete:
        s = s[:last_complete]
        depth_c = 0; depth_s = 1
    s += "}" * max(depth_c, 0)
    s += "]" * max(depth_s, 0)
    return s


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


# ---------- sanitasi: pagar anti prompt-injection ----------
_CTRL_RE = __import__("re").compile(r"[\x00-\x1f\x7f]")

def sanitize_text(s, max_len=120):
    """Bersihkan teks eksternal sebelum masuk konteks LLM.

    Hapus kontrol/zero-width chars (visually-hidden, RTL override, dsb.),
    ganti newline dengan spasi (teks grup bukan instruksi multi-baris),
    dan potong panjangnya. Teks ini dianggap DATA, bukan instruksi —
    system prompt sudah melarang LLM mengikuti arahan dari dalam data.
    """
    if s is None:
        return None
    t = _CTRL_RE.sub(" ", str(s))
    t = t.replace("​", " ").replace("­", " ")  # zero-width & soft hyphen
    t = " ".join(t.split())
    return t[:max_len]


def check_staleness(initial_price, current_price, max_drift_pct=0.50):
    """TRUE bila harga sudah melonjak/anjlok melewati max_drift_pct sejak
    MATA mulai mengevaluasi → keputusan BUY harus dibatalkan (beli di pucuk).

    initial/current = harga USD (positif). Nilai tidak valid → False
    (jangan block trade karena data rusak). max_drift_pct=0.50 = 50%
    (default runtime diatur setting DB `agent_max_drift`).
    """
    try:
        i, c, m = float(initial_price), float(current_price), float(max_drift_pct)
    except (TypeError, ValueError):
        return False
    if i <= 0 or c <= 0:
        return False
    return (c / i - 1.0) > m


# ---------- context builders ----------
def _pair_context(pair):
    tx5 = (pair.get("txns") or {}).get("m5") or {}
    tx1h = (pair.get("txns") or {}).get("h1") or {}
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    age_min = None
    if pair.get("pairCreatedAt"):
        age_min = int((time.time() * 1000 - pair["pairCreatedAt"]) / 60000)
    return {
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "name": (pair.get("baseToken") or {}).get("name"),
        "mcap_usd": dx.mcap_of(pair),
        "liquidity_usd": dx.liq_usd_or_none(pair),
        "pool_age_minutes": age_min,
        "volume_usd": {"m5": vol.get("m5"), "h1": vol.get("h1")},
        "price_change_pct": {"m5": chg.get("m5"), "h1": chg.get("h1")},
        "txns_m5": {"buys": tx5.get("buys"), "sells": tx5.get("sells")},
        "txns_h1": {"buys": tx1h.get("buys"), "sells": tx1h.get("sells")},
        "dex": pair.get("dexId"), "pool_type": dx.pool_type(pair),
    }




# ---------- position review (manager role) ----------
def review_position(pos, pair):
    price = float(pair.get("priceUsd") or 0)
    pnl = price / pos["entry_price_usd"] - 1 if price > 0 else 0
    ctx = {
        "position": {
            "symbol": pos["symbol"], "pnl_pct": round(pnl * 100, 1),
            "tp1_done": bool(pos["tp1_done"]),
            "minutes_held": int((time.time() - pos["ts"]) / 60),
            "thresholds": {"tp1": pos.get("tp1_x") or db.get("tp1"),
                           "tp2": pos.get("tp2_x") or db.get("tp2"),
                           "sl": pos.get("sl_x") or db.get("sl")},
        },
        "market": _pair_context(pair),
        "memory": {"lessons": [l["text"] for l in db.recent_lessons(5)]},
    }
    raw = _call(MANAGER_SYSTEM, json.dumps(ctx), 700)
    d = _parse_json(raw)
    action = str(d.get("action", "HOLD")).upper()
    if action not in ("HOLD", "SELL_HALF", "SELL_ALL"):
        action = "HOLD"
    reason = sanitize_text(d.get("reason"), 250)
    db.log_decision(ca=pos["ca"], symbol=pos["symbol"], role="manager",
                    action=action, conviction=0, size_mult=0, reason=reason, risks="")
    return action, reason


# ---------- lessons ----------
def _lessons_from_bullets(text):
    """Fallback: petik pelajaran dari daftar poin saat _parse_json gagal total
    (model menjawab berupa bullet/numbered list, bukan JSON)."""
    if not text:
        return []
    import re
    out = []
    for m in re.finditer(r"^\s*(?:\d+[\.\)]|[-*•])\s*(.+)$", text, re.MULTILINE):
        t = m.group(1).strip()
        # buang penanda markdown yang tersisa (mis. "**pelajaran**")
        t = t.strip("*_`").strip()
        if t:
            out.append(t)
    return out


def derive_lessons():
    closed = db.closed_positions_with_decisions(20)
    if len(closed) < 5:
        return []
    # 2000 token: prompt 15-20 posisi closed itu panjang, dan via 9router
    # (DeepSeek/GLM) anggaran 800 terpotong di tengah array JSON — sumber
    # ValueError "tidak ada JSON di respons LLM" (lapangan 2026-10-23).
    raw = _call(LESSONS_SYSTEM, json.dumps(closed), 2000)
    try:
        items = _parse_json(raw)
    except ValueError:
        # model menjawab berupa daftar poin bahasa Indonesia → petik manual
        items = _lessons_from_bullets(raw)
    if not isinstance(items, list):
        items = _lessons_from_bullets(raw)
    out = []
    for t in items[:3]:
        t = str(t).strip()[:300]
        if t:
            db.add_lesson(t)
            out.append(t)
    return out


# ---------- 💬 TANYA: Q&A bebas grounded ke data bot ----------
QA_SYSTEM = """You are hoodsniper's assistant. Answer the admin's question in casual bahasa
Indonesia, grounded ONLY on the JSON data given (positions, stats, decisions, callers,
settings). If the data doesn't contain the answer, say so honestly — never invent numbers.
Keep it under 150 words."""


def tanya(question, context):
    return _call(QA_SYSTEM, json.dumps({"question": question, "data": context}), 700)
