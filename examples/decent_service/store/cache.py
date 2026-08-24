"""In-process cache with TTL. Used by the handlers and the pricing layer."""

import time


class TTLCache:
    def __init__(self, ttl_seconds=300, max_entries=10000):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data = {}

    def get(self, key):
        entry = self._data.get(key)
        if entry is None:
            return None
        value, stored_at = entry
        if time.time() - stored_at > self.ttl:
            del self._data[key]
            return None
        return value

    def put(self, key, value):
        self._data[key] = (value, time.time())

    def invalidate(self, prefix):
        for key in list(self._data):
            if key.startswith(prefix):
                del self._data[key]
