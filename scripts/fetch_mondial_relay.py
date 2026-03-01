#!/usr/bin/env python3
"""
Fetch Mondial Relay / InPost pickup points for Belgium.

The Mondial Relay website renders results server-side. We submit the search form
with Belgian postal codes and parse the HTML response to extract point data.

Output: data/mondial_relay_raw.json
"""

import json
import re
import time
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from collections import Counter

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "mondial_relay_raw.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
}

SEARCH_URL = "https://www.mondialrelay.be/fr-be/trouver-le-point-relais-le-plus-proche-de-chez-moi/"


class MRPointParser(HTMLParser):
    """Parse Mondial Relay search results HTML to extract point data."""

    def __init__(self):
        super().__init__()
        self.points = []
        self._in_card = False
        self._in_title = False
        self._in_address = False
        self._current = {}
        self._depth = 0
        self._capture_text = False
        self._text_buffer = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")

        # Card containers for parcelshop or locker
        if "parcelshop-card-body" in cls or "locker-card-body" in cls:
            self._in_card = True
            self._current = {
                "type": "locker" if "locker" in cls else "parcelshop",
            }

        if self._in_card:
            # Card title has the point name
            if "card-title" in cls:
                self._in_title = True
                self._text_buffer = ""
                self._capture_text = True

            # Card text has address lines
            if "card-text" in cls:
                self._in_address = True
                self._text_buffer = ""
                self._capture_text = True

            # Point ID is in a small span/div with the BE-XXXXX pattern
            if tag == "span" or tag == "div":
                # Check for point ID in text (handled in handle_data)
                pass

    def handle_endtag(self, tag):
        if self._in_title and tag in ("h5", "h6", "div", "span", "a"):
            name = self._text_buffer.strip()
            if name:
                self._current["name"] = name
            self._in_title = False
            self._capture_text = False

        if self._in_address and tag in ("p", "div"):
            addr = self._text_buffer.strip()
            if addr:
                self._current.setdefault("address_lines", []).append(addr)
            self._in_address = False
            self._capture_text = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return

        if self._capture_text:
            self._text_buffer += " " + text

        if self._in_card:
            # Look for point ID pattern: BE-XXXXX
            id_match = re.search(r"BE-(\d{5})", text)
            if id_match:
                self._current["point_id"] = id_match.group(0)
                # When we find the ID, we can finalize this point
                if "name" in self._current:
                    self.points.append(dict(self._current))


def extract_coords_from_html(html):
    """Try to extract Leaflet marker coordinates from the page HTML."""
    coords = {}

    # Pattern 1: L.marker([lat, lng]) or new L.LatLng(lat, lng)
    for match in re.finditer(r'L\.marker\(\[([0-9.]+),\s*([0-9.]+)\]', html):
        lat, lng = float(match.group(1)), float(match.group(2))
        coords[f"{lat:.5f},{lng:.5f}"] = (lat, lng)

    # Pattern 2: LatLng(lat, lng)
    for match in re.finditer(r'LatLng\(([0-9.]+),\s*([0-9.]+)\)', html):
        lat, lng = float(match.group(1)), float(match.group(2))
        coords[f"{lat:.5f},{lng:.5f}"] = (lat, lng)

    # Pattern 3: {"lat": xxx, "lng": xxx} or {lat: xxx, lng: xxx}
    for match in re.finditer(r'"?lat"?\s*:\s*([0-9.]+)\s*,\s*"?l(?:ng|on)"?\s*:\s*([0-9.]+)', html):
        lat, lng = float(match.group(1)), float(match.group(2))
        coords[f"{lat:.5f},{lng:.5f}"] = (lat, lng)

    # Pattern 4: data-lat="xxx" data-lng="xxx"
    for match in re.finditer(r'data-lat=["\']([0-9.]+)["\'].*?data-l(?:ng|on)=["\']([0-9.]+)["\']', html):
        lat, lng = float(match.group(1)), float(match.group(2))
        coords[f"{lat:.5f},{lng:.5f}"] = (lat, lng)

    return list(coords.values())


def extract_all_point_data(html):
    """Extract both point metadata and coordinates from the full HTML."""
    points = []

    # Extract point IDs, names, addresses and types from card structure
    # Also look for coordinate data

    # Pattern: find all BE-XXXXX IDs with surrounding context
    # The page structure has cards with name, address, and ID visible

    # Try regex-based extraction for the structured result list
    # Each result typically has: name, address lines, postal code, city, point ID

    # Pattern for parcelshop cards
    card_pattern = re.compile(
        r'(?:parcelshop-card-body|locker-card-body).*?'
        r'card-title[^>]*>.*?<[^>]*>([^<]+)<.*?'  # name
        r'(?:card-text[^>]*>.*?)?'
        r'(BE-\d{5})',
        re.DOTALL | re.IGNORECASE
    )

    for match in card_pattern.finditer(html):
        name = match.group(1).strip()
        point_id = match.group(2)
        card_type = "locker" if "locker" in match.group(0).lower() else "parcelshop"

        points.append({
            "point_id": point_id,
            "name": name,
            "type": card_type,
        })

    return points


def fetch_search_results(postal_code):
    """Submit a search for the given postal code and parse results."""
    # The form submits as a GET with query parameters
    params = urllib.parse.urlencode({
        "PostCode": str(postal_code),
        "Country": "BE",
    })
    url = f"{SEARCH_URL}?{params}"

    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")
        return html
    except Exception as e:
        print(f"  Error fetching postal code {postal_code}: {e}")
        return ""


