"""Unit tests for app.services.worker_client — the only module that talks to
the deployed Cloudflare Worker. Exercises the response → exception mapping
without any network (httpx.MockTransport)."""

import httpx
import pytest

from app.services import worker_client
from app.services.worker_client import (
    WorkerBadRequest,
    WorkerError,
    WorkerTimeout,
    probe_all,
)


@pytest.fixture(autouse=True)
def _worker_env(monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://worker.test/")
    monkeypatch.setenv("PROBE_SECRET", "test-secret")
    yield
    worker_client._client = None


def _install(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(worker_client, "_client", client)


async def test_returns_parsed_json_on_200(monkeypatch):
    captured = {}

    def handler(request):
        captured["secret"] = request.headers.get("x-probe-secret")
        captured["body"] = request.content
        return httpx.Response(200, json={"name": "x", "type": "A", "results": []})

    _install(monkeypatch, handler)
    out = await probe_all("lab.kudithipudi.org", "A")
    assert out == {"name": "x", "type": "A", "results": []}
    assert captured["secret"] == "test-secret"
    assert b"lab.kudithipudi.org" in captured["body"]


async def test_400_becomes_worker_bad_request(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(400, json={"error": "invalid name"}))
    with pytest.raises(WorkerBadRequest) as exc:
        await probe_all("bad_host", "A")
    assert "invalid name" in str(exc.value)


async def test_401_becomes_worker_error_not_bad_request(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(WorkerError) as exc:
        await probe_all("lab.kudithipudi.org", "A")
    assert not isinstance(exc.value, WorkerBadRequest)


async def test_500_becomes_worker_error(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(WorkerError):
        await probe_all("lab.kudithipudi.org", "A")


async def test_non_json_200_becomes_worker_error(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(WorkerError):
        await probe_all("lab.kudithipudi.org", "A")


async def test_timeout_becomes_worker_timeout(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    _install(monkeypatch, handler)
    with pytest.raises(WorkerTimeout):
        await probe_all("lab.kudithipudi.org", "A")


async def test_connect_error_becomes_worker_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    _install(monkeypatch, handler)
    with pytest.raises(WorkerError):
        await probe_all("lab.kudithipudi.org", "A")
