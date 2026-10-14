"""Gunicorn worker that writes the lab's pinned access-log line (standards §7).

uvicorn.workers.UvicornWorker hands HTTP to uvicorn, whose own access logger
ignores gunicorn's `access_log_format` and writes `127.0.0.1:0 - "GET / HTTP/1.0" 200`.
This worker turns uvicorn's access log off and wraps the app in a small ASGI
middleware that writes `%(t)s %(h)s "%(r)s" %(s)s %(b)s %(L)ss` to gunicorn's
access log instead (same file, same reopen-on-USR1 handling):

    [14/Oct/2026:09:12:03 -0500] 203.0.113.7 "GET /app/ HTTP/1.1" 200 5123 0.004211s

Use it with `worker_class = "app.access_log.AccessLogUvicornWorker"`.
This file is identical across lab apps; copy it, don't fork it.
"""
import logging
import time

from uvicorn.workers import UvicornWorker


class AccessLogMiddleware:
    def __init__(self, app, logger: logging.Logger) -> None:
        self.app = app
        self.logger = logger

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        start = time.perf_counter()
        status = 500
        sent = 0

        async def send_and_count(message):
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_and_count)
        finally:
            self.logger.info(_line(scope, status, sent, time.perf_counter() - start))


def _client_ip(scope) -> str:
    # Same rule as each app's rate limiter: Cloudflare's header, else the peer.
    for name, value in scope.get("headers", ()):
        if name == b"cf-connecting-ip":
            return value.decode("latin-1").strip()
    client = scope.get("client")
    return client[0] if client else "-"


def _line(scope, status: int, sent: int, seconds: float) -> str:
    path = scope.get("raw_path") or scope.get("path", "").encode()
    target = path.decode("latin-1")
    if scope.get("query_string"):
        target += "?" + scope["query_string"].decode("latin-1")
    request = f'{scope["method"]} {target} HTTP/{scope.get("http_version", "1.1")}'
    return (
        f"{time.strftime('[%d/%b/%Y:%H:%M:%S %z]')} {_client_ip(scope)} "
        f'"{request}" {status} {sent or "-"} {seconds:.6f}s'
    )


class AccessLogUvicornWorker(UvicornWorker):
    CONFIG_KWARGS = {**UvicornWorker.CONFIG_KWARGS, "access_log": False}

    def load_wsgi(self) -> None:
        super().load_wsgi()
        self.wsgi = AccessLogMiddleware(self.wsgi, self.log.access_log)
