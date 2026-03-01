#!/usr/bin/env python3
"""
Parse and merge competitor pickup point data from multiple sources:
  1. OSM Overpass API (lockers + post offices) — covers DHL, PostNL, DPD, Amazon, etc.
  2. bpost PUDO API — authoritative bpost data (offices, post points, lockers)
  3. GLS API — full Belgian parcelshop/locker network
  4. Mondial Relay website — parcelshops and lockers

Source priority: Provider APIs > OSM when duplicates exist within 50m.
Output: data/competitors.json
"""

import json
import math
from collections import Counter
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Raw data sources
LOCKERS_RAW = DATA_DIR / "competitors_lockers_raw.json"
POSTOFFICES_RAW = DATA_DIR / "competitors_postoffices_raw.json"
BPOST_API_RAW = DATA_DIR / "competitors_api_raw.json"
GLS_RAW = DATA_DIR / "gls_raw.json"
MONDIAL_RELAY_RAW = DATA_DIR / "mondial_relay_raw.json"
OUT_PATH = DATA_DIR / "competitors.json"

# Belgium extent derived from statistical sector centroids + ~5km buffer.
BE_LAT_MIN = 49.46
BE_LAT_MAX = 51.55
BE_LNG_MIN = 2.51
BE_LNG_MAX = 6.43

# Operator classification rules: (substring → operator key)
# Checked case-insensitively against operator, brand, and network tags.
# Order matters — first match wins.
OPERATOR_RULES = [
    ("dhl",           "dhl"),
    ("deutsche post", "dhl"),
    ("postnl",        "postnl"),
    ("de buren",      "postnl"),      # de Buren is PostNL's locker brand
    ("dpd",           "dpd"),
    ("gls",           "gls"),
    ("mondial relay", "mondialrelay"),
    ("mondialrelay",  "mondialrelay"),
    ("inpost",        "inpost"),
    ("amazon",        "amazon"),
    ("ups",           "ups"),
    ("vinted",        "vinted"),
    ("budbee",        "budbee"),
    ("cubee",         "cubee"),
    ("qubee",         "cubee"),
    ("la poste",      "laposte"),     # French postal service (border)
    ("colissimo",     "laposte"),
    ("chronopost",    "laposte"),
    ("post luxembourg", "post_lux"),  # Luxembourg postal (bbox overlap)
    ("post packup",   "post_lux"),
    ("packup",        "post_lux"),
    # bpost variants — own network, not competitors, but part of landscape
    ("bpost",         "bpost"),
    ("b-post",        "bpost"),
    ("bbox",          "bpost"),
]

# Operators that have dedicated API data — skip their OSM entries in favor of API
API_SOURCED_OPERATORS = {"bpost", "gls", "mondialrelay"}


def classify_operator(tags: dict) -> str:
    """Classify an OSM element's operator from its tags."""
    search_fields = []
    for key in ("operator", "brand", "network", "name"):
        val = tags.get(key, "")
        if val:
            search_fields.append(val.lower())

    combined = " ".join(search_fields)

    for substring, op_key in OPERATOR_RULES:
        if substring in combined:
            return op_key

    return "other"


def haversine(lat1, lng1, lat2, lng2):
    """Distance in meters between two points."""
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Source loaders ────────────────────────────────────────────────────────────

def load_osm_points() -> list[dict]:
    """Load and classify OSM Overpass data, excluding API-sourced operators."""
    points = []

    for raw_path, point_type in [(LOCKERS_RAW, "locker"), (POSTOFFICES_RAW, "post_office")]:
        if not raw_path.exists():
            continue
        with open(raw_path) as f:
            data = json.load(f)

        for elem in data.get("elements", []):
            elem_type = elem.get("type")
            if elem_type == "node":
                lat, lng = elem.get("lat"), elem.get("lon")
            elif elem_type == "way":
                center = elem.get("center", {})
                lat, lng = center.get("lat"), center.get("lon")
            else:
                continue

            if lat is None or lng is None:
                continue

            tags = elem.get("tags", {})
            operator = classify_operator(tags)

            # Skip operators that have better API data
            if operator in API_SOURCED_OPERATORS:
                continue

            points.append({
                "lat": lat,
                "lng": lng,
                "name": tags.get("name"),
                "operator": operator,
                "type": point_type,
                "source": "osm",
            })

    return points


