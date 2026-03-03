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
import hashlib
import heapq
import json
import math
import os
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from pathlib import Path

DATA_DIR  = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
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

SV_ANALYSIS_VERSION = "2.0"

# --- SV Pipeline v2.0 Configuration ---
# All tuning knobs in one place for rapid experimentation.
SV_CONFIG = {
    "screening_stride": 3,          # take every Nth corridor point for screening
    "screening_threshold": 4,       # min placement_score to flag as "interesting"
    "detail_fov_set": [60, 90, 120],  # FOVs for detail capture
    "look_toward_range": 2,         # ±N adjacent viewpoints for converging views
    "clustering_radius_m": 35,      # how close VPs must be to form a candidate group
    "max_images_per_candidate": 12, # cap images sent to Opus per candidate group
    "candidate_min_score": 5,       # min placement_score for candidate conversion
    "opus_prompt_variant": "v1",    # swap to test different assessment prompts
    "fallback_max_attempts": 3,     # max download fallback attempts per image
    "fallback_offset_m": 5,         # offset distance for fallback position
    "fallback_heading_delta": 20,   # heading rotation for fallback (degrees)
    "fallback_pano_max_dist_m": 40, # max distance for nearby pano_id fallback
    "source_outdoor": True,         # add source=outdoor to SV API (filters indoor panos)
}

# Locker size variants.  Source: 13m Ghent unit = 26 modules → ~0.5m/module;
# depth/height estimated from industry norms.  Update when bpost confirms exact specs.
LOCKER_SIZES = {
    "compact":  {"w": 0.6, "d": 0.7, "h": 2.0, "clearance": 1.2},
    "standard": {"w": 1.2, "d": 0.7, "h": 2.0, "clearance": 1.5},
    "large":    {"w": 2.4, "d": 0.7, "h": 2.0, "clearance": 1.5},
    "xl":       {"w": 4.8, "d": 0.7, "h": 2.0, "clearance": 2.0},
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


def _bearing(lat1, lng1, lat2, lng2):
    """Forward azimuth in degrees [0, 360) from point 1 to point 2."""
    dL = (lng2 - lng1) * DEG_TO_RAD
    x  = math.cos(lat2 * DEG_TO_RAD) * math.sin(dL)
    y  = (math.cos(lat1 * DEG_TO_RAD) * math.sin(lat2 * DEG_TO_RAD)
          - math.sin(lat1 * DEG_TO_RAD) * math.cos(lat2 * DEG_TO_RAD) * math.cos(dL))
    return (math.atan2(x, y) / DEG_TO_RAD) % 360


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
        # Start from original candidate dict to preserve sv_* and other fields
        enriched = dict(cand)
        enriched.update({
            "id": i + 1,
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "sector": cand.get("sector", ""),
            "source": cand.get("source", "sv_corridor"),
            "ml_score": ml_score,
            "address": address,
            "suggested_site": suggested_site,
            "nearest_existing_m": round(nearest_existing_m),
            "competitors_nearby": len(comp_nearby),
            "location_context": location_context,
            "nearby_pois": all_pois,
            "pop_gain": cand.get("pop_gain", 0),
            "commentary": cand.get("commentary", ""),
            "contact_info": cand.get("contact_info", ""),
            "status": cand.get("status", "proposed"),
        })
        described.append(enriched)

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
                          f"&fov=90&pitch=10&source=outdoor&key={api_key}")
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


# --- Street graph helpers (used by build_sv_corridor) ---

def _fetch_street_graph(lat_min, lat_max, lng_min, lng_max):
    """Fetch the walkable street network for a bounding box from Overpass, with cache.

    Returns (nodes, graph):
      nodes: {osm_node_id: (lat, lng)}
      graph: {osm_node_id: [(neighbor_id, dist_m), ...]}  -- undirected

    Returns ({}, {}) on any failure so the caller can fall back to straight-line routing.
    """
    cache_key = f"streets:{lat_min:.5f},{lat_max:.5f},{lng_min:.5f},{lng_max:.5f}"
    cache_sha  = hashlib.sha1(cache_key.encode()).hexdigest()
    cache_path = CACHE_DIR / f"{cache_sha}.json"

    raw = None
    if cache_path.exists():
        try:
            with open(cache_path) as fh:
                raw = json.load(fh)
            print(f"  _fetch_street_graph: cache hit ({cache_sha[:8]}…)")
        except Exception as e:
            print(f"  _fetch_street_graph: cache read error ({e}), re-fetching")
            raw = None

    if raw is None:
        bbox_str = f"{lat_min:.5f},{lng_min:.5f},{lat_max:.5f},{lng_max:.5f}"
        query = (
            f"[out:json][timeout:25];\n"
            f"(\n"
            f'  way["highway"~"^(primary|secondary|tertiary|residential|unclassified'
            f'|living_street|pedestrian)$"]({bbox_str});\n'
            f'  way["highway"="service"]["service"!="driveway"]({bbox_str});\n'
            f");\nout geom;"
        )
        try:
            raw = overpass_query(query, retries=2)
            if not raw.get("elements"):
                print("  _fetch_street_graph: Overpass returned 0 elements")
                return {}, {}
            CACHE_DIR.mkdir(exist_ok=True)
            with open(cache_path, "w") as fh:
                json.dump(raw, fh)
        except Exception as e:
            print(f"  _fetch_street_graph: fetch failed ({e})")
            return {}, {}

    nodes  = {}
    graph  = defaultdict(list)
    for way in raw.get("elements", []):
        if way.get("type") != "way":
            continue
        way_nids = way.get("nodes", [])
        way_geom = way.get("geometry", [])
        if len(way_nids) != len(way_geom) or len(way_nids) < 2:
            continue
        for i, nid in enumerate(way_nids):
            g = way_geom[i]
            nodes[nid] = (g["lat"], g["lon"])
        # Build undirected edges (oneway ignored — pedestrian survey, not vehicle routing)
        for i in range(len(way_nids) - 1):
            a_id, b_id = way_nids[i], way_nids[i + 1]
            g_a, g_b   = way_geom[i], way_geom[i + 1]
            dist = haversine(g_a["lat"], g_a["lon"], g_b["lat"], g_b["lon"])
            graph[a_id].append((b_id, dist))
            graph[b_id].append((a_id, dist))

    n_edges = sum(len(v) for v in graph.values())
    print(f"  _fetch_street_graph: {len(nodes)} nodes, {n_edges} directed edges")
    return nodes, dict(graph)


def _route_on_graph(nodes, graph, from_lat, from_lng, to_lat, to_lng,
                    max_snap_m=100):
    """Dijkstra shortest path between two coordinates on the street graph.

    Snaps each coordinate to the nearest graph node. Returns None if either
    snap exceeds max_snap_m or no connected path exists.

    Returns list of (lat, lng) tuples, or None.
    """
    if not nodes or not graph:
        return None

    def _snap(lat, lng):
        best_id, best_dist = None, float("inf")
        for nid, (nlat, nlng) in nodes.items():
            d = haversine(lat, lng, nlat, nlng)
            if d < best_dist:
                best_dist, best_id = d, nid
        return best_id, best_dist

    from_id, from_snap = _snap(from_lat, from_lng)
    to_id,   to_snap   = _snap(to_lat,   to_lng)

    if from_snap > max_snap_m or to_snap > max_snap_m:
        print(f"  _route_on_graph: snap too far "
              f"({from_snap:.0f}m / {to_snap:.0f}m > {max_snap_m}m), fallback")
        return None

    if from_id == to_id:
        nlat, nlng = nodes[from_id]
        return [(nlat, nlng)]

    dist_map = {from_id: 0.0}
    prev_map = {}
    pq = [(0.0, from_id)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist_map.get(u, float("inf")):
            continue
        if u == to_id:
            break
        for v, w in graph.get(u, []):
            nd = d + w
            if nd < dist_map.get(v, float("inf")):
                dist_map[v] = nd
                prev_map[v] = u
                heapq.heappush(pq, (nd, v))

    if to_id not in dist_map:
        print("  _route_on_graph: no path found, fallback")
        return None

    path_ids = []
    cur = to_id
    while cur in prev_map:
        path_ids.append(cur)
        cur = prev_map[cur]
    path_ids.append(from_id)
    path_ids.reverse()
    return [nodes[nid] for nid in path_ids]


def _sample_path(waypoints, spacing_m=22):
    """Sample a multi-segment path at regular intervals.

    Walks the dense node sequence and emits one point every spacing_m metres.
    Bearing is computed per segment so heading arrows are perpendicular to the
    actual street direction at each viewpoint.

    Returns list of {lat, lng, travel_bearing, head_left, head_right}.
    """
    if len(waypoints) < 2:
        return []

    samples = []
    carry   = 0.0  # metres already consumed toward the next spacing window

    for i in range(len(waypoints) - 1):
        lat1, lng1 = waypoints[i]
        lat2, lng2 = waypoints[i + 1]
        seg_len = haversine(lat1, lng1, lat2, lng2)
        if seg_len < 0.001:
            continue  # skip degenerate / duplicate nodes

        bear = _bearing(lat1, lng1, lat2, lng2)
        hl   = (bear - 90) % 360
        hr   = (bear + 90) % 360
        next_in_seg = spacing_m - carry

        while next_in_seg <= seg_len:
            frac = next_in_seg / seg_len
            samples.append({
                "lat":            round(lat1 + frac * (lat2 - lat1), 7),
                "lng":            round(lng1 + frac * (lng2 - lng1), 7),
                "travel_bearing": round(bear, 1),
                "head_left":      round(hl, 1),
                "head_right":     round(hr, 1),
            })
            next_in_seg += spacing_m

        carry = seg_len - (next_in_seg - spacing_m)

    # Always ensure the first waypoint is represented
    lat0, lng0 = waypoints[0]
    if not samples or haversine(lat0, lng0, samples[0]["lat"], samples[0]["lng"]) > 1.0:
        bear0 = _bearing(lat0, lng0, waypoints[1][0], waypoints[1][1])
        samples.insert(0, {
            "lat":            round(lat0, 7),
            "lng":            round(lng0, 7),
            "travel_bearing": round(bear0, 1),
            "head_left":      round((bear0 - 90) % 360, 1),
            "head_right":     round((bear0 + 90) % 360, 1),
        })

    return samples


# --- Street View corridor (ML-guided) ---

def _expand_hot_zone_coverage(nodes, graph, corridor_pts, idw_fn,
                               hot_thresh, corridor_thresh, spacing_m=22):
    """Return extra viewpoints on hot-zone streets not covered by the main corridor.

    Builds the hot subgraph (edges where both endpoints score >= hot_thresh),
    finds which hot nodes are reachable from the current corridor, then samples
    every uncovered hot edge (midpoint > 15 m from any existing corridor point).

    Returns a list of corridor-point dicts (same schema as build_sv_corridor).
    """
    if not nodes or not graph:
        return []

    # Score every graph node via IDW
    node_scores = {nid: idw_fn(lat, lng) for nid, (lat, lng) in nodes.items()}
    hot_nodes = {nid for nid, s in node_scores.items() if s >= hot_thresh}
    if not hot_nodes:
        return []

    # Hot subgraph: only edges where both endpoints are hot
    hot_graph = {}
    for nid in hot_nodes:
        nbrs = [(nbr, d) for nbr, d in graph.get(nid, []) if nbr in hot_nodes]
        if nbrs:
            hot_graph[nid] = nbrs

    # Nodes already "covered" by the current corridor (within 15 m)
    covered = {
        nid for nid, (nlat, nlng) in nodes.items()
        if any(haversine(nlat, nlng, p["lat"], p["lng"]) < 15 for p in corridor_pts)
    }

    # Seed BFS from hot nodes that touch the current corridor
    seed = hot_nodes & covered
    if not seed:
        # Relax to 50 m if no hot node is directly on the corridor
        seed = {
            nid for nid in hot_nodes
            if any(haversine(nodes[nid][0], nodes[nid][1],
                             p["lat"], p["lng"]) < 50 for p in corridor_pts)
        }
    if not seed:
        return []

    # BFS through hot subgraph to find all reachable hot nodes
    reachable = set(seed)
    queue = list(seed)
    while queue:
        cur = queue.pop(0)
        for nbr, _ in hot_graph.get(cur, []):
            if nbr not in reachable:
                reachable.add(nbr)
                queue.append(nbr)

    # Sample every reachable hot edge not already covered by the corridor
    extra_pts = []
    seen_edges = set()
    for cur_id in reachable:
        cur_lat, cur_lng = nodes[cur_id]
        for nbr_id, _ in hot_graph.get(cur_id, []):
            if nbr_id not in reachable:
                continue
            edge_key = frozenset([cur_id, nbr_id])
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)

            nbr_lat, nbr_lng = nodes[nbr_id]
            mid_lat = (cur_lat + nbr_lat) / 2
            mid_lng = (cur_lng + nbr_lng) / 2
            if any(haversine(mid_lat, mid_lng, p["lat"], p["lng"]) < 15
                   for p in corridor_pts):
                continue  # already covered

            for pt in _sample_path([(cur_lat, cur_lng), (nbr_lat, nbr_lng)], spacing_m):
                score = idw_fn(pt["lat"], pt["lng"])
                if score >= corridor_thresh:
                    extra_pts.append({
                        "lat":            pt["lat"],
                        "lng":            pt["lng"],
                        "idw_score":      round(score, 4),
                        "head_left":      pt["head_left"],
                        "head_right":     pt["head_right"],
                        "travel_bearing": pt["travel_bearing"],
                        "is_anchor":      False,
                    })

    return extra_pts


