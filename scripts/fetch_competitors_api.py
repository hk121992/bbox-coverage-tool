#!/usr/bin/env python3
"""
Fetch competitor parcel pickup points from provider APIs.
Queries bpost PUDO API and web-scrapes DPD, PostNL, GLS, Mondial Relay.

Output: data/competitors_api_raw.json
"""

import json
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import concurrent.futures
from pathlib import Path
from collections import Counter

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "competitors_api_raw.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


# ── Helpers ─────────────────────────────────────────────────────────────────

def get_belgian_postal_codes():
    """Get a list of Belgian postal codes to use as query seeds.
    Belgian postal codes: 1000-9999. We query a representative subset."""
    # All Belgian postal codes by hundreds — ensures full national coverage
    # Each query returns 200 nearest points (covering ~9km radius for lockers)
    # With ~100-code spacing, overlap ensures full coverage
    return list(range(1000, 10000, 100))


def http_get(url, headers=None, timeout=20):
    """Simple HTTP GET with error handling."""
    hdrs = {**HEADERS, **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def http_get_json(url, headers=None, timeout=20):
    """HTTP GET returning parsed JSON."""
    data = http_get(url, headers, timeout)
    return json.loads(data)


# ── bpost PUDO API ──────────────────────────────────────────────────────────

BPOST_URL = "https://pudo.bpost.be/Locator"
BPOST_TYPE_MAP = {"1": "post_office", "2": "post_point", "4": "locker"}


def fetch_bpost_zone(zone_code):
    """Fetch bpost points near a postal zone. Type 7 = all (1+2+4)."""
    params = {
        "Function": "search",
        "Partner": "999999",
        "AppId": "A001",
        "Format": "xml",
        "Language": "NL",
        "Country": "BE",
        "Limit": "200",
        "Zone": str(zone_code),
        "Type": "7",
    }
    url = BPOST_URL + "?" + urllib.parse.urlencode(params)
    try:
        xml_data = http_get(url)
        return parse_bpost_xml(xml_data)
    except Exception as e:
        return []


def parse_bpost_xml(xml_data):
    """Parse bpost PUDO API XML response."""
    points = []
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return []

    for poi in root.iter("Poi"):
        rec = poi.find("Record")
        if rec is None:
            continue

        lat = rec.findtext("Latitude")
        lng = rec.findtext("Longitude")
        if lat is None or lng is None:
            continue
        try:
            lat_f, lng_f = float(lat), float(lng)
        except (ValueError, TypeError):
            continue

        point_id = rec.findtext("Id", "")
        point_type = rec.findtext("Type", "")
        name = rec.findtext("Name", "")
        street = rec.findtext("Street", "")
        number = rec.findtext("Number", "")
        zipcode = rec.findtext("Zip", "")
        city = rec.findtext("City", "")

        points.append({
            "id": f"bpost_{point_id}",
            "lat": lat_f,
            "lng": lng_f,
            "name": name,
            "operator": "bpost",
            "type": BPOST_TYPE_MAP.get(point_type, "other"),
            "subtype": point_type,
            "address": f"{street} {number}, {zipcode} {city}".strip(),
            "source": "bpost_pudo",
        })

    return points


def scrape_bpost():
    """Query bpost PUDO API across Belgian postal codes."""
    print("=== bpost PUDO API ===")
    zones = get_belgian_postal_codes()
    all_points = {}

    # Use thread pool for parallel requests (API is fast)
    def fetch_zone(zone):
        time.sleep(0.05)  # Gentle rate limit
        return fetch_bpost_zone(zone)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_zone, z): z for z in zones}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            zone = futures[future]
            try:
                points = future.result()
                for p in points:
                    if p["id"] not in all_points:
                        all_points[p["id"]] = p
            except Exception:
                pass
            done += 1
            if done % 20 == 0:
                print(f"  {done}/{len(zones)} zones queried, {len(all_points)} unique points")

    points = list(all_points.values())
    types = Counter(p["type"] for p in points)
    print(f"  bpost total: {len(points)} ({dict(types)})")
    return points


# ── DPD Belgium ─────────────────────────────────────────────────────────────

def scrape_dpd_via_geowidget():
    """Scrape DPD parcelshops using their public geowidget/pickup API."""
    print("\n=== DPD Belgium ===")

    # DPD uses a geowidget for their parcelshop finder
    # Try the DPD Pickup BE SOAP endpoint
    all_points = {}

    # Try the DPD public Pickup locator API
    test_urls = [
        # DPD geowidget public API
        "https://geowidget.dpd.com/api/pickup/BE/1000?limit=500",
        # DPD BE pickup search
        "https://pickup.dpd.be/api/v1/parcelshops?countryCode=BE&postalCode=1000&maxResults=500",
        # DPD group international
        "https://api.dpd.com/pickup/parcelshopfinder/v5/parcelshops?countryIsoAlpha2Code=BE&limit=500",
    ]

    for url in test_urls:
        try:
            data = http_get_json(url, {"Accept": "application/json"})
            print(f"  Endpoint works: {url[:70]}")
            # Parse based on response structure
            if isinstance(data, list):
                print(f"  Got {len(data)} results (array)")
            elif isinstance(data, dict):
                print(f"  Response keys: {list(data.keys())[:10]}")
            return parse_dpd_data(data)
        except Exception as e:
            err = str(e)[:80]
            print(f"  {url[:50]}... -> {err}")

    print("  No working DPD API found, skipping")
    return []


