# dns-geo-check

Resolves a DNS record (`A` / `AAAA`) for a hostname from **8 geographic
vantage points** and reports, per region: the Cloudflare colo that actually
ran the lookup, the resolved IP(s), the CNAME chain, TTLs, and the query
latency.

This exists as the backend for the "DNS Geo Check" card on
[net-tools](https://lab.kudithipudi.org/net-tools) — net-tools is a static,
no-backend page, so the fan-out and the Cloudflare API token have to live
server-side.

## What it is

Two pieces:

- **`worker/`** — a Cloudflare Worker + a `DnsProbe` Durable Object, deployed
  to Cloudflare (not to this box). The Worker authenticates an
  `X-Probe-Secret` header, then fans out with `Promise.allSettled` to 8
  Durable Objects, each pinned to a region via `locationHint`
  (`wnam enam weur eeur apac-se oc afr sam`). Each DO resolves the record via
  DNS-over-HTTPS against `cloudflare-dns.com` from wherever it landed, times
  the query, and reads the serving colo from the response `cf-ray` header.
- **`app/`** — a small FastAPI service proxied at `/dns-geo-check/`. It
  rate-limits per IP, does a lightweight hostname sanity check, calls the
  Worker with the shared secret, and returns the aggregated JSON. Partial
  results are a `200` — one region timing out does not fail the whole call.

`POST /check {"name": "lab.kudithipudi.org", "type": "A"}` →
```json
{
  "name": "lab.kudithipudi.org",
  "type": "A",
  "results": [
    { "region": "wnam", "colo": "SJC", "status": 0,
      "answers": [{ "name": "lab.kudithipudi.org", "type": "A", "ttl": 300, "data": "..." }],
      "cnameChain": [], "latencyMs": 24 },
    { "region": "afr", "colo": "JNB", "latencyMs": null, "error": "timeout" }
  ]
}
```

**Stateless.** The only thing stored anywhere is an ephemeral per-IP
rate-limit counter (`db/schema.sql`), pruned continuously.

### Why not a per-city sandbox?

Cloudflare Sandboxes/Containers can't run `traceroute` (egress is TCP 80/443
+ DNS only), can't target a city, and need Workers Paid. Durable Objects can
be pinned to ~11 broad regions via `locationHint` and run on the **Workers
Free plan**, so that's what this uses.

