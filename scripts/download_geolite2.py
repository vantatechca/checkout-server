"""
One-time (or occasional re-run) download of the MaxMind GeoLite2-City
database used by services/geoip.py for city-level visitor geolocation.

Requires MAXMIND_LICENSE_KEY in .env — a free account + license key from
https://www.maxmind.com/en/geolite2/signup-service. MaxMind updates
GeoLite2 databases twice weekly; re-running this occasionally keeps
lookups current, but nothing breaks if you don't (services/geoip.py
degrades to returning None if the file is missing entirely).

Usage:
    cd /srv/shared/checkout-server   (or checkout-server-staging)
    python scripts/download_geolite2.py
"""
import io
import os
import sys
import tarfile

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings

DOWNLOAD_URL = "https://download.maxmind.com/app/geoip_download"
DEST_PATH = os.path.join("data", "GeoLite2-City.mmdb")


def main() -> None:
    license_key = (settings.MAXMIND_LICENSE_KEY or "").strip()
    if not license_key:
        print("[FAIL] MAXMIND_LICENSE_KEY not set in .env — get a free key at "
              "https://www.maxmind.com/en/geolite2/signup-service")
        sys.exit(1)

    print("Downloading GeoLite2-City...")
    resp = httpx.get(
        DOWNLOAD_URL,
        params={"edition_id": "GeoLite2-City", "license_key": license_key, "suffix": "tar.gz"},
        timeout=60.0,
        follow_redirects=True,
    )
    if resp.status_code != 200:
        print(f"[FAIL] Download failed ({resp.status_code}): {resp.text[:300]}")
        sys.exit(1)

    os.makedirs("data", exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        mmdb_member = next((m for m in tar.getmembers() if m.name.endswith(".mmdb")), None)
        if not mmdb_member:
            print("[FAIL] No .mmdb file found in the downloaded archive")
            sys.exit(1)
        extracted = tar.extractfile(mmdb_member)
        with open(DEST_PATH, "wb") as f:
            f.write(extracted.read())

    size_mb = os.path.getsize(DEST_PATH) / (1024 * 1024)
    print(f"[OK] Saved {DEST_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
