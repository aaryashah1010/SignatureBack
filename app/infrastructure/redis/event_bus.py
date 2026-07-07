import logging
from typing import Any

logger = logging.getLogger(__name__)


class RedisEventBus:
    """No-op event bus (Redis removed).

    At this scale the only thing Redis backed was a 60s cache on the signer's
    pending-documents query (a single trivial DB call) plus a pub/sub channel with
    no subscribers. Both were removed. This keeps the same interface so callers are
    unchanged: writes/publishes do nothing, and reads always report a cache miss,
    so the pending-documents query goes straight to Postgres.
    """

    def __init__(self) -> None:
        # No connection — nothing to set up.
        pass

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        return None

    async def set_json(self, key: str, payload: list[dict], ttl_seconds: int = 60) -> None:
        return None

    async def get_json(self, key: str) -> list[dict] | None:
        # Always a cache miss → caller queries the database directly.
        return None

    async def invalidate_key(self, key: str) -> None:
        return None