def load_bpost_pudo() -> list[dict]:
    """Load bpost PUDO API data (offices, post points, lockers)."""
    if not BPOST_API_RAW.exists():
        return []

    with open(BPOST_API_RAW) as f:
        data = json.load(f)

    points = []
    for p in data.get("points", []):
        if p.get("operator") != "bpost":
            continue
        points.append({
            "lat": p["lat"],
            "lng": p["lng"],
            "name": p.get("name"),
            "operator": "bpost",
            "type": p.get("type", "other"),  # locker, post_point, post_office
            "source": "bpost_pudo",
        })

    return points


def load_gls() -> list[dict]:
    """Load GLS API data (parcelshops and lockers)."""
    if not GLS_RAW.exists():
        return []

    with open(GLS_RAW) as f:
        data = json.load(f)

    points = []
    for p in data.get("points", []):
        lat, lng = p.get("lat"), p.get("lng")
        if lat is None or lng is None:
            continue

        name = p.get("name", "")
        ptype = p.get("type", "shop").lower()

        # GLS "powered by bbox" lockers are bpost infrastructure shared with GLS.
        # Classify as bpost (own network) since they're bbox lockers.
        if "bbox" in name.lower():
            operator = "bpost"
            ptype = "locker"
        else:
            operator = "gls"
            ptype = "locker" if ptype == "locker" else "parcelshop"

        points.append({
            "lat": lat,
            "lng": lng,
            "name": name,
            "operator": operator,
            "type": ptype,
            "source": "gls_api",
        })

    return points


def load_mondial_relay() -> list[dict]:
    """Load Mondial Relay browser-scraped data."""
    if not MONDIAL_RELAY_RAW.exists():
        return []

    with open(MONDIAL_RELAY_RAW) as f:
        data = json.load(f)

    points = []
    for p in data.get("points", []):
        lat, lng = p.get("lat"), p.get("lng")
        if lat is None or lng is None:
            continue

        ptype = p.get("type", "parcelshop")
        if ptype not in ("locker", "parcelshop"):
            ptype = "parcelshop"

        points.append({
            "lat": lat,
            "lng": lng,
            "name": p.get("name"),
            "operator": "mondialrelay",
            "type": ptype,
            "source": "mondialrelay_web",
        })

    return points


# ── Deduplication ─────────────────────────────────────────────────────────────

# Source priority: higher = preferred when resolving duplicates
SOURCE_PRIORITY = {
    "bpost_pudo": 3,
    "gls_api": 3,
    "mondialrelay_web": 3,
    "osm": 1,
}


def deduplicate(points: list[dict], threshold_m: float = 50.0) -> list[dict]:
    """Remove duplicates within threshold_m of each other with same operator.
    When duplicates found, keep the higher-priority source."""
    # Sort by source priority (highest first) so API data is kept over OSM
    points.sort(key=lambda p: SOURCE_PRIORITY.get(p.get("source", "osm"), 0), reverse=True)

    kept = []
    for p in points:
        is_dup = False
        for k in kept:
            if k["operator"] == p["operator"]:
                dist = haversine(p["lat"], p["lng"], k["lat"], k["lng"])
                if dist < threshold_m:
                    is_dup = True
                    break
        if not is_dup:
            kept.append(p)
    return kept


# ── Belgium filter ────────────────────────────────────────────────────────────

