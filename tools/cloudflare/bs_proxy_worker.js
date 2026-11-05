/**
 * Cloudflare Worker — proxy JSON Blockscout (Robinhood Chain).
 *
 * Kenapa ada: Cloudflare memberi "managed challenge" ke IP datacenter VPS
 * (header cf-mitigated: challenge), jadi bot selalu kena 403. Worker ini
 * mengambil data lewat egress jaringan Cloudflare, bukan dari IP VPS.
 *
 * Pakai:
 *   GET /?url=https%3A%2F%2Frobinhoodchain.blockscout.com%2Fapi%2Fv2%2F...
 *   GET /api/v2/tokens/0xabc.../holders        (jalur pendek, host dikunci)
 *
 * Allowlist host ketat: Worker hanya mau meneruskan ke Blockscout Robinhood.
 * Tidak ada data rahasia yang lewat — semua endpoint ini data publik on-chain.
 */

const ALLOWED_HOSTS = new Set([
  "robinhoodchain.blockscout.com",
]);

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,OPTIONS",
  "access-control-allow-headers": "content-type",
};

const CACHE_TTL = 15; // detik — cukup menyerap burst, masih segar untuk sniper

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== "GET") {
      return json({ error: "method not allowed" }, 405);
    }

    const u = new URL(request.url);
    let target;

    const raw = u.searchParams.get("url");
    if (raw) {
      try {
        target = new URL(raw);
      } catch {
        return json({ error: "url tidak valid" }, 400);
      }
    } else {
      // jalur pendek: /api/v2/... → host Blockscout
      if (!u.pathname.startsWith("/api/")) {
        return json({ error: "path harus /api/... atau pakai ?url=" }, 400);
      }
      target = new URL("https://robinhoodchain.blockscout.com" + u.pathname + u.search);
      u.searchParams.forEach((v, k) => {
        if (k !== "url") target.searchParams.set(k, v);
      });
    }

    if (!ALLOWED_HOSTS.has(target.hostname)) {
      return json({ error: "host tidak diizinkan" }, 403);
    }

    const cache = caches.default;
    const cacheKey = new Request(target.toString(), { method: "GET" });

    let resp = await cache.match(cacheKey);
    if (!resp) {
      const upstream = await fetch(target.toString(), {
        method: "GET",
        headers: {
          accept: "application/json",
          "user-agent": "hoodsniper-worker/1.0",
        },
        cf: { cacheTtl: CACHE_TTL, cacheEverything: true },
      });
      const body = await upstream.text();
      resp = new Response(body, {
        status: upstream.status,
        headers: {
          ...CORS,
          "content-type": upstream.headers.get("content-type") || "application/json",
          "x-proxy-status": String(upstream.status),
        },
      });
      if (upstream.ok) {
        resp.headers.set("cache-control", `public, max-age=${CACHE_TTL}`);
        await cache.put(cacheKey, resp.clone());
      }
    }
    return resp;
  },
};

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...CORS, "content-type": "application/json" },
  });
}