def parse_dpd_data(data):
    """Parse DPD API response."""
    points = []
    items = data if isinstance(data, list) else data.get("parcelShops", data.get("items", []))
    for shop in items:
        lat = shop.get("latitude") or shop.get("lat")
        lng = shop.get("longitude") or shop.get("lng") or shop.get("lon")
        if not lat or not lng:
            continue
        pid = shop.get("parcelShopId") or shop.get("id") or shop.get("psfid", "")
        points.append({
            "id": f"dpd_{pid}",
            "lat": float(lat),
            "lng": float(lng),
            "name": shop.get("company", shop.get("name", "")),
            "operator": "dpd",
            "type": "parcelshop",
            "address": shop.get("street", ""),
            "source": "dpd_api",
        })
    print(f"  DPD parsed: {len(points)} points")
    return points


# ── PostNL Belgium ──────────────────────────────────────────────────────────

def scrape_postnl():
    """Scrape PostNL pickup locations for Belgium."""
    print("\n=== PostNL Belgium ===")

    test_urls = [
        # PostNL Shipment API Location endpoint
        ("https://api.postnl.nl/shipment/v2_1/locations/nearest?CountryCode=BE&PostalCode=1000&DeliveryOptions=PG",
         {"apikey": "fWPcxJKBcMD4YpDhbPDEZkSfHcb7a2VT"}),
        # PostNL public API v2
        ("https://api.postnl.nl/v2/locations?CountryCode=BE&PostalCode=1000",
         {"apikey": "fWPcxJKBcMD4YpDhbPDEZkSfHcb7a2VT"}),
    ]

    for url, extra_headers in test_urls:
        try:
            data = http_get_json(url, extra_headers)
            print(f"  Endpoint works: {url[:70]}")
            if isinstance(data, dict):
                print(f"  Response keys: {list(data.keys())[:10]}")
            return parse_postnl_data(data)
        except Exception as e:
            err = str(e)[:80]
            print(f"  {url[:50]}... -> {err}")

    print("  No working PostNL API found, skipping")
    return []


def parse_postnl_data(data):
    """Parse PostNL location API response."""
    points = []
    locations = data.get("GetLocationsResult", {}).get("ResponseLocation", [])
    if not locations:
        locations = data.get("locations", [])
    for loc in locations:
        addr = loc.get("Address", {})
        lat = addr.get("Latitude") or loc.get("latitude")
        lng = addr.get("Longitude") or loc.get("longitude")
        if not lat or not lng:
            continue
        pid = loc.get("LocationCode", loc.get("id", ""))
        points.append({
            "id": f"postnl_{pid}",
            "lat": float(lat),
            "lng": float(lng),
            "name": loc.get("Name", ""),
            "operator": "postnl",
            "type": "pickup_point",
            "address": f"{addr.get('Street', '')} {addr.get('HouseNr', '')}, {addr.get('Zipcode', '')} {addr.get('City', '')}".strip(),
            "source": "postnl_api",
        })
    print(f"  PostNL parsed: {len(points)} points")
    return points


# ── GLS Belgium ─────────────────────────────────────────────────────────────

def scrape_gls():
    """Query GLS parcelshop finder."""
    print("\n=== GLS Belgium ===")

    # Try various GLS endpoints
    test_urls = [
        "https://gls-group.com/app/service/open/rest/BE/en/rstt001?zipCode=1000",
        "https://gls-group.eu/app/service/open/rest/BE/en/rstt001?zipCode=1000",
        "https://www.gls-one.be/api/parcelShops?country=BE&zip=1000",
    ]

    for url in test_urls:
        try:
            data = http_get_json(url)
            print(f"  Endpoint works: {url[:70]}")
            if isinstance(data, dict):
                print(f"  Response keys: {list(data.keys())[:10]}")
                if "error" in str(data).lower():
                    print(f"  But contains error: {str(data)[:200]}")
                    continue
            return []  # Parse if we get valid data
        except Exception as e:
            err = str(e)[:80]
            print(f"  {url[:50]}... -> {err}")

    print("  No working GLS API found, skipping")
    return []


# ── Mondial Relay ───────────────────────────────────────────────────────────

def scrape_mondial_relay():
    """Scrape Mondial Relay / InPost pickup points for Belgium."""
    print("\n=== Mondial Relay / InPost Belgium ===")

    test_urls = [
        "https://api-pl-points.ecommerce.mondialrelay.com/REST/Parcelshop/search?country=BE&postcode=1000&limit=50",
        "https://widget.mondialrelay.com/parcelshop-picker/v4_0/services/parcelshop-picker.svc/SearchPR",
    ]

    for url in test_urls:
        try:
            data = http_get_json(url)
            print(f"  Endpoint works: {url[:70]}")
            return []
        except Exception as e:
            err = str(e)[:80]
            print(f"  {url[:50]}... -> {err}")

    print("  No working Mondial Relay API found, skipping")
    return []


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    results = {}

    # bpost - main data source (working API)
    results["bpost_pudo"] = scrape_bpost()

    # Try other providers
    results["dpd"] = scrape_dpd_via_geowidget()
    results["postnl"] = scrape_postnl()
    results["gls"] = scrape_gls()
    results["mondialrelay"] = scrape_mondial_relay()

    # Combine
    all_points = []
    print("\n=== Summary ===")
    for source, points in results.items():
        all_points.extend(points)
        print(f"  {source}: {len(points)} points")

    print(f"\n  TOTAL: {len(all_points)} points from APIs")

    # Save
    output = {
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": {k: len(v) for k, v in results.items()},
        "points": all_points,
    }

    with open(OUT_PATH, "w") as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"\nSaved to: {OUT_PATH}")
    print(f"File size: {OUT_PATH.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
