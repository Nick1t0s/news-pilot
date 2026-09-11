from __future__ import annotations

import logging
from pathlib import Path

import asyncpg
import pgvector.asyncpg

from app.config import project_root

log = logging.getLogger("db")

_SCHEMA_FILE = project_root() / "app" / "db" / "schema.sql"


def normalize_dsn(dsn: str) -> str:
    """Accepts both `postgresql://...` and SQLAlchemy-style `postgresql+asyncpg://...`."""
    return dsn.replace("+asyncpg", "")


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    target = normalize_dsn(dsn)
    conn = await asyncpg.connect(target)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()
    pool = await asyncpg.create_pool(
        target,
        min_size=min_size,
        max_size=max_size,
        init=pgvector.asyncpg.register_vector,
    )
    log.info("db pool created")
    return pool


async def init_schema(pool: asyncpg.Pool, dimensions: int) -> None:
    ddl = _schema_ddl(dimensions)
    async with pool.acquire() as conn:
        await conn.execute(ddl)
    log.info("schema ensured (vector dimensions=%d)", dimensions)


def _schema_ddl(dimensions: int) -> str:
    file: Path = project_root() / "app" / "db" / "schema.sql"
    return file.read_text(encoding="utf-8").replace("__DIM__", str(dimensions))