def _build_screening_sv_calls(corridor_pts, stride=3):
    """Sparse alternating L/R subset of corridor points for a cheap screening pass.

    Takes every stride-th corridor point, alternating which side (left/right) so
    both sides of the street are sampled across the route without doubling every call.
    stride=3 on 79 pts → ~27 calls (vs 158 for full L+R).
    """
    calls = []
    for i, pt in enumerate(corridor_pts):
        if i % stride != 0:
            continue
        side    = "left" if (i // stride) % 2 == 0 else "right"
        heading = pt["head_left"] if side == "left" else pt["head_right"]
        calls.append({
            "lat":          pt["lat"],
            "lng":          pt["lng"],
            "heading":      heading,
            "pitch":        5,
            "fov":          90,
            "idw_score":    pt["idw_score"],
            "side":         side,
            "is_anchor":    pt.get("is_anchor", False),
            "viewpoint_idx": i,
        })
    return calls


def _add_junction_lookaround(corridor_pts, nodes, graph, snap_radius_m=15):
    """Add SV calls for street-graph junction branches not covered by left/right.

    For each corridor point within snap_radius_m of a junction node (degree >= 3),
    checks every connected branch bearing.  Branches within ±45° of the existing
    left or right heading (fov=90 each) are already covered; all others get an
    extra call with side='junction'.

    Mutates corridor_pts in-place: adds is_junction=True and junction_branches.
    Returns list of extra sv_call dicts.
    """
    if not nodes or not graph:
        return []
    junction_nodes = {nid for nid, nbrs in graph.items() if len(nbrs) >= 3}
    if not junction_nodes:
        return []

    extra_calls = []
    for pt in corridor_pts:
        best_jnid, best_dist = None, float("inf")
        for jnid in junction_nodes:
            jlat, jlng = nodes[jnid]
            d = haversine(pt["lat"], pt["lng"], jlat, jlng)
            if d < best_dist:
                best_dist, best_jnid = d, jnid

        if best_dist > snap_radius_m:
            continue

        jlat, jlng    = nodes[best_jnid]
        travel_bear   = pt["travel_bearing"]
        hl = (travel_bear - 90) % 360
        hr = (travel_bear + 90) % 360

        branch_bearings = [
            round(_bearing(jlat, jlng, nodes[nbr][0], nodes[nbr][1]), 1)
            for nbr, _ in graph[best_jnid]
        ]
        pt["is_junction"]      = True
        pt["junction_branches"] = branch_bearings

        for bear in branch_bearings:
            diff_l = abs((bear - hl + 180) % 360 - 180)
            diff_r = abs((bear - hr + 180) % 360 - 180)
            if diff_l <= 45 or diff_r <= 45:
                continue  # already well-covered
            extra_calls.append({
                "lat":          pt["lat"],
                "lng":          pt["lng"],
                "heading":      bear,
                "pitch":        5,
                "fov":          90,
                "idw_score":    pt["idw_score"],
                "side":         "junction",
                "is_anchor":    pt.get("is_anchor", False),
                "viewpoint_idx": pt.get("viewpoint_idx", 0),
            })

    n_junctions = sum(1 for p in corridor_pts if p.get("is_junction"))
    if extra_calls:
        print(f"  Junction look-around: {n_junctions} junction pts → "
              f"+{len(extra_calls)} extra calls")
    return extra_calls


def _build_detail_sv_calls(corridor_pts, interesting_coords, nodes, graph, radius_m=35):
    """Full L+R calls for corridor points within radius_m of any interesting coord.

    Also appends junction look-around calls for those in-zone points.
    interesting_coords is a list of (lat, lng) tuples flagged by the screening pass.
    """
    in_zone = [
        pt for pt in corridor_pts
        if any(haversine(pt["lat"], pt["lng"], lat, lng) < radius_m
               for lat, lng in interesting_coords)
    ]
    calls = [
        {
            "lat":          pt["lat"],
            "lng":          pt["lng"],
            "heading":      h,
            "pitch":        5,
            "fov":          90,
            "idw_score":    pt["idw_score"],
            "side":         side,
            "is_anchor":    pt.get("is_anchor", False),
            "viewpoint_idx": pt.get("viewpoint_idx", 0),
        }
        for pt in in_zone
        for side, h in [("left", pt["head_left"]), ("right", pt["head_right"])]
    ]
    if nodes and graph:
        calls += _add_junction_lookaround(in_zone, nodes, graph)
    print(f"  Detail pass: {len(in_zone)} in-zone pts → {len(calls)} calls "
          f"({len(interesting_coords)} interesting coords, radius={radius_m}m)")
    return calls


def _build_detail_sv_calls_v2(corridor_pts, interesting_coords, nodes, graph,
                               config=None):
    """v2.0 Multi-angle detail capture for interesting corridor locations.

    For each viewpoint near an interesting coord, captures:
    - Standard L+R (fov=90) — existing perpendicular views
    - Junction branches (fov=90) — branch directions at intersections
    - Look-toward (fov=90) — adjacent VPs ±N looking TOWARD candidate
    - Wide context (fov=120) — broader scene understanding
    - Tight detail (fov=60) — close-up of wall/frontage

    All calls include travel_bearing for fallback offset.
    Returns list of SV call dicts.
    """
    cfg = config or SV_CONFIG
    radius_m = cfg.get("clustering_radius_m", 35)
    look_range = cfg.get("look_toward_range", 2)
    fov_set = cfg.get("detail_fov_set", [60, 90, 120])

    # Find in-zone viewpoints (near an interesting coord)
    in_zone_idxs = set()
    for i, pt in enumerate(corridor_pts):
        for lat, lng in interesting_coords:
            if haversine(pt["lat"], pt["lng"], lat, lng) < radius_m:
                in_zone_idxs.add(i)
                break
    in_zone = [corridor_pts[i] for i in sorted(in_zone_idxs)]

    calls = []

    # Standard L+R (fov=90)
    for pt in in_zone:
        for side, h in [("left", pt["head_left"]), ("right", pt["head_right"])]:
            calls.append({
                "lat":           pt["lat"],
                "lng":           pt["lng"],
                "heading":       h,
                "pitch":         5,
                "fov":           90,
                "idw_score":     pt["idw_score"],
                "side":          side,
                "is_anchor":     pt.get("is_anchor", False),
                "viewpoint_idx": pt.get("viewpoint_idx", 0),
                "travel_bearing": pt.get("travel_bearing", 0),
                "capture_type":  "standard",
            })

    # Wide context (fov=120) — L+R at wider FOV
    if 120 in fov_set:
        for pt in in_zone:
            for side, h in [("left", pt["head_left"]), ("right", pt["head_right"])]:
                calls.append({
                    "lat":           pt["lat"],
                    "lng":           pt["lng"],
                    "heading":       h,
                    "pitch":         5,
                    "fov":           120,
                    "idw_score":     pt["idw_score"],
                    "side":          f"wide_{side}",
                    "is_anchor":     pt.get("is_anchor", False),
                    "viewpoint_idx": pt.get("viewpoint_idx", 0),
                    "travel_bearing": pt.get("travel_bearing", 0),
                    "capture_type":  "wide",
                })

    # Tight detail (fov=60) — best-side only (or both if no best_side known yet)
    if 60 in fov_set:
        for pt in in_zone:
            for side, h in [("left", pt["head_left"]), ("right", pt["head_right"])]:
                calls.append({
                    "lat":           pt["lat"],
                    "lng":           pt["lng"],
                    "heading":       h,
                    "pitch":         5,
                    "fov":           60,
                    "idw_score":     pt["idw_score"],
                    "side":          f"tight_{side}",
                    "is_anchor":     pt.get("is_anchor", False),
                    "viewpoint_idx": pt.get("viewpoint_idx", 0),
                    "travel_bearing": pt.get("travel_bearing", 0),
                    "capture_type":  "tight",
                })

    # Look-toward: adjacent viewpoints looking TOWARD the interesting coord
    # For each interesting coord, find the ±N closest corridor points and
    # compute a heading from that adjacent VP toward the interesting coord
    corridor_idx_map = {pt.get("viewpoint_idx", i): i
                        for i, pt in enumerate(corridor_pts)}
    for int_lat, int_lng in interesting_coords:
        # Find the closest corridor point to this interesting coord
        closest_idx = min(range(len(corridor_pts)),
                          key=lambda i: haversine(corridor_pts[i]["lat"],
                                                   corridor_pts[i]["lng"],
                                                   int_lat, int_lng))
        # Look at adjacent viewpoints
        for delta in range(-look_range, look_range + 1):
            adj_idx = closest_idx + delta
            if delta == 0 or adj_idx < 0 or adj_idx >= len(corridor_pts):
                continue
            adj_pt = corridor_pts[adj_idx]
            # Only if this adjacent point is not already in the zone
            # (to avoid duplicates with standard L/R)
            if adj_idx in in_zone_idxs:
                continue
            # Heading from adjacent VP toward the interesting coord
            toward_heading = _bearing(adj_pt["lat"], adj_pt["lng"], int_lat, int_lng)
            calls.append({
                "lat":           adj_pt["lat"],
                "lng":           adj_pt["lng"],
                "heading":       round(toward_heading, 1),
                "pitch":         5,
                "fov":           90,
                "idw_score":     adj_pt.get("idw_score", 0),
                "side":          f"look_toward_{adj_pt.get('viewpoint_idx', adj_idx)}",
                "is_anchor":     adj_pt.get("is_anchor", False),
                "viewpoint_idx": adj_pt.get("viewpoint_idx", adj_idx),
                "travel_bearing": adj_pt.get("travel_bearing", 0),
                "capture_type":  "look_toward",
                "target_coord":  (int_lat, int_lng),
            })

    # Junction look-around (existing function)
    if nodes and graph:
        junction_calls = _add_junction_lookaround(in_zone, nodes, graph)
        for jc in junction_calls:
            jc["travel_bearing"] = jc.get("travel_bearing", 0)
            jc["capture_type"] = "junction"
        calls += junction_calls

    # Deduplicate: same (viewpoint_idx, side) only once
    seen = set()
    deduped = []
    for c in calls:
        key = (c["viewpoint_idx"], c["side"])
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    print(f"  Detail v2: {len(in_zone)} in-zone pts → {len(deduped)} calls "
          f"({len(interesting_coords)} interesting coords, radius={radius_m}m)")
    by_type = {}
    for c in deduped:
        t = c.get("capture_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    print(f"    Breakdown: {by_type}")
    return deduped


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

    n_hot = len(hot)
    if n_hot <= 8:
        # Brute force for small sets (8! = 40320)
        best_order, best_score = max(
            ((p, _score_order(p)) for p in itertools.permutations(range(n_hot))),
            key=lambda x: x[1])
    else:
        # Greedy nearest-neighbor + 2-opt for large sets
        print(f"  build_sv_corridor: {n_hot} hot cells, using greedy+2-opt ordering")
        coords = [(float(hot.iloc[i]["lat"]), float(hot.iloc[i]["lng"])) for i in range(n_hot)]
        visited = [False] * n_hot
        order = [0]
        visited[0] = True
        for _ in range(n_hot - 1):
            last = order[-1]
            best_next, best_d = -1, float("inf")
            for j in range(n_hot):
                if not visited[j]:
                    d = haversine(coords[last][0], coords[last][1],
                                  coords[j][0], coords[j][1])
                    if d < best_d:
                        best_d, best_next = d, j
            visited[best_next] = True
            order.append(best_next)
        # 2-opt improvement (max 5 passes to bound runtime)
        cur_score = _score_order(tuple(order))
        for _pass in range(5):
            improved = False
            for i in range(1, n_hot - 1):
                for j in range(i + 1, n_hot):
                    new_order = order[:i] + order[i:j+1][::-1] + order[j+1:]
                    new_score = _score_order(tuple(new_order))
                    if new_score > cur_score:
                        order = new_order
                        cur_score = new_score
                        improved = True
            if not improved:
                break
        best_order = tuple(order)
        best_score = cur_score
    anchors = [hot.iloc[i] for i in best_order]

    # Fetch street graph for anchor bbox + 50m margin
    all_lats = [float(a["lat"]) for a in anchors]
    all_lngs = [float(a["lng"]) for a in anchors]
    mid_lat  = sum(all_lats) / len(all_lats)
    lat_m    = 1 / 111_000
    lng_m    = 1 / (111_000 * math.cos(mid_lat * DEG_TO_RAD))
    margin   = 50
    graph_nodes, graph_edges = _fetch_street_graph(
        min(all_lats) - margin * lat_m,
        max(all_lats) + margin * lat_m,
        min(all_lngs) - margin * lng_m,
        max(all_lngs) + margin * lng_m,
    )
    use_street_graph = bool(graph_nodes)

    # Route each anchor-to-anchor segment on the street graph
    all_pts = []
    for i in range(len(anchors) - 1):
        a, b       = anchors[i], anchors[i + 1]
        alat, alng = float(a["lat"]), float(a["lng"])
        blat, blng = float(b["lat"]), float(b["lng"])

        routed = None
        if use_street_graph:
            routed = _route_on_graph(graph_nodes, graph_edges, alat, alng, blat, blng)

        if routed is None:
            # Fallback: straight-line synthetic waypoints
            total_m = haversine(alat, alng, blat, blng)
            n_steps = max(1, int(total_m / spacing_m))
            routed  = [(alat + t / n_steps * (blat - alat),
                        alng + t / n_steps * (blng - alng))
                       for t in range(n_steps + 1)]

        for pt in _sample_path(routed, spacing_m):
            all_pts.append({
                "lat":            pt["lat"],
                "lng":            pt["lng"],
                "idw_score":      round(_idw(pt["lat"], pt["lng"]), 4),
                "head_left":      pt["head_left"],
                "head_right":     pt["head_right"],
                "travel_bearing": pt["travel_bearing"],
                "is_anchor":      False,
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

    # Expand coverage to all reachable hot-zone street edges
    n_extra = 0
    if use_street_graph and deduped:
        extra = _expand_hot_zone_coverage(
            graph_nodes, graph_edges, deduped, _idw,
            hot_thresh, corridor_thresh, spacing_m,
        )
        n_extra = len(extra)
        for p in extra:
            if not any(haversine(p["lat"], p["lng"], q["lat"], q["lng"]) < 5
                       for q in deduped):
                deduped.append(p)
        if n_extra:
            print(f"  Hot-zone expansion: +{n_extra} extra viewpoints on uncovered hot streets")

    # Filter to hot corridor
    corridor = [p for p in deduped if p["idw_score"] >= corridor_thresh]
    if not corridor:
        print(f"  build_sv_corridor: no points above corridor_thresh {corridor_thresh}, skipping")
        return None

    # Tag each corridor point with its index (used by screening/detail pass helpers)
    for i, pt in enumerate(corridor):
        pt["viewpoint_idx"] = i

    sv_calls = [
        {"lat": p["lat"], "lng": p["lng"], "heading": h,
         "pitch": 5, "fov": 90, "idw_score": p["idw_score"],
         "side": side, "is_anchor": p.get("is_anchor", False),
         "viewpoint_idx": i}
        for i, p in enumerate(corridor)
        for side, h in [("left", p["head_left"]), ("right", p["head_right"])]
    ]

    routing_mode = "street-graph+hot-expansion" if (use_street_graph and n_extra) else (
        "street-graph" if use_street_graph else "straight-line"
    )
    print(f"  SV corridor: {len(anchors)} anchors → {len(corridor)} viewpoints "
          f"→ {len(sv_calls)} calls (corridor avg IDW={best_score:.3f}, "
          f"routing={routing_mode})")

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
            "routing_mode": routing_mode,
            "n_hot_expansion_pts": n_extra,
        },
        "anchor_order": [
            {"lat": float(a["lat"]), "lng": float(a["lng"]), "score": float(a["score"])}
            for a in anchors
        ],
        "corridor_points": corridor,
        "sv_calls": sv_calls,
        # Internal: graph passed to two-pass analysis helpers; not JSON-serialised
        "_graph_nodes": graph_nodes,
        "_graph_edges": graph_edges,
    }


# --- SV v2.0: Smart download with metadata & fallback ---

def _fetch_sv_metadata(lat, lng, api_key):
    """Fetch Street View metadata for a location.

    Returns dict with pano_id, actual coords, distance from requested location,
    or None if no coverage.
    """
    meta_url = (f"https://maps.googleapis.com/maps/api/streetview/metadata?"
                f"location={lat},{lng}&key={api_key}")
    try:
        req = urllib.request.Request(meta_url, headers=HEADERS)
        meta = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        if meta.get("status") != "OK":
            return None
        loc = meta.get("location", {})
        actual_lat = loc.get("lat", lat)
        actual_lng = loc.get("lng", lng)
        dist = haversine(lat, lng, actual_lat, actual_lng)
        return {
            "pano_id": meta.get("pano_id"),
            "lat": actual_lat,
            "lng": actual_lng,
            "dist_from_requested": round(dist, 1),
            "date": meta.get("date", ""),
            "status": "OK",
        }
    except Exception:
        return None


def _validate_sv_image(img_path):
    """Check if a downloaded SV image is valid (not a gray placeholder).

    Google returns a tiny gray image when no imagery is available.
    Returns False for placeholder/invalid images.
    """
    try:
        size = img_path.stat().st_size
        if size < 5000:  # Gray placeholders are typically < 5KB
            return False
        return True
    except Exception:
        return False


def _offset_point(lat, lng, bearing_deg, distance_m):
    """Move a point along a bearing by distance_m metres. Returns (new_lat, new_lng)."""
    d = distance_m / R_EARTH
    brng = bearing_deg * DEG_TO_RAD
    lat1 = lat * DEG_TO_RAD
    lng1 = lng * DEG_TO_RAD
    lat2 = math.asin(math.sin(lat1) * math.cos(d) +
                      math.cos(lat1) * math.sin(d) * math.cos(brng))
    lng2 = lng1 + math.atan2(math.sin(brng) * math.sin(d) * math.cos(lat1),
                              math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return round(math.degrees(lat2), 7), round(math.degrees(lng2), 7)


def _download_sv_image(lat, lng, heading, pitch, fov, api_key, img_path):
    """Download a single SV image. Returns True if valid image saved."""
    source = "&source=outdoor" if SV_CONFIG.get("source_outdoor", True) else ""
    sv_url = (f"https://maps.googleapis.com/maps/api/streetview?"
              f"size=800x600&location={lat},{lng}"
              f"&heading={heading}&pitch={pitch}&fov={fov}{source}&key={api_key}")
    try:
        req = urllib.request.Request(sv_url, headers=HEADERS)
        resp = urllib.request.urlopen(req, timeout=15)
        data = resp.read()
        with open(img_path, "wb") as fh:
            fh.write(data)
        return _validate_sv_image(img_path)
    except Exception:
        return False


def _download_sv_with_pano(pano_id, heading, pitch, fov, api_key, img_path):
    """Download SV image using a specific pano_id. Returns True if valid."""
    source = "&source=outdoor" if SV_CONFIG.get("source_outdoor", True) else ""
    sv_url = (f"https://maps.googleapis.com/maps/api/streetview?"
              f"size=800x600&pano={pano_id}"
              f"&heading={heading}&pitch={pitch}&fov={fov}{source}&key={api_key}")
    try:
        req = urllib.request.Request(sv_url, headers=HEADERS)
        resp = urllib.request.urlopen(req, timeout=15)
        data = resp.read()
        with open(img_path, "wb") as fh:
            fh.write(data)
        return _validate_sv_image(img_path)
    except Exception:
        return False


def _classify_image_heuristic(img_path):
    """Cheap PIL heuristic to detect likely indoor images.

    Interior photos typically have dark upper bands (ceilings instead of sky)
    and lower overall brightness variance.

    Returns dict with sky_brightness (0-255) and likely_indoor (bool).
    """
    try:
        from PIL import Image
        img = Image.open(img_path).convert("L")  # grayscale
        w, h = img.size
        # Top 15% of image = sky/ceiling band
        sky_band = img.crop((0, 0, w, int(h * 0.15)))
        sky_pixels = list(sky_band.getdata())
        sky_brightness = sum(sky_pixels) / len(sky_pixels) if sky_pixels else 128
        # Full image brightness for variance check
        all_pixels = list(img.getdata())
        avg = sum(all_pixels) / len(all_pixels)
        variance = sum((p - avg) ** 2 for p in all_pixels) / len(all_pixels)
        # Indoor: dark ceiling (sky_brightness < 80) or very low variance
        likely_indoor = sky_brightness < 80 or (sky_brightness < 110 and variance < 1500)
        return {
            "sky_brightness": round(sky_brightness, 1),
            "brightness_variance": round(variance, 1),
            "likely_indoor": likely_indoor,
        }
    except Exception:
        return {"sky_brightness": 128, "brightness_variance": 3000, "likely_indoor": False}


def _download_sv_with_fallback(call, api_key, img_path, config=None):
    """Smart SV download with fallback strategies.

    Strategy chain (stop on first valid image):
    1. Primary: exact coords + heading
    2. Offset: ±offset_m along travel direction
    3. Rotated heading: ±heading_delta from original
    4. Use nearby pano_id from metadata (if within max_dist)

    Records which strategy worked on the call dict under 'download_strategy'.
    Returns True if a valid image was saved, False otherwise.
    """
    cfg = config or SV_CONFIG
    lat, lng = call["lat"], call["lng"]
    heading = call["heading"]
    pitch = call.get("pitch", 5)
    fov = call.get("fov", 90)
    travel_bearing = call.get("travel_bearing", heading)

    # Strategy 1: Primary — exact coords + heading
    if _download_sv_image(lat, lng, heading, pitch, fov, api_key, img_path):
        call["download_strategy"] = "primary"
        return True

    # Strategy 2: Offset along travel direction
    offset_m = cfg.get("fallback_offset_m", 5)
    for direction in [0, 180]:  # forward and backward
        off_bearing = (travel_bearing + direction) % 360
        off_lat, off_lng = _offset_point(lat, lng, off_bearing, offset_m)
        meta = _fetch_sv_metadata(off_lat, off_lng, api_key)
        if meta and meta["dist_from_requested"] < cfg.get("fallback_pano_max_dist_m", 40):
            if _download_sv_image(off_lat, off_lng, heading, pitch, fov, api_key, img_path):
                call["download_strategy"] = f"offset_{'+' if direction == 0 else '-'}{offset_m}m"
                time.sleep(0.1)
                return True
        time.sleep(0.1)

    # Strategy 3: Rotated heading
    delta = cfg.get("fallback_heading_delta", 20)
    for rot in [delta, -delta]:
        new_heading = (heading + rot) % 360
        if _download_sv_image(lat, lng, new_heading, pitch, fov, api_key, img_path):
            call["download_strategy"] = f"rotated_{'+' if rot > 0 else ''}{rot}deg"
            return True

    # Strategy 4: Use nearby pano_id
    meta = _fetch_sv_metadata(lat, lng, api_key)
    if meta and meta.get("pano_id"):
        max_dist = cfg.get("fallback_pano_max_dist_m", 40)
        if meta["dist_from_requested"] <= max_dist:
            if _download_sv_with_pano(meta["pano_id"], heading, pitch, fov, api_key, img_path):
                call["download_strategy"] = f"pano_{meta['pano_id'][:8]}_dist{meta['dist_from_requested']}m"
                return True

    call["download_strategy"] = "failed"
    return False


def download_sv_corridor_images(sv_calls, img_dir, prefix="", use_fallback=True):
    """Download Street View images for every call in the ML corridor.

    Images are saved as {prefix}sv_{viewpoint_idx}_{side}.png.
    Add prefix="screen_" or "detail_" to keep passes separate.
    Skips quietly if no Google Maps API key is configured.

    v2.0: With use_fallback=True, uses smart fallback strategies for
    blocked/unavailable views.
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

    print(f"  Downloading {len(sv_calls)} corridor Street View images (prefix='{prefix}')...")
    saved = 0
    skipped = 0
    fallback_used = 0

    for call in sv_calls:
        lat, lng = call["lat"], call["lng"]
        side     = call["side"]
        vidx     = call.get("viewpoint_idx", 0)
        fname    = img_dir / f"{prefix}sv_{vidx}_{side}.png"

        if fname.exists() and _validate_sv_image(fname):
            saved += 1
            skipped += 1
            call["download_strategy"] = "cached"
            call["heuristic"] = _classify_image_heuristic(fname)
            continue  # already downloaded (e.g. on re-run)

        if use_fallback:
            if _download_sv_with_fallback(call, api_key, fname):
                saved += 1
                call["heuristic"] = _classify_image_heuristic(fname)
                strategy = call.get("download_strategy", "unknown")
                if strategy != "primary":
                    fallback_used += 1
            # rate limit
            time.sleep(0.15)
        else:
            # Legacy direct download (no fallback)
            meta_url = (f"https://maps.googleapis.com/maps/api/streetview/metadata?"
                        f"location={lat},{lng}&key={api_key}")
            try:
                req  = urllib.request.Request(meta_url, headers=HEADERS)
                meta = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
                if meta.get("status") != "OK":
                    continue
            except Exception:
                continue

            heading = call["heading"]
            source = "&source=outdoor" if SV_CONFIG.get("source_outdoor", True) else ""
            sv_url = (f"https://maps.googleapis.com/maps/api/streetview?"
                      f"size=800x600&location={lat},{lng}"
                      f"&heading={heading}&pitch={call.get('pitch', 5)}"
                      f"&fov={call.get('fov', 90)}{source}&key={api_key}")
            try:
                req2  = urllib.request.Request(sv_url, headers=HEADERS)
                resp2 = urllib.request.urlopen(req2, timeout=15)
                with open(fname, "wb") as fh:
                    fh.write(resp2.read())
                saved += 1
                call["heuristic"] = _classify_image_heuristic(fname)
            except Exception as e:
                print(f"    corridor SV {prefix}{vidx}_{side}: download failed: {e}")

            time.sleep(0.15)

    stats = f"  Corridor images saved: {saved}/{len(sv_calls)}"
    if skipped:
        stats += f" ({skipped} cached)"
    if fallback_used:
        stats += f" ({fallback_used} via fallback)"
    print(stats)


def analyze_sv_corridor_images(sv_calls, img_dir, prefix="", model="claude-sonnet-4-6",
                                output_path=None, interesting_threshold=6):
    """Analyse downloaded corridor SV images with Claude vision.

    Groups images by viewpoint_idx, sends each group to Claude with the
    locker placement prompt, and writes a JSON results file.

    Screening pass (sonnet): identify interesting coords for detail pass.
    Detail pass (opus):      final placement assessment with markup coordinates.

    Returns a dict with keys: meta, viewpoints, interesting_coords, top_candidates.
    Returns None if ANTHROPIC_API_KEY is not set.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    except ImportError:
        pass

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("  analyze_sv_corridor_images: no ANTHROPIC_API_KEY, skipping")
        return None

    import base64
    from datetime import datetime

    img_dir = Path(img_dir)

    # Build index: viewpoint_idx → {lat, lng, idw_score, images: [path, ...]}
    vp_index = {}
    for call in sv_calls:
        vidx = call.get("viewpoint_idx", 0)
        if vidx not in vp_index:
            vp_index[vidx] = {
                "viewpoint_idx": vidx,
                "lat":           call["lat"],
                "lng":           call["lng"],
                "idw_score":     call["idw_score"],
                "is_anchor":     call.get("is_anchor", False),
                "is_junction":   call.get("is_junction", False),
                "images":        [],
            }
        img_path = img_dir / f"{prefix}sv_{vidx}_{call['side']}.png"
        if img_path.exists():
            vp_index[vidx]["images"].append(str(img_path))

    # Remove viewpoints with no downloaded images
    vp_index = {k: v for k, v in vp_index.items() if v["images"]}
    print(f"  analyze_sv_corridor_images: {len(vp_index)} viewpoints with images "
          f"(pass prefix='{prefix}', model={model})")

    locker_prompt = (
        "You are assessing a street location for a bpost bbox parcel locker installation.\n\n"
        "Available locker sizes:\n"
        "  Compact:  0.6m wide × 0.7m deep × 2.0m tall  (needs 1.2m clear passage)\n"
        "  Standard: 1.2m wide × 0.7m deep × 2.0m tall  (needs 1.5m clear passage)\n"
        "  Large:    2.4m wide × 0.7m deep × 2.0m tall  (needs 1.5m clear passage)\n"
        "  XL:       4.8m+ wide × 0.7m deep × 2.0m tall (needs 2.0m clear passage)\n\n"
        "Requirements for ANY size: level paved ground, unobstructed wall/frontage,\n"
        "no blocking of exits, wheelchair access, or shop entrances.\n\n"
        "Images show {sides} of the street at this location. "
        "Respond ONLY with valid JSON (no markdown fences):\n"
        '{{\n'
        '  "largest_viable_size": "compact"|"standard"|"large"|"xl"|"none",\n'
        '  "placement_score": 0-10,\n'
        '  "best_side": "left"|"right"|"junction"|"none",\n'
        '  "placement_x_pct": 0-100,\n'
        '  "available_width_m": 1.5,\n'
        '  "footpath_width_estimate": "e.g. ~2.5m, adequate for standard",\n'
        '  "obstacles": ["list"],\n'
        '  "wall_available": true|false,\n'
        '  "surface": "paved"|"cobblestone"|"unpaved"|"unclear",\n'
        '  "notes": "one sentence on key factor"\n'
        '}}\n'
        'placement_x_pct: horizontal center of locker as % of image width (0=left, 100=right).\n'
        'available_width_m: estimated clear wall space in metres.'
    )

    results = []
    for vidx in sorted(vp_index.keys()):
        vp = vp_index[vidx]
        sides_str = " and ".join(
            Path(p).stem.split("_")[-1] for p in vp["images"]
        )
        prompt = locker_prompt.format(sides=sides_str)

        content = []
        for img_path in vp["images"]:
            with open(img_path, "rb") as fh:
                raw = fh.read()
            b64 = base64.standard_b64encode(raw).decode()
            # Auto-detect media type from file header (Google SV returns JPEG despite .png ext)
            if raw[:3] == b'\xff\xd8\xff':
                media_type = "image/jpeg"
            elif raw[:8] == b'\x89PNG\r\n\x1a\n':
                media_type = "image/png"
            else:
                media_type = "image/jpeg"  # safe default
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
        content.append({"type": "text", "text": prompt})

        try:
            req_body = json.dumps({
                "model":      model,
                "max_tokens": 512,
                "messages":   [{"role": "user", "content": content}],
            }).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=req_body,
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=30)
            body = json.loads(resp.read().decode())
            raw  = body["content"][0]["text"].strip()
            # Strip any accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            analysis = json.loads(raw)
        except Exception as e:
            print(f"    viewpoint {vidx}: analysis failed ({e})")
            analysis = {"largest_viable_size": "none", "placement_score": 0,
                        "notes": f"analysis error: {e}"}

        results.append({**vp, "analysis": analysis})
        time.sleep(0.3)

    # Interesting coords = viewpoints scoring >= interesting_threshold
    interesting_coords = [
        (r["lat"], r["lng"])
        for r in results
        if r["analysis"].get("placement_score", 0) >= interesting_threshold
        and r["analysis"].get("largest_viable_size", "none") != "none"
    ]

    # Top candidates sorted by placement_score
    top_candidates = sorted(
        [r for r in results if r["analysis"].get("placement_score", 0) >= interesting_threshold],
        key=lambda x: x["analysis"].get("placement_score", 0),
        reverse=True,
    )[:10]

    n_pass = sum(1 for r in results if r["analysis"].get("largest_viable_size", "none") != "none")
    output_dict = {
        "meta": {
            "pass":         "screening" if "screen" in prefix else "detail",
            "sector":       "unknown",
            "model":        model,
            "version":      SV_ANALYSIS_VERSION,
            "n_analysed":   len(results),
            "n_viable":     n_pass,
            "at":           datetime.utcnow().isoformat(),
        },
        "viewpoints":        results,
        "interesting_coords": interesting_coords,
        "top_candidates":    top_candidates,
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        class _SafeEnc(json.JSONEncoder):
            def default(self, obj):
                import numpy as np
                if isinstance(obj, (np.integer,)):  return int(obj)
                if isinstance(obj, (np.floating,)): return float(obj)
                return super().default(obj)

        with open(output_path, "w") as fh:
            json.dump(output_dict, fh, indent=2, cls=_SafeEnc)
        print(f"  Analysis saved → {output_path.name} "
              f"({n_pass}/{len(results)} viable, {len(interesting_coords)} interesting)")

    return output_dict


def _cluster_candidates(sv_calls, interesting_coords, config=None):
    """Cluster viewpoints by proximity to interesting coordinates.

    Groups all SV calls that are within clustering_radius_m of the same
    interesting coord into candidate groups. Returns list of groups, each
    a dict with 'center' (lat, lng), 'calls' (list of call dicts).
    """
    cfg = config or SV_CONFIG
    radius = cfg.get("clustering_radius_m", 35)

    groups = []
    for int_lat, int_lng in interesting_coords:
        nearby = [c for c in sv_calls
                  if haversine(c["lat"], c["lng"], int_lat, int_lng) < radius]
        if nearby:
            groups.append({
                "center": (int_lat, int_lng),
                "calls": nearby,
            })

    # Merge overlapping groups (share > 50% of calls)
    merged = []
    used = set()
    for i, g1 in enumerate(groups):
        if i in used:
            continue
        current = dict(g1)
        call_set = set(id(c) for c in g1["calls"])
        for j, g2 in enumerate(groups):
            if j <= i or j in used:
                continue
            g2_set = set(id(c) for c in g2["calls"])
            overlap = len(call_set & g2_set)
            if overlap > 0.5 * min(len(call_set), len(g2_set)):
                # Merge: use centroid of both centers
                c1, c2 = current["center"], g2["center"]
                current["center"] = (
                    round((c1[0] + c2[0]) / 2, 7),
                    round((c1[1] + c2[1]) / 2, 7),
                )
                # Union of calls (deduplicate by id)
                existing_ids = {id(c) for c in current["calls"]}
                for c in g2["calls"]:
                    if id(c) not in existing_ids:
                        current["calls"].append(c)
                        existing_ids.add(id(c))
                call_set = existing_ids
                used.add(j)
        merged.append(current)
        used.add(i)

    return merged


def analyze_sv_corridor_grouped(sv_calls, img_dir, interesting_coords,
                                 prefix="detail_", config=None,
                                 output_path=None):
    """v2.0 Grouped multi-angle Opus analysis.

    Clusters viewpoints by proximity to interesting coords, sends ALL images
    for each candidate group in a single Opus call. This gives Opus the full
    context — multiple angles, converging views, wide + tight shots — for
    a comprehensive placement assessment.

    Returns dict with: meta, candidate_groups, top_candidates.
    """
    cfg = config or SV_CONFIG
    max_images = cfg.get("max_images_per_candidate", 12)

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    except ImportError:
        pass

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("  analyze_sv_corridor_grouped: no ANTHROPIC_API_KEY, skipping")
        return None

    import base64
    from datetime import datetime

    img_dir = Path(img_dir)

    # Cluster candidates
    groups = _cluster_candidates(sv_calls, interesting_coords, cfg)
    print(f"  Grouped analysis: {len(groups)} candidate groups from "
          f"{len(interesting_coords)} interesting coords")

    # Build image inventory for each group
    for group in groups:
        images = []
        seen_paths = set()
        for call in group["calls"]:
            vidx = call.get("viewpoint_idx", 0)
            side = call["side"]
            img_path = img_dir / f"{prefix}sv_{vidx}_{side}.png"
            if img_path.exists() and str(img_path) not in seen_paths:
                if _validate_sv_image(img_path):
                    heuristic = call.get("heuristic") or _classify_image_heuristic(img_path)
                    images.append({
                        "path": str(img_path),
                        "viewpoint_idx": vidx,
                        "side": side,
                        "capture_type": call.get("capture_type", "standard"),
                        "fov": call.get("fov", 90),
                        "heading": call.get("heading", 0),
                        "heuristic_indoor": heuristic.get("likely_indoor", False),
                    })
                    seen_paths.add(str(img_path))
        # Prioritize: standard > tight > wide > look_toward > junction
        type_priority = {"standard": 0, "tight": 1, "wide": 2, "look_toward": 3, "junction": 4}
        images.sort(key=lambda x: type_priority.get(x["capture_type"], 5))
        group["images"] = images[:max_images]

    # Prompt for grouped analysis
    grouped_prompt = (
        "You are assessing a street location for a bpost bbox parcel locker installation.\n\n"
        "You are viewing MULTIPLE angles of the SAME candidate location — including:\n"
        "- Standard left/right views (perpendicular to street, fov=90°)\n"
        "- Wide context views (fov=120°) for broader scene understanding\n"
        "- Tight detail views (fov=60°) for close-up of wall/frontage\n"
        "- 'Look-toward' views from nearby positions converging on this spot\n"
        "- Junction branch views where streets meet\n\n"
        "Cross-reference all available angles to build a complete understanding of the space.\n"
        "If a view is blocked by a vehicle or obstacle, check other angles to see behind it.\n\n"
        "IMPORTANT — SCENE CLASSIFICATION:\n"
        "Some images may show INTERIOR spaces (inside shops, malls, gyms, covered galleries)\n"
        "or UNSUITABLE scenes (parks with no wall, construction sites, narrow alleys, parking lots).\n"
        "You MUST classify the scene before assessing placement:\n"
        "- scene_type: 'exterior' | 'interior' | 'covered' (covered = arcade, gallery, canopy)\n"
        "- is_viable_exterior: false if scene is fundamentally unsuitable for outdoor locker placement\n"
        "- interior_image_indices: list of 0-based image indices showing indoor/covered scenes\n"
        "If ALL images are interior/covered → set placement_score=0 and is_viable_exterior=false.\n"
        "Interior photos show store aisles, gym equipment, mall galleries, indoor ceilings, etc.\n\n"
        "Available locker sizes:\n"
        "  Compact:  0.6m wide × 0.7m deep × 2.0m tall  (needs 1.2m clear passage)\n"
        "  Standard: 1.2m wide × 0.7m deep × 2.0m tall  (needs 1.5m clear passage)\n"
        "  Large:    2.4m wide × 0.7m deep × 2.0m tall  (needs 1.5m clear passage)\n"
        "  XL:       4.8m+ wide × 0.7m deep × 2.0m tall (needs 2.0m clear passage)\n\n"
        "Requirements for ANY size: level paved ground, unobstructed wall/frontage,\n"
        "no blocking of exits, wheelchair access, or shop entrances.\n\n"
        "Images show {n_images} views ({image_summary}) of the candidate location.\n\n"
        "Respond ONLY with valid JSON (no markdown fences):\n"
        '{{\n'
        '  "scene_type": "exterior"|"interior"|"covered",\n'
        '  "is_viable_exterior": true|false,\n'
        '  "interior_image_indices": [],\n'
        '  "largest_viable_size": "compact"|"standard"|"large"|"xl"|"none",\n'
        '  "placement_score": 0-10,\n'
        '  "placement_confidence": 0.0-1.0,\n'
        '  "confidence": "high"|"medium"|"low",\n'
        '  "best_side": "left"|"right"|"junction"|"none",\n'
        '  "best_image_idx": 0,\n'
        '  "placement_x_pct": 0-100,\n'
        '  "frame_coverage_pct": 60,\n'
        '  "available_width_m": 1.5,\n'
        '  "footpath_width_estimate": "e.g. ~2.5m, adequate for standard",\n'
        '  "obstacles": ["list"],\n'
        '  "blocked_views": ["list of image indices that are blocked/obstructed"],\n'
        '  "wall_available": true|false,\n'
        '  "surface": "paved"|"cobblestone"|"unpaved"|"unclear",\n'
        '  "notes": "2-3 sentences synthesising findings from multiple angles"\n'
        '}}\n'
        'scene_type: classify the overall scene — exterior (street), interior (indoors), or covered (arcade/gallery).\n'
        'is_viable_exterior: false if the scene is indoors, covered, or lacks any suitable outdoor wall/frontage.\n'
        'interior_image_indices: 0-based indices of images that show indoor or covered scenes.\n'
        'placement_x_pct: horizontal center of locker as % of best_image_idx width.\n'
        'placement_confidence: 0.0-1.0 — how confident you are in the exact placement_x_pct location.\n'
        'frame_coverage_pct: what % of image width the available wall/frontage spans (e.g. 40 if wall covers 40% of frame).\n'
        'best_image_idx: 0-based index into the images provided (choose the clearest EXTERIOR view).\n'
        'blocked_views: indices of images that are blocked by vehicles, construction, etc.'
    )

    all_results = []
    model = "claude-opus-4-6"

    for gi, group in enumerate(groups):
        images = group["images"]
        if not images:
            print(f"    Group {gi + 1}: no valid images, skipping")
            continue

        # Build image summary string
        by_type = {}
        for img in images:
            t = img["capture_type"]
            by_type[t] = by_type.get(t, 0) + 1
        summary_parts = [f"{v} {k}" for k, v in sorted(by_type.items())]
        image_summary = ", ".join(summary_parts)

        prompt = grouped_prompt.format(
            n_images=len(images),
            image_summary=image_summary,
        )

        content = []
        for i, img_info in enumerate(images):
            with open(img_info["path"], "rb") as fh:
                raw = fh.read()
            b64 = base64.standard_b64encode(raw).decode()
            if raw[:3] == b'\xff\xd8\xff':
                media_type = "image/jpeg"
            elif raw[:8] == b'\x89PNG\r\n\x1a\n':
                media_type = "image/png"
            else:
                media_type = "image/jpeg"
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
            # Add label for each image
            indoor_flag = " [HEURISTIC: likely indoor]" if img_info.get("heuristic_indoor") else ""
            label = (f"Image {i}: VP{img_info['viewpoint_idx']} "
                     f"{img_info['side']} (fov={img_info['fov']}° "
                     f"{img_info['capture_type']}){indoor_flag}")
            content.append({"type": "text", "text": label})
        content.append({"type": "text", "text": prompt})

        try:
            req_body = json.dumps({
                "model":      model,
                "max_tokens": 1024,
                "messages":   [{"role": "user", "content": content}],
            }).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=req_body,
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=60)
            body = json.loads(resp.read().decode())
            raw_text = body["content"][0]["text"].strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
            analysis = json.loads(raw_text)
        except Exception as e:
            print(f"    Group {gi + 1}: analysis failed ({e})")
            analysis = {"largest_viable_size": "none", "placement_score": 0,
                        "confidence": "low",
                        "notes": f"analysis error: {e}"}

        center = group["center"]
        # Find best viewpoint (closest to center with best images)
        best_vp = None
        for call in group["calls"]:
            if best_vp is None:
                best_vp = call
            elif haversine(call["lat"], call["lng"], center[0], center[1]) < \
                 haversine(best_vp["lat"], best_vp["lng"], center[0], center[1]):
                best_vp = call

        result = {
            "group_idx": gi,
            "center_lat": center[0],
            "center_lng": center[1],
            "n_images": len(images),
            "image_types": by_type,
            "images": images,
            "analysis": analysis,
            "viewpoint_idx": best_vp.get("viewpoint_idx", 0) if best_vp else 0,
            "lat": best_vp["lat"] if best_vp else center[0],
            "lng": best_vp["lng"] if best_vp else center[1],
            "idw_score": best_vp.get("idw_score", 0) if best_vp else 0,
        }
        all_results.append(result)
        score = analysis.get("placement_score", 0)
        size = analysis.get("largest_viable_size", "none")
        conf = analysis.get("confidence", "?")
        scene = analysis.get("scene_type", "?")
        viable = analysis.get("is_viable_exterior", True)
        scene_tag = f" [INTERIOR]" if scene != "exterior" or not viable else ""
        print(f"    Group {gi + 1}: score={score}/10 size={size} "
              f"confidence={conf} scene={scene}{scene_tag} ({len(images)} images)")
        time.sleep(0.5)

    # Filter top candidates
    min_score = cfg.get("candidate_min_score", 5)
    top_candidates = sorted(
        [r for r in all_results
         if r["analysis"].get("placement_score", 0) >= min_score
         and r["analysis"].get("wall_available", False)
         and r["analysis"].get("is_viable_exterior", True)],
        key=lambda x: x["analysis"].get("placement_score", 0),
        reverse=True,
    )

    # Build interesting coords for compatibility
    interesting_out = [
        (r["lat"], r["lng"])
        for r in all_results
        if r["analysis"].get("placement_score", 0) >= min_score
    ]

    n_viable = sum(1 for r in all_results
                   if r["analysis"].get("largest_viable_size", "none") != "none")

    output_dict = {
        "meta": {
            "pass":         "grouped_detail",
            "sector":       "unknown",
            "model":        model,
            "version":      SV_ANALYSIS_VERSION,
            "n_groups":     len(groups),
            "n_analysed":   len(all_results),
            "n_viable":     n_viable,
            "n_top":        len(top_candidates),
            "config":       {k: v for k, v in cfg.items() if not k.startswith("_")},
            "at":           datetime.utcnow().isoformat(),
        },
        "candidate_groups":  all_results,
        "top_candidates":    top_candidates,
        "interesting_coords": interesting_out,
        # Compatibility: viewpoints list (one per group)
        "viewpoints":        all_results,
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        class _SafeEnc(json.JSONEncoder):
            def default(self, obj):
                try:
                    import numpy as np
                    if isinstance(obj, (np.integer,)):  return int(obj)
                    if isinstance(obj, (np.floating,)): return float(obj)
                except ImportError:
                    pass
                return super().default(obj)

        with open(output_path, "w") as fh:
            json.dump(output_dict, fh, indent=2, cls=_SafeEnc)
        print(f"  Grouped analysis saved → {output_path.name} "
              f"({n_viable}/{len(all_results)} viable, "
              f"{len(top_candidates)} top candidates)")

    return output_dict


def _sv_candidates_to_ground_truth(grouped_analysis, corridor_meta, data_dir,
                                     config=None):
    """Convert SV corridor top candidates to ground-truth candidate format.

    Bridges the SV pipeline output into the enrichment pipeline's expected
    dict structure so we can reuse reverse_geocode, describe_candidates,
    collect_zoning_data, etc.

    Returns list of candidate dicts compatible with the ground-truth pipeline.
    """
    cfg = config or SV_CONFIG
    top = grouped_analysis.get("top_candidates", [])
    if not top:
        print("  No top candidates to convert")
        return []

    # Load baseline lockers + competitors for distance calc
    project_root = Path(__file__).resolve().parent.parent
    baseline = []
    competitors = []
    try:
        bbox_path = project_root / "data" / "bbox.json"
        if bbox_path.exists():
            with open(bbox_path) as f:
                baseline = json.load(f)
    except Exception:
        pass
    try:
        comp_path = project_root / "data" / "competitors.json"
        if comp_path.exists():
            with open(comp_path) as f:
                competitors = json.load(f)
    except Exception:
        pass

    candidates = []
    for i, cand in enumerate(top):
        lat = cand.get("lat", cand.get("center_lat", 0))
        lng = cand.get("lng", cand.get("center_lng", 0))
        analysis = cand.get("analysis", {})

        # Compute nearest existing locker
        nearest_m = 9999
        for loc in baseline:
            d = haversine(lat, lng, loc.get("lat", 0), loc.get("lng", 0))
            if d < nearest_m:
                nearest_m = d

        # Count competitors within 500m
        comp_nearby = sum(
            1 for c in competitors
            if haversine(lat, lng, c.get("lat", 0), c.get("lng", 0)) < 500
        )

        candidates.append({
            "id": i + 1,
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "sector": corridor_meta.get("meta", {}).get("sector", "unknown"),
            "source": "sv_corridor",
            "ml_score": cand.get("idw_score", 0),
            "site_score": 0,
            "gt_score": 0,
            "address": "",  # Filled by reverse_geocode_address
            "suggested_site": None,  # Filled by describe_candidates
            "nearest_existing_m": round(nearest_m),
            "competitors_nearby": comp_nearby,
            "pop_gain": 0,
            "commentary": "",
            "contact_info": "",
            "status": "proposed",
            # SV-specific fields carried forward
            "sv_viewpoint_idx": cand.get("viewpoint_idx", 0),
            "sv_group_idx": cand.get("group_idx", 0),
            "sv_placement_score": analysis.get("placement_score", 0),
            "sv_largest_viable_size": analysis.get("largest_viable_size", "none"),
            "sv_confidence": analysis.get("confidence", "low"),
            "sv_surface": analysis.get("surface", "unclear"),
            "sv_wall_available": analysis.get("wall_available", False),
            "sv_obstacles": analysis.get("obstacles", []),
            "sv_notes": analysis.get("notes", ""),
            "sv_available_width_m": analysis.get("available_width_m", 0),
            "sv_footpath_estimate": analysis.get("footpath_width_estimate", ""),
            "sv_best_side": analysis.get("best_side", "none"),
            "sv_scene_type": analysis.get("scene_type", "exterior"),
            "sv_is_viable_exterior": analysis.get("is_viable_exterior", True),
            "sv_interior_image_indices": analysis.get("interior_image_indices", []),
            "sv_placement_confidence": analysis.get("placement_confidence", 0.5),
            "sv_frame_coverage_pct": analysis.get("frame_coverage_pct", 60),
            "sv_images": cand.get("images", []),
            "sv_analysis": analysis,
        })

    print(f"  Converted {len(candidates)} SV candidates to ground-truth format")
    return candidates


def _enrich_sv_candidates_with_claude(candidates, zoning, center, commune, region,
                                       img_dir=None, sv_corridor=None):
    """Final Opus assessment for SV corridor candidates.

    Extends enrich_with_claude() pattern — sends SV corridor images + satellite +
    all enrichment data for comprehensive feasibility assessment.

    Includes all 4 locker sizes and SV analysis findings in the prompt.
    If no candidate is truly feasible, sets recommendation.winner_id = null.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    except ImportError:
        pass

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("  _enrich_sv_candidates_with_claude: no ANTHROPIC_API_KEY, skipping")
        return None

    import base64
    from datetime import datetime

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        print("  _enrich_sv_candidates_with_claude: anthropic SDK not installed, skipping")
        return None

    print(f"  Final Opus assessment for {len(candidates)} SV candidates...")

    # Cap candidates to stay under Anthropic's 100 image limit
    # Budget: ~7 images per candidate (1 satellite + 6 SV), so max ~14 candidates
    MAX_IMAGES = 95  # leave headroom
    IMAGES_PER_CANDIDATE = 7  # 1 satellite + 6 SV
    max_candidates = max(3, MAX_IMAGES // IMAGES_PER_CANDIDATE)
    if len(candidates) > max_candidates:
        # Sort by SV placement score descending, take top N
        candidates_ranked = sorted(candidates,
                                   key=lambda c: c.get("sv_placement_score", 0),
                                   reverse=True)
        assessed = candidates_ranked[:max_candidates]
        skipped = candidates_ranked[max_candidates:]
        print(f"    Capping to top {max_candidates} candidates (by SV score) to stay under image limit")
        # Mark skipped candidates as not assessed
        for c in skipped:
            c["verdict"] = "Not assessed"
            c["commentary"] = "Candidate not assessed in final review — lower SV placement score"
    else:
        assessed = candidates

    # Build evidence JSON for each candidate
    evidence = []
    for c in assessed:
        ev = {
            "id": c["id"],
            "lat": c["lat"],
            "lng": c["lng"],
            "address": c.get("address", ""),
            "sector": c.get("sector", ""),
            "source": c.get("source", "sv_corridor"),
            "ml_score": c.get("ml_score", 0),
            "nearest_existing_m": c.get("nearest_existing_m", 9999),
            "competitors_nearby": c.get("competitors_nearby", 0),
            "sv_placement_score": c.get("sv_placement_score", 0),
            "sv_largest_viable_size": c.get("sv_largest_viable_size", "none"),
            "sv_confidence": c.get("sv_confidence", "low"),
            "sv_surface": c.get("sv_surface", ""),
            "sv_wall_available": c.get("sv_wall_available", False),
            "sv_obstacles": c.get("sv_obstacles", []),
            "sv_notes": c.get("sv_notes", ""),
            "sv_available_width_m": c.get("sv_available_width_m", 0),
            "sv_footpath_estimate": c.get("sv_footpath_estimate", ""),
        }
        # Add enrichment data if available
        if c.get("location_context"):
            ev["location_context"] = c["location_context"]
        if c.get("nearby_pois"):
            ev["nearby_pois"] = c["nearby_pois"][:5]
        if c.get("suggested_site"):
            ev["suggested_site"] = c["suggested_site"]
        if c.get("zoning_data"):
            ev["zoning_data"] = c["zoning_data"]
        if c.get("heritage_zones"):
            ev["heritage_zones"] = c["heritage_zones"]
        if c.get("physical_context"):
            ev["physical_context"] = c["physical_context"]
        if c.get("business_details"):
            ev["business_details"] = c["business_details"]
        evidence.append(ev)

    # Regulation names by region
    reg_names = {
        "Brussels-Capital": "COBAT (Code bruxellois de l'Aménagement du Territoire)",
        "Wallonia": "CoDT (Code du Développement Territorial)",
        "Flanders": "VCRO (Vlaamse Codex Ruimtelijke Ordening)",
    }
    reg = reg_names.get(region, "applicable regional planning code")

    prompt = (
        f"You are a senior urban logistics consultant assessing parcel locker placement "
        f"in {commune}, {region} (Belgium). The {reg} applies.\n\n"
        f"## Available locker sizes\n"
        f"  Compact:  0.6m W × 0.7m D × 2.0m H  (needs 1.2m clear passage)\n"
        f"  Standard: 1.2m W × 0.7m D × 2.0m H  (needs 1.5m clear passage)\n"
        f"  Large:    2.4m W × 0.7m D × 2.0m H  (needs 1.5m clear passage)\n"
        f"  XL:       4.8m+ W × 0.7m D × 2.0m H (needs 2.0m clear passage)\n\n"
        f"## Space requirements\n"
        f"- Level paved ground, firm surface\n"
        f"- Unobstructed wall or building frontage\n"
        f"- Must not block emergency exits, wheelchair access, or shop entrances\n"
        f"- 1.5m minimum clear passage for pedestrians (1.2m for compact only)\n\n"
        f"## Candidate evidence\n"
        f"Each candidate includes street-level observations from multiple camera angles,\n"
        f"satellite imagery, and enrichment data from OpenStreetMap and regional planning APIs.\n\n"
        f"The SV corridor analysis has already scored each candidate on a 0-10 scale.\n"
        f"Your task is to provide an independent feasibility assessment using ALL available data,\n"
        f"including the street-level and satellite imagery provided.\n\n"
        f"```json\n{json.dumps(evidence, indent=2, default=str)}\n```\n\n"
        f"## Instructions\n"
        f"1. Examine ALL images for each candidate (satellite + street view angles)\n"
        f"2. Cross-reference the SV analysis findings with what you see in the images\n"
        f"3. Consider zoning, heritage, and physical infrastructure data\n"
        f"4. Determine the LARGEST viable locker size, considering all 4 options\n"
        f"5. If NO candidate is truly feasible, say so explicitly and explain why\n\n"
        f"Respond ONLY with valid JSON (no markdown fences):\n"
        f'{{\n'
        f'  "candidate_assessments": {{\n'
        f'    "<id>": {{\n'
        f'      "commentary": "2-3 sentence summary of feasibility",\n'
        f'      "physical_feasibility": {{\n'
        f'        "verdict": "Feasible"|"Marginal"|"Not feasible",\n'
        f'        "footpath_assessment": "...",\n'
        f'        "space_assessment": "...",\n'
        f'        "accessibility": "...",\n'
        f'        "visibility_traffic": "...",\n'
        f'        "obstacles": "..."\n'
        f'      }},\n'
        f'      "visual_observations": [\n'
        f'        {{"label": "short label", "description": "1-2 sentences"}}\n'
        f'      ],\n'
        f'      "contact_details": {{\n'
        f'        "site_type": "private"|"public",\n'
        f'        "business_name": "...",\n'
        f'        "parent_company": "...",\n'
        f'        "contact_approach": "specific recommendation",\n'
        f'        "phone_website": "...",\n'
        f'        "commune_authority": "..."\n'
        f'      }}\n'
        f'    }}\n'
        f'  }},\n'
        f'  "zoning_analysis": {{\n'
        f'    "zone_classification": "...",\n'
        f'    "applicable_regulations": "...",\n'
        f'    "permits_required": "...",\n'
        f'    "approval_timeline": "...",\n'
        f'    "special_plan_restrictions": "..."\n'
        f'  }},\n'
        f'  "recommendation": {{\n'
        f'    "winner_id": <int or null if none feasible>,\n'
        f'    "reasoning": "3-5 sentences explaining the recommendation",\n'
        f'    "next_steps": ["Step 1: ...", "Step 2: ...", "Step 3: ..."]\n'
        f'  }}\n'
        f'}}'
    )

    # Build content with images
    content = []

    # Add satellite + SV images for assessed candidates only
    for c in assessed:
        cid = c["id"]
        # Satellite image
        if img_dir:
            sat_path = Path(img_dir) / f"candidate_{cid}_satellite.png"
            if sat_path.exists():
                with open(sat_path, "rb") as fh:
                    raw = fh.read()
                b64 = base64.standard_b64encode(raw).decode()
                mt = "image/jpeg" if raw[:3] == b'\xff\xd8\xff' else "image/png"
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mt, "data": b64},
                })
                content.append({"type": "text",
                                "text": f"Candidate {cid}: satellite view"})

        # SV corridor images (max 6 per candidate)
        sv_images = c.get("sv_images", [])[:6]
        for si, img_info in enumerate(sv_images):
            img_path = Path(img_info["path"]) if isinstance(img_info, dict) else Path(img_info)
            if img_path.exists():
                with open(img_path, "rb") as fh:
                    raw = fh.read()
                b64 = base64.standard_b64encode(raw).decode()
                mt = "image/jpeg" if raw[:3] == b'\xff\xd8\xff' else "image/png"
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mt, "data": b64},
                })
                label = f"Candidate {cid}: street view"
                if isinstance(img_info, dict):
                    label += f" ({img_info.get('side', '')} fov={img_info.get('fov', 90)}°)"
                content.append({"type": "text", "text": label})

    content.append({"type": "text", "text": prompt})

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=16000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": content}],
        )

        # Extract text from response (skip thinking blocks)
        raw_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_text = block.text.strip()
                break

        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        result = json.loads(raw_text)

    except Exception as e:
        print(f"  Final Opus assessment failed: {e}")
        return None

    # Merge results onto candidates
    assessments = result.get("candidate_assessments", {})
    for c in candidates:
        cid = str(c["id"])
        if cid in assessments:
            a = assessments[cid]
            c["commentary"] = a.get("commentary", "")
            c["physical_feasibility"] = a.get("physical_feasibility", {})
            c["visual_observations"] = a.get("visual_observations", [])
            c["contact_details"] = a.get("contact_details", {})
            # Build backward-compatible contact_info string
            cd = c["contact_details"]
            parts = [cd.get("business_name", ""), cd.get("phone_website", "")]
            c["contact_info"] = " | ".join(p for p in parts if p)

    # Compute cost
    input_tokens = getattr(response, "usage", None)
    cost = 0
    if input_tokens:
        cost = (getattr(input_tokens, "input_tokens", 0) * 15 +
                getattr(input_tokens, "output_tokens", 0) * 75) / 1_000_000

    enrichment = {
        "zoning_analysis": result.get("zoning_analysis", {}),
        "zoning_findings": json.dumps(result.get("zoning_analysis", {})),
        "recommendation": result.get("recommendation", {}),
        "overall_recommendation": result.get("recommendation", {}).get("reasoning", ""),
        "model_used": "claude-opus-4-6",
        "enriched_at": datetime.utcnow().isoformat(),
        "cost_usd": round(cost, 4),
    }

    winner_id = result.get("recommendation", {}).get("winner_id")
    if winner_id:
        print(f"  Recommendation: Candidate #{winner_id}")
    else:
        print(f"  Recommendation: No feasible candidate")
    print(f"  Cost: ${enrichment['cost_usd']:.4f}")

    return enrichment


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
    parser.add_argument("--resume-from", choices=["detail", "enrich", "assess", "pdf"],
                        help="v2.0: Resume SV pipeline from a specific stage (skips earlier stages)")
    parser.add_argument("--sv-only", action="store_true",
                        help="Skip ground-truth Steps 2-8, run only SV corridor pipeline")
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

    # Resolve commune/region early (needed for both paths)
    commune, region = reverse_geocode_commune(center[0], center[1])
    area_name = args.name or f"{commune.replace(' ', '-').replace('/', '-')}_{args.radius}km"

    # Prepare report directory
    resume = getattr(args, "resume_from", None)
    date_str = time.strftime("%Y%m%d")
    report_dir = REPORTS_DIR / f"{area_name}_{date_str}"
    # When resuming, prefer existing directory with checkpoints
    if resume:
        existing = sorted(REPORTS_DIR.glob(f"{area_name}_*"), reverse=True)
        for d in existing:
            if d.is_dir() and (d / "sv_screen_analysis.json").exists():
                report_dir = d
                print(f"  Resuming from existing report directory: {d.name}")
                break
    report_dir.mkdir(parents=True, exist_ok=True)
    img_dir = report_dir / "images"
    img_dir.mkdir(exist_ok=True)

    scored = []
    zoning = {}
    osm_features = {}

    if not getattr(args, "sv_only", False):
        # Step 2: Query OSM data
        osm_features = fetch_osm_data(center, args.radius)

        # Step 3: Identify ALL nearby candidate sites (collect a pool)
        all_candidates = identify_candidates(center, args.radius, data, osm_features,
                                             max_candidates=args.candidates * 5)

        if not all_candidates:
            print("\nNo candidates found in this area. Try a larger radius or different center.")
            if not args.sector:
                return

        if all_candidates:
            # Step 4: Describe/score all candidates (cheap, local)
            ml_mode = data.get("ml_heatmap") is not None
            if ml_mode:
                all_scored = describe_candidates(all_candidates, osm_features, data, center=center)
            else:
                all_scored = score_candidates(all_candidates, osm_features, data, center=center)

            # Step 5: Zoning research
            zoning = build_zoning_research(center, commune, region)

            # Steps 3b-5b: Collect zoning, business, physical data for ALL candidates
            collect_zoning_data(all_scored, commune, region)
            collect_business_data(all_scored)
            collect_physical_data(all_scored)

            # Take top N candidates for the report
            n = args.candidates
            scored = all_scored[:n]

            # Step 6: Download imagery for current batch
            download_candidate_images(scored, img_dir)
            download_overview_image(center, scored, img_dir)
    else:
        print("  --sv-only: skipping ground-truth Steps 2-8, jumping to SV corridor")
        zoning = build_zoning_research(center, commune, region)

    # Step 6b: SV Corridor v2.0 — full pipeline with checkpoint/resume
    sv_corridor = None

    if args.sector:
        sv_corridor = build_sv_corridor(args.sector)
        if sv_corridor:
            pts    = sv_corridor["corridor_points"]
            gn     = sv_corridor.get("_graph_nodes", {})
            ge     = sv_corridor.get("_graph_edges", {})

            screen_analysis = None
            detail_analysis = None
            sv_candidates = []
            sv_enrichment = None

            # ── PASS 1: SCREENING ──────────────────────────────────
            screen_json = report_dir / "sv_screen_analysis.json"
            if resume and resume != "detail":
                # Skip screening — load from checkpoint
                if screen_json.exists():
                    with open(screen_json) as f:
                        screen_analysis = json.load(f)
                    print(f"  Loaded screening checkpoint ({screen_analysis.get('meta',{}).get('n_analysed',0)} viewpoints)")
            if screen_analysis is None and (resume is None or resume == "detail"):
                screen_calls = _build_screening_sv_calls(pts, stride=SV_CONFIG["screening_stride"])
                download_sv_corridor_images(screen_calls, img_dir, prefix="screen_")
                screen_analysis = analyze_sv_corridor_images(
                    screen_calls, img_dir,
                    prefix="screen_",
                    model="claude-sonnet-4-6",
                    output_path=screen_json,
                    interesting_threshold=SV_CONFIG["screening_threshold"],
                )
            # Patch sector code into screening meta
            if screen_analysis and screen_analysis.get("meta", {}).get("sector") == "unknown":
                screen_analysis["meta"]["sector"] = args.sector
                screen_analysis["meta"]["sector_code"] = args.sector
                with open(screen_json, "w") as f:
                    json.dump(screen_analysis, f, indent=2, default=str)

            elif screen_analysis is None and screen_json.exists():
                with open(screen_json) as f:
                    screen_analysis = json.load(f)

            # ── PASS 2: MULTI-ANGLE DETAIL ─────────────────────────
            detail_json = report_dir / "sv_detail_analysis.json"
            interesting = (screen_analysis or {}).get("interesting_coords", [])

            if resume in ("enrich", "assess", "pdf") and detail_json.exists():
                # Skip detail — load from checkpoint
                with open(detail_json) as f:
                    detail_analysis = json.load(f)
                print(f"  Loaded detail checkpoint ({detail_analysis.get('meta',{}).get('n_groups', detail_analysis.get('meta',{}).get('n_analysed',0))} groups)")
            elif interesting:
                # v2.0: Multi-angle detail capture + grouped analysis
                detail_calls = _build_detail_sv_calls_v2(pts, interesting, gn, ge,
                                                          config=SV_CONFIG)
                download_sv_corridor_images(detail_calls, img_dir, prefix="detail_")
                detail_analysis = analyze_sv_corridor_grouped(
                    detail_calls, img_dir, interesting,
                    prefix="detail_",
                    config=SV_CONFIG,
                    output_path=detail_json,
                )
            else:
                print("  No interesting spots found in screening pass")

            # Patch sector code into detail meta
            if detail_analysis and detail_analysis.get("meta", {}).get("sector") == "unknown":
                detail_analysis["meta"]["sector"] = args.sector
                detail_analysis["meta"]["sector_code"] = args.sector
                with open(detail_json, "w") as f:
                    json.dump(detail_analysis, f, indent=2, default=str)

            # ── CANDIDATE CONVERSION & ENRICHMENT ──────────────────
            enrichment_json = report_dir / "sv_enrichment.json"

            if resume in ("assess", "pdf") and enrichment_json.exists():
                with open(enrichment_json) as f:
                    sv_enrichment_data = json.load(f)
                    sv_candidates = sv_enrichment_data.get("candidates", [])
                    sv_enrichment = sv_enrichment_data
                print(f"  Loaded enrichment checkpoint ({len(sv_candidates)} candidates)")
            elif detail_analysis:
                top = detail_analysis.get("top_candidates", [])
                if top:
                    # Convert to ground-truth format
                    sv_candidates = _sv_candidates_to_ground_truth(
                        detail_analysis, sv_corridor, report_dir,
                        config=SV_CONFIG)

                    if sv_candidates and resume != "pdf":
                        # Reverse geocode addresses
                        for c in sv_candidates:
                            c["address"] = reverse_geocode_address(c["lat"], c["lng"])
                            time.sleep(1.1)

                        # Run enrichment pipeline (reuse ground-truth functions)
                        try:
                            osm_for_sv = fetch_osm_data(center, args.radius)
                            sv_candidates = describe_candidates(sv_candidates, osm_for_sv, data, center)
                        except Exception as e:
                            print(f"  describe_candidates: {e}")

                        try:
                            collect_zoning_data(sv_candidates, commune, region)
                        except Exception as e:
                            print(f"  collect_zoning_data: {e}")

                        try:
                            collect_business_data(sv_candidates)
                        except Exception as e:
                            print(f"  collect_business_data: {e}")

                        try:
                            collect_physical_data(sv_candidates)
                        except Exception as e:
                            print(f"  collect_physical_data: {e}")

                        # Download satellite images for each candidate
                        download_candidate_images(sv_candidates, img_dir)
                        download_overview_image(center, sv_candidates, img_dir)

                        # Final Opus assessment
                        sv_enrichment = _enrich_sv_candidates_with_claude(
                            sv_candidates, zoning, center, commune, region,
                            img_dir=img_dir, sv_corridor=sv_corridor)

                        # Save enrichment checkpoint
                        if sv_enrichment:
                            checkpoint = dict(sv_enrichment)
                            checkpoint["candidates"] = sv_candidates
                            with open(enrichment_json, "w") as f:
                                json.dump(checkpoint, f, indent=2, default=str)
                            print(f"  Enrichment saved → {enrichment_json.name}")
                else:
                    print("  No top candidates from detail analysis")

            # ── CONDITIONAL MARKUP ─────────────────────────────────
            if detail_analysis and sv_candidates:
                import importlib.util
                _sv_spec = importlib.util.spec_from_file_location(
                    "markup_sv", Path(__file__).resolve().parent / "markup_sv.py")
                _sv_mod = importlib.util.module_from_spec(_sv_spec)
                _sv_spec.loader.exec_module(_sv_mod)
                markup_sv_image_3d = _sv_mod.markup_sv_image_3d
                markup_sv_image = _sv_mod.markup_sv_image
                generate_sv_report = _sv_mod.generate_sv_report
                _should_markup = _sv_mod._should_markup
                marked = 0
                for c in sv_candidates:
                    if not _should_markup(c):
                        reason = c.get("markup_skipped_reason", "verdict not Feasible/Marginal")
                        print(f"    skip markup: candidate {c.get('id','?')} — {reason}")
                        continue
                    vidx = c.get("sv_viewpoint_idx", 0)
                    best_side = c.get("sv_best_side", "left")
                    analysis = c.get("sv_analysis", {})
                    sv_images = c.get("sv_images", [])

                    # Build prioritised search: sv_images paths first, then fallback patterns
                    # Priority: standard best_side > tight best_side > wide > any standard
                    # best_image_idx from Opus used as tiebreaker within same tier
                    best_img_idx = analysis.get("best_image_idx")
                    def _img_priority(img_info):
                        ct = img_info.get("capture_type", "standard")
                        side = img_info.get("side", "")
                        indoor_penalty = 10 if img_info.get("heuristic_indoor", False) else 0
                        idx_bonus = 0 if img_info.get("_orig_idx") == best_img_idx else 1
                        base_side = best_side.split("_")[-1] if "_" in best_side else best_side
                        if base_side in side and ct == "standard": return (0 + indoor_penalty) * 10 + idx_bonus
                        if base_side in side and ct == "tight": return (2 + indoor_penalty) * 10 + idx_bonus
                        if ct == "wide": return (4 + indoor_penalty) * 10 + idx_bonus
                        if ct == "standard": return (6 + indoor_penalty) * 10 + idx_bonus
                        return (8 + indoor_penalty) * 10 + idx_bonus

                    indexed_images = []
                    for i, img in enumerate(sv_images):
                        if isinstance(img, dict):
                            d = dict(img)
                            d["_orig_idx"] = i
                            indexed_images.append(d)

                    sorted_images = sorted(indexed_images, key=_img_priority)

                    found = False
                    for img_info in sorted_images:
                        src = Path(img_info["path"])
                        if src.exists():
                            side_label = img_info.get("side", best_side)
                            img_fov = img_info.get("fov", 90)
                            out = img_dir / f"sv_marked_{vidx}_{side_label}.png"
                            if markup_sv_image_3d(src, analysis, out, fov=img_fov):
                                marked += 1
                                found = True
                            break

                    if not found:
                        # Fallback: try reconstructing filenames (default FOV=90)
                        for side in [best_side, "left", "right",
                                     f"wide_{best_side}", f"tight_{best_side}"]:
                            src = img_dir / f"detail_sv_{vidx}_{side}.png"
                            if src.exists():
                                out = img_dir / f"sv_marked_{vidx}_{side}.png"
                                if markup_sv_image_3d(src, analysis, out, fov=90):
                                    marked += 1
                                break
                print(f"  Marked {marked} viable candidate images")

                # Generate sv_report.json
                generate_sv_report(detail_analysis, img_dir,
                                   report_dir / "sv_report.json",
                                   enriched_candidates=sv_candidates)

            # ── PDF GENERATION ─────────────────────────────────────
            try:
                import importlib.util
                _pdf_spec = importlib.util.spec_from_file_location(
                    "sv_report_to_pdf", Path(__file__).resolve().parent / "sv_report_to_pdf.py")
                _pdf_mod = importlib.util.module_from_spec(_pdf_spec)
                _pdf_spec.loader.exec_module(_pdf_mod)
                generate_sv_report_pdf = _pdf_mod.generate_sv_report_pdf
                generate_sv_report_pdf(report_dir)
            except Exception as e:
                print(f"  PDF generation failed: {e}")
                import traceback
                traceback.print_exc()

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
