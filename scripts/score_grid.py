#!/usr/bin/env python3
"""
Score a 100m grid across Belgium with the trained XGBoost placement model.

Generates two output files:
  - data/ml_scores.json    (frontend heatmap overlay, ~2-3MB)
  - data/ml_heatmap.parquet (backend detailed scores for ground truth, ~20-40MB)

Uses multiprocessing.Pool to parallelize the enrichment across CPU cores.
Expected runtime: ~20-40 min depending on core count.
"""

import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool, cpu_count
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ENRICHED_DIR = DATA_DIR / "enriched"
TRAINING_DIR = DATA_DIR / "training"

# Grid resolution
GRID_STEP_LAT = 0.0009    # ~100m in latitude
GRID_STEP_LNG = 0.00141   # ~100m in longitude at 50.5°N

# Belgium bbox (with small padding beyond centroid extremes)
LAT_MIN, LAT_MAX = 49.50, 51.50
LNG_MIN, LNG_MAX = 2.55, 6.40

# Max distance to nearest centroid to be considered "inside Belgium"
MAX_CENTROID_DIST_M = 2000

# Batch size for multiprocessing
BATCH_SIZE = 5000

# Progress reporting interval
PROGRESS_INTERVAL = 50_000

# ---------------------------------------------------------------------------
# Spatial utilities (from notebook Cell 3)
# ---------------------------------------------------------------------------
R_EARTH = 6_371_000
DEG_TO_RAD = math.pi / 180
CELL_SIZE = 0.01  # ~1.1 km grid cells for spatial index