**Caveat:** `locationHint` is best-effort ("a data center selected to
minimize latency from the hinted location"), and when a Worker fetches
`cloudflare-dns.com` the request is served by a nearby colo. So each result
reflects *"what 1.1.1.1 returns, resolved from `<colo>`"* — **not** "what a
user in `<country>` sees" (1.1.1.1 does not send EDNS Client Subnet).

**Geo-spread finding (2026-09-03):** 8 regions consistently resolve to
6–7 distinct colos. Placement is best-effort *and slightly fluid* — a
hibernated DO can wake in a different in-region colo — so exact colos vary
between runs and between `PROBE_GEN` generations. Representative:
`wnam`→DFW/SJC/MIA, `enam`→EWR/IAD, `weur`→AMS/MAD/MRS, `eeur`→ARN/WAW,
`apac-se`→HKG/SIN, `oc`→AKL/MEL/BNE, `afr`→MAD/CDG/AMS, and **`sam`→EWR on
every generation tried (v1–v3)** — Cloudflare treats Newark as the
lowest-latency colo for the `sam` hint (much South-American transit peers in
the NY/Miami area), and there is no finer-grained South America hint.

The per-region view is still meaningful: regional DNS steering resolves
correctly — `netflix.com` returns AWS `us-west-2` addresses from `oc` vs
`us-east-1` from `wnam`, and `www.microsoft.com`'s Akamai edge IP differs per
colo. The "region" column is the requested hint; the "colo" column is where
the lookup actually ran.

## Stack

Python 3.12 · FastAPI · gunicorn (uvicorn worker) · httpx · SQLite
(aiosqlite, rate-limit table only). Cloudflare Worker: plain JS module worker
+ SQLite-backed Durable Object, `wrangler`. Per the lab standards in
`/var/www/plans/standards.md`. No Jinja/Tailwind UI — a pure JSON API
consumed by net-tools.

## Run locally

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env          # then set WORKER_URL + PROBE_SECRET
venv/bin/uvicorn app.main:app  # http://127.0.0.1:8000
```

Tests (the Worker call is mocked — no network):

```bash
venv/bin/python -m pytest
```

## Deploy

### 1. The Cloudflare Worker

Needs a Cloudflare account on the (free) Workers plan, an API token with
**Workers Scripts: Edit** + **Workers Durable Objects** permissions, and the
account ID.

```bash
cd worker
export CLOUDFLARE_API_TOKEN=…  CLOUDFLARE_ACCOUNT_ID=…
npx wrangler@4.128.0 deploy
printf %s "$PROBE_SECRET" | npx wrangler@4.128.0 secret put PROBE_SECRET
npx wrangler@4.128.0 deployments list   # confirm the version
```

Note the printed `https://dns-geo-check.<subdomain>.workers.dev` URL.

Sanity checks:

```bash
# 401 without the secret
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://dns-geo-check.<sub>.workers.dev/ \
  -H 'content-type: application/json' -d '{"name":"lab.kudithipudi.org","type":"A"}'
# 200 + 8 results with it
curl -s -X POST https://dns-geo-check.<sub>.workers.dev/ \
  -H "X-Probe-Secret: $PROBE_SECRET" -H 'content-type: application/json' \
  -d '{"name":"www.cloudflare.com","type":"A"}' | jq '.results[]|{region,colo,latencyMs}'
```

### 2. The FastAPI service

Behind nginx, unix socket, gunicorn, systemd (`dns-geo-check.service`, checked
into the repo root, `User=www-data`):

```bash
sudo cp dns-geo-check.service /etc/systemd/system/dns-geo-check.service
sudo chgrp www-data /var/www/dns-geo-check && sudo chmod g+w /var/www/dns-geo-check
sudo chown -R www-data:www-data /var/www/dns-geo-check/data /var/www/dns-geo-check/app/logs
sudo systemctl daemon-reload
sudo systemctl enable --now dns-geo-check
```

`.env` (chmod 600, `www-data`) must have real `WORKER_URL` and `PROBE_SECRET`
(the latter identical to the Worker secret).

nginx location block (see the lab vhost):

```nginx
location /dns-geo-check/ {
    include proxy_params;
    proxy_read_timeout 45s;
    proxy_connect_timeout 10s;
    rewrite ^/dns-geo-check(/.*)$ $1 break;
    proxy_pass http://unix:/var/www/dns-geo-check/dns-geo-check.sock;
}
```

## Env vars

| Var | Default | Purpose |
| --- | --- | --- |
| `DB_PATH` | `data/dns-geo-check.db` | SQLite location (rate-limit table only). |
| `RATE_LIMIT_PER_MINUTE` | `20` | Per-IP cap on `POST /check` over the trailing window. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Trailing window (seconds) the cap above applies over. |
| `WORKER_URL` | *(blank)* | The deployed probe Worker URL. `/check` → 503 until set. |
| `PROBE_SECRET` | *(blank)* | Shared secret sent as `X-Probe-Secret`; matches the Worker secret. |
| `REQUEST_TIMEOUT_SECONDS` | `20.0` | Outer timeout on the Worker's aggregated response. |
| `LOG_LEVEL` | `info` | App log verbosity (`debug`/`info`/`warning`/...). |

## Logs

Local files under `app/logs/`, not journald: `access.log` (one line per
request) and `app.log` (boot output + app logging, incl. one
`dns-geo <name> <type> regions=8 ok=… …` line per check). Rotation via the
host-level `/etc/logrotate.d/lab-apps` policy.

## Security notes

- **Auth to the Worker**: the Worker is public but rejects any request
  without the `X-Probe-Secret` shared secret *before* touching a Durable
  Object, so it can't be used as a free DNS fan-out by anyone else.
- **No SSRF surface**: resolution happens at Cloudflare, not from this box —
  there is no outbound fetch to the target — so the hostname check is only a
  lightweight sanity filter, not a security boundary.
- **Reflected content**: resolved IPs / colo strings in the response
  originate from DNS and Cloudflare, but the net-tools card still renders
  every response-derived value as text (`escapeHTML`), never as markup.
- **Abuse / cost control**: `POST /check` is capped per IP
  (`RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_WINDOW_SECONDS`, default 20/60s),
  recorded in SQLite so the limit holds across gunicorn workers.
