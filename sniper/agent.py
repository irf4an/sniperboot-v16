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

# ---- pagar keras: LLM tidak bisa keluar dari rentang ini ----
SIZE_MULT_MIN = 0.25
TP1_RANGE = (0.20, 3.0)
TP2_RANGE = (0.30, 10.0)
SL_RANGE = (-0.60, -0.10)

SCREENER_SYSTEM = """You are the entry-decision module of hoodsniper, a meme token sniper on Robinhood Chain.
The "caller" field holds the track record of the specific person who posted this call (calls_bought, hit_tp1, full_tp, stopped) \u2014 a caller with many stopped and few TP is a strong reason to size down or SKIP; a proven caller justifies more conviction. A token call has ALREADY passed hard filters (mcap limit, min liquidity, dev-holding and holder-concentration checks). Your job is judgment on top of that, using the framework below.

Respond with ONLY a JSON object, no prose, no markdown fences:
{"action":"BUY"|"SKIP","conviction":0-100,"size_mult":0.25-MAXMULT,"tp1":0.2-3.0,"tp2":0.3-10.0,"sl":-0.6--0.1,"reason":"2-3 short clauses separated by '; ', <=240 chars, bahasa Indonesia","risks":"<=140 chars, specific to this token","blacklist_caller":false}

EVALUATION FRAMEWORK \u2014 judge in this order, every time:
1) AGE & STRUCTURE: pool_age_minutes <5 = embryonic (sniper/dev playground; demand overwhelming evidence), 5-30 = early, >30 = starts to have a track record. A brand-new pool that is already falling is distribution, not a dip.
2) LIQUIDITY QUALITY: weigh liquidity_usd absolute AND against mcap. liq/mcap <10% = fragile (the price is fiction), 10-30% = normal for memes, >30% = sturdy. A big mcap standing on thin liquidity is a trap, not strength.
3) MOMENTUM: price_change_pct m5 is entry timing, h1 is the trend. A vertical move (m5 >= +100%) means you are late: chasing is acceptable only at cut size with a fast TP1, and m5 >= +150% defaults to SKIP. Negative m5 on a minutes-old pool = early holders exiting.
4) FLOW PRESSURE: compare buys vs sells (txns_m5, txns_h1) but weigh VALUE, not just count \u2014 many small buys against a few sells while price falls means large sellers are distributing (bearish). One-sided buy flow with rising price = accumulation.
5) SAFETY RESIDUE: hard filters passed, but holder data minutes after launch may be unverified; price unverified data into size_mult instead of pretending it is clean, and treat verified-bad concentration as a SKIP.
6) SOURCE & CALLER: apply the source/caller track record and the lessons in memory. A proven source can rescue a borderline setup; an unproven one cannot.

CONVICTION CALIBRATION (be stingy \u2014 the admin grades your conviction against real outcomes):
- 85-100: exceptional only \u2014 sturdy liquidity, one-sided flow, sane momentum, proven source. Rare.
- 70-84: strong setup with one visible flaw.
- 55-69: decent but flawed; BUY only at reduced size.
- 35-54: marginal \u2014 default SKIP.
- 0-34: clear reject.

OUTPUT DISCIPLINE:
- reason: 2-3 clauses separated by '; ' \u2014 each clause = one factor + its ACTUAL numbers + the implication. No number, no claim. Generic filler ("potensi bagus", "cukup menarik") is forbidden.
- risks: name the single most likely way THIS specific trade loses money, with its number when there is one.
- size_mult: 1.0 = normal. <1 is MANDATORY when any of these hold: age <5 min, liq/mcap <15%, m5 >= +100%, holder data unverified. >1 only for 85+ conviction setups.
- tp1/tp2 are profit fractions (0.5 = +50%), sl is loss fraction (-0.3 = -30%). Defaults tp1=0.5, tp2=1.0, sl=-0.3. Vertical entries take profit faster (lower tp1); sturdy setups may aim further.

Set "blacklist_caller": true ONLY when this caller's track record is clearly toxic (>=4 bought calls with >=75% stopped/rugged and zero full TPs). A blacklisted caller's future calls are auto-skipped without asking you \u2014 this is real authority, use it carefully.

You cannot exceed the caps; out-of-range values will be clamped anyway."""

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


