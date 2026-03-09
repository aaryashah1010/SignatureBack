"""
SQL Server client – runs synchronous SQLAlchemy queries in a thread pool
so the async event loop is never blocked.

Connection is established lazily; a missing / wrong SQLSERVER_URL simply
makes is_available return False, causing all repository methods to return
empty / None results without crashing the application.
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class SqlServerClient:
    """Thread-safe, lazy-initialising wrapper around a SQLAlchemy sync engine.

    Usage::

        client = SqlServerClient(url="mssql+pyodbc://...")
        rows   = await client.execute_query("SELECT * FROM ext_users WHERE ...")
    """

    def __init__(self, connection_url: str) -> None:
        self._url = connection_url
        self._engine: Any = None  # sqlalchemy Engine, created lazily
        self._available: bool = False
        self._init_attempted: bool = False

    # ── Engine bootstrap ──────────────────────────────────────────────────────

    def _ensure_engine(self) -> None:
        """Create the SQLAlchemy sync engine on first call (thread-pool context)."""
        if self._init_attempted:
            return
        self._init_attempted = True

        if not self._url:
            logger.info("SQLSERVER_URL is not configured; SQL Server integration is disabled.")
            return

        try:
            from sqlalchemy import create_engine

            engine = create_engine(
                self._url,
                pool_size=5,
                max_overflow=10,
                pool_timeout=60,
                pool_pre_ping=False,  # Skip per-ping; connect lazily on first query
                connect_args={"timeout": 30},  # pyodbc TCP connect timeout (seconds)
            )
            # Do NOT call engine.connect() here — that blocks for the full TCP
            # connect + TLS + auth round-trip on every process restart (~15 s).
            # The engine is marked available immediately; the first real query
            # will surface any connectivity error in its own error handler.
            self._engine = engine
            self._available = True
            logger.info("SQL Server engine created (connection deferred to first query).")
        except Exception as exc:  # noqa: BLE001
            logger.warning("SQL Server not available (integration features limited): %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    async def execute_query(self, sql: str, params: dict | None = None) -> list[dict]:
        """Execute a parameterised SELECT query and return rows as dicts.

        Runs in a thread pool to avoid blocking the async event loop.
        Returns an empty list when SQL Server is unavailable.
        """
        return await asyncio.to_thread(self._sync_execute, sql, params or {})

    async def execute_non_query(self, sql: str, params: dict | None = None) -> int:
        """Execute a parameterised INSERT / UPDATE / DELETE statement.

        Runs in a thread pool to avoid blocking the async event loop.
        Returns the number of rows affected, or -1 when SQL Server is unavailable.
        """
        return await asyncio.to_thread(self._sync_execute_non_query, sql, params or {})

    @property
    def is_available(self) -> bool:
        return self._available

    # ── Sync helpers (run inside thread pool) ─────────────────────────────────

    def _sync_execute_non_query(self, sql: str, params: dict) -> int:
        self._ensure_engine()
        if not self._engine:
            return -1

        from sqlalchemy import text

        try:
            with self._engine.begin() as conn:  # auto-commit on success
                result = conn.execute(text(sql), params)
                return result.rowcount
        except Exception as exc:  # noqa: BLE001
            logger.error("SQL Server non-query failed: %s | SQL: %.200s", exc, sql)
            return -1

    def _sync_execute(self, sql: str, params: dict) -> list[dict]:
        self._ensure_engine()
        if not self._engine:
            return []

        from sqlalchemy import text

        try:
            with self._engine.connect() as conn:
                result = conn.execute(text(sql), params)
                keys = list(result.keys())
                return [dict(zip(keys, row)) for row in result.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.error("SQL Server query failed: %s | SQL: %.200s", exc, sql)
            return []
