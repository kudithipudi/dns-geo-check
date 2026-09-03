from app.services.worker_client import WorkerError, WorkerTimeout

_FAKE_RESULTS = {
    "name": "lab.kudithipudi.org",
    "type": "A",
    "results": [
        {
            "region": "wnam",
            "colo": "SJC",
            "status": 0,
            "answers": [{"name": "lab.kudithipudi.org", "type": "A", "ttl": 300, "data": "203.0.113.10"}],
            "cnameChain": [],
            "latencyMs": 24,
        },
        {
            "region": "afr",
            "colo": "JNB",
            "status": 0,
            "answers": [],
            "cnameChain": [],
            "latencyMs": None,
            "error": "timeout",
        },
    ],
}


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_index_redirects_to_net_tools(client):
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://lab.kudithipudi.org/net-tools"


async def test_check_rejects_ip_address(client):
    resp = await client.post("/check", json={"name": "8.8.8.8", "type": "A"})
    assert resp.status_code == 400


async def test_check_rejects_bare_word(client):
    resp = await client.post("/check", json={"name": "localhost", "type": "A"})
    assert resp.status_code == 400


async def test_check_rejects_bad_type(client):
    resp = await client.post("/check", json={"name": "lab.kudithipudi.org", "type": "MX"})
    assert resp.status_code == 422


async def test_check_returns_partial_results(client, monkeypatch):
    async def fake_probe_all(name, record_type):
        return _FAKE_RESULTS

    monkeypatch.setattr("app.main.probe_all", fake_probe_all)

    resp = await client.post("/check", json={"name": "lab.kudithipudi.org", "type": "A"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "lab.kudithipudi.org"
    assert len(body["results"]) == 2
    assert body["results"][0]["colo"] == "SJC"
    assert body["results"][0]["answers"][0]["data"] == "203.0.113.10"
    assert body["results"][1]["error"] == "timeout"


async def test_check_worker_error_is_502(client, monkeypatch):
    async def boom(name, record_type):
        raise WorkerError("nope")

    monkeypatch.setattr("app.main.probe_all", boom)
    resp = await client.post("/check", json={"name": "lab.kudithipudi.org", "type": "A"})
    assert resp.status_code == 502


async def test_check_worker_timeout_is_504(client, monkeypatch):
    async def slow(name, record_type):
        raise WorkerTimeout("slow")

    monkeypatch.setattr("app.main.probe_all", slow)
    resp = await client.post("/check", json={"name": "lab.kudithipudi.org", "type": "A"})
    assert resp.status_code == 504


async def test_check_rate_limited(client, monkeypatch):
    async def fake_probe_all(name, record_type):
        return _FAKE_RESULTS

    monkeypatch.setattr("app.main.probe_all", fake_probe_all)
    monkeypatch.setattr("app.main.settings.rate_limit_per_minute", 1)

    first = await client.post("/check", json={"name": "lab.kudithipudi.org", "type": "A"})
    assert first.status_code == 200
    second = await client.post("/check", json={"name": "lab.kudithipudi.org", "type": "A"})
    assert second.status_code == 429


async def test_check_503_when_worker_unconfigured(client, monkeypatch):
    monkeypatch.setattr("app.main.settings.worker_url", "")
    resp = await client.post("/check", json={"name": "lab.kudithipudi.org", "type": "A"})
    assert resp.status_code == 503