def _pair_entry_price(pair):
    try:
        return float((pair or {}).get("priceUsd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _memory_context():
    lessons = db.recent_lessons(5)
    decisions = db.recent_decisions(5)
    return {
        "lessons": [l["text"] for l in lessons],
        "recent_decisions": [
            {"symbol": d["symbol"], "action": d["action"], "conviction": d["conviction"],
             "reason": d["reason"], "outcome": d.get("outcome")}
            for d in decisions
        ],
    }


# ---------- entry decision (screener role) ----------
def decide_entry(pair, ca, source, safety_note, caller=None):
    base_usd = db.get("buy_usd")
    max_mult = db.get("agent_size_mult_max")
    insurance_note_sanitized = sanitize_text(safety_note, 120)
    caller_sanitized = None
    if caller:
        caller_sanitized = {
            "id": caller.get("id"),
            "name": sanitize_text(caller.get("name"), 80),
            "track_record": caller.get("track_record"),
        }
    ctx = {
        "token": _pair_context(pair),
        "safety_check": insurance_note_sanitized,
        "source": {"name": sanitize_text(source["name"], 80), "mcap_limit": source["mcap_max"],
                   "track_record": db.source_stats(source["name"])},
        "caller": caller_sanitized,
        "initial_price_usd": _pair_entry_price(pair),
        "base_buy_usd": base_usd,
        "defaults": {"tp1": db.get("tp1"), "tp2": db.get("tp2"), "sl": db.get("sl")},
        "memory": _memory_context(),
    }
    # Panggilan LLM dicoba MAKSIMAL 2x: kegagalan sesaat (timeout/putus
    # gateway/JSON rusak) tidak boleh langsung menjatuhkan keputusan ke
    # jalur fallback — di lapangan (2026-10-22) gateway publik beberapa
    # kali gagal menjawab dan tiap kegagalan mengorbankan satu sampel
    # keputusan. Gagal dua-duanya → exception naik ke pemanggil seperti
    # sebelumnya (listener memperlakukannya sebagai "Mata tidak menjawab").
    d = None
    last_err = None
    for attempt in (1, 2):
        try:
            raw = _call(SCREENER_SYSTEM.replace("MAXMULT", str(max_mult)),
                        json.dumps(ctx), 1200)
            d = _parse_json(raw)
            break
        except Exception as e:
            last_err = e
            if attempt == 1:
                print(f"[agent] decide_entry percobaan 1 gagal ({e}) — "
                      f"mengulang sekali dalam 3 detik")
                time.sleep(3)
    if d is None:
        raise last_err

    action = "BUY" if str(d.get("action", "")).upper() == "BUY" else "SKIP"
    # ---------- sanitasi pagar keras: teks bebas LLM dipangkas & dibersihkan ----------
    decision = {
        "action": action,
        "conviction": int(_clamp(d.get("conviction"), 0, 100, 50)),
        "size_mult": _clamp(d.get("size_mult"), SIZE_MULT_MIN, max_mult, 1.0),
        "tp1": _clamp(d.get("tp1"), *TP1_RANGE, db.get("tp1")),
        "tp2": _clamp(d.get("tp2"), *TP2_RANGE, db.get("tp2")),
        "sl": _clamp(d.get("sl"), *SL_RANGE, db.get("sl")),
        "reason": sanitize_text(d.get("reason"), 250),
        "risks": sanitize_text(d.get("risks"), 150),
        "blacklist_caller": bool(d.get("blacklist_caller")),
    }
    if decision["tp2"] <= decision["tp1"]:
        decision["tp2"] = decision["tp1"] + 0.25
    db.log_decision(ca=ca, symbol=ctx["token"]["symbol"] or "?", role="screener",
                    action=decision["action"], conviction=decision["conviction"],
                    size_mult=decision["size_mult"], reason=decision["reason"],
                    risks=decision["risks"])
    return decision


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
