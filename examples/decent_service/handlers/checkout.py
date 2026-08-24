"""Checkout handler."""

import logging

from store.pricing import total_for_basket

log = logging.getLogger(__name__)


def checkout(request, catalog, payments):
    items = request.json.get("items", [])
    if not items:
        return {"error": "empty basket"}, 400

    total = total_for_basket(items, catalog)
    try:
        receipt = payments.charge(request.user_id, total)
    except payments.Declined as exc:
        log.warning("payment declined for %s: %s", request.user_id, exc)
        return {"error": "declined"}, 402

    return {"receipt": receipt.id, "total": total}, 200