def haversine(lat1, lng1, lat2, lng2):
    """Distance in meters between two WGS84 points."""
    d_lat = (lat2 - lat1) * DEG_TO_RAD
    d_lng = (lng2 - lng1) * DEG_TO_RAD
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(lat1 * DEG_TO_RAD) * math.cos(lat2 * DEG_TO_RAD) *
         math.sin(d_lng / 2) ** 2)
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_spatial_index(points, key_lat="lat", key_lng="lng"):
    """Grid-based spatial index for fast radius queries."""
    index = defaultdict(list)
    for i, p in enumerate(points):
        lat = p[key_lat] if isinstance(p, dict) else getattr(p, key_lat)
        lng = p[key_lng] if isinstance(p, dict) else getattr(p, key_lng)
        k = (int(lat // CELL_SIZE), int(lng // CELL_SIZE))
        index[k].append(i)
    return index


def find_within_radius(index, points, lat, lng, radius_m,
                       key_lat="lat", key_lng="lng"):
    """Find all points within radius_m. Returns [(idx, dist)]."""
    radius_deg = radius_m / 111_000
    cell_r = math.ceil(radius_deg / CELL_SIZE)
    base_lat = int(lat // CELL_SIZE)
    base_lng = int(lng // CELL_SIZE)
    results = []
    for dlat in range(-cell_r, cell_r + 1):
        for dlng in range(-cell_r, cell_r + 1):
            k = (base_lat + dlat, base_lng + dlng)
            for idx in index.get(k, []):
                p = points[idx]
                plat = p[key_lat] if isinstance(p, dict) else getattr(p, key_lat)
                plng = p[key_lng] if isinstance(p, dict) else getattr(p, key_lng)
                d = haversine(lat, lng, plat, plng)
                if d <= radius_m:
                    results.append((idx, d))
    return results


def count_within_radius(index, points, lat, lng, radius_m,
                        key_lat="lat", key_lng="lng"):
    """Count points within radius_m."""
    return len(find_within_radius(index, points, lat, lng, radius_m,
                                  key_lat, key_lng))


def nearest_distance(index, points, lat, lng, max_radius=50_000,
                     key_lat="lat", key_lng="lng"):
    """Distance to the nearest point (meters). Returns inf if none found."""
    hits = find_within_radius(index, points, lat, lng, max_radius,
                              key_lat, key_lng)
    if not hits:
        return float("inf")
    return min(d for _, d in hits)


# ---------------------------------------------------------------------------
# Worker globals (set by init_worker, used by enrich_batch)
# ---------------------------------------------------------------------------
_competitors = None
_supermarkets = None
_lockers = None
_osm_elements = None
_comp_idx = None
_super_idx = None
_locker_idx = None
_osm_indices = None
_centroid_idx = None
_centroids = None
_sector_lookup = None


def init_worker(data_dir_str):
    """Initialize worker process: load data and build spatial indices."""
    global _competitors, _supermarkets, _lockers, _osm_elements
    global _comp_idx, _super_idx, _locker_idx, _osm_indices
    global _centroid_idx, _centroids, _sector_lookup

    data_dir = Path(data_dir_str)

    # Load point datasets
    with open(data_dir / "competitors.json") as f:
        _competitors = json.load(f)
    with open(data_dir / "supermarkets.json") as f:
        _supermarkets = json.load(f)
    with open(data_dir / "bbox.json") as f:
        _lockers = json.load(f)
    with open(data_dir / "centroids.json") as f:
        _centroids = json.load(f)

    # Load OSM data
    osm_path = data_dir / "enriched" / "osm_raw_elements.json"
    if osm_path.exists():
        with open(osm_path) as f:
            _osm_elements = json.load(f)
    else:
        print("WARNING: OSM data not found. OSM features will be 0.", flush=True)
        _osm_elements = {k: [] for k in
                         ["bus_stop", "rail_tram", "shop",
                          "amenity", "parking", "footway"]}

    # Build spatial indices
    _comp_idx = build_spatial_index(_competitors)
    _super_idx = build_spatial_index(_supermarkets)
    _locker_idx = build_spatial_index(_lockers)
    _centroid_idx = build_spatial_index(_centroids)
    _osm_indices = {}
    for cat, elements in _osm_elements.items():
        _osm_indices[cat] = build_spatial_index(elements)

    # Build sector feature lookup
    # Combines centroids.json + sectors.json (area) + competitive_coverage + quadrants + demand
    _sector_lookup = {}

    # Base from centroids
    for c in _centroids:
        _sector_lookup[c["sc"]] = {
            "pop": c["pop"],
            "dens": c["dens"],
            "zone": c["zone"],
            "lat": c["lat"],
            "lng": c["lng"],
            "demand": c.get("demand", 0),
            "ageRatio": c.get("ageRatio", 0),
            "incomeIdx": c.get("incomeIdx", 0),
        }

    # Add area from sectors.json (just properties, skip geometry)
    with open(data_dir / "sectors.json") as f:
        sectors_data = json.load(f)
    for feat in sectors_data["features"]:
        sc = feat["properties"]["sc"]
        if sc in _sector_lookup:
            _sector_lookup[sc]["area"] = feat["properties"].get("area", 0)
            _sector_lookup[sc]["region"] = feat["properties"].get("region", "")

    # Add competitive coverage
    with open(data_dir / "competitive_coverage.json") as f:
        comp_cov = json.load(f)
    comp_cov_sectors = comp_cov.get("sectors", comp_cov)
    # Remove meta keys
    for key in ["meta", "travelMinutes", "totalCompetitors", "totalSectors"]:
        comp_cov_sectors.pop(key, None)
    for sc, info in comp_cov_sectors.items():
        if sc in _sector_lookup:
            _sector_lookup[sc]["cc"] = info.get("cc", 0)
            _sector_lookup[sc]["oc"] = info.get("oc", 0)
            _sector_lookup[sc]["gap"] = info.get("gap", 1.0)
            _sector_lookup[sc]["num_operators"] = len(info.get("ops", []))

    # Add quadrants
    with open(data_dir / "strategic_quadrants.json") as f:
        quad_data = json.load(f)
    quad_sectors = quad_data.get("sectors", {})
    for sc, quadrant in quad_sectors.items():
        if sc in _sector_lookup:
            _sector_lookup[sc]["quadrant"] = quadrant

    # Add demand scores (ageRatio, incomeIdx may be more precise here)
    with open(data_dir / "demand_scores.json") as f:
        demand_data = json.load(f)
    demand_map = {d["sc"]: d for d in demand_data}
    for sc, d in demand_map.items():
        if sc in _sector_lookup:
            _sector_lookup[sc]["demand"] = d.get("demand", _sector_lookup[sc].get("demand", 0))
            _sector_lookup[sc]["ageRatio"] = d.get("ageRatio", _sector_lookup[sc].get("ageRatio", 0))
            _sector_lookup[sc]["incomeIdx"] = d.get("incomeIdx", _sector_lookup[sc].get("incomeIdx", 0))


def enrich_batch(batch):
    """
    Enrich a batch of (lat, lng) grid points.
    Returns list of (lat, lng, sc, feature_vector) tuples.
    feature_vector is a list of 33 floats matching model feature order.
    """
    results = []
    for lat, lng in batch:
        # Find nearest centroid → sector assignment
        hits = find_within_radius(_centroid_idx, _centroids, lat, lng,
                                  MAX_CENTROID_DIST_M)
        if not hits:
            continue  # outside Belgium

        nearest_idx = min(hits, key=lambda x: x[1])[0]
        sc = _centroids[nearest_idx]["sc"]
        sector = _sector_lookup.get(sc)
        if not sector:
            continue

        # Sector-level features
        pop = sector.get("pop", 0)
        dens = sector.get("dens", 0)
        demand = sector.get("demand", 0)
        ageRatio = sector.get("ageRatio", 0)
        incomeIdx = sector.get("incomeIdx", 0)
        area = sector.get("area", 0)
        cc = sector.get("cc", 0)
        oc = sector.get("oc", 0)
        gap = sector.get("gap", 1.0)
        num_operators = sector.get("num_operators", 0)

        # Proximity counts
        competitors_500m = count_within_radius(
            _comp_idx, _competitors, lat, lng, 500)
        competitors_1km = count_within_radius(
            _comp_idx, _competitors, lat, lng, 1000)
        supermarkets_500m = count_within_radius(
            _super_idx, _supermarkets, lat, lng, 500)
        supermarkets_1km = count_within_radius(
            _super_idx, _supermarkets, lat, lng, 1000)
        bpost_500m = count_within_radius(
            _locker_idx, _lockers, lat, lng, 500)
        bpost_1km = count_within_radius(
            _locker_idx, _lockers, lat, lng, 1000)
        bpost_2km = count_within_radius(
            _locker_idx, _lockers, lat, lng, 2000)

        # Nearest distances
        dist_nearest_competitor = nearest_distance(
            _comp_idx, _competitors, lat, lng)
        dist_nearest_supermarket = nearest_distance(
            _super_idx, _supermarkets, lat, lng)
        dist_nearest_other_locker = nearest_distance(
            _locker_idx, _lockers, lat, lng)

        # OSM counts
        bus_stops_300m = count_within_radius(
            _osm_indices["bus_stop"], _osm_elements["bus_stop"], lat, lng, 300)
        rail_tram_300m = count_within_radius(
            _osm_indices["rail_tram"], _osm_elements["rail_tram"], lat, lng, 300)
        shops_300m = count_within_radius(
            _osm_indices["shop"], _osm_elements["shop"], lat, lng, 300)
        amenities_300m = count_within_radius(
            _osm_indices["amenity"], _osm_elements["amenity"], lat, lng, 300)
        parking_500m = count_within_radius(
            _osm_indices["parking"], _osm_elements["parking"], lat, lng, 500)
        footways_300m = count_within_radius(
            _osm_indices["footway"], _osm_elements["footway"], lat, lng, 300)

        # Zone one-hot
        zone = sector.get("zone", "urban")
        zone_rural = 1.0 if zone == "rural" else 0.0
        zone_suburban = 1.0 if zone == "suburban" else 0.0
        zone_urban = 1.0 if zone == "urban" else 0.0

        # Quadrant one-hot
        quadrant = sector.get("quadrant", "blue_ocean")
        quad_battleground = 1.0 if quadrant == "battleground" else 0.0
        quad_blue_ocean = 1.0 if quadrant == "blue_ocean" else 0.0
        quad_crowded_niche = 1.0 if quadrant == "crowded_niche" else 0.0
        quad_frontier = 1.0 if quadrant == "frontier" else 0.0

        # Feature vector in model's exact feature order
        fv = [
            pop, dens, demand, ageRatio, incomeIdx, area,
            cc, oc, gap, num_operators,
            competitors_500m, competitors_1km,
            supermarkets_500m, supermarkets_1km,
            bpost_500m, bpost_1km, bpost_2km,
            dist_nearest_competitor, dist_nearest_supermarket,
            dist_nearest_other_locker,
            bus_stops_300m, rail_tram_300m, shops_300m,
            amenities_300m, parking_500m, footways_300m,
            zone_rural, zone_suburban, zone_urban,
            quad_battleground, quad_blue_ocean, quad_crowded_niche,
            quad_frontier,
        ]

        results.append((lat, lng, sc, fv))

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    t_start = time.time()

    # -----------------------------------------------------------------------
    # 1. Generate grid
    # -----------------------------------------------------------------------
    print("Step 1: Generating 100m grid over Belgium...")
    lats = np.arange(LAT_MIN, LAT_MAX, GRID_STEP_LAT)
    lngs = np.arange(LNG_MIN, LNG_MAX, GRID_STEP_LNG)
    print(f"  Lat steps: {len(lats)}, Lng steps: {len(lngs)}")
    print(f"  Raw grid points: {len(lats) * len(lngs):,}")

    # Generate all (lat, lng) pairs
    grid_points = []
    for lat in lats:
        for lng in lngs:
            grid_points.append((float(lat), float(lng)))

    print(f"  Total grid points: {len(grid_points):,}")

    # Split into batches
    batches = []
    for i in range(0, len(grid_points), BATCH_SIZE):
        batches.append(grid_points[i:i + BATCH_SIZE])
    print(f"  Batches: {len(batches)} (size {BATCH_SIZE})")

    # -----------------------------------------------------------------------
    # 2. Parallel enrichment
    # -----------------------------------------------------------------------
    n_workers = min(cpu_count(), 8)
    print(f"\nStep 2: Enriching grid points ({n_workers} workers)...")

    all_results = []
    n_processed = 0
    t_enrich_start = time.time()

    with Pool(n_workers, initializer=init_worker,
              initargs=(str(DATA_DIR),)) as pool:
        for batch_result in pool.imap_unordered(enrich_batch, batches):
            all_results.extend(batch_result)
            n_processed += BATCH_SIZE
            if n_processed % PROGRESS_INTERVAL < BATCH_SIZE:
                elapsed = time.time() - t_enrich_start
                rate = n_processed / elapsed if elapsed > 0 else 0
                est_remaining = (len(grid_points) - n_processed) / rate if rate > 0 else 0
                print(f"  {n_processed:>10,} / {len(grid_points):,} scored "
                      f"({len(all_results):,} in Belgium) "
                      f"[{elapsed:.0f}s elapsed, ~{est_remaining:.0f}s remaining]",
                      flush=True)

    t_enrich = time.time() - t_enrich_start
    print(f"\n  Enrichment complete: {len(all_results):,} points in Belgium "
          f"({t_enrich:.0f}s)")

    if not all_results:
        print("ERROR: No points scored. Check data files.", file=sys.stderr)
        sys.exit(1)

    # -----------------------------------------------------------------------
    # 3. Batch prediction
    # -----------------------------------------------------------------------
    print("\nStep 3: Running model predictions...")

    model_data = joblib.load(TRAINING_DIR / "locker_placement_model.joblib")
    model = model_data["model"]
    feature_names = model_data["feature_names"]
    metrics = model_data.get("metrics", [])
    mean_auc = np.mean([m["auc_roc"] for m in metrics]) if metrics else 0

    # Extract feature matrix
    lats_out = np.array([r[0] for r in all_results], dtype=np.float32)
    lngs_out = np.array([r[1] for r in all_results], dtype=np.float32)
    scs_out = [r[2] for r in all_results]
    X = np.array([r[3] for r in all_results], dtype=np.float32)

    print(f"  Feature matrix shape: {X.shape}")
    print(f"  Model features: {len(feature_names)}")

    # Replace inf with large value (model can't handle inf)
    X = np.nan_to_num(X, nan=0.0, posinf=50000.0, neginf=0.0)

    scores = model.predict_proba(X)[:, 1]
    print(f"  Scores: min={scores.min():.4f}, max={scores.max():.4f}, "
          f"mean={scores.mean():.4f}, median={np.median(scores):.4f}")

    # -----------------------------------------------------------------------
    # 4. Save backend parquet
    # -----------------------------------------------------------------------
    print("\nStep 4: Saving backend parquet...")

    df = pd.DataFrame({
        "lat": lats_out,
        "lng": lngs_out,
        "score": scores.astype(np.float32),
        "sc": pd.Categorical(scs_out),
    })

    parquet_path = DATA_DIR / "ml_heatmap.parquet"
    df.to_parquet(parquet_path, index=False, compression="snappy")
    parquet_size_mb = parquet_path.stat().st_size / (1024 * 1024)
    print(f"  Saved {parquet_path} ({parquet_size_mb:.1f} MB, {len(df):,} rows)")

    # -----------------------------------------------------------------------
    # 5. Aggregate per sector + save frontend JSON
    # -----------------------------------------------------------------------
    print("\nStep 5: Aggregating per sector and saving frontend JSON...")

    sectors_out = {}
    for sc, group in df.groupby("sc", observed=True):
        best_idx = group["score"].idxmax()
        sectors_out[sc] = {
            "score": round(float(group["score"].max()), 4),
            "mean": round(float(group["score"].mean()), 4),
            "best_lat": round(float(group.loc[best_idx, "lat"]), 5),
            "best_lng": round(float(group.loc[best_idx, "lng"]), 5),
            "n_scored": int(len(group)),
        }

    # Compute stats
    stats = {
        "mean": round(float(scores.mean()), 4),
        "median": round(float(np.median(scores)), 4),
        "std": round(float(scores.std()), 4),
        "p25": round(float(np.percentile(scores, 25)), 4),
        "p75": round(float(np.percentile(scores, 75)), 4),
        "p90": round(float(np.percentile(scores, 90)), 4),
        "p95": round(float(np.percentile(scores, 95)), 4),
        "min": round(float(scores.min()), 4),
        "max": round(float(scores.max()), 4),
    }

    output = {
        "meta": {
            "model": "xgboost_v1",
            "auc_roc": round(mean_auc, 4),
            "training_regions": model_data.get("training_regions", []),
            "grid_resolution_m": 100,
            "total_scored": len(all_results),
            "total_sectors": len(sectors_out),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "stats": stats,
        "sectors": sectors_out,
    }

    json_path = DATA_DIR / "ml_scores.json"
    with open(json_path, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    json_size_mb = json_path.stat().st_size / (1024 * 1024)
    print(f"  Saved {json_path} ({json_size_mb:.1f} MB, {len(sectors_out)} sectors)")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    t_total = time.time() - t_start
    print(f"\nDone in {t_total:.0f}s ({t_total / 60:.1f} min)")
    print(f"  Grid points scored: {len(all_results):,}")
    print(f"  Sectors covered: {len(sectors_out)}")
    print(f"  Score distribution: mean={stats['mean']}, "
          f"median={stats['median']}, p75={stats['p75']}, p90={stats['p90']}")
    print(f"  Backend: {parquet_path} ({parquet_size_mb:.1f} MB)")
    print(f"  Frontend: {json_path} ({json_size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