def build_belgium_filter():
    """Build a proximity filter using sector centroids if available."""
    centroids_path = DATA_DIR / "centroids.json"
    if not centroids_path.exists():
        # Fallback: simple bbox filter
        def bbox_filter(lat, lng):
            return BE_LAT_MIN <= lat <= BE_LAT_MAX and BE_LNG_MIN <= lng <= BE_LNG_MAX
        return bbox_filter, "bbox"

    with open(centroids_path) as f:
        centroids = json.load(f)

    GRID = 0.05  # ~5.5km cells
    centroid_grid: dict[tuple, list] = {}
    for c in centroids:
        key = (int(c["lat"] / GRID), int(c["lng"] / GRID))
        centroid_grid.setdefault(key, []).append((c["lat"], c["lng"]))

    BUFFER_M = 5000

    def proximity_filter(lat, lng):
        base_lat = int(lat / GRID)
        base_lng = int(lng / GRID)
        for dlat in (-1, 0, 1):
            for dlng in (-1, 0, 1):
                cell = centroid_grid.get((base_lat + dlat, base_lng + dlng))
                if cell:
                    for clat, clng in cell:
                        if haversine(lat, lng, clat, clng) <= BUFFER_M:
                            return True
        return False

    return proximity_filter, "proximity (5km from sector)"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Loading data sources ===\n")

    # 1. OSM data (for operators without API data)
    osm_points = load_osm_points()
    osm_ops = Counter(p["operator"] for p in osm_points)
    print(f"OSM (non-API operators): {len(osm_points)} points")
    for op, count in osm_ops.most_common():
        print(f"  {op}: {count}")

    # 2. bpost PUDO API
    bpost_points = load_bpost_pudo()
    bpost_types = Counter(p["type"] for p in bpost_points)
    print(f"\nbpost PUDO API: {len(bpost_points)} points ({dict(bpost_types)})")

    # 3. GLS API
    gls_points = load_gls()
    gls_ops = Counter(p["operator"] for p in gls_points)
    print(f"\nGLS API: {len(gls_points)} points")
    for op, count in gls_ops.most_common():
        print(f"  {op}: {count}")

    # 4. Mondial Relay
    mr_points = load_mondial_relay()
    mr_types = Counter(p["type"] for p in mr_points)
    print(f"\nMondial Relay: {len(mr_points)} points ({dict(mr_types)})")

    # Merge all sources
    all_points = osm_points + bpost_points + gls_points + mr_points
    print(f"\n=== Combined raw total: {len(all_points)} ===")

    # Geographic filter
    near_belgium, filter_type = build_belgium_filter()
    before = len(all_points)
    all_points = [p for p in all_points if near_belgium(p["lat"], p["lng"])]
    print(f"\nAfter Belgium {filter_type} filter: {len(all_points)} (-{before - len(all_points)})")

    # Remove bpost lockers (already in bbox.json — avoid double-counting own network)
    # Keep bpost post offices and post points as they represent existing infrastructure
    before = len(all_points)
    all_points = [p for p in all_points
                  if not (p["operator"] == "bpost" and p["type"] == "locker")]
    print(f"After removing bpost lockers (in bbox.json): {len(all_points)} (-{before - len(all_points)})")

    # Deduplicate (50m threshold, API data preferred over OSM)
    before = len(all_points)
    all_points = deduplicate(all_points, threshold_m=50.0)
    print(f"After deduplication (50m): {len(all_points)} (-{before - len(all_points)})")

    # Strip internal "source" field before output
    for p in all_points:
        p.pop("source", None)

    # Save
    with open(OUT_PATH, "w") as f:
        json.dump(all_points, f, ensure_ascii=False)

    # Report
    ops = Counter(p["operator"] for p in all_points)
    types = Counter(p["type"] for p in all_points)

    print(f"\n=== Output ===")
    print(f"Saved to: {OUT_PATH}")
    print(f"File size: {OUT_PATH.stat().st_size / 1024:.1f} KB")
    print(f"Total: {len(all_points)} points")
    print(f"\nBy type:")
    for t, count in types.most_common():
        print(f"  {t}: {count}")
    print(f"\nBy operator:")
    for op, count in ops.most_common():
        print(f"  {op}: {count}")


if __name__ == "__main__":
    main()
