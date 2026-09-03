import ipaddress
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.db import check_and_record_rate_limit, connect, init_db
from app.models import CheckRequest, CheckResponse, RegionResult
from app.services import worker_client
from app.services.worker_client import (
    WorkerBadRequest,
    WorkerError,
    WorkerTimeout,
    probe_all,
)

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if not settings.worker_url or not settings.probe_secret:
        logger.warning(
            "WORKER_URL / PROBE_SECRET not set — POST /check will return 503 "
            "until both are configured in .env"
        )
    logger.info("Startup complete")
    yield
    await worker_client.close_client()


# Ensure the SQLite parent dir exists before the lifespan runs.
Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="dns-geo-check", lifespan=lifespan)


@app.middleware("http")
async def _no_store(request: Request, call_next):
    """Cloudflare fronts this deploy and edge-caches responses with no origin
    cache header. Every response here is a fresh probe or a health check —
    never serve a stale one."""
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()  # first hop = original client
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


# One DNS label: alphanumeric ends, hyphens only in the interior (RFC 1123).
# Kept in step with the Worker's own NAME_RE (worker/worker.js) so anything
# this app accepts, the Worker also accepts — a mismatch would turn a
# borderline hostname into a confusing 502 instead of a clean 400.
_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _validate_hostname(raw: str) -> str:
    """Lightweight check that `raw` is a plausible public hostname. Resolution
    happens at Cloudflare, not from this box, so there is no SSRF surface —
    this only keeps obvious junk out of the DoH query."""
    name = raw.strip().rstrip(".").lower()
    if not name or len(name) > 253:
        raise ValueError("Enter a hostname to look up.")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise ValueError("Enter a hostname, not an IP address.")
    if "." not in name or name == "localhost" or name.endswith(".local"):
        raise ValueError("Enter a fully-qualified public hostname (e.g. lab.kudithipudi.org).")
    labels = name.split(".")
    if not all(_LABEL_RE.match(label) for label in labels):
        raise ValueError("That doesn't look like a valid hostname.")
    return name


@app.post("/check", response_model=CheckResponse)
async def check(payload: CheckRequest, request: Request) -> CheckResponse:
    conn = await connect()
    try:
        allowed = await check_and_record_rate_limit(
            conn,
            ip=_client_ip(request),
            route="check",
            limit=settings.rate_limit_per_minute,
            window_seconds=settings.rate_limit_window_seconds,
        )
    finally:
        await conn.close()
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests — please slow down and try again in a minute.",
        )

    try:
        name = _validate_hostname(payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not settings.worker_url or not settings.probe_secret:
        raise HTTPException(
            status_code=503,
            detail="The probe worker is not configured yet.",
        )

    started = time.monotonic()
    try:
        raw = await probe_all(name, payload.type)
    except WorkerTimeout as exc:
        logger.error("probe worker timeout for %s %s: %s", name, payload.type, exc)
        raise HTTPException(status_code=504, detail="The probe timed out.") from exc
    except WorkerBadRequest as exc:
        logger.warning("probe worker rejected %s %s: %s", name, payload.type, exc)
        raise HTTPException(
            status_code=400, detail="That hostname was rejected by the resolver."
        ) from exc
    except WorkerError as exc:
        logger.error("probe worker error for %s %s: %s", name, payload.type, exc)
        raise HTTPException(status_code=502, detail="The probe worker failed.") from exc

    results = [RegionResult(**r) for r in raw.get("results", [])]
    ok = sum(1 for r in results if not r.error)
    colos = ",".join(sorted({r.colo for r in results if r.colo}))
    logger.info(
        "dns-geo %s %s regions=%d ok=%d err=%d %.1fs colos=%s",
        name, payload.type, len(results), ok, len(results) - ok,
        time.monotonic() - started, colos or "-",
    )
    return CheckResponse(name=name, type=payload.type, results=results)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def index():
    # Pure JSON API with no UI of its own — it's consumed by the "DNS Geo
    # Check" card on net-tools. A visitor landing here directly gets sent
    # to where the feature actually lives.
    return RedirectResponse("https://lab.kudithipudi.org/net-tools", status_code=302)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