def parse_page_completely(html):
    """Extract all point data from a search results page."""
    points = []

    if not html:
        return points

    # Extract all BE-XXXXX point IDs
    point_ids = re.findall(r'BE-\d{5}', html)

    # Extract coordinates from the map initialization
    coords = extract_coords_from_html(html)

    # Extract structured data from the HTML using multiple patterns

    # Pattern: Look for sections with point data between card elements
    # The HTML structure typically shows:
    #   <div class="...card-body parcelshop-card-body">
    #     <h5 class="card-title"><a>NAME</a></h5>
    #     <p class="card-text">ADDRESS</p>
    #     <p class="card-text">POSTAL CITY</p>
    #     <span>BE-XXXXX</span>
    #   </div>

    # Use a more robust regex
    blocks = re.split(r'(?=(?:parcelshop|locker)-card-body)', html)

    for block in blocks[1:]:  # Skip first chunk (before first card)
        point = {}

        # Determine type
        point["type"] = "locker" if block.startswith("locker") else "parcelshop"

        # Extract point ID
        id_match = re.search(r'(BE-\d{5})', block)
        if id_match:
            point["point_id"] = id_match.group(1)
        else:
            continue  # Skip blocks without ID

        # Extract name from card-title
        name_match = re.search(r'card-title[^>]*>.*?(?:<a[^>]*>)?([^<]+)', block, re.DOTALL)
        if name_match:
            point["name"] = name_match.group(1).strip()

        # Extract address from card-text
        addr_matches = re.findall(r'card-text[^>]*>([^<]+)', block)
        if addr_matches:
            point["address"] = " ".join(m.strip() for m in addr_matches if m.strip())

            # Try to extract postal code and city from address
            pc_match = re.search(r'(\d{4})\s+([A-Z][A-Za-zÀ-ÿ\s-]+)', point["address"])
            if pc_match:
                point["postal_code"] = pc_match.group(1)
                point["city"] = pc_match.group(2).strip()

        points.append(point)

    return points


def geocode_from_address(address, postal_code=""):
    """Use Nominatim to geocode an address (fallback for missing coords)."""
    query = f"{address}, Belgium"
    if postal_code:
        query = f"{address}, {postal_code}, Belgium"

    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "be",
    })
    url = f"https://nominatim.openstreetmap.org/search?{params}"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "bbox-coverage-tool/1.0",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode("utf-8"))
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None, None


def main():
    print("=== Mondial Relay Belgium Scraper ===")

    all_points = {}  # point_id → point data
    all_coords = []  # (lat, lng) from map markers

    # Belgian postal codes: 1000-9999
    # Query every 100th postal code for broad coverage
    postal_codes = list(range(1000, 10000, 100))

    print(f"Querying {len(postal_codes)} postal codes...")

    for i, pc in enumerate(postal_codes):
        html = fetch_search_results(pc)
        if not html:
            continue

        # Extract points from this page
        page_points = parse_page_completely(html)
        page_coords = extract_coords_from_html(html)

        new_count = 0
        for p in page_points:
            pid = p.get("point_id")
            if pid and pid not in all_points:
                all_points[pid] = p
                new_count += 1

        # Collect coordinates
        for coord in page_coords:
            all_coords.append(coord)

        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(postal_codes)} postal codes, "
                  f"{len(all_points)} unique points, "
                  f"{len(all_coords)} coordinate pairs")

        # Rate limiting - be respectful
        time.sleep(0.5)

    print(f"\nExtracted {len(all_points)} unique Mondial Relay points")
    print(f"Found {len(all_coords)} coordinate pairs from maps")

    # Match coordinates to points
    # The coordinates appear in the same order as the points on each page
    # For points without coordinates, we'll geocode them
    points_list = list(all_points.values())

    # Stats
    types = Counter(p.get("type", "unknown") for p in points_list)
    print(f"By type: {dict(types)}")

    has_coords = sum(1 for p in points_list if p.get("lat"))
    print(f"Points with coordinates: {has_coords}/{len(points_list)}")

    # If we don't have coordinates from the map, try geocoding
    # (only for points that have an address)
    if has_coords < len(points_list) * 0.5:
        print("\nGeocoding points without coordinates (via Nominatim)...")
        geocoded = 0
        for p in points_list:
            if p.get("lat"):
                continue
            addr = p.get("address", "")
            pc = p.get("postal_code", "")
            if addr:
                lat, lng = geocode_from_address(addr, pc)
                if lat and lng:
                    p["lat"] = lat
                    p["lng"] = lng
                    geocoded += 1
                time.sleep(1.1)  # Nominatim rate limit: 1 req/sec

                if geocoded % 50 == 0:
                    print(f"  Geocoded {geocoded} points...")

                # Limit geocoding to avoid excessive Nominatim usage
                if geocoded >= 500:
                    print("  Geocoding limit reached (500)")
                    break

        print(f"  Total geocoded: {geocoded}")

    # Final stats
    has_coords = sum(1 for p in points_list if p.get("lat"))
    print(f"\nFinal: {len(points_list)} points, {has_coords} with coordinates")

    # Save
    output = {
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "totalPoints": len(points_list),
        "pointsWithCoords": has_coords,
        "points": points_list,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=None)

    print(f"Saved to: {OUT_PATH}")
    print(f"File size: {OUT_PATH.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
