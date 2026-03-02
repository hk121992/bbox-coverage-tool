#!/usr/bin/env python3
"""
Ground Truth Agent — Per-Coordinate Local Analysis (v3)

Given a single set of coordinates (from MCLP, supermarket top-up, or user request),
finds nearby candidate placement sites, scores them, and optionally enriches with
Claude Opus 4.6 commentary, zoning research, and contact information.

The coordinate source is opaque — the methodology is reusable regardless of how
the target location was determined.

Usage:
  python3 scripts/local_analysis.py --sector 21009A051 --radius 0.5
  python3 scripts/local_analysis.py --center 50.835,4.370 --radius 0.5 --enrich
  python3 scripts/local_analysis.py --lat 50.835 --lng 4.370 --radius 0.5 --enrich
  python3 scripts/local_analysis.py --sector 21009A051 --enrich --candidates 4

Output: data/local_reports/{name}_{date}/report.json
"""

import argparse
import json
import math
import os
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REPORTS_DIR = DATA_DIR / "local_reports"

# --- Constants ---
R_EARTH = 6371000
DEG_TO_RAD = math.pi / 180
CELL_SIZE = 0.01
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "bbox-coverage-tool/1.0 (ground-truth-agent)"}

PLANNING_PORTALS = {
    "Brussels-Capital": {
        "base": "https://urbanisme.brussels",
        "permits": "https://openpermits.brussels",
    },
    "Flanders": {
        "base": "https://omgevingsloket.be",
        "permits": "https://omgevingsloketpubliek.omgeving.vlaanderen.be",
    },
    "Wallonia": {
        "base": "https://lampspw.wallonie.be/dgo4/site_amenagement",
        "permits": "https://lampspw.wallonie.be/dgo4/site_amenagement/permis",
    },
}

# POI types suitable as locker placement sites
PLACEMENT_TYPES = {
    "convenience", "supermarket", "pharmacy", "post_office",
    "bakery", "bank", "newsagent", "chemist", "greengrocer",
}

# Source priority for candidate ranking
SOURCE_PRIORITY = {
    "ml_heatmap_peak": 0,
    "mclp_centroid": 1,
    "transit_cluster": 2,
    "supermarket": 3,
    "commercial_cluster": 4,
}

TILE_SIZE = 256  # Slippy map tile size in pixels


