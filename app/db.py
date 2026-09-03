import logging
from pathlib import Path

import aiosqlite

from app.config import get_settings

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


async def connect(db_path: str | None = None) -> aiosqlite.Connection:
    db_path = db_path or get_settings().db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def init_db(db_path: str | None = None) -> None:
    conn = await connect(db_path)
    try:
        schema = _SCHEMA_PATH.read_text()
        await conn.executescript(schema)
        await conn.commit()
        logger.info("Database schema applied")
    finally:
        await conn.close()


async def get_db():
    conn = await connect()
    try:
        yield conn
    finally:
        await conn.close()


async def check_and_record_rate_limit(
    conn: aiosqlite.Connection,
    *,
    ip: str,
    route: str,
    limit: int,
    window_seconds: int,
) -> bool:
    """Record a hit for (ip, route) and return whether it's within `limit`
    hits in the trailing `window_seconds`. Also prunes hits for this route
    older than the window, so the table doesn't grow unbounded."""
    offset = f"-{window_seconds} seconds"
    await conn.execute(
        "DELETE FROM rate_limit_hits WHERE route = ?"
        " AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
        (route, offset),
    )
    cur = await conn.execute(
        "SELECT COUNT(*) FROM rate_limit_hits WHERE route = ? AND ip = ?"
        " AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
        (route, ip, offset),
    )
    row = await cur.fetchone()
    if row[0] >= limit:
        await conn.commit()
        return False
    await conn.execute(
        "INSERT INTO rate_limit_hits (ip, route) VALUES (?, ?)", (ip, route)
    )
    await conn.commit()
    return True
