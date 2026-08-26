"""
Local IP geolocation via a self-hosted MaxMind GeoLite2-City database — no
per-request network call, no external API cost/rate-limit, just an
in-memory binary lookup (microseconds), safe to call synchronously inline
in the request path.

The .mmdb file itself is NOT part of this repo (see .gitignore) — it's a
~70MB binary downloaded per-environment via scripts/download_geolite2.py,
which needs a free MaxMind account + license key (MAXMIND_LICENSE_KEY in
.env) that only a human can obtain. Until that file exists, lookup()
returns None for everything and callers fall back to whatever coarser
signal they already have (e.g. Cloudflare's CF-IPCountry header) — this
module is designed to degrade silently, never to be a hard dependency.
"""
import logging
import os

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join("data", "GeoLite2-City.mmdb")

_reader = None
_load_attempted = False


def _get_reader():
    global _reader, _load_attempted
    if _reader is None and not _load_attempted:
        _load_attempted = True
        try:
            import geoip2.database
            _reader = geoip2.database.Reader(_DB_PATH)
        except Exception as e:
            logger.warning(f"GeoLite2 database not available ({e}) — city/region lookups will return None")
    return _reader


def lookup(ip: str) -> dict | None:
    """
    Returns {"city": str|None, "region": str|None, "country": str|None}
    (country as a 2-letter ISO code, matching the existing CF-IPCountry
    convention this replaces/augments) or None if the IP can't be
    resolved — a private/reserved IP, the database file isn't
    downloaded yet, or the address simply isn't in it. Never raises.
    """
    if not ip:
        return None
    reader = _get_reader()
    if not reader:
        return None
    try:
        import geoip2.errors
        result = reader.city(ip)
        subdivision = result.subdivisions.most_specific
        return {
            "city":    result.city.name,
            "region":  subdivision.name if subdivision else None,
            "country": result.country.iso_code,
        }
    except Exception as e:
        # Covers geoip2.errors.AddressNotFoundError (private/reserved IPs,
        # unassigned ranges) and any other lookup failure alike — none of
        # these should ever be surfaced as an error to the caller.
        logger.debug(f"GeoIP lookup miss for {ip}: {e}")
        return None
