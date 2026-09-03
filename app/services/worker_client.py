"""Client for the deployed Cloudflare Worker that runs the region fan-out.

The Worker (see worker/worker.js) authenticates an X-Probe-Secret header,
then calls 8 region-pinned Durable Objects that each resolve the record via
DNS-over-HTTPS. This module is the only thing that talks to it.
"""

import httpx

from app.config import get_settings

# One pooled client for the process lifetime (standards §8) — a fresh client
# per call would pay a TLS handshake to the Worker each time. The per-request
# timeout is set from Settings here rather than on the pool.
_client: httpx.AsyncClient | None = None


class WorkerError(Exception):
    """The Worker returned a non-2xx status or an unparseable body."""


class WorkerBadRequest(WorkerError):
    """The Worker rejected the request payload (HTTP 4xx that isn't auth) —
    e.g. a hostname that slipped past this app's sanity check but not the
    Worker's. Surfaced to the caller as a 400, not a 502."""


class WorkerTimeout(Exception):
    """The Worker did not respond within request_timeout_seconds."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=settings.request_timeout_seconds,
                write=5.0,
                pool=5.0,
            ),
            headers={"User-Agent": "dns-geo-check/1.0"},
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def probe_all(name: str, record_type: str) -> dict:
    """POST the record to the Worker and return its aggregated JSON:
    {"name": ..., "type": ..., "results": [{region, colo, status, answers,
    cnameChain, latencyMs, error?}, ...]}."""
    settings = get_settings()
    client = _get_client()
    try:
        resp = await client.post(
            settings.worker_url,
            headers={"X-Probe-Secret": settings.probe_secret},
            json={"name": name, "type": record_type},
        )
    except httpx.TimeoutException as exc:
        raise WorkerTimeout(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise WorkerError(f"Could not reach the probe worker: {exc}") from exc

    if resp.status_code == 400:
        detail = ""
        try:
            detail = f" ({resp.json().get('error', '')})"
        except ValueError:
            pass
        raise WorkerBadRequest(f"Probe worker rejected the request{detail}.")
    if resp.status_code >= 400:
        # 401/403 (bad shared secret), 5xx, etc. — our problem, not the caller's.
        raise WorkerError(f"Probe worker returned HTTP {resp.status_code}.")
    try:
        return resp.json()
    except ValueError as exc:
        raise WorkerError("Probe worker returned a non-JSON response.") from exc
