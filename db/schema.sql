-- Canonical schema for dns-geo-check.
-- Applied idempotently on startup via app/db.py (CREATE TABLE IF NOT EXISTS).

-- One row per rate-limited request, used to throttle abuse of POST /check
-- (each call fans out to 8 Cloudflare Durable Objects, so it's worth capping).
-- Hits older than the limiting window are pruned as new ones are recorded,
-- so this stays small. A plain table (rather than an in-process counter) so
-- the limit is enforced consistently across all gunicorn workers, which
-- don't share memory. No other data is stored anywhere in this app — every
-- check is stateless.
CREATE TABLE IF NOT EXISTS rate_limit_hits (
    ip TEXT NOT NULL,
    route TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_route_ip_time ON rate_limit_hits (route, ip, created_at);
