"""v16.5 Solusi B: verifikasi setting default baru + migrasi DB lama.

1) config.SETTING_DEFAULTS: buy_slippage 0.25, scanner_interval_sec 5.
2) db.init() baru mengisi DB dengan nilai default baru.
3) DB lama: scanner_interval_sec 15 → 5 dimigrasi otomatis. buy_slippage
   TIDAK dimigrasi — mengubah toleransi slippage berarti mengubah risiko
   modal, jadi harus lewat keputusan operator di panel Settings.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpdb.close()
os.environ["DB_PATH"] = _tmpdb.name

from sniper import config, db          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


db.init()
check("default config buy_slippage == '0.25'",
      config.SETTING_DEFAULTS.get("buy_slippage") == "0.25",
      config.SETTING_DEFAULTS.get("buy_slippage"))
check("default config scanner_interval_sec == '5'",
      config.SETTING_DEFAULTS.get("scanner_interval_sec") == "5",
      config.SETTING_DEFAULTS.get("scanner_interval_sec"))
check("DB baru buy_slippage = 0.25", db.get("buy_slippage") == 0.25,
      str(db.get("buy_slippage")))
check("DB baru scanner_interval_sec = 5",
      db.get("scanner_interval_sec") == 5,
      str(db.get("scanner_interval_sec")))
check("DB scanner_mcap_max default = 45000",
      db.get("scanner_mcap_max") == 45000,
      str(db.get("scanner_mcap_max")))

# DB lama: buy_slippage SENGAJA tidak dimigrasi otomatis.
# Toleransi slippage = risiko modal, jadi perubahan 0.60 → 0.25 harus
# lewat keputusan operator (panel Settings), bukan efek samping restart.
db.set_("buy_slippage", "0.30")
db.set_("scanner_interval_sec", "15")
db.init()
check("buy_slippage DB lama TIDAK disentuh migrasi (0.30 tetap)",
      db.get("buy_slippage") == 0.30, str(db.get("buy_slippage")))
check("migrasi DB lama 15 → 5", db.get("scanner_interval_sec") == 5,
      str(db.get("scanner_interval_sec")))

# setting custom user TIDAK boleh ditimpa
db.set_("buy_slippage", "0.40")
db.init()
check("setting custom user 0.40 tidak ditimpa", db.get("buy_slippage") == 0.40,
      str(db.get("buy_slippage")))

print()
print(f"TOTAL: {len(PASS)} OK, {len(FAIL)} FAIL")
if FAIL:
    sys.exit(1)
print("SEMUA TES LULUS.")
