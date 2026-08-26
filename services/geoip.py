"""
IP geolocation for the admin Visits tab — city/region/country via
ip-api.com's free tier (no signup, no API key, 45 requests/minute),
backed by a persistent cache table (models/ip_geo_cache.py) keyed by IP
address so the same address is only ever looked up once, no matter how
many Visit/Order rows share it.

Deliberately NOT called from the checkout page itself (main.py) — that's
a customer-facing hot path where a live third-party network call has no
place. This is only ever called lazily, from admin GET endpoints
(routes/admin.py), when an admin actually opens the Visits tab.
"""
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ip_geo_cache import IpGeoCache

logger = logging.getLogger(__name__)

IP_API_URL = "http://ip-api.com/json/{ip}"
IP_API_FIELDS = "status,city,regionName,countryCode"


async def _live_lookup(ip: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(IP_API_URL.format(ip=ip), params={"fields": IP_API_FIELDS})
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("status") != "success":
            return None
        return {
            "city":    data.get("city") or None,
            "region":  data.get("regionName") or None,
            "country": data.get("countryCode") or None,
        }
    except Exception as e:
        logger.warning(f"ip-api.com lookup failed for {ip}: {e}")
        return None


async def enrich_locations(db: AsyncSession, ip_addresses, max_live_lookups: int = 40) -> dict:
    """
    Bulk-resolves {"city", "region", "country"} (or None) for a batch of
    IPs in one call: checks the cache table for all of them in a single
    query, then does at most `max_live_lookups` live ip-api.com calls for
    whichever ones aren't cached yet — keeps a single admin page load
    bounded in time and well under ip-api's free-tier rate limit even
    across repeated refreshes. Any misses beyond that cap just come back
    as None for now (shown as country-only, or blank) and resolve
    naturally the next time an admin views the tab, once cached.

    Returns {ip_address: {"city":..., "region":..., "country":...} | None}.
    """
    ips = {ip for ip in ip_addresses if ip}
    if not ips:
        return {}

    result = await db.execute(select(IpGeoCache).where(IpGeoCache.ip_address.in_(ips)))
    cached_rows = {row.ip_address: row for row in result.scalars().all()}

    geo_by_ip: dict = {}
    for ip in ips:
        row = cached_rows.get(ip)
        if row:
            geo_by_ip[ip] = {"city": row.city, "region": row.region, "country": row.country} if row.resolved else None

    # All live lookups first (pure HTTP, no DB involved), then ONE batch
    # write at the end — a commit/rollback per iteration here previously
    # triggered a known flaky aiomysql/SQLAlchemy pool_pre_ping issue
    # (see the do_ping patch note in database.py) far more often than a
    # normal single-commit request does, since a request enriching many
    # cache-miss IPs could churn the connection pool dozens of times.
    misses = [ip for ip in ips if ip not in geo_by_ip][:max_live_lookups]
    for ip in misses:
        geo = await _live_lookup(ip)
        geo_by_ip[ip] = geo
        db.add(IpGeoCache(
            ip_address=ip,
            city=geo["city"] if geo else None,
            region=geo["region"] if geo else None,
            country=geo["country"] if geo else None,
            resolved=geo is not None,
        ))

    if misses:
        try:
            await db.commit()
        except Exception as e:
            logger.warning(f"Could not cache geo lookups for {len(misses)} IP(s): {e}")
            await db.rollback()

    return geo_by_ip
