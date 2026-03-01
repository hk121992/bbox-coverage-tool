#!/usr/bin/env python3
"""
Fetch GLS Belgium parcelshop locations via their public API.

The GLS website at gls-group.com/BE/en/depot-parcelshop/ uses a PSE (ParcelShopEngine)
widget that makes API calls to api.gls-group.net. The API key is embedded in a
<script> tag attribute on the page.

Output: data/gls_raw.json
"""

import json
import re
import time
import urllib.request
from pathlib import Path
from collections import Counter

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "gls_raw.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
GLS_PAGE = "https://gls-group.com/BE/en/depot-parcelshop/"
GLS_API = "https://api.gls-group.net/parcel-shop-management/v2/available-public-parcel-shops"


def get_api_key():
    """Extract the PSM API key from the GLS depot-parcelshop page."""
    print("Fetching GLS page to extract API key...")
    req = urllib.request.Request(GLS_PAGE, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8")

    # Look for <script src="...pse.js" psmapikey="..." ...>
    match = re.search(r'psmapikey=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if match:
        key = match.group(1)
        print(f"  Found API key: {key[:8]}...{key[-4:]}")
        return key

    raise RuntimeError("Could not find psmapikey attribute on GLS page")


def fetch_gls_shops(api_key, lat, lng, limit=20):
    """Fetch nearest GLS parcelshops from a coordinate."""
    params = (
        f"?latitude={lat:.2f}&longitude={lng:.2f}"
        f"&limit={limit}&minAvailableRate=0"
        f"&type=SHOP,LOCKER,SHOPINSHOP,KEEPER"
    )
    url = GLS_API + params
    req = urllib.request.Request(url, headers={
        **HEADERS,
        "apikey": api_key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data", [])
    except Exception as e:
        return []


def build_grid():
    """Build a grid of query points covering Belgium."""
    # Belgium bounds: lat 49.5-51.55, lng 2.5-6.45
    # Step 0.15° ≈ 16km — with 20 results per query covering ~10km radius,
    # this ensures full coverage with overlap
    points = []
    lat = 49.50
    while lat <= 51.55:
        lng = 2.50
        while lng <= 6.45:
            points.append((round(lat, 2), round(lng, 2)))
            lng += 0.15
        lat += 0.15
    return points


def main():
    api_key = get_api_key()
    grid = build_grid()
    print(f"Grid points: {len(grid)}")

    all_shops = {}  # parcelShopId → shop dict
    errors = 0

    for i, (lat, lng) in enumerate(grid):
        shops = fetch_gls_shops(api_key, lat, lng)
        for shop in shops:
            pid = shop.get("parcelShopId", "")
            addr = shop.get("address", {})
            country = addr.get("countryCode", "")

            # Only keep Belgian shops
            if country != "BE" or not pid:
                continue

            if pid not in all_shops:
                all_shops[pid] = {
                    "id": f"gls_{pid}",
                    "parcelShopId": pid,
                    "partnerId": shop.get("partnerId", ""),
                    "lat": addr.get("latitude"),
                    "lng": addr.get("longitude"),
                    "name": shop.get("name", ""),
                    "type": shop.get("type", "").lower(),
                    "street": addr.get("street", ""),
                    "houseNumber": addr.get("houseNumber", ""),
                    "zipCode": addr.get("zipCode", ""),
                    "city": addr.get("city", ""),
                }

        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(grid)} queries, {len(all_shops)} unique Belgian shops")

        # Gentle rate limiting
        time.sleep(0.08)

    points = list(all_shops.values())
    print(f"\nTotal unique GLS Belgian points: {len(points)}")

    # Stats
    types = Counter(p["type"] for p in points)
    print(f"By type: {dict(types)}")

    # Save
    output = {
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "totalPoints": len(points),
        "points": points,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=None)

    print(f"Saved to: {OUT_PATH}")
    print(f"File size: {OUT_PATH.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
