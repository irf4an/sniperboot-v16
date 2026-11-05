"""🧪 Uji rantai egress Blockscout (v16.13) — TANPA jaringan.

Konteks: Cloudflare memberi managed challenge (403) ke IP VPS, jadi request
langsung selalu gagal. safety._get harus jatuh ke Worker sendiri lalu ke
r.jina.ai, dan tetap menghasilkan JSON yang sama.

Lingkup:
  1. direct 403 → otomatis fallback ke jina, hasil JSON valid.
  2. direct 403 → cooldown, panggilan berikutnya tidak mencoba direct lagi.
  3. Worker dipakai lebih dulu daripada jina kalau setting diisi.
  4. Parse envelope jina {"code":...,"data":{"text":...}} → JSON asli.
  5. Parse wrapper "Markdown Content:" → JSON asli.
  6. Cache 10 detik: panggilan kedua tidak menyentuh jaringan.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper import safety  # noqa: E402


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.text = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    def json(self):
        import json
        return json.loads(self.text)


def _reset():
    safety._CACHE.clear()
    safety._direct_blocked_until = 0.0


def test_direct_403_falls_back_to_jina():
    _reset()
    calls = {"direct": 0, "jina": 0}
    orig_direct, orig_jina = safety._get_direct, safety._get_jina

    def bad_direct(url):
        calls["direct"] += 1
        raise RuntimeError("HTTP Error 403")

    def ok_jina(url):
        calls["jina"] += 1
        return {"items": [{"address": {"hash": "0xabc"}, "value": "10"}]}

    safety._get_direct, safety._get_jina = bad_direct, ok_jina
    try:
        d = safety._get("/tokens/0xdead/holders")
        assert d["items"][0]["value"] == "10"
        assert calls["direct"] == 1 and calls["jina"] == 1
        print("PASS direct 403 → fallback jina, JSON utuh")
    finally:
        safety._get_direct, safety._get_jina = orig_direct, orig_jina


def test_direct_cooldown_after_403():
    _reset()
    calls = {"direct": 0, "jina": 0}
    orig_direct, orig_jina = safety._get_direct, safety._get_jina

    def bad_direct(url):
        calls["direct"] += 1
        raise RuntimeError("HTTP Error 403")

    def ok_jina(url):
        calls["jina"] += 1
        return {"ok": url}

    safety._get_direct, safety._get_jina = bad_direct, ok_jina
    try:
        safety._get("/addresses/0x1")          # direct 403 → cooldown
        safety._get("/addresses/0x2")          # harus skip direct
        assert calls["direct"] == 1, f"direct dipanggil {calls['direct']}x (harus 1)"
        assert calls["jina"] == 2
        print("PASS cooldown 10 menit: direct tidak dicoba berulang setelah 403")
    finally:
        safety._get_direct, safety._get_jina = orig_direct, orig_jina


def test_worker_used_before_jina():
    _reset()
    orig_direct, orig_worker, orig_jina = (safety._get_direct, safety._get_worker,
                                           safety._get_jina)
    orig_get = safety.db.get
    calls = {"worker": 0, "jina": 0}

    def bad_direct(url):
        raise RuntimeError("HTTP Error 403")

    def ok_worker(url):
        calls["worker"] += 1
        return {"via": "worker"}

    def ok_jina(url):
        calls["jina"] += 1
        return {"via": "jina"}

    safety._get_direct, safety._get_worker, safety._get_jina = bad_direct, ok_worker, ok_jina
    safety.db.get = lambda k, cast=str: "https://bs.example/fetch" if k == "blockscout_proxy_url" else orig_get(k, cast)
    try:
        d = safety._get("/addresses/0x3")
        assert d["via"] == "worker", d
        assert calls == {"worker": 1, "jina": 0}, calls
        print("PASS Worker dipakai sebelum jina")
    finally:
        safety._get_direct, safety._get_worker, safety._get_jina = (orig_direct, orig_worker,
                                                                    orig_jina)
        safety.db.get = orig_get


def test_parse_envelope_and_markdown():
    env = Resp(200, '{"code":200,"status":20000,"data":{"text":"{\\"a\\":1}"}}')
    assert safety._parse_json(env) == {"a": 1}
    md = Resp(200, "Title: x\n\nURL Source: y\n\nMarkdown Content:\n{\"b\": 2}")
    assert safety._parse_json(md) == {"b": 2}
    print("PASS parse envelope jina + wrapper Markdown Content")


def test_cache_prevents_second_call():
    _reset()
    orig_direct = safety._get_direct
    calls = {"n": 0}

    def ok_direct(url):
        calls["n"] += 1
        return {"n": calls["n"]}

    safety._get_direct = ok_direct
    try:
        a = safety._get("/addresses/0xcache")
        b = safety._get("/addresses/0xcache")
        assert a == b == {"n": 1}, (a, b)
        assert calls["n"] == 1, calls
        print("PASS cache 10 detik: panggilan kedua tidak ke jaringan")
    finally:
        safety._get_direct = orig_direct


if __name__ == "__main__":
    test_parse_envelope_and_markdown()
    test_direct_403_falls_back_to_jina()
    test_direct_cooldown_after_403()
    test_worker_used_before_jina()
    test_cache_prevents_second_call()
    print("\n✅ SEMUA TES EGRESS BLOCKSCOUT LULUS")
