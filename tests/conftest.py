import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.main as main
from app.db import init_db


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """An AsyncClient wired to a throwaway SQLite db, so tests never touch a
    shared rate-limit table. Worker config is stubbed so /check reaches the
    probe path (individual tests monkeypatch probe_all)."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app-test.db"))
    monkeypatch.setattr(main.settings, "worker_url", "https://worker.test/")
    monkeypatch.setattr(main.settings, "probe_secret", "test-secret")
    # ASGITransport doesn't run lifespan hooks, so apply the schema here.
    await init_db(str(tmp_path / "app-test.db"))
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c
