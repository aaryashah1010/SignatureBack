import json
from typing import Any

from redis.asyncio import Redis

from app.core.config import get_settings


class RedisEventBus:
    def __init__(self) -> None:
        settings = get_settings()
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True)

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        await self.redis.publish(channel, json.dumps(payload))

    async def set_json(self, key: str, payload: list[dict], ttl_seconds: int = 60) -> None:
        await self.redis.setex(key, ttl_seconds, json.dumps(payload))

    async def get_json(self, key: str) -> list[dict] | None:
        value = await self.redis.get(key)
        return json.loads(value) if value else None

    async def invalidate_key(self, key: str) -> None:
        await self.redis.delete(key)
