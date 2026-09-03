/**
 * DNS Geo Check — Cloudflare Worker + Durable Object.
 *
 * The `fetch` handler authenticates an `X-Probe-Secret` header, then fans out
 * to 8 region-pinned Durable Objects. Each DO resolves the requested record
 * via DNS-over-HTTPS (1.1.1.1) from wherever `locationHint` placed it, times
 * the query, and reports the Cloudflare colo that actually served it.
 *
 * Backs the "DNS Geo Check" card on https://lab.kudithipudi.org/client-info,
 * called server-side by /var/www/dns-geo-check (never from the browser).
 *
 * Deploy:  CLOUDFLARE_API_TOKEN=… CLOUDFLARE_ACCOUNT_ID=… npx wrangler deploy
 * Secret:  printf %s "$SECRET" | npx wrangler secret put PROBE_SECRET
 * Logs:    npx wrangler tail
 */

import { DurableObject } from "cloudflare:workers";

const REGIONS = ["wnam", "enam", "weur", "eeur", "apac-se", "oc", "afr", "sam"];
const VALID_TYPES = new Set(["A", "AAAA"]);
const TYPE_NAMES = { 1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV", 257: "CAA" };
const NAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/i;

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stripDot(s) {
  return typeof s === "string" && s.endsWith(".") ? s.slice(0, -1) : s;
}

function coloFromRay(ray) {
  if (!ray) return null;
  const i = ray.lastIndexOf("-");
  return i === -1 ? null : ray.slice(i + 1);
}

export default {
  async fetch(req, env) {
    if (req.method !== "POST") return json({ error: "method not allowed" }, 405);
    if (!env.PROBE_SECRET || (req.headers.get("x-probe-secret") || "") !== env.PROBE_SECRET) {
      return json({ error: "unauthorized" }, 401);
    }

    let body;
    try {
      body = await req.json();
    } catch {
      return json({ error: "invalid JSON body" }, 400);
    }
    const name = typeof body?.name === "string" ? body.name.trim().replace(/\.$/, "") : "";
    const type = typeof body?.type === "string" ? body.type.toUpperCase() : "A";
    if (!name || name.length > 253 || !NAME_RE.test(name)) {
      return json({ error: "invalid name" }, 400);
    }
    if (!VALID_TYPES.has(type)) {
      return json({ error: "type must be A or AAAA" }, 400);
    }

    const settled = await Promise.allSettled(
      REGIONS.map((region) => {
        const id = env.DNS_PROBE.idFromName("probe-v1-" + region);
        const stub = env.DNS_PROBE.get(id, { locationHint: region });
        return stub.probe(name, type);
      })
    );

    const results = settled.map((s, i) =>
      s.status === "fulfilled"
        ? { region: REGIONS[i], ...s.value }
        : { region: REGIONS[i], error: String((s.reason && s.reason.message) || s.reason) }
    );

    return json({ name, type, results });
  },
};

export class DnsProbe extends DurableObject {
  /** Resolve `name`/`type` via DoH from this DO's location; return a plain
   *  object (RPC-serialisable). Never throws — failures come back as `error`. */
  async probe(name, type) {
    const url =
      "https://cloudflare-dns.com/dns-query?name=" +
      encodeURIComponent(name) +
      "&type=" +
      type;

    const t0 = Date.now();
    let resp;
    try {
      resp = await fetch(url, {
        headers: { accept: "application/dns-json" },
        signal: AbortSignal.timeout(10000),
      });
    } catch (e) {
      return {
        colo: await this._traceColo(),
        error: e && e.name === "TimeoutError" ? "timeout" : String(e),
      };
    }
    const latencyMs = Date.now() - t0;
    const colo = coloFromRay(resp.headers.get("cf-ray")) || (await this._traceColo());

    if (!resp.ok) {
      return { colo, latencyMs, error: "DoH HTTP " + resp.status };
    }

    let data;
    try {
      data = await resp.json();
    } catch {
      return { colo, latencyMs, error: "DoH returned a non-JSON body" };
    }

    const answerArr = Array.isArray(data.Answer) ? data.Answer : [];
    const cnameChain = answerArr
      .filter((a) => a.type === 5)
      .map((a) => stripDot(a.data));
    const answers = answerArr
      .filter((a) => a.type === 1 || a.type === 28)
      .map((a) => ({
        name: stripDot(a.name),
        type: TYPE_NAMES[a.type] || String(a.type),
        ttl: typeof a.TTL === "number" ? a.TTL : null,
        data: String(a.data),
      }));

    return {
      colo,
      status: typeof data.Status === "number" ? data.Status : null,
      answers,
      cnameChain,
      latencyMs,
    };
  }

  /** Fallback colo lookup when the DoH response carried no cf-ray header. */
  async _traceColo() {
    try {
      const r = await fetch("https://one.one.one.one/cdn-cgi/trace", {
        signal: AbortSignal.timeout(3000),
      });
      const m = (await r.text()).match(/^colo=(.+)$/m);
      return m ? m[1] : null;
    } catch {
      return null;
    }
  }
}
