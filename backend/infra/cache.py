import json
import os
from typing import Any, Optional

import redis


class RedisCache:
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:16379/0")
        self.key_prefix = os.getenv("REDIS_KEY_PREFIX", "keyan_zhushou")
        self.default_ttl = int(os.getenv("REDIS_CACHE_TTL_SECONDS", "300"))
        self._client = None

    def _get_client(self):
        # Redis 客户端按需创建；仅导入模块不会立刻连接外部服务。
        if self._client is None:
            self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.key_prefix}:{key}"

    def get_json(self, key: str) -> Optional[Any]:
        try:
            # 读取失败和键不存在都按缓存未命中处理，业务随后可以回源 PostgreSQL。
            value = self._get_client().get(self._key(key))
            if not value:
                return None
            return json.loads(value)
        except Exception:
            return None

    def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False)
            # 缓存必须设置过期时间，避免旧副本永久覆盖数据库中的新事实。
            self._get_client().setex(self._key(key), ttl or self.default_ttl, payload)
        except Exception:
            return

    def delete(self, key: str) -> None:
        try:
            self._get_client().delete(self._key(key))
        except Exception:
            return

cache = RedisCache()
