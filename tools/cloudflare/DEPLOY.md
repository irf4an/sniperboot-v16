# Deploy Worker Proxy Blockscout — Panduan

Tujuan: request ke Blockscout keluar lewat jaringan Cloudflare, bukan dari IP
VPS. Ini menghilangkan `403 cf-mitigated: challenge` yang sekarang memaksa bot
memakai jalur cadangan (~0,7 s per panggilan).

Hasil akhir: satu URL `https://<nama>.<subdomain>.workers.dev` yang dipakai bot
sebagai setting `blockscout_proxy_url`.

---

## Opsi A — Dashboard (tanpa terminal, ±3 menit)

1. Buka https://dash.cloudflare.com → login akun pemilik `ahmdirfnnna.my.id`.
2. Menu kiri: **Workers & Pages** → tombol **Create application**.
3. Pilih **Create Worker** (bukan Pages).
4. Beri nama, misal `bs-proxy` → **Deploy**.
5. Klik **Edit code** (atau **</> Edit code**).
6. Hapus semua isi editor, lalu **paste seluruh isi file `bs_proxy_worker.js`**.
7. Klik **Deploy** (kanan atas) → tunggu "Success".
8. Catat URL yang muncul, bentuknya:
   `https://bs-proxy.<subdomain-lu>.workers.dev`

### Tes cepat (buka di browser)

- `https://bs-proxy.<subdomain>.workers.dev/health`
  → harus muncul `{"ok":true,"service":"bs-proxy","version":"1.0",...}`
- `https://bs-proxy.<subdomain>.workers.dev/?url=https%3A%2F%2Frobinhoodchain.blockscout.com%2Fapi%2Fv2%2Fstats`
  → harus muncul JSON statistik chain (`average_block_time`, `coin_price`, dst)

Kalau dua tes itu benar, kirim URL dasar (tanpa `/?url=`) ke saya.

---

## Opsi B — Terminal / wrangler

Butuh **Cloudflare API token**:

- **Account → Workers Scripts → Edit**
- **Account → Account Settings → Read**

```bash
export CLOUDFLARE_API_TOKEN="<token>"
export CLOUDFLARE_ACCOUNT_ID="<account-id>"

mkdir -p ~/bs-proxy && cd ~/bs-proxy
cp /path/bs_proxy_worker.js index.js
npx wrangler@latest deploy index.js --name bs-proxy --compatibility-date 2026-01-01
```

Wrangler akan mencetak URL `https://bs-proxy.<subdomain>.workers.dev`.

Verifikasi:

```bash
curl -s "https://bs-proxy.<subdomain>.workers.dev/health"
curl -s "https://bs-proxy.<subdomain>.workers.dev/?url=https%3A%2F%2Frobinhoodchain.blockscout.com%2Fapi%2Fv2%2Fstats" | head -c 200
```

---

## Yang saya lakukan setelah dapat URL

1. Set setting bot:
   `blockscout_proxy_url = https://bs-proxy.<subdomain>.workers.dev`
2. Bot otomatis memakai urutan: **direct → Worker → jina.ai**.
3. Ukur latensi sebelum/sesudah, lalu laporkan angkanya.
4. Jaring pengaman jina tetap ada, jadi kalau Worker mati bot tidak buta lagi.

---

## Catatan keamanan

- Worker hanya meneruskan ke `robinhoodchain.blockscout.com` (allowlist host).
  URL selain itu ditolak 403 — tidak bisa dipakai jadi open proxy.
- Tidak ada token/rahasia di dalam Worker. Semua endpoint yang lewat adalah
  data publik on-chain.
- Worker tidak menyimpan data; cache hanya 15 detik di edge Cloudflare.
- Kalau mau pakai domain sendiri (`bs.ahmdirfnnna.my.id`), tambahkan
  **Workers Route** di zona itu → arahkan ke Worker `bs-proxy`. Opsional,
  `workers.dev` sudah cukup.
