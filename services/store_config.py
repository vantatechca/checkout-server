"""
Per-source-store payment-method configuration, backed by a CSV file.

Why CSV (not DB):
  * 600+ stores is too many for env CSV vars but small for a CSV file.
  * No DB migration needed — drop a file, restart (or wait for auto-reload).
  * Easy to bulk-edit in Excel / a text editor.
  * Easy to version-control if you want.

CSV format (`data/source_stores.csv` by default):

    domain,card,altcoin,onramp,notes
    pittsburghpeptides.com,1,0,0,US store
    victoriapeps.ca,1,0,0,CA flagship
    hamiltonpeptides.com,1,1,0,
    torontopeptides.ca,0,0,0,disabled — chargeback risk

Boolean values: 1/0, true/false, yes/no, y/n, on/off — all case-insensitive.
Blank = treat as "no override" (falls back to global env defaults).

Lookup is O(1) via an in-memory dict. The file's mtime is checked on each
request — if it changed, we reload automatically. No server restart needed
to add/remove/edit a store.

If the CSV doesn't exist, every lookup returns None and the caller falls
back to global env flags (the current pre-CSV behavior).
"""
import csv
import logging
import os
import threading
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# Default path — overridable via STORE_CONFIG_CSV env var.
DEFAULT_CSV_PATH = "data/source_stores.csv"

_TRUTHY = {"1", "true", "yes", "y", "on", "t"}
_FALSY  = {"0", "false", "no", "n", "off", "f", ""}


def _csv_path() -> str:
    return (getattr(settings, "STORE_CONFIG_CSV", "") or DEFAULT_CSV_PATH).strip()


def _normalize_domain(d: str) -> str:
    # NOTE: do NOT use .lstrip("www.") — lstrip strips any leading chars in the
    # SET {w, .}, so e.g. "westcoastpeps.com" -> "estcoastpeps.com". Strip the
    # literal "www." prefix only.
    d = (d or "").strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
    if d.startswith("www."):
        d = d[4:]
    return d


def _parse_bool(val: str) -> Optional[bool]:
    """Returns True / False / None (None = "no override")."""
    if val is None:
        return None
    s = str(val).strip().lower()
    if s == "":
        return None
    if s in _TRUTHY: return True
    if s in _FALSY:  return False
    return None


# In-memory cache: domain -> {card, altcoin, onramp, notes}
_cache: dict[str, dict] = {}
_cache_mtime: float = 0.0
_cache_lock = threading.Lock()


def _load_if_changed() -> None:
    """Cheap mtime check — reload only if the file changed since last read."""
    global _cache, _cache_mtime
    path = _csv_path()
    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        # File missing — keep whatever cache we have (likely empty) and don't spam logs.
        if _cache_mtime != -1:
            logger.info(f"[store_config] CSV not found at {path} — using global env defaults")
            _cache_mtime = -1   # sentinel — don't log again until file appears
        return

    if mtime == _cache_mtime:
        return

    with _cache_lock:
        if mtime == _cache_mtime:
            return
        new_cache: dict[str, dict] = {}
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    raw_dom = (row.get("domain", "") or "").strip()
                    # Allow `# comment` lines in the CSV for visual section
                    # grouping — skip them.
                    if raw_dom.startswith("#"):
                        continue
                    dom = _normalize_domain(raw_dom)
                    if not dom:
                        continue
                    new_cache[dom] = {
                        "card":    _parse_bool(row.get("card")),
                        "altcoin": _parse_bool(row.get("altcoin")),
                        "onramp":  _parse_bool(row.get("onramp")),
                        "notes":   (row.get("notes") or "").strip(),
                    }
        except Exception as e:
            logger.error(f"[store_config] failed to parse {path}: {e}")
            return
        _cache = new_cache
        _cache_mtime = mtime
        logger.info(f"[store_config] loaded {len(_cache)} stores from {path}")


def get(source_domain: str) -> Optional[dict]:
    """
    Returns the config row for a source domain, or None if not found.
    A returned row's boolean fields may individually be None (= no override).
    """
    _load_if_changed()
    return _cache.get(_normalize_domain(source_domain))


def is_enabled(source_domain: str, method: str, default: bool) -> bool:
    """
    Decide if a payment method is enabled for a source store.

    Order of precedence:
      1. CSV row's per-method flag (True/False) — if set, wins.
      2. `default` argument — usually the global env flag.

    `method` must be one of: "card", "altcoin", "onramp".
    """
    cfg = get(source_domain)
    if not cfg:
        return default
    val = cfg.get(method)
    if val is None:
        return default
    return bool(val)


def all_known() -> dict[str, dict]:
    """Return the full cache — for admin UI listings."""
    _load_if_changed()
    return dict(_cache)