def lat_lng_to_tile(lat, lng, zoom):
    """Convert lat/lng to slippy map tile x, y at given zoom."""
    n = 2 ** zoom
    x = int((lng + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def lat_lng_to_pixel(lat, lng, zoom):
    """Convert lat/lng to absolute pixel x, y at given zoom."""
    n = 2 ** zoom
    px = (lng + 180) / 360 * n * TILE_SIZE
    py = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n * TILE_SIZE
    return px, py


def load_sector_summary(sector_code=None, center=None):
    """Load demographic and competitive data for a sector.

    If sector_code is provided, looks up directly.
    If only center (lat, lng) is provided, finds nearest sector centroid first.
    Returns dict with population, density, demand, competition, quadrant.
    """
    # Load centroids for sector lookup
    centroids_path = DATA_DIR / "centroids.json"
    if not centroids_path.exists():
        return None
    with open(centroids_path) as f:
        centroids = json.load(f)

    # Resolve sector code from center if needed
    if not sector_code and center:
        best_dist = float("inf")
        for c in centroids:
            d = haversine(center[0], center[1], c["lat"], c["lng"])
            if d < best_dist:
                best_dist = d
                sector_code = c["sc"]

    if not sector_code:
        return None

    # Find centroid data
    centroid_data = None
    for c in centroids:
        if c["sc"] == sector_code:
            centroid_data = c
            break

    summary = {
        "sector": sector_code,
        "population": centroid_data.get("pop", 0) if centroid_data else 0,
        "density": centroid_data.get("dens", 0) if centroid_data else 0,
        "zone": centroid_data.get("zone", "unknown") if centroid_data else "unknown",
        "demand": centroid_data.get("demand", 0) if centroid_data else 0,
    }

    # Competitive coverage
    cc_path = DATA_DIR / "competitive_coverage.json"
    if cc_path.exists():
        with open(cc_path) as f:
            cc = json.load(f)
        sector_cc = cc.get("sectors", {}).get(sector_code, {})
        summary["competitor_count"] = sector_cc.get("cc", 0)
        summary["coverage_gap"] = sector_cc.get("gap", 1.0)
        summary["operators"] = sector_cc.get("ops", [])

    # Strategic quadrant
    sq_path = DATA_DIR / "strategic_quadrants.json"
    if sq_path.exists():
        with open(sq_path) as f:
            sq = json.load(f)
        summary["quadrant"] = sq.get("sectors", {}).get(sector_code, "unknown")

    return summary


# --- Spatial utilities ---

def haversine(lat1, lng1, lat2, lng2):
    d_lat = (lat2 - lat1) * DEG_TO_RAD
    d_lng = (lng2 - lng1) * DEG_TO_RAD
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(lat1 * DEG_TO_RAD) * math.cos(lat2 * DEG_TO_RAD) *
         math.sin(d_lng / 2) ** 2)
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_spatial_index(points, key_lat="lat", key_lng="lng"):
    index = defaultdict(list)
    for i, p in enumerate(points):
        k = (int(p[key_lat] // CELL_SIZE), int(p[key_lng] // CELL_SIZE))
        index[k].append(i)
    return index


def find_within_radius(index, points, lat, lng, radius_m, key_lat="lat", key_lng="lng"):
    radius_deg = radius_m / 111000
    cell_r = math.ceil(radius_deg / CELL_SIZE)
    base_lat = int(lat // CELL_SIZE)
    base_lng = int(lng // CELL_SIZE)
    results = []
    for dlat in range(-cell_r, cell_r + 1):
        for dlng in range(-cell_r, cell_r + 1):
            k = (base_lat + dlat, base_lng + dlng)
            for idx in index.get(k, []):
                p = points[idx]
                d = haversine(lat, lng, p[key_lat], p[key_lng])
                if d <= radius_m:
                    results.append((idx, d))
    return results


# --- Step 1: Load baseline data ---

def load_baseline(args):
    print("Step 1: Loading baseline data...")

    with open(DATA_DIR / "bbox.json") as f:
        bbox = json.load(f)
    with open(DATA_DIR / "centroids.json") as f:
        centroids = json.load(f)
    with open(DATA_DIR / "competitors.json") as f:
        competitors = json.load(f)
    with open(DATA_DIR / "supermarkets.json") as f:
        supermarkets = json.load(f)

    placements = []
    for pfile in ["placements.json", "placements_demand.json"]:
        path = DATA_DIR / pfile
        if path.exists():
            with open(path) as f:
                pdata = json.load(f)
            key = str(args.travel_time)
            if key in pdata:
                placements.extend(pdata[key].get("placements", []))

    approved = []
    if args.approved:
        apath = Path(args.approved)
        if apath.exists():
            with open(apath) as f:
                approved = json.load(f)
            print(f"  Loaded {len(approved)} approved locations")

    # Load ML heatmap (generated by scripts/score_grid.py)
    ml_heatmap = None
    ml_heatmap_path = DATA_DIR / "ml_heatmap.parquet"
    if ml_heatmap_path.exists():
        try:
            import pandas as pd
            ml_heatmap = pd.read_parquet(ml_heatmap_path)
            print(f"  ML heatmap loaded: {len(ml_heatmap):,} grid points")
        except Exception as e:
            print(f"  Warning: could not load ML heatmap: {e}")
    else:
        print("  ML heatmap not found, will use OSM-based candidate search")

    center = None
    if args.center:
        parts = args.center.split(",")
        center = (float(parts[0]), float(parts[1]))
    elif getattr(args, "lat", None) and getattr(args, "lng", None):
        center = (args.lat, args.lng)
    elif args.sector:
        for c in centroids:
            if c["sc"] == args.sector:
                center = (c["lat"], c["lng"])
                break
        if not center:
            raise ValueError(f"Sector {args.sector} not found in centroids.json")

    baseline_lockers = [{"lat": b["lat"], "lng": b["lng"]} for b in bbox]
    for a in approved:
        baseline_lockers.append({"lat": a["lat"], "lng": a["lng"]})

    print(f"  bbox: {len(bbox)}, approved: {len(approved)}, "
          f"centroids: {len(centroids)}, competitors: {len(competitors)}")

    return {
        "center": center,
        "bbox": bbox,
        "centroids": centroids,
        "competitors": competitors,
        "supermarkets": supermarkets,
        "placements": placements,
        "approved": approved,
        "baseline_lockers": baseline_lockers,
        "ml_heatmap": ml_heatmap,
    }


# --- Step 2: Query OSM data ---

def overpass_query(query, retries=3):
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(OVERPASS_URL, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"    Overpass query failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"    Overpass query failed after {retries} attempts: {e}")
                return {"elements": []}


def fetch_osm_data(center, radius_km):
    print("Step 2: Querying OSM data...")
    lat, lng = center
    d_lat = radius_km / 111.0
    d_lng = radius_km / (111.0 * math.cos(lat * DEG_TO_RAD))
    south, north = lat - d_lat, lat + d_lat
    west, east = lng - d_lng, lng + d_lng
    bbox = f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"

    features = {"transit": [], "commerce": [], "parking": [], "buildings": [], "pedestrian": []}

    def _coords(el):
        elat = el.get("lat") or (el.get("center", {}).get("lat"))
        elng = el.get("lon") or (el.get("center", {}).get("lon"))
        return elat, elng

    # Transit
    print("  Fetching transit...")
    q = f"""[out:json][timeout:60];
(node["highway"="bus_stop"]({bbox});
 node["railway"="station"]({bbox});
 node["railway"="halt"]({bbox});
 node["amenity"="bus_station"]({bbox});
 node["railway"="tram_stop"]({bbox}););
out center tags;"""
    result = overpass_query(q)
    for el in result.get("elements", []):
        elat, elng = _coords(el)
        if not elat or not elng:
            continue
        tags = el.get("tags", {})
        subtype = "bus_stop"
        if tags.get("railway") in ("station", "halt"):
            subtype = "rail_station"
        elif tags.get("railway") == "tram_stop":
            subtype = "tram_stop"
        elif tags.get("amenity") == "bus_station":
            subtype = "bus_station"
        features["transit"].append({
            "lat": elat, "lng": elng, "type": "transit", "subtype": subtype,
            "name": tags.get("name", ""),
        })
    print(f"    {len(features['transit'])} transit features")
    time.sleep(3)

    # Commerce
    print("  Fetching commerce...")
    q = f"""[out:json][timeout:60];
(node["shop"]({bbox});
 node["amenity"~"restaurant|cafe|bank|pharmacy|post_office"]({bbox}););
out center tags;"""
    result = overpass_query(q)
    for el in result.get("elements", []):
        elat, elng = _coords(el)
        if not elat or not elng:
            continue
        tags = el.get("tags", {})
        subtype = tags.get("shop") or tags.get("amenity") or "unknown"
        features["commerce"].append({
            "lat": elat, "lng": elng, "type": "commerce", "subtype": subtype,
            "name": tags.get("name", ""),
        })
    print(f"    {len(features['commerce'])} commerce features")
    time.sleep(3)

    # Parking
    print("  Fetching parking...")
    q = f"""[out:json][timeout:60];
(node["amenity"="parking"]({bbox});
 way["amenity"="parking"]({bbox});
 node["amenity"="bicycle_parking"]({bbox}););
out center tags;"""
    result = overpass_query(q)
    for el in result.get("elements", []):
        elat, elng = _coords(el)
        if not elat or not elng:
            continue
        tags = el.get("tags", {})
        features["parking"].append({
            "lat": elat, "lng": elng, "type": "parking",
            "subtype": tags.get("amenity", "parking"),
            "name": tags.get("name", ""),
        })
    print(f"    {len(features['parking'])} parking features")
    time.sleep(3)

    # Buildings
    print("  Fetching buildings...")
    q = f"""[out:json][timeout:60];
(way["building"~"residential|apartments|commercial|retail|office|industrial"]({bbox}););
out center tags;"""
    result = overpass_query(q)
    for el in result.get("elements", []):
        elat, elng = _coords(el)
        if not elat or not elng:
            continue
        tags = el.get("tags", {})
        features["buildings"].append({
            "lat": elat, "lng": elng, "type": "building",
            "subtype": tags.get("building", "unknown"),
            "name": tags.get("name", ""),
        })
    print(f"    {len(features['buildings'])} building features")
    time.sleep(3)

    # Pedestrian infrastructure
    print("  Fetching pedestrian infra...")
    q = f"""[out:json][timeout:60];
(way["highway"="footway"]({bbox});
 way["highway"="pedestrian"]({bbox});
 node["highway"="crossing"]({bbox}););
out center tags;"""
    result = overpass_query(q)
    for el in result.get("elements", []):
        elat, elng = _coords(el)
        if not elat or not elng:
            continue
        tags = el.get("tags", {})
        features["pedestrian"].append({
            "lat": elat, "lng": elng, "type": "pedestrian",
            "subtype": tags.get("highway", "footway"),
            "name": tags.get("name", ""),
        })
    print(f"    {len(features['pedestrian'])} pedestrian features")

    return features


# --- Step 3: Identify candidate micro-locations ---

def find_commercial_clusters(osm_commerce, center, radius_m, n_clusters=3):
    """Find density peaks in commerce data using simple grid-based clustering."""
    lat0, lng0 = center
    # Filter commerce within radius
    nearby = [f for f in osm_commerce
              if haversine(lat0, lng0, f["lat"], f["lng"]) <= radius_m]
    if len(nearby) < 3:
        return []

    # Grid-based density: 50m cells
    cell = 0.0005  # ~50m
    grid = defaultdict(list)
    for f in nearby:
        k = (int(f["lat"] / cell), int(f["lng"] / cell))
        grid[k].append(f)

    # Sort cells by density, take top N
    top_cells = sorted(grid.items(), key=lambda x: -len(x[1]))[:n_clusters]
    clusters = []
    for (clat, clng), features in top_cells:
        if len(features) < 3:
            continue
        avg_lat = sum(f["lat"] for f in features) / len(features)
        avg_lng = sum(f["lng"] for f in features) / len(features)
        clusters.append({
            "lat": avg_lat, "lng": avg_lng, "sector": None,
            "source": "commercial_cluster", "pop_gain": 0,
            "name_hint": f"Commercial cluster ({len(features)} shops)",
        })
    return clusters


def find_ml_peaks(ml_heatmap, center, radius_km, n_peaks=5, dedup_m=100):
    """Find top ML heatmap peaks near center using bounding-box filter.

    Always returns the top N peaks relative to the local area — no absolute
    score threshold, since a 0.15 in rural Ardennes is still the best local option.
    """
    lat0, lng0 = center
    d_lat = radius_km / 111.0
    d_lng = radius_km / (111.0 * math.cos(lat0 * DEG_TO_RAD))

    # Bounding-box filter on DataFrame
    mask = (
        (ml_heatmap["lat"] >= lat0 - d_lat) &
        (ml_heatmap["lat"] <= lat0 + d_lat) &
        (ml_heatmap["lng"] >= lng0 - d_lng) &
        (ml_heatmap["lng"] <= lng0 + d_lng)
    )
    local = ml_heatmap[mask].sort_values("score", ascending=False)

    if local.empty:
        return []

    # Deduplicate within dedup_m
    peaks = []
    for _, row in local.iterrows():
        too_close = False
        for p in peaks:
            if haversine(row["lat"], row["lng"], p["lat"], p["lng"]) < dedup_m:
                too_close = True
                break
        if not too_close:
            peaks.append({
                "lat": row["lat"],
                "lng": row["lng"],
                "sector": row.get("sc", None),
                "source": "ml_heatmap_peak",
                "ml_score": round(float(row["score"]), 4),
                "pop_gain": 0,
                "name_hint": f"ML peak (score {row['score']:.3f})",
            })
        if len(peaks) >= n_peaks:
            break

    return peaks


def _osm_candidate_search(center, radius_km, data, osm_features, max_candidates=4):
    """Fallback OSM-based candidate search (used when ML heatmap unavailable)."""
    lat, lng = center
    radius_m = radius_km * 1000
    candidates = []

    # Source 1: Supermarkets / shops from OSM commerce data
    for f in osm_features["commerce"]:
        d = haversine(lat, lng, f["lat"], f["lng"])
        if d <= radius_m and f.get("name"):
            candidates.append({
                "lat": f["lat"], "lng": f["lng"], "sector": None,
                "source": "commerce_poi",
                "pop_gain": 0,
                "name_hint": f["name"],
                "poi_type": f.get("subtype", "shop"),
                "dist_from_center": round(d),
            })

    # Source 2: Supermarkets from baseline data
    for s in data["supermarkets"]:
        d = haversine(lat, lng, s["lat"], s["lng"])
        if d <= radius_m:
            candidates.append({
                "lat": s["lat"], "lng": s["lng"], "sector": None,
                "source": "supermarket",
                "pop_gain": 0,
                "name_hint": s.get("name", ""),
                "poi_type": "supermarket",
                "dist_from_center": round(d),
            })

    # Source 3: Transit hubs
    for f in osm_features["transit"]:
        if f["subtype"] in ("rail_station", "bus_station", "tram_stop") and f.get("name"):
            d = haversine(lat, lng, f["lat"], f["lng"])
            if d <= radius_m:
                candidates.append({
                    "lat": f["lat"], "lng": f["lng"], "sector": None,
                    "source": "transit_hub",
                    "pop_gain": 0,
                    "name_hint": f["name"],
                    "poi_type": f["subtype"],
                    "dist_from_center": round(d),
                })

    # Source 4: Commercial density cluster centers
    clusters = find_commercial_clusters(osm_features["commerce"], center, radius_m)
    for c in clusters:
        c["dist_from_center"] = round(haversine(lat, lng, c["lat"], c["lng"]))
    candidates.extend(clusters)

    # Sort by: placement-friendly type first, then distance from center
    def sort_key(c):
        is_placement = 0 if c.get("poi_type", "") in PLACEMENT_TYPES else 1
        return (is_placement, c.get("dist_from_center", 9999))

    candidates.sort(key=sort_key)
    return candidates[:max_candidates]


def identify_candidates(center, radius_km, data, osm_features, max_candidates=4):
    """Find candidate placement sites near the given coordinates.

    Primary source: ML heatmap peaks (when ml_heatmap.parquet is available).
    Fallback: OSM-based search (supermarkets, transit hubs, commercial clusters).
    """
    print("Step 3: Identifying candidate sites near target coordinates...")
    lat, lng = center
    radius_m = radius_km * 1000
    candidates = []

    ml_heatmap = data.get("ml_heatmap")
    if ml_heatmap is not None:
        # Primary: ML heatmap peaks
        peaks = find_ml_peaks(ml_heatmap, center, radius_km, n_peaks=max_candidates * 3)
        candidates.extend(peaks)
        print(f"  ML heatmap peaks found: {len(peaks)}")
    else:
        # Fallback: OSM-based search
        print("  ML heatmap unavailable, using OSM-based candidate search")
        osm_cands = _osm_candidate_search(center, radius_km, data, osm_features,
                                           max_candidates=max_candidates * 5)
        candidates.extend(osm_cands)
        print(f"  OSM candidate sites found: {len(candidates)}")

    # Assign sector codes where missing
    centroid_index = build_spatial_index(data["centroids"])
    for cand in candidates:
        if not cand.get("sector"):
            nearest = find_within_radius(
                centroid_index, data["centroids"], cand["lat"], cand["lng"], 2000
            )
            if nearest:
                nearest.sort(key=lambda x: x[1])
                cand["sector"] = data["centroids"][nearest[0][0]]["sc"]

    # Deduplicate within 50m
    deduped = []
    for cand in candidates:
        too_close = False
        for existing in deduped:
            if haversine(cand["lat"], cand["lng"], existing["lat"], existing["lng"]) < 50:
                # Prefer higher ml_score or placement-friendly POI types
                cand_better = cand.get("ml_score", 0) > existing.get("ml_score", 0)
                if not cand_better:
                    cand_placement = cand.get("poi_type", "") in PLACEMENT_TYPES
                    exist_placement = existing.get("poi_type", "") in PLACEMENT_TYPES
                    cand_better = cand_placement and not exist_placement
                if cand_better:
                    deduped.remove(existing)
                    deduped.append(cand)
                too_close = True
                break
        if not too_close:
            deduped.append(cand)

    # Remove candidates too close to existing/approved lockers
    locker_index = build_spatial_index(data["baseline_lockers"])
    filtered = []
    for cand in deduped:
        nearby_lockers = find_within_radius(
            locker_index, data["baseline_lockers"], cand["lat"], cand["lng"], 200
        )
        if not nearby_lockers:
            filtered.append(cand)

    # Sort: ML peaks by ml_score descending, OSM candidates by placement type + distance
    def sort_key(c):
        if c.get("ml_score") is not None:
            return (0, -c["ml_score"])
        is_placement = 0 if c.get("poi_type", "") in PLACEMENT_TYPES else 1
        return (1, is_placement, c.get("dist_from_center", 9999))

    filtered.sort(key=sort_key)

    # Cap at max_candidates
    filtered = filtered[:max_candidates]
    print(f"  After dedup + filter + cap: {len(filtered)}")
    return filtered


# --- Address geocoding ---

def reverse_geocode_address(lat, lng):
    """Get street-level address from coordinates via Nominatim."""
    params = urllib.parse.urlencode({
        "lat": lat, "lon": lng, "format": "json",
        "zoom": 18, "addressdetails": 1,
    })
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        addr = data.get("address", {})
        parts = []
        road = addr.get("road", "")
        number = addr.get("house_number", "")
        if road:
            parts.append(f"{road} {number}".strip())
        postcode = addr.get("postcode", "")
        city = addr.get("city") or addr.get("town") or addr.get("municipality", "")
        if postcode or city:
            parts.append(f"{postcode} {city}".strip())
        return ", ".join(parts) if parts else data.get("display_name", "")[:80]
    except Exception as e:
        print(f"    Warning: address geocode failed for {lat},{lng}: {e}")
        return ""


def reverse_geocode_commune(lat, lng):
    """Get commune name and region from coordinates via Nominatim."""
    params = urllib.parse.urlencode({
        "lat": lat, "lon": lng, "format": "json",
        "zoom": 10, "addressdetails": 1,
    })
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        addr = data.get("address", {})
        commune = (addr.get("city") or addr.get("town") or
                   addr.get("municipality") or addr.get("village", "Unknown"))
        iso = addr.get("ISO3166-2-lvl4", "")
        state = addr.get("state", "") + " " + addr.get("region", "")
        if iso == "BE-BRU" or "Bruxelles" in state or "Brussels" in state or "Brussel" in state:
            region = "Brussels-Capital"
        elif iso == "BE-WAL" or "Wallonie" in state or "Walloni" in state or "wallonne" in state.lower():
            region = "Wallonia"
        else:
            region = "Flanders"
        return commune, region
    except Exception as e:
        print(f"  Warning: reverse geocode failed: {e}")
        return "Unknown", "Unknown"


# --- Step 4: Score each candidate ---

def build_score_explanation(category, osm_features, indices, lat, lng, score, nearby_data):
    """Build a human-readable explanation for a single score category."""
    if category == "transit":
        bus_names = []
        rail_names = []
        for idx, d in nearby_data:
            feat = osm_features["transit"][idx]
            name = feat.get("name", "")
            if feat["subtype"] == "bus_stop" and d <= 300:
                if name and len(bus_names) < 2:
                    bus_names.append(f"{name} ({round(d)}m)")
            elif feat["subtype"] in ("rail_station", "tram_stop"):
                if name:
                    rail_names.append(f"{name} ({round(d)}m)")

        bus_count = sum(1 for idx, d in nearby_data
                        if d <= 300 and osm_features["transit"][idx]["subtype"] == "bus_stop")
        parts = []
        if bus_count:
            parts.append(f"{bus_count} bus stop{'s' if bus_count != 1 else ''} within 300m")
        if rail_names:
            parts.append("; ".join(rail_names[:2]))
        if not parts:
            return "No transit stops found within range"
        return "; ".join(parts)

    elif category == "commerce":
        count = len(nearby_data)
        named = []
        for idx, d in nearby_data[:3]:
            feat = osm_features["commerce"][idx]
            if feat.get("name"):
                named.append(f"{feat['name']} ({round(d)}m)")
        parts = [f"{count} shop{'s' if count != 1 else ''}/amenities within 300m"]
        if named:
            parts.append("incl. " + ", ".join(named[:2]))
        return "; ".join(parts)

    elif category == "building":
        residential = sum(1 for idx, d in nearby_data
                          if osm_features["buildings"][idx]["subtype"] in ("residential", "apartments"))
        commercial = sum(1 for idx, d in nearby_data
                         if osm_features["buildings"][idx]["subtype"] in ("commercial", "retail", "office"))
        if residential + commercial == 0:
            return "No classified buildings nearby"
        return f"{residential} residential + {commercial} commercial buildings"

    elif category == "pedestrian":
        count = len(nearby_data)
        if count == 0:
            return "No pedestrian infrastructure within 150m"
        density = "good" if count >= 6 else ("moderate" if count >= 3 else "limited")
        return f"{count} sidewalks/crossings within 150m ({density})"

    elif category == "parking":
        count = len(nearby_data)
        if count == 0:
            return "No parking within 200m"
        named = []
        for idx, d in nearby_data[:2]:
            feat = osm_features["parking"][idx]
            if feat.get("name"):
                named.append(f"{feat['name']} ({round(d)}m)")
        base = f"{count} parking facilit{'ies' if count != 1 else 'y'} within 200m"
        if named:
            return base + " incl. " + ", ".join(named)
        return base

    return ""


def score_candidates(candidates, osm_features, data, center=None):
    """Legacy OSM-based 0-100 scoring (used when ML heatmap unavailable)."""
    print("Step 4: Scoring candidates (legacy OSM mode)...")

    indices = {}
    for cat in ["transit", "commerce", "parking", "buildings", "pedestrian"]:
        indices[cat] = build_spatial_index(osm_features[cat])

    locker_index = build_spatial_index(data["baseline_lockers"])
    comp_index = build_spatial_index(data["competitors"])

    # Area-average building counts
    area_building_counts = []
    for cand in candidates:
        nearby = find_within_radius(indices["buildings"], osm_features["buildings"],
                                    cand["lat"], cand["lng"], 300)
        area_building_counts.append(len(nearby))
    avg_buildings = max(1, sum(area_building_counts) / max(1, len(area_building_counts)))

    scored = []
    for i, cand in enumerate(candidates):
        lat, lng = cand["lat"], cand["lng"]

        # Transit (0-25)
        transit_nearby = find_within_radius(indices["transit"], osm_features["transit"], lat, lng, 500)
        bus_count = sum(1 for idx, d in transit_nearby
                        if d <= 300 and osm_features["transit"][idx]["subtype"] == "bus_stop")
        rail_count = sum(1 for idx, d in transit_nearby
                         if osm_features["transit"][idx]["subtype"] in ("rail_station", "tram_stop"))
        transit_score = min(25, min(bus_count, 5) * 3 + rail_count * 10)

        # Commerce (0-30)
        commerce_nearby = find_within_radius(indices["commerce"], osm_features["commerce"], lat, lng, 300)
        commerce_score = min(30, min(len(commerce_nearby), 15) * 2)

        # Building mix (0-20)
        building_nearby = find_within_radius(indices["buildings"], osm_features["buildings"], lat, lng, 300)
        residential = sum(1 for idx, d in building_nearby
                          if osm_features["buildings"][idx]["subtype"] in ("residential", "apartments"))
        commercial_bld = sum(1 for idx, d in building_nearby
                             if osm_features["buildings"][idx]["subtype"] in ("commercial", "retail", "office"))
        mix_raw = residential * 0.4 + commercial_bld * 0.6
        building_score = min(20, round(20 * mix_raw / max(1, avg_buildings * 0.5)))

        # Pedestrian (0-15)
        ped_nearby = find_within_radius(indices["pedestrian"], osm_features["pedestrian"], lat, lng, 150)
        pedestrian_score = min(15, round(min(len(ped_nearby), 8) * 1.875))

        # Parking (0-10)
        parking_nearby = find_within_radius(indices["parking"], osm_features["parking"], lat, lng, 200)
        parking_count = len(parking_nearby)
        parking_score = 0 if parking_count == 0 else (7 if parking_count == 1 else 10)

        site_score = transit_score + commerce_score + building_score + pedestrian_score + parking_score

        # Score explanations
        nearby_map = {
            "transit": transit_nearby,
            "commerce": commerce_nearby,
            "building": building_nearby,
            "pedestrian": ped_nearby,
            "parking": parking_nearby,
        }
        score_explanations = {}
        for cat, score_val in [("transit", transit_score), ("commerce", commerce_score),
                               ("building", building_score), ("pedestrian", pedestrian_score),
                               ("parking", parking_score)]:
            cat_key = "buildings" if cat == "building" else cat
            score_explanations[cat] = build_score_explanation(
                cat, osm_features, indices, lat, lng, score_val, nearby_map[cat]
            )

        # Nearest existing locker
        locker_nearby = find_within_radius(locker_index, data["baseline_lockers"], lat, lng, 5000)
        nearest_existing_m = min((d for _, d in locker_nearby), default=9999)

        # Competitors nearby
        comp_nearby = find_within_radius(comp_index, data["competitors"], lat, lng, 500)

        # Nearby POIs (top 8)
        all_pois = []
        for cat in ["transit", "commerce", "parking"]:
            for idx, d in find_within_radius(indices[cat], osm_features[cat], lat, lng, 300):
                feat = osm_features[cat][idx]
                if feat.get("name"):
                    all_pois.append({
                        "name": feat["name"], "type": feat["subtype"], "dist_m": round(d),
                        "lat": feat["lat"], "lng": feat["lng"],
                    })
        all_pois.sort(key=lambda x: x["dist_m"])
        all_pois = all_pois[:8]

        # Find suggested placement site
        def _poi_dist_from_target(poi):
            if center:
                return round(haversine(center[0], center[1], poi["lat"], poi["lng"]))
            return poi["dist_m"]

        suggested_site = None
        for poi in all_pois:
            if poi["type"] in PLACEMENT_TYPES:
                suggested_site = {"name": poi["name"], "type": poi["type"], "dist_m": _poi_dist_from_target(poi)}
                break
        if not suggested_site and all_pois:
            for poi in all_pois:
                if poi["type"] not in ("bus_stop", "rail_station", "tram_stop",
                                       "bicycle_parking", "parking"):
                    suggested_site = {"name": poi["name"], "type": poi["type"], "dist_m": _poi_dist_from_target(poi)}
                    break

        # Strip internal lat/lng from POI list
        for poi in all_pois:
            poi.pop("lat", None)
            poi.pop("lng", None)

        # Reverse geocode
        print(f"  Geocoding address for candidate #{i+1}...")
        address = reverse_geocode_address(lat, lng)
        time.sleep(1.1)

        scored.append({
            "id": i + 1,
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "sector": cand.get("sector", ""),
            "source": cand["source"],
            "address": address,
            "site_score": site_score,
            "ml_score": cand.get("ml_score"),
            "pop_gain": cand.get("pop_gain", 0),
            "nearest_existing_m": round(nearest_existing_m),
            "competitors_nearby": len(comp_nearby),
            "breakdown": {
                "transit": transit_score,
                "commerce": commerce_score,
                "building": building_score,
                "pedestrian": pedestrian_score,
                "parking": parking_score,
            },
            "score_explanations": score_explanations,
            "nearby_pois": all_pois,
            "suggested_site": suggested_site,
            "commentary": "",
            "contact_info": "",
            "status": "proposed",
        })

        site_hint = f" -> {suggested_site['name']}" if suggested_site else ""
        print(f"  #{i+1}: Score={site_score} (T:{transit_score} C:{commerce_score} "
              f"B:{building_score} P:{pedestrian_score} Pk:{parking_score}) "
              f"src={cand['source']}{site_hint}")

    # Sort by site score descending
    scored.sort(key=lambda x: -x["site_score"])
    for i, s in enumerate(scored):
        s["id"] = i + 1

    return scored


def describe_candidates(candidates, osm_features, data, center=None):
    """Describe each candidate using OSM data for the report and Opus enrichment.

    No competitive 0-100 score — ranking is by ml_score descending.
    Builds location_context with counts and named POI details.
    """
    print("Step 4: Describing candidates (ML heatmap mode)...")

    indices = {}
    for cat in ["transit", "commerce", "parking", "buildings", "pedestrian"]:
        indices[cat] = build_spatial_index(osm_features[cat])

    locker_index = build_spatial_index(data["baseline_lockers"])
    comp_index = build_spatial_index(data["competitors"])

    described = []
    for i, cand in enumerate(candidates):
        lat, lng = cand["lat"], cand["lng"]

        # Transit context
        transit_nearby = find_within_radius(indices["transit"], osm_features["transit"], lat, lng, 500)
        bus_stops_300m = sum(1 for idx, d in transit_nearby
                             if d <= 300 and osm_features["transit"][idx]["subtype"] == "bus_stop")
        rail_tram_500m = sum(1 for idx, d in transit_nearby
                              if osm_features["transit"][idx]["subtype"] in ("rail_station", "tram_stop"))
        transit_details = []
        for idx, d in sorted(transit_nearby, key=lambda x: x[1])[:5]:
            feat = osm_features["transit"][idx]
            if feat.get("name"):
                transit_details.append({
                    "name": feat["name"], "type": feat["subtype"], "dist_m": round(d)
                })

        # Commerce context
        commerce_nearby = find_within_radius(indices["commerce"], osm_features["commerce"], lat, lng, 300)
        commerce_details = []
        for idx, d in sorted(commerce_nearby, key=lambda x: x[1])[:8]:
            feat = osm_features["commerce"][idx]
            if feat.get("name"):
                commerce_details.append({
                    "name": feat["name"], "type": feat["subtype"], "dist_m": round(d)
                })

        # Parking context
        parking_nearby = find_within_radius(indices["parking"], osm_features["parking"], lat, lng, 200)

        # Pedestrian context
        ped_nearby = find_within_radius(indices["pedestrian"], osm_features["pedestrian"], lat, lng, 150)

        location_context = {
            "transit": {
                "bus_stops_300m": bus_stops_300m,
                "rail_tram_500m": rail_tram_500m,
                "details": transit_details,
            },
            "commerce": {
                "shops_300m": len(commerce_nearby),
                "details": commerce_details,
            },
            "parking": {
                "spots_200m": len(parking_nearby),
            },
            "pedestrian": {
                "footways_150m": len(ped_nearby),
            },
        }

        # Nearest existing locker
        locker_nearby = find_within_radius(locker_index, data["baseline_lockers"], lat, lng, 5000)
        nearest_existing_m = min((d for _, d in locker_nearby), default=9999)

        # Competitors nearby
        comp_nearby = find_within_radius(comp_index, data["competitors"], lat, lng, 500)

        # Nearby POIs (top 8) — store lat/lng for distance-from-target calc
        all_pois = []
        for cat in ["transit", "commerce", "parking"]:
            for idx, d in find_within_radius(indices[cat], osm_features[cat], lat, lng, 300):
                feat = osm_features[cat][idx]
                if feat.get("name"):
                    all_pois.append({
                        "name": feat["name"], "type": feat["subtype"], "dist_m": round(d),
                        "lat": feat["lat"], "lng": feat["lng"],
                    })
        all_pois.sort(key=lambda x: x["dist_m"])
        all_pois = all_pois[:8]

        # Find suggested placement site
        def _poi_dist_from_target(poi):
            if center:
                return round(haversine(center[0], center[1], poi["lat"], poi["lng"]))
            return poi["dist_m"]

        suggested_site = None
        for poi in all_pois:
            if poi["type"] in PLACEMENT_TYPES:
                suggested_site = {"name": poi["name"], "type": poi["type"], "dist_m": _poi_dist_from_target(poi)}
                break
        if not suggested_site and all_pois:
            for poi in all_pois:
                if poi["type"] not in ("bus_stop", "rail_station", "tram_stop",
                                       "bicycle_parking", "parking"):
                    suggested_site = {"name": poi["name"], "type": poi["type"], "dist_m": _poi_dist_from_target(poi)}
                    break

        # Strip internal lat/lng from POI list
        for poi in all_pois:
            poi.pop("lat", None)
            poi.pop("lng", None)

        # Reverse geocode for address
        print(f"  Geocoding address for candidate #{i+1}...")
        address = reverse_geocode_address(lat, lng)
        time.sleep(1.1)  # Nominatim rate limit

        ml_score = cand.get("ml_score", 0)
        described.append({
            "id": i + 1,
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "sector": cand.get("sector", ""),
            "source": cand["source"],
            "ml_score": ml_score,
            "address": address,
            "suggested_site": suggested_site,
            "nearest_existing_m": round(nearest_existing_m),
            "competitors_nearby": len(comp_nearby),
            "location_context": location_context,
            "nearby_pois": all_pois,
            "pop_gain": cand.get("pop_gain", 0),
            "commentary": "",
            "contact_info": "",
            "status": "proposed",
        })

        site_hint = f" -> {suggested_site['name']}" if suggested_site else ""
        print(f"  #{i+1}: ML={ml_score:.4f} src={cand['source']}{site_hint} "
              f"transit={bus_stops_300m}bus/{rail_tram_500m}rail "
              f"commerce={len(commerce_nearby)} parking={len(parking_nearby)}")

    # Sort by ml_score descending
    described.sort(key=lambda x: -x.get("ml_score", 0))
    for i, s in enumerate(described):
        s["id"] = i + 1

    return described


# --- Step 5: Zoning & planning research ---

def build_zoning_research(center, commune, region):
    print("Step 5: Building zoning research section...")
    lat, lng = center
    portal = PLANNING_PORTALS.get(region, PLANNING_PORTALS["Brussels-Capital"])

    research_prompts = [
        f'Search for recent building permit applications ("permis d\'urbanisme" or "stedenbouwkundige vergunning") in {commune} near coordinates {lat:.4f}, {lng:.4f}',
        f'Check if {commune} has announced any urban renewal, densification, or redevelopment plans for the area around {lat:.4f}, {lng:.4f}',
    ]

    if region == "Brussels-Capital":
        research_prompts.extend([
            f'Search for any PPAS (Plan Particulier d\'Affectation du Sol) affecting {commune}',
            f'Check the PRD (Plan Regional de Developpement) for strategic development zones near {commune}',
        ])
    elif region == "Flanders":
        research_prompts.extend([
            f'Search for any RUP (Ruimtelijk Uitvoeringsplan) or BPA (Bijzonder Plan van Aanleg) for {commune}',
            f'Check {commune} for recent "omgevingsvergunning" applications near {lat:.4f}, {lng:.4f}',
        ])
    elif region == "Wallonia":
        research_prompts.extend([
            f'Search for any SOL (Schema d\'Orientation Local) for {commune}',
            f'Check for recent "permis d\'urbanisme" in {commune} on the Wallonia planning portal',
        ])

    return {
        "commune": commune,
        "region": region,
        "planning_portal": portal["base"],
        "permits_portal": portal["permits"],
        "research_prompts": research_prompts,
    }


# --- Step 3b: Regional zoning queries ---

def _query_brussels_pras(lat, lng):
    """Query PRAS zoning at coordinate via BruGIS WFS."""
    dlat = 0.001
    dlng = 0.002
    bbox = f"{lng-dlng},{lat-dlat},{lng+dlng},{lat+dlat}"
    url = (f"https://gis.urban.brussels/geoserver/ows?"
           f"SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
           f"&TYPENAME=PERSPECTIVE_FR:Affectations"
           f"&BBOX={bbox},EPSG:4326"
           f"&outputFormat=application/json&COUNT=3")
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        features = data.get("features", [])
        if features:
            props = features[0].get("properties", {})
            return {
                "zone_type": props.get("AFFECTATION", ""),
                "zone_name_fr": props.get("NAME_FR", ""),
                "zone_name_nl": props.get("NAME_NL", ""),
                "regulation_url": props.get("URL_P_FR", ""),
            }
    except Exception as e:
        print(f"    Brussels PRAS query warning: {e}")
    return {}


def _query_wallonia_pds(lat, lng):
    """Query Plan de secteur at coordinate via SPW ArcGIS REST."""
    url = (f"https://geoservices.wallonie.be/arcgis/rest/services/"
           f"AMENAGEMENT_TERRITOIRE/PDS/MapServer/identify?"
           f"geometry={lng},{lat}&geometryType=esriGeometryPoint&sr=4326"
           f"&layers=all&tolerance=5&mapExtent=0,0,1,1&imageDisplay=100,100,96"
           f"&returnGeometry=false&f=json")
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results", [])
        zone_name = ""
        commune_name = ""
        for r in results:
            layer = r.get("layerName", "")
            attrs = r.get("attributes", {})
            if "Secteurs" in layer and not zone_name:
                zone_name = attrs.get("NOM", layer)
            if "communales" in layer.lower() and not commune_name:
                commune_name = attrs.get("NOM", "")
        return {
            "zone_type": "Plan de secteur",
            "zone_name_fr": zone_name or "Unknown",
            "zone_name_nl": "",
            "regulation_url": "https://geoportail.wallonie.be/catalogue/7fe2f305-1302-4297-b67e-792f55acd834.html",
        }
    except Exception as e:
        print(f"    Wallonia PDS query warning: {e}")
    return {}


def _query_flanders_gewestplan(lat, lng):
    """Query Gewestplan at coordinate via Geopunt WFS (best-effort)."""
    dlat = 0.001
    dlng = 0.002
    bbox = f"{lng-dlng},{lat-dlat},{lng+dlng},{lat+dlat}"
    url = (f"https://geo.api.vlaanderen.be/gewestplan/wfs?"
           f"SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
           f"&BBOX={bbox},EPSG:4326"
           f"&outputFormat=application/json&COUNT=3")
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        features = data.get("features", [])
        if features:
            props = features[0].get("properties", {})
            return {
                "zone_type": props.get("BESTTYP", props.get("type", "Gewestplan")),
                "zone_name_fr": "",
                "zone_name_nl": props.get("BESTNAAM", props.get("naam", "")),
                "regulation_url": "https://omgeving.vlaanderen.be/nl/gewestplan",
            }
    except Exception as e:
        print(f"    Flanders Gewestplan query warning: {e}")
    return {}


def query_zoning(lat, lng, region):
    """Query zoning classification — routes to correct regional API."""
    if region == "Brussels-Capital":
        return _query_brussels_pras(lat, lng)
    elif region == "Wallonia":
        return _query_wallonia_pds(lat, lng)
    elif region == "Flanders":
        return _query_flanders_gewestplan(lat, lng)
    return {}


def query_heritage_zones(lat, lng, region):
    """Query heritage/overlay zones (Brussels only for now)."""
    if region != "Brussels-Capital":
        return []
    dlat = 0.001
    dlng = 0.002
    bbox = f"{lng-dlng},{lat-dlat},{lng+dlng},{lat+dlat}"
    url = (f"https://gis.urban.brussels/geoserver/ows?"
           f"SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
           f"&TYPENAME=BUP_DRU_PRAS_WFS_READER:A10_AF_ZICHEE"
           f"&BBOX={bbox},EPSG:4326"
           f"&outputFormat=application/json&COUNT=3")
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        zones = []
        for f in data.get("features", []):
            props = f.get("properties", {})
            zones.append({
                "type_fr": props.get("CAT_FR", ""),
                "type_nl": props.get("CAT_DU", ""),
            })
        return zones
    except Exception as e:
        print(f"    Heritage zone query warning: {e}")
    return []


def query_special_plans(lat, lng, region):
    """Query special zoning plans (PPAS/PCA/BPA) near coordinate."""
    if region == "Brussels-Capital":
        where_clause = f"within_distance(geo_point_2d,geom'POINT({lng} {lat})',1000m)"
        params = urllib.parse.urlencode({"limit": 5, "where": where_clause})
        url = f"https://opendata.brussels.be/api/explore/v2.1/catalog/datasets/ppas/records?{params}"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read().decode("utf-8"))
            plans = []
            for r in data.get("results", []):
                plans.append({
                    "name": r.get("namefr", r.get("namedut", "")),
                    "date": r.get("dateapp", ""),
                    "status": r.get("status_fr", r.get("status_en", "")),
                })
            return plans
        except Exception as e:
            print(f"    PPAS query warning: {e}")
    return []


def collect_zoning_data(candidates, commune, region):
    """Step 3b: Collect zoning data for all candidates."""
    print("Step 3b: Querying regional zoning data...")
    for i, c in enumerate(candidates):
        lat, lng = c["lat"], c["lng"]
        print(f"  Candidate #{c['id']}: querying {region} zoning...")
        c["zoning_data"] = query_zoning(lat, lng, region)
        c["heritage_zones"] = query_heritage_zones(lat, lng, region)
        c["special_plans"] = query_special_plans(lat, lng, region)
        if c["zoning_data"]:
            zone = c["zoning_data"].get("zone_name_fr") or c["zoning_data"].get("zone_name_nl") or "?"
            print(f"    Zone: {zone}")
        if c["heritage_zones"]:
            print(f"    Heritage: {c['heritage_zones'][0].get('type_fr', '?')}")
        if c["special_plans"]:
            print(f"    Special plans: {len(c['special_plans'])} nearby")
        time.sleep(0.5)


# --- Step 4: Business & contact lookup ---

def lookup_business_osm(lat, lng, name):
    """Query OSM for business details including contact tags."""
    safe_name = name.replace('"', '\\"')
    query = f'[out:json][timeout:10];node["name"~"{safe_name}",i](around:150,{lat},{lng});out tags;'
    url = OVERPASS_URL + "?" + urllib.parse.urlencode({"data": query})
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        elements = data.get("elements", [])
        if elements:
            tags = elements[0].get("tags", {})
            return {
                "phone": tags.get("phone", tags.get("contact:phone", "")),
                "website": tags.get("website", tags.get("contact:website", "")),
                "opening_hours": tags.get("opening_hours", ""),
                "operator": tags.get("operator", ""),
                "brand": tags.get("brand", ""),
                "brand_wikidata": tags.get("brand:wikidata", ""),
            }
    except Exception as e:
        print(f"    OSM business lookup warning: {e}")
    return {}


def collect_business_data(candidates):
    """Step 4: Collect business details for all candidates."""
    print("Step 4: Looking up business details...")
    for c in candidates:
        site = c.get("suggested_site")
        if site and site.get("name"):
            print(f"  Candidate #{c['id']}: looking up {site['name']}...")
            c["business_details"] = lookup_business_osm(c["lat"], c["lng"], site["name"])
            if c["business_details"].get("phone"):
                print(f"    Phone: {c['business_details']['phone']}")
            if c["business_details"].get("website"):
                print(f"    Website: {c['business_details']['website']}")
            time.sleep(2)  # Overpass rate limit
        else:
            c["business_details"] = {}


# --- Step 5: Physical infrastructure queries ---

def query_physical_context(lat, lng):
    """Query OSM for footway widths, surfaces, and obstacles near candidate."""
    query = f"""[out:json][timeout:25];
(
  way["highway"~"footway|pedestrian|path"](around:150,{lat},{lng});
  way["highway"="residential"]["sidewalk"](around:100,{lat},{lng});
  node["amenity"="bench"](around:50,{lat},{lng});
  node["barrier"](around:50,{lat},{lng});
  node["natural"="tree"](around:50,{lat},{lng});
  node["amenity"~"waste_basket|vending_machine"](around:50,{lat},{lng});
  node["man_made"~"utility_pole|street_cabinet"](around:50,{lat},{lng});
);
out tags;"""
    url = OVERPASS_URL + "?" + urllib.parse.urlencode({"data": query})
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode("utf-8"))
        elements = data.get("elements", [])

        footway_widths = []
        surfaces = []
        sidewalk_streets = []
        obstacles = []

        for el in elements:
            tags = el.get("tags", {})
            el_type = el.get("type", "")

            if el_type == "way":
                width = tags.get("width", "")
                surface = tags.get("surface", "")
                sidewalk = tags.get("sidewalk", "")
                name = tags.get("name", tags.get("highway", "unnamed"))

                if width:
                    footway_widths.append({"name": name, "width": width})
                if surface:
                    surfaces.append({"name": name, "surface": surface})
                if sidewalk:
                    sidewalk_streets.append({"name": name, "sidewalk": sidewalk})

            elif el_type == "node":
                for key in ["amenity", "barrier", "natural", "man_made"]:
                    if key in tags:
                        obstacles.append({
                            "type": tags[key],
                            "name": tags.get("name", ""),
                        })

        return {
            "footway_widths": footway_widths,
            "surfaces": surfaces[:10],
            "sidewalk_streets": sidewalk_streets[:10],
            "obstacles": obstacles,
        }
    except Exception as e:
        print(f"    Physical context query warning: {e}")
    return {"footway_widths": [], "surfaces": [], "sidewalk_streets": [], "obstacles": []}


def collect_physical_data(candidates):
    """Step 5: Collect physical infrastructure data for all candidates."""
    print("Step 5: Querying physical infrastructure...")
    for c in candidates:
        print(f"  Candidate #{c['id']}: footways, surfaces, obstacles...")
        c["physical_context"] = query_physical_context(c["lat"], c["lng"])
        widths = c["physical_context"].get("footway_widths", [])
        obstacles = c["physical_context"].get("obstacles", [])
        if widths:
            print(f"    Footway widths: {', '.join(w['width'] + 'm' for w in widths[:3])}")
        sidewalks = c["physical_context"].get("sidewalk_streets", [])
        if sidewalks:
            print(f"    Streets with sidewalks: {len(sidewalks)}")
        if obstacles:
            from collections import Counter
            obs_types = Counter(o["type"] for o in obstacles)
            print(f"    Obstacles: {dict(obs_types)}")
        time.sleep(3)  # Overpass rate limit


# --- Step 6: Google Maps imagery ---

def download_candidate_images(candidates, img_dir):
    """Step 6: Download satellite + street view images via Google Maps API."""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    except ImportError:
        pass

    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("Step 6: Skipping Google Maps imagery (no GOOGLE_MAPS_API_KEY in .env)")
        # Fall back to ESRI satellite tiles
        _download_esri_tiles(candidates, img_dir)
        return

    print("Step 6: Downloading Google Maps imagery...")
    for c in candidates:
        lat, lng = c["lat"], c["lng"]
        cid = c["id"]

        # Satellite with red marker
        sat_url = (f"https://maps.googleapis.com/maps/api/staticmap?"
                   f"center={lat},{lng}&zoom=18&size=800x600"
                   f"&maptype=satellite&markers=color:red|{lat},{lng}"
                   f"&key={api_key}")
        sat_path = img_dir / f"candidate_{cid}_satellite.png"
        try:
            req = urllib.request.Request(sat_url, headers=HEADERS)
            resp = urllib.request.urlopen(req, timeout=15)
            with open(sat_path, "wb") as f:
                f.write(resp.read())
            print(f"  #{cid}: satellite saved ({sat_path.stat().st_size // 1024}KB)")
        except Exception as e:
            print(f"  #{cid}: satellite download failed: {e}")

        # Street View — check availability first
        meta_url = (f"https://maps.googleapis.com/maps/api/streetview/metadata?"
                    f"location={lat},{lng}&key={api_key}")
        try:
            req = urllib.request.Request(meta_url, headers=HEADERS)
            resp = urllib.request.urlopen(req, timeout=10)
            meta = json.loads(resp.read().decode("utf-8"))
            if meta.get("status") == "OK":
                sv_url = (f"https://maps.googleapis.com/maps/api/streetview?"
                          f"size=800x600&location={lat},{lng}"
                          f"&fov=90&pitch=10&key={api_key}")
                sv_path = img_dir / f"candidate_{cid}_streetview.png"
                req2 = urllib.request.Request(sv_url, headers=HEADERS)
                resp2 = urllib.request.urlopen(req2, timeout=15)
                with open(sv_path, "wb") as f:
                    f.write(resp2.read())
                print(f"  #{cid}: street view saved ({sv_path.stat().st_size // 1024}KB)")
            else:
                print(f"  #{cid}: no street view available at this location")
        except Exception as e:
            print(f"  #{cid}: street view download failed: {e}")

        time.sleep(0.2)


# --- Street View corridor (ML-guided) ---

def build_sv_corridor(sector_code, hot_thresh=0.60, corridor_thresh=0.45, spacing_m=22):
    """Build a Street View API corridor through the ML hot zone of a sector.

    Finds all grid cells scoring >= hot_thresh, chooses the anchor ordering that
    maximises the average IDW score along the corridor between them, interpolates
    sample points every spacing_m metres, and returns two headed API calls (left /
    right of travel direction) per point that scores >= corridor_thresh.

    Returns a dict with keys: meta, anchor_order, corridor_points, sv_calls.
    Returns None if ml_heatmap.parquet is missing or the sector has no hot points.
    """
    heatmap_path = DATA_DIR / "ml_heatmap.parquet"
    if not heatmap_path.exists():
        print("  build_sv_corridor: ml_heatmap.parquet not found, skipping")
        return None

    try:
        import pandas as pd
        import itertools
    except ImportError:
        print("  build_sv_corridor: pandas not available, skipping")
        return None

    df  = pd.read_parquet(heatmap_path)
    sec = df[df["sc"] == sector_code].copy().reset_index(drop=True)

    if sec.empty:
        print(f"  build_sv_corridor: no ML data for sector {sector_code}, skipping")
        return None

    def _idw(lat, lng, power=2):
        d2 = (sec["lat"] - lat) ** 2 + (sec["lng"] - lng) ** 2
        w  = 1.0 / (d2 + 1e-12) ** power
        return float((w * sec["score"]).sum() / w.sum())

    def _bearing(lat1, lng1, lat2, lng2):
        dL = (lng2 - lng1) * DEG_TO_RAD
        x  = math.cos(lat2 * DEG_TO_RAD) * math.sin(dL)
        y  = (math.cos(lat1 * DEG_TO_RAD) * math.sin(lat2 * DEG_TO_RAD)
              - math.sin(lat1 * DEG_TO_RAD) * math.cos(lat2 * DEG_TO_RAD) * math.cos(dL))
        return (math.atan2(x, y) / DEG_TO_RAD) % 360

    hot = sec[sec["score"] >= hot_thresh].copy()
    if len(hot) < 2:
        print(f"  build_sv_corridor: <2 anchors at score >= {hot_thresh}, skipping")
        return None

    # Best linear ordering through anchors (maximise corridor IDW)
    def _score_order(order):
        pts = [hot.iloc[i] for i in order]
        mids = [_idw((pts[i]["lat"] + pts[i+1]["lat"]) / 2,
                     (pts[i]["lng"] + pts[i+1]["lng"]) / 2)
                for i in range(len(pts) - 1)]
        return sum(mids) / len(mids)

    best_order, best_score = max(
        ((p, _score_order(p)) for p in itertools.permutations(range(len(hot)))),
        key=lambda x: x[1])
    anchors = [hot.iloc[i] for i in best_order]

    # Interpolate sample points
    all_pts = []
    for i in range(len(anchors) - 1):
        a, b   = anchors[i], anchors[i + 1]
        alat, alng = float(a["lat"]), float(a["lng"])
        blat, blng = float(b["lat"]), float(b["lng"])
        total_m    = haversine(alat, alng, blat, blng)
        n_steps    = max(1, int(total_m / spacing_m))
        bear = _bearing(alat, alng, blat, blng)
        hl   = (bear - 90) % 360
        hr   = (bear + 90) % 360
        for j in range(n_steps + 1):
            t   = j / n_steps
            lat = alat + t * (blat - alat)
            lng = alng + t * (blng - alng)
            all_pts.append({
                "lat": round(lat, 7), "lng": round(lng, 7),
                "idw_score": round(_idw(lat, lng), 4),
                "head_left": round(hl, 1), "head_right": round(hr, 1),
                "travel_bearing": round(bear, 1), "is_anchor": False,
            })

    # Deduplicate (< 5 m apart)
    deduped = []
    for p in all_pts:
        if not any(haversine(p["lat"], p["lng"], q["lat"], q["lng"]) < 5
                   for q in deduped):
            deduped.append(p)

    # Flag anchor positions
    for a in anchors:
        alat, alng = float(a["lat"]), float(a["lng"])
        for p in deduped:
            if abs(p["lat"] - alat) < 1e-5 and abs(p["lng"] - alng) < 1e-5:
                p["is_anchor"] = True
                p["anchor_score"] = round(float(a["score"]), 4)
                break

    # Filter to hot corridor
    corridor = [p for p in deduped if p["idw_score"] >= corridor_thresh]
    if not corridor:
        print(f"  build_sv_corridor: no points above corridor_thresh {corridor_thresh}, skipping")
        return None

    sv_calls = [
        {"lat": p["lat"], "lng": p["lng"], "heading": h,
         "pitch": 5, "fov": 90, "idw_score": p["idw_score"],
         "side": side, "is_anchor": p.get("is_anchor", False)}
        for p in corridor
        for side, h in [("left", p["head_left"]), ("right", p["head_right"])]
    ]

    print(f"  SV corridor: {len(anchors)} anchors → {len(corridor)} viewpoints "
          f"→ {len(sv_calls)} calls (corridor avg IDW={best_score:.3f})")

    return {
        "meta": {
            "sector": sector_code,
            "hot_threshold": hot_thresh,
            "corridor_threshold": corridor_thresh,
            "sample_spacing_m": spacing_m,
            "n_anchors": len(anchors),
            "n_viewpoints": len(corridor),
            "n_sv_calls": len(sv_calls),
            "corridor_avg_idw": round(best_score, 4),
        },
        "anchor_order": [
            {"lat": float(a["lat"]), "lng": float(a["lng"]), "score": float(a["score"])}
            for a in anchors
        ],
        "corridor_points": corridor,
        "sv_calls": sv_calls,
    }


def download_sv_corridor_images(sv_calls, img_dir):
    """Download Street View images for every call in the ML corridor.

    Images are saved as sv_corridor_{n}_left.png / sv_corridor_{n}_right.png.
    Skips quietly if no Google Maps API key is configured.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    except ImportError:
        pass

    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("  download_sv_corridor_images: no GOOGLE_MAPS_API_KEY, skipping")
        return

    print(f"  Downloading {len(sv_calls)} corridor Street View images...")
    counters = {}   # (lat, lng) → per-position index for filename
    saved = 0

    for call in sv_calls:
        lat, lng    = call["lat"], call["lng"]
        heading     = call["heading"]
        side        = call["side"]
        pos_key     = (lat, lng)
        counters[pos_key] = counters.get(pos_key, 0) + 1
        n           = counters[pos_key]

        # Check availability
        meta_url = (f"https://maps.googleapis.com/maps/api/streetview/metadata?"
                    f"location={lat},{lng}&key={api_key}")
        try:
            req  = urllib.request.Request(meta_url, headers=HEADERS)
            meta = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
            if meta.get("status") != "OK":
                continue
        except Exception:
            continue

        sv_url = (f"https://maps.googleapis.com/maps/api/streetview?"
                  f"size=800x600&location={lat},{lng}"
                  f"&heading={heading}&pitch={call.get('pitch', 5)}"
                  f"&fov={call.get('fov', 90)}&key={api_key}")
        fname  = img_dir / f"sv_corridor_{n}_{side}.png"
        try:
            req2  = urllib.request.Request(sv_url, headers=HEADERS)
            resp2 = urllib.request.urlopen(req2, timeout=15)
            with open(fname, "wb") as fh:
                fh.write(resp2.read())
            saved += 1
        except Exception as e:
            print(f"    corridor SV {n}_{side}: download failed: {e}")

        time.sleep(0.15)

    print(f"  Corridor images saved: {saved}/{len(sv_calls)}")


def _download_esri_tiles(candidates, img_dir):
    """Fallback: download ESRI satellite tiles with center marker."""
    print("Step 6: Downloading ESRI satellite tiles (fallback)...")
    try:
        from PIL import Image, ImageDraw
        import io
    except ImportError:
        print("  Warning: Pillow not installed, skipping satellite imagery")
        return

    ZOOM = 18
    GRID = 3

    for c in candidates:
        lat, lng = c["lat"], c["lng"]
        cid = c["id"]
        cx, cy = lat_lng_to_tile(lat, lng, ZOOM)
        half = GRID // 2

        img = Image.new("RGB", (TILE_SIZE * GRID, TILE_SIZE * GRID))
        for dx in range(-half, half + 1):
            for dy in range(-half, half + 1):
                tx, ty = cx + dx, cy + dy
                tile_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{ZOOM}/{ty}/{tx}"
                try:
                    req = urllib.request.Request(tile_url, headers=HEADERS)
                    resp = urllib.request.urlopen(req, timeout=10)
                    tile = Image.open(io.BytesIO(resp.read()))
                    img.paste(tile, ((dx + half) * TILE_SIZE, (dy + half) * TILE_SIZE))
                except Exception:
                    pass

        # Draw red crosshair at candidate location
        abs_px, abs_py = lat_lng_to_pixel(lat, lng, ZOOM)
        origin_px, origin_py = lat_lng_to_pixel(
            *[lat + (half * TILE_SIZE) / (TILE_SIZE * 2 ** ZOOM) * 360 / (2 * math.pi),
              lng - (half * TILE_SIZE) / (TILE_SIZE * 2 ** ZOOM) * 360][0:1],  # placeholder
            ZOOM
        ) if False else (0, 0)
        # Simpler: calculate pixel offset from top-left tile corner
        tl_tile_x, tl_tile_y = cx - half, cy - half
        marker_x = (abs_px - tl_tile_x * TILE_SIZE)
        marker_y = (abs_py - tl_tile_y * TILE_SIZE)

        draw = ImageDraw.Draw(img)
        r = 12
        draw.ellipse([marker_x - r, marker_y - r, marker_x + r, marker_y + r],
                      outline="red", width=3)
        draw.line([marker_x - r - 4, marker_y, marker_x + r + 4, marker_y],
                   fill="red", width=2)
        draw.line([marker_x, marker_y - r - 4, marker_x, marker_y + r + 4],
                   fill="red", width=2)

        out_path = img_dir / f"candidate_{cid}_satellite.png"
        img.save(str(out_path))
        print(f"  #{cid}: satellite saved ({out_path.stat().st_size // 1024}KB)")


def download_overview_image(center, candidates, img_dir):
    """Download an overview satellite image with all candidates marked."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io
    except ImportError:
        print("  Warning: Pillow not installed, skipping overview image")
        return

    img_dir = Path(img_dir)
    out_path = img_dir / "overview_satellite.png"

    # Try Google Maps Static API first
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if api_key:
        markers = "&".join(
            f"markers=color:orange|label:{c['id']}|{c['lat']},{c['lng']}"
            for c in candidates
        )
        # Add center crosshair
        markers += f"&markers=color:red|{center[0]},{center[1]}"
        url = (f"https://maps.googleapis.com/maps/api/staticmap?"
               f"center={center[0]},{center[1]}&zoom=17&size=800x600"
               f"&maptype=satellite&{markers}&key={api_key}")
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            resp = urllib.request.urlopen(req, timeout=15)
            with open(out_path, "wb") as f:
                f.write(resp.read())
            print(f"  Overview satellite saved (Google Maps, {out_path.stat().st_size // 1024}KB)")
            return
        except Exception as e:
            print(f"  Google Maps overview failed: {e}, trying ESRI fallback")

    # ESRI tile fallback
    ZOOM = 17
    GRID = 5
    half = GRID // 2
    cx, cy = lat_lng_to_tile(center[0], center[1], ZOOM)

    img = Image.new("RGB", (TILE_SIZE * GRID, TILE_SIZE * GRID))
    for dx in range(-half, half + 1):
        for dy in range(-half, half + 1):
            tx, ty = cx + dx, cy + dy
            tile_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{ZOOM}/{ty}/{tx}"
            try:
                req = urllib.request.Request(tile_url, headers=HEADERS)
                resp = urllib.request.urlopen(req, timeout=10)
                tile = Image.open(io.BytesIO(resp.read()))
                img.paste(tile, ((dx + half) * TILE_SIZE, (dy + half) * TILE_SIZE))
            except Exception:
                pass

    tl_tile_x, tl_tile_y = cx - half, cy - half
    draw = ImageDraw.Draw(img)

    # Draw red crosshair at center
    abs_cx, abs_cy = lat_lng_to_pixel(center[0], center[1], ZOOM)
    mx = abs_cx - tl_tile_x * TILE_SIZE
    my = abs_cy - tl_tile_y * TILE_SIZE
    r = 8
    draw.line([mx - r - 4, my, mx + r + 4, my], fill="red", width=2)
    draw.line([mx, my - r - 4, mx, my + r + 4], fill="red", width=2)

    # Draw numbered circles at each candidate
    for c in candidates:
        abs_px, abs_py = lat_lng_to_pixel(c["lat"], c["lng"], ZOOM)
        px = abs_px - tl_tile_x * TILE_SIZE
        py = abs_py - tl_tile_y * TILE_SIZE
        cr = 16
        # Orange filled circle with white border
        draw.ellipse([px - cr, py - cr, px + cr, py + cr], fill=(255, 140, 0), outline="white", width=2)
        # Number in center
        num = str(c["id"])
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), num, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((px - tw / 2, py - th / 2 - 2), num, fill="white", font=font)

    # Crop to 800x600 from center
    w, h = img.size
    left = max(0, w // 2 - 400)
    top = max(0, h // 2 - 300)
    img = img.crop((left, top, left + 800, top + 600))

    img.save(str(out_path))
    print(f"  Overview satellite saved (ESRI, {out_path.stat().st_size // 1024}KB)")


# --- Step 7 (optional): Claude Opus 4.6 enrichment ---

def annotate_street_view(img_path, observations):
    """Annotate a Street View image with numbered observation markers and legend.

    Args:
        img_path: Path to the street view PNG image
        observations: List of dicts with 'label' and 'description' keys
    Returns:
        Path to the annotated image (same directory, _annotated suffix)
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  Warning: Pillow not installed, skipping annotation")
        return img_path

    if not observations or not Path(img_path).exists():
        return img_path

    img = Image.open(img_path)
    w, h = img.size

    # Create legend strip below the image
    line_height = 18
    legend_height = max(40, len(observations) * line_height + 16)
    annotated = Image.new("RGB", (w, h + legend_height), (255, 255, 255))
    annotated.paste(img, (0, 0))

    draw = ImageDraw.Draw(annotated)

    # Draw numbered circles on the image (spread across the image)
    n = len(observations)
    for i in range(n):
        # Spread markers across the image width
        cx = int(w * (i + 1) / (n + 1))
        cy = int(h * 0.35)  # Upper third of image
        r = 14

        # Draw circle with number
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 140, 0), outline=(255, 255, 255), width=2)
        # Draw number in circle
        num_text = str(i + 1)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
        except (OSError, IOError):
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), num_text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2 - 1), num_text, fill=(255, 255, 255), font=font)

    # Draw legend
    try:
        legend_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 12)
        label_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 11)
    except (OSError, IOError):
        legend_font = ImageFont.load_default()
        label_font = legend_font

    y_offset = h + 6
    for i, obs in enumerate(observations):
        # Number circle
        cx, cy = 16, y_offset + 7
        draw.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], fill=(255, 140, 0))
        draw.text((cx - 3, cy - 6), str(i + 1), fill=(255, 255, 255), font=label_font)

        # Label + description
        label_text = obs.get("label", "")
        desc = obs.get("description", "")
        text = f"{label_text}: {desc}" if desc else label_text
        # Truncate if too long
        max_chars = int(w / 6.5)
        if len(text) > max_chars:
            text = text[:max_chars - 3] + "..."
        draw.text((32, y_offset + 1), text, fill=(30, 30, 30), font=label_font)
        y_offset += line_height

    out_path = Path(img_path).parent / (Path(img_path).stem + "_annotated.png")
    annotated.save(str(out_path))
    return out_path


def enrich_with_claude(candidates, zoning, center, commune, region, img_dir=None):
    """Call Claude Opus 4.6 with extended thinking + vision for deep site analysis."""
    print("Step 7: Enriching with Claude Opus 4.6...")

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    except ImportError:
        print("  Warning: python-dotenv not installed, skipping enrichment")
        return None

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("  Warning: ANTHROPIC_API_KEY not set, skipping enrichment")
        return None

    try:
        import anthropic
    except ImportError:
        print("  Warning: anthropic SDK not installed, skipping enrichment")
        return None

    import base64

    # Build comprehensive candidate evidence
    cand_evidence = []
    for c in candidates:
        site = c.get("suggested_site") or {}
        evidence = {
            "id": c["id"],
            "lat": c["lat"],
            "lng": c["lng"],
            "address": c.get("address", ""),
            "sector": c["sector"],
            "source": c["source"],
            "suggested_site": site.get("name", "Unknown"),
            "suggested_site_type": site.get("type", ""),
            "suggested_site_dist_m": site.get("dist_m", 0),
            "pop_gain": c.get("pop_gain", 0),
            "nearest_existing_m": c["nearest_existing_m"],
            "competitors_nearby": c["competitors_nearby"],
            "nearby_pois": [{"name": p["name"], "type": p["type"], "dist_m": p["dist_m"]}
                           for p in c.get("nearby_pois", [])[:8]],
            # Zoning and research data
            "zoning_data": c.get("zoning_data", {}),
            "heritage_zones": c.get("heritage_zones", []),
            "special_plans": c.get("special_plans", []),
            "business_details": c.get("business_details", {}),
            "physical_context": c.get("physical_context", {}),
        }
        # ML heatmap mode: include ml_score and location_context
        if "ml_score" in c and c["ml_score"] is not None:
            evidence["ml_score"] = c["ml_score"]
            evidence["location_context"] = c.get("location_context", {})
        # Legacy OSM mode: include site_score and breakdown
        if "site_score" in c:
            evidence["site_score"] = c["site_score"]
            evidence["breakdown"] = c.get("breakdown", {})
            evidence["score_explanations"] = c.get("score_explanations", {})
        cand_evidence.append(evidence)

    # Build message content with text + images
    content_blocks = []

    # Regional regulation names
    reg_map = {
        "Brussels-Capital": "COBAT (Code bruxellois de l'Aménagement du Territoire)",
        "Wallonia": "CoDT (Code du Développement Territorial)",
        "Flanders": "VCRO (Vlaamse Codex Ruimtelijke Ordening)",
    }
    regulation = reg_map.get(region, "Belgian urban planning code")

    prompt_text = f"""You are a senior site analyst for bpost conducting a ground-truth assessment for parcel locker placement in {commune}, {region}, Belgium.

A bpost parcel locker is approximately 1.2m wide × 0.6m deep × 1.8m tall. It needs:
- Level, paved ground with at least 1.5m clear width for pedestrian passage alongside
- Unobstructed wall or frontage space
- Good visibility and foot traffic
- No blocking of emergency exits, wheelchair access, or shop entrances

CANDIDATE EVIDENCE (scores, zoning, business details, physical infrastructure):
{json.dumps(cand_evidence, indent=2)}

ZONING CONTEXT:
- Region: {region}
- Applicable regulation: {regulation}
- Planning portal: {zoning.get("planning_portal", "")}
- Permits portal: {zoning.get("permits_portal", "")}

Below are satellite and street view images for each candidate (where available). The red marker on satellite images shows the exact candidate location.

REQUIRED OUTPUT — respond with ONLY valid JSON (no markdown fences):

{{
  "candidate_assessments": {{
    "<id>": {{
      "commentary": "2-3 sentence high-level summary of this location's suitability. Do NOT repeat details from physical_feasibility — focus on strategic positioning and overall recommendation.",
      "physical_feasibility": {{
        "verdict": "Feasible|Marginal|Not feasible",
        "footpath_assessment": "Assessment of footpath width from data + imagery...",
        "space_assessment": "Wall/frontage space availability...",
        "accessibility": "Level ground, no barriers, ADA compliance...",
        "visibility_traffic": "Foot traffic and visibility assessment...",
        "obstacles": "Nearby obstacles (trees, bollards, terraces, furniture)..."
      }},
      "visual_observations": [
        {{
          "label": "Short label (e.g. 'Wide footpath', 'Existing vending machine', 'Building frontage')",
          "description": "1-2 sentence description of what you observe at this location that supports or undermines feasibility"
        }}
      ],
      "contact_details": {{
        "site_type": "private|public",
        "business_name": "Name of business or 'Public space'",
        "parent_company": "Corporate parent if chain, empty if independent/public",
        "contact_approach": "Specific approach recommendation (e.g. 'Contact Delhaize Real Estate dept' or 'Contact {commune} Service Urbanisme for sidewalk permit')",
        "phone_website": "Known phone/website from OSM data, or corporate contact if chain",
        "commune_authority": "{commune} - Service Urbanisme / Stedenbouw"
      }}
    }}
  }},
  "zoning_analysis": {{
    "zone_classification": "Actual zone type from API data for this area",
    "applicable_regulations": "{regulation} - relevant articles for street furniture/commercial equipment",
    "permits_required": "Specific permit types needed for a parcel locker installation",
    "approval_timeline": "Estimated timeline for permit approval in {region}",
    "special_plan_restrictions": "Any PPAS/PCA/BPA restrictions, or 'None identified'"
  }},
  "recommendation": {{
    "winner_id": <integer or null if none suitable>,
    "reasoning": "3-5 sentences explaining why this candidate is best, using physical feasibility as primary filter...",
    "next_steps": [
      "Step 1: ...",
      "Step 2: ...",
      "Step 3: ..."
    ]
  }}
}}

IMPORTANT for visual_observations: Include 3-5 observations per candidate based on what you see in the Street View / satellite imagery and the physical context data. These will be used to annotate screenshots in the report."""

    content_blocks.append({"type": "text", "text": prompt_text})

    # Add images for each candidate
    if img_dir:
        img_dir = Path(img_dir)
        for c in candidates:
            cid = c["id"]
            for img_type, label in [("satellite", "Satellite view"), ("streetview", "Street View")]:
                img_path = img_dir / f"candidate_{cid}_{img_type}.png"
                if img_path.exists():
                    try:
                        with open(img_path, "rb") as f:
                            img_data = base64.standard_b64encode(f.read()).decode("utf-8")
                        content_blocks.append({
                            "type": "text",
                            "text": f"\n--- Candidate #{cid}: {label} ---"
                        })
                        content_blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": img_data,
                            }
                        })
                        print(f"  Attached image: candidate_{cid}_{img_type}.png")
                    except Exception as e:
                        print(f"  Warning: could not attach {img_path.name}: {e}")

    try:
        client = anthropic.Anthropic(api_key=api_key)
        print("  Calling Claude Opus 4.6 with extended thinking...")
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=16000,
            thinking={
                "type": "adaptive",
            },
            messages=[{"role": "user", "content": content_blocks}],
        )

        # Extract text from response (skip thinking blocks)
        response_text = ""
        for block in response.content:
            if block.type == "text":
                response_text = block.text.strip()
                break

        # Strip markdown code fences if present
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
            if response_text.endswith("```"):
                response_text = response_text[:-3].strip()

        enrichment = json.loads(response_text)

        # Cost tracking
        usage = response.usage
        input_tokens = getattr(usage, "input_tokens", 0)
        output_tokens = getattr(usage, "output_tokens", 0)
        # Opus 4.6 pricing: $15/M input, $75/M output
        cost_usd = (input_tokens * 15 + output_tokens * 75) / 1_000_000
        print(f"  Claude Opus 4.6 enrichment successful "
              f"(in: {input_tokens:,} tokens, out: {output_tokens:,} tokens, "
              f"est. cost: ${cost_usd:.2f})")

        # Merge assessments onto candidates
        assessments = enrichment.get("candidate_assessments", {})
        for c in candidates:
            cid_str = str(c["id"])
            assessment = assessments.get(cid_str, {})
            c["commentary"] = assessment.get("commentary", "")
            c["physical_feasibility"] = assessment.get("physical_feasibility", {})
            c["visual_observations"] = assessment.get("visual_observations", [])
            c["contact_details"] = assessment.get("contact_details", {})
            # Keep backward-compatible contact_info string
            cd = c["contact_details"]
            c["contact_info"] = cd.get("contact_approach", "")

        # Annotate Street View images with visual observations
        if img_dir:
            img_dir = Path(img_dir)
            for c in candidates:
                cid = c["id"]
                sv_path = img_dir / f"candidate_{cid}_streetview.png"
                observations = c.get("visual_observations", [])
                if sv_path.exists() and observations:
                    annotated_path = annotate_street_view(sv_path, observations)
                    print(f"  #{cid}: annotated street view saved ({Path(annotated_path).name})")

        return {
            "zoning_analysis": enrichment.get("zoning_analysis", {}),
            "zoning_findings": json.dumps(enrichment.get("zoning_analysis", {})),
            "recommendation": enrichment.get("recommendation", {}),
            "overall_recommendation": enrichment.get("recommendation", {}).get("reasoning", ""),
            "model_used": "claude-opus-4-6",
            "enriched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cost_usd": cost_usd,
        }

    except Exception as e:
        print(f"  Warning: Claude API enrichment failed: {e}")
        import traceback
        traceback.print_exc()
        return None


# --- Step 7: Generate report ---

def generate_report(center, radius_km, travel_time, candidates, zoning, data,
                    area_name, osm_counts=None, ai_enrichment=None, target_sector=None,
                    report_dir=None, sector_summary=None, sv_corridor=None):
    print("Step 8: Generating report...")

    ml_heatmap_available = data.get("ml_heatmap") is not None
    has_ml_scores = any(c.get("ml_score") is not None for c in candidates)

    top = candidates[0] if candidates else None
    top_summary = ""
    if top:
        site_hint = ""
        if top.get("suggested_site"):
            site_hint = f" near {top['suggested_site']['name']}"
        if has_ml_scores:
            top_summary = (f"#{top['id']} {top.get('sector', '?')}{site_hint} "
                           f"(ML: {top.get('ml_score', 0):.4f}) - {top['source']}")
        else:
            top_summary = (f"#{top['id']} {top.get('sector', '?')}{site_hint} "
                           f"(Score: {top.get('site_score', 0)}) - {top['source']}")

    summary = {
        "total_candidates": len(candidates),
        "top_candidate_summary": top_summary,
    }
    if has_ml_scores:
        ml_scores = [c["ml_score"] for c in candidates if c.get("ml_score") is not None]
        summary["avg_ml_score"] = round(sum(ml_scores) / max(1, len(ml_scores)), 4)
    else:
        avg_score = round(sum(c.get("site_score", 0) for c in candidates) / max(1, len(candidates)), 1)
        summary["avg_site_score"] = avg_score

    report = {
        "meta": {
            "area_name": area_name,
            "center": list(center),
            "radius_km": radius_km,
            "travel_time": travel_time,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "baseline_lockers": len(data["baseline_lockers"]),
            "approved_lockers": len(data["approved"]),
            "osm_feature_counts": osm_counts or {},
            "target_sector": target_sector or "",
            "sector_summary": sector_summary or {},
            "ml_heatmap_available": ml_heatmap_available,
            "sv_corridor": sv_corridor or {},
        },
        "candidates": candidates,
        "zoning_research": {k: v for k, v in zoning.items() if k != "research_prompts"},
        "ai_enrichment": ai_enrichment or {},
        "images": {},
        "summary": summary,
    }

    # Use provided report_dir or create one
    if report_dir is None:
        date_str = time.strftime("%Y%m%d")
        report_dir = REPORTS_DIR / f"{area_name}_{date_str}"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "images").mkdir(exist_ok=True)

    out_path = report_dir / "report.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\nReport saved to: {out_path}")
    print(f"File size: {out_path.stat().st_size / 1024:.1f} KB")
    print(f"\n=== Summary ===")
    print(f"Area: {area_name}")
    print(f"ML heatmap: {'Yes' if ml_heatmap_available else 'No (OSM fallback)'}")
    print(f"Candidates: {len(candidates)}")
    if has_ml_scores:
        print(f"Avg ML score: {summary.get('avg_ml_score', 0):.4f}")
    else:
        print(f"Avg site score: {summary.get('avg_site_score', 0)}")
    if top:
        print(f"Top candidate: {top_summary}")
    if ai_enrichment:
        print(f"AI enrichment: Yes ({ai_enrichment.get('model_used', 'unknown')})")
    else:
        print("AI enrichment: No (run with --enrich to enable)")

    return out_path


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Ground Truth Agent - Per-Coordinate Local Analysis (v3)")
    parser.add_argument("--center", help="Center coordinates as LAT,LNG (e.g. 50.835,4.370)")
    parser.add_argument("--lat", type=float, help="Target latitude (use with --lng)")
    parser.add_argument("--lng", type=float, help="Target longitude (use with --lat)")
    parser.add_argument("--sector", help="Sector code to center on (e.g. 21009A051)")
    parser.add_argument("--radius", type=float, default=0.5, help="Analysis radius in km (default: 0.5)")
    parser.add_argument("--travel-time", type=int, default=5, help="Travel time in minutes (default: 5)")
    parser.add_argument("--candidates", type=int, default=4, help="Number of candidate sites (default: 4)")
    parser.add_argument("--max-iterations", type=int, default=10,
                        help="Max enrichment iterations to find feasible site (default: 10)")
    parser.add_argument("--max-cost", type=float, default=5.0,
                        help="Max cumulative Claude API cost in USD before stopping iteration (default: $5.00)")
    parser.add_argument("--approved", help="Path to approved_locations.json")
    parser.add_argument("--name", help="Area name for the report (auto-generated if omitted)")
    parser.add_argument("--enrich", action="store_true", help="Enrich with Claude API commentary")
    args = parser.parse_args()

    if not args.center and not args.sector and not (args.lat and args.lng):
        parser.error("One of --center, --sector, or --lat/--lng is required")

    # Step 1: Load baseline
    data = load_baseline(args)
    # Allow --lat/--lng to override center from load_baseline
    if args.lat and args.lng:
        data["center"] = (args.lat, args.lng)
    center = data["center"]

    # Load sector summary (demographics, competition, quadrant)
    sector_summary = load_sector_summary(sector_code=args.sector, center=center)
    if sector_summary:
        q = sector_summary.get("quadrant", "?")
        print(f"  Sector {sector_summary['sector']}: pop={sector_summary['population']}, "
              f"demand={sector_summary['demand']:.0f}, competitors={sector_summary.get('competitor_count', 0)}, "
              f"quadrant={q}")

    # Step 2: Query OSM data
    osm_features = fetch_osm_data(center, args.radius)

    # Step 3: Identify ALL nearby candidate sites (collect a pool)
    all_candidates = identify_candidates(center, args.radius, data, osm_features,
                                         max_candidates=args.candidates * 5)  # Larger pool for iteration

    if not all_candidates:
        print("\nNo candidates found in this area. Try a larger radius or different center.")
        return

    # Step 4: Describe/score all candidates (cheap, local)
    ml_mode = data.get("ml_heatmap") is not None
    if ml_mode:
        all_scored = describe_candidates(all_candidates, osm_features, data, center=center)
    else:
        all_scored = score_candidates(all_candidates, osm_features, data, center=center)

    # Step 5: Zoning research
    commune, region = reverse_geocode_commune(center[0], center[1])
    area_name = args.name or f"{commune.replace(' ', '-').replace('/', '-')}_{args.radius}km"
    zoning = build_zoning_research(center, commune, region)

    # Steps 3b-5b: Collect zoning, business, physical data for ALL candidates
    collect_zoning_data(all_scored, commune, region)
    collect_business_data(all_scored)
    collect_physical_data(all_scored)

    # Prepare report directory + images
    date_str = time.strftime("%Y%m%d")
    report_dir = REPORTS_DIR / f"{area_name}_{date_str}"
    report_dir.mkdir(parents=True, exist_ok=True)
    img_dir = report_dir / "images"
    img_dir.mkdir(exist_ok=True)

    # Take top N candidates for the report
    n = args.candidates
    scored = all_scored[:n]

    # Step 6: Download imagery for current batch
    download_candidate_images(scored, img_dir)
    download_overview_image(center, scored, img_dir)

    # Step 6b: Build ML corridor + download Street View sweep (sector mode only)
    sv_corridor = None
    if args.sector:
        sv_corridor = build_sv_corridor(args.sector)
        if sv_corridor:
            download_sv_corridor_images(sv_corridor["sv_calls"], img_dir)

    # Step 7 (optional): Claude API enrichment with feasibility iteration
    ai_enrichment = None
    if args.enrich:
        ai_enrichment = enrich_with_claude(scored, zoning, center, commune, region, img_dir)
        cumulative_cost = ai_enrichment.get("cost_usd", 0) if ai_enrichment else 0

        # Feasibility iteration: if no candidate is feasible, try next batch
        iteration = 1
        offset = n
        while (ai_enrichment and iteration < args.max_iterations
               and offset < len(all_scored)):
            # Check if any current candidate is feasible
            has_feasible = any(
                c.get("physical_feasibility", {}).get("verdict", "").lower().startswith("feasible")
                for c in scored
            )
            if has_feasible:
                break

            # Check cost budget
            if cumulative_cost >= args.max_cost:
                print(f"\n  Cost limit reached (${cumulative_cost:.2f} >= ${args.max_cost:.2f}), stopping iteration")
                break

            print(f"\n--- Iteration {iteration + 1}: No feasible candidates found, trying next batch "
                  f"(cumulative cost: ${cumulative_cost:.2f} / ${args.max_cost:.2f}) ---")
            next_batch = all_scored[offset:offset + n]
            if not next_batch:
                break

            # Re-number and download images for new batch
            for i, c in enumerate(next_batch):
                c["id"] = len(scored) + i + 1
            download_candidate_images(next_batch, img_dir)

            # Enrich new batch
            batch_enrichment = enrich_with_claude(next_batch, zoning, center, commune, region, img_dir)
            if batch_enrichment:
                cumulative_cost += batch_enrichment.get("cost_usd", 0)

            # Merge: replace worst candidates with any feasible ones from new batch
            feasible_new = [c for c in next_batch
                           if c.get("physical_feasibility", {}).get("verdict", "").lower().startswith("feasible")]
            if feasible_new:
                # Replace lowest-scored non-feasible candidates
                non_feasible = [c for c in scored
                               if not c.get("physical_feasibility", {}).get("verdict", "").lower().startswith("feasible")]
                for new_c in feasible_new[:len(non_feasible)]:
                    if non_feasible:
                        old = non_feasible.pop()
                        idx = scored.index(old)
                        scored[idx] = new_c
                # Update ai_enrichment with latest
                if batch_enrichment:
                    ai_enrichment = batch_enrichment

            offset += n
            iteration += 1

        if ai_enrichment:
            ai_enrichment["total_cost_usd"] = cumulative_cost
            print(f"\n  Total enrichment cost: ${cumulative_cost:.2f}")

        # Rank candidates by recommendation order:
        # 1. Feasible first, then Marginal, then Not feasible
        # 2. AI-recommended winner promoted to #1
        # 3. Within same verdict tier, sort by ml_score (or site_score) descending
        verdict_order = {"feasible": 0, "marginal": 1, "not feasible": 2}
        def _rank_key(c):
            verdict = c.get("physical_feasibility", {}).get("verdict", "Not feasible").lower()
            tier = verdict_order.get(verdict, 2)
            score = c.get("ml_score") if c.get("ml_score") is not None else c.get("site_score", 0)
            return (tier, -score)
        scored.sort(key=_rank_key)

        # Promote AI winner to position #1
        winner_id = (ai_enrichment or {}).get("recommendation", {}).get("winner_id")
        if winner_id:
            for i, c in enumerate(scored):
                if c["id"] == winner_id:
                    scored.insert(0, scored.pop(i))
                    break

        # Re-number 1..N
        for i, c in enumerate(scored):
            c["id"] = i + 1

    # Step 8: Generate report
    osm_counts = {cat: len(feats) for cat, feats in osm_features.items()}
    generate_report(center, args.radius, args.travel_time, scored, zoning, data,
                    area_name, osm_counts, ai_enrichment, target_sector=args.sector,
                    report_dir=report_dir, sector_summary=sector_summary,
                    sv_corridor=sv_corridor)


if __name__ == "__main__":
    main()
