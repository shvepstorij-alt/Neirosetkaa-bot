"""Short-lived shop catalogue snapshot.

Prices are re-read before they are stated once the snapshot is older than
SUPPORT_CATALOG_TTL_SECONDS (default 15 minutes). A failed re-read keeps the
previous snapshot for context and must not be sent as a price.
"""
import copy
import os
import time


def ttl_seconds():
    raw = os.environ.get('SUPPORT_CATALOG_TTL_SECONDS', '900').strip()
    try:
        value = int(raw)
    except ValueError:
        value = 900
    if value < 60:
        value = 60
    if value > 6 * 3600:
        value = 6 * 3600
    return value


def usable(facts):
    if not isinstance(facts, dict):
        return False
    if facts.get('catalog_status') in ('available', 'truncated'):
        return True
    apple = facts.get('appstore') if isinstance(facts.get('appstore'), dict) else {}
    return apple.get('status') in ('available', 'truncated')


class CatalogCache:
    def __init__(self):
        self._rows = {}

    def age(self, uid):
        row = self._rows.get(uid)
        if not row:
            return None
        return max(0.0, time.time() - row['at'])

    def get(self, uid):
        row = self._rows.get(uid)
        if not row:
            return None
        return copy.deepcopy(row['facts'])

    def put(self, uid, facts, at=None):
        self._rows[uid] = {'at': time.time() if at is None else float(at), 'facts': copy.deepcopy(facts)}


async def load_catalog(cache, shop, uid):
    """Return (facts, freshness). Freshness status is fresh, refreshed, refresh_failed or unavailable."""
    cached = cache.get(uid)
    age = cache.age(uid)
    ttl = ttl_seconds()
    if cached is not None and age is not None and age <= ttl:
        return cached, {'status': 'fresh', 'age_seconds': int(age), 'ttl_seconds': ttl}
    try:
        facts = await shop.facts(uid)
    except Exception:
        facts = None
    if usable(facts):
        cache.put(uid, facts)
        return facts, {'status': 'refreshed', 'age_seconds': 0, 'ttl_seconds': ttl}
    if cached is not None:
        return cached, {'status': 'refresh_failed', 'age_seconds': int(age or 0), 'ttl_seconds': ttl}
    if isinstance(facts, dict):
        return facts, {'status': 'unavailable', 'age_seconds': None, 'ttl_seconds': ttl}
    return {'catalog': [], 'catalog_status': 'not_connected', 'appstore': {'status': 'not_connected', 'regions': []}}, {
        'status': 'unavailable', 'age_seconds': None, 'ttl_seconds': ttl}
