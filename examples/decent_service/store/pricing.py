"""Price lookups, cached."""

from store.cache import TTLCache

_cache = TTLCache(ttl_seconds=60)


def price_for(sku, catalog):
    cached = _cache.get(sku)
    if cached is not None:
        return cached
    price = catalog.lookup(sku)
    _cache.put(sku, price)
    return price


def total_for_basket(items, catalog):
    total = 0
    for item in items:
        total += price_for(item["sku"], catalog) * item["quantity"]
    return round(total, 2)
