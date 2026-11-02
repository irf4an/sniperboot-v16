"""Format angka & alamat untuk notifikasi — satu sumber kebenaran.

Semua modul (listener, tracker, control_bot, pons_radar) memakai helper di
sini supaya gaya angka tidak berbeda antar-kanal. Fungsi murni: tanpa I/O,
aman dipanggil dari mana pun.
"""


def usd_compact(v, fallback="—") -> str:
    """$812 / $3.1k / $52k (tanpa .0) / $1.2M — `fallback` bila tak valid."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return fallback
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1000:
        return f"${v / 1000:.1f}k".replace(".0k", "k")
    return f"${v:,.0f}"


def liq(v) -> str:
    """Baris likuiditas; pool v4 tanpa data Dexscreener → 'liq n/a (v4 on-chain)'."""
    return "liq n/a (v4 on-chain)" if v is None else f"liq ${v:,.0f}"


def short_addr(addr, label="", labels=None) -> str:
    """Label DB (`labels[addr]`) → label operator → 0xabc…def."""
    a = (addr or "").lower()
    if labels and a in labels:
        return labels[a]
    if label and str(label).strip():
        return str(label).strip()
    return f"{a[:6]}…{a[-4:]}" if len(a) >= 10 else (a or "?")
