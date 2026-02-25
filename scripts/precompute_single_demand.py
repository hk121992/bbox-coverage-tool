#!/usr/bin/env python3
"""
Precompute greedy MCLP placement for a single travel time, demand-weighted.
Uses demand = pop × ageRatio × incomeIdx as the coverage weight.
Writes result to placements_demand.json atomically using a file lock.

Usage: python3 scripts/precompute_single_demand.py <travel_minutes>
"""

import json
import math
import time
import sys
import os
from pathlib import Path
from collections import defaultdict

import numpy as np

DATA_DIR = Path("/Users/henry/Desktop/bbox-coverage-tool/data")

ZONE_TO_IDX = {"urban": 0, "suburban": 1, "rural": 2}
RADII_ARRAY = np.array([400, 600, 4000], dtype=np.float64)
CELL_SIZE = 0.01
DEG_TO_RAD = 0.017453293
R_EARTH = 6371000


def build_spatial_index(lats, lngs):
    index = defaultdict(list)
    cell_lats = (lats // CELL_SIZE).astype(int)
    cell_lngs = (lngs // CELL_SIZE).astype(int)
    for i in range(len(lats)):
        key = (cell_lats[i], cell_lngs[i])
        index[key].append(i)
    return index


def get_nearby_indices(index, lat, lng, radius_deg):
    cell_radius = math.ceil(radius_deg / CELL_SIZE)
    base_lat = int(lat // CELL_SIZE)
    base_lng = int(lng // CELL_SIZE)
    results = []
    for dlat in range(-cell_radius, cell_radius + 1):
        for dlng in range(-cell_radius, cell_radius + 1):
            key = (base_lat + dlat, base_lng + dlng)
            if key in index:
                results.extend(index[key])
    return results


def haversine_vec(lat1, lng1, lats2, lngs2):
    d_lat = (lats2 - lat1) * DEG_TO_RAD
    d_lng = (lngs2 - lng1) * DEG_TO_RAD
    a = (np.sin(d_lat / 2) ** 2 +
         math.cos(lat1 * DEG_TO_RAD) * np.cos(lats2 * DEG_TO_RAD) *
         np.sin(d_lng / 2) ** 2)
    return R_EARTH * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def run_greedy(centroids_lat, centroids_lng, centroids_weight, centroids_zone_idx,
               bbox_lat, bbox_lng, centroids_sc, time_multiplier, travel_min):
    n = len(centroids_lat)
    total_weight = centroids_weight.sum()
    if total_weight == 0:
        return [], 0.0

    radii = RADII_ARRAY * time_multiplier
    max_radius = radii[2]
    max_radius_deg = max_radius / 111000

    centroid_index = build_spatial_index(centroids_lat, centroids_lng)

    # Compute existing bbox coverage
    covered = np.zeros(n, dtype=bool)
    for bi in range(len(bbox_lat)):
        nearby = get_nearby_indices(centroid_index, bbox_lat[bi], bbox_lng[bi], max_radius_deg)
        if not nearby:
            continue
        nearby = np.array(nearby)
        uncov_mask = ~covered[nearby]
        if not uncov_mask.any():
            continue
        nearby_uncov = nearby[uncov_mask]
        dists = haversine_vec(bbox_lat[bi], bbox_lng[bi],
                              centroids_lat[nearby_uncov], centroids_lng[nearby_uncov])
        zone_radii = radii[centroids_zone_idx[nearby_uncov]]
        covered[nearby_uncov[dists <= zone_radii]] = True

    covered_weight = centroids_weight[covered].sum()
    start_coverage = float(covered_weight / total_weight * 100)

    print(f"[{travel_min}min-demand] Start coverage: {start_coverage:.1f}% "
          f"({covered_weight:,.0f} / {total_weight:,.0f} demand)")
    sys.stdout.flush()

    placements = []
    iteration = 0
    has_weight = centroids_weight > 0
    coverage_cap = 0.99 * total_weight

    while covered_weight < coverage_cap:
        candidate_mask = has_weight & ~covered
        candidate_indices = np.where(candidate_mask)[0]

        if len(candidate_indices) == 0:
            break

        best_idx = -1
        best_gain = 0.0
        best_newly = None

        for ci in candidate_indices:
            clat, clng = centroids_lat[ci], centroids_lng[ci]
            nearby = get_nearby_indices(centroid_index, clat, clng, max_radius_deg)
            if not nearby:
                continue
            nearby = np.array(nearby)
            uncov_mask = ~covered[nearby]
            if not uncov_mask.any():
                continue
            nearby_uncov = nearby[uncov_mask]
            dists = haversine_vec(clat, clng,
                                  centroids_lat[nearby_uncov], centroids_lng[nearby_uncov])
            zone_radii = radii[centroids_zone_idx[nearby_uncov]]
            within = dists <= zone_radii
            if not within.any():
                continue
            newly = nearby_uncov[within]
            gain = float(centroids_weight[newly].sum())
            if gain > best_gain:
                best_gain = gain
                best_idx = ci
                best_newly = newly

        if best_idx == -1 or best_gain == 0:
            break

        covered[best_newly] = True
        covered_weight += best_gain
        iteration += 1

        cum_pct = round(float(covered_weight / total_weight * 100), 2)
        placements.append({
            "sc": centroids_sc[best_idx],
            "lat": round(float(centroids_lat[best_idx]), 5),
            "lng": round(float(centroids_lng[best_idx]), 5),
            "gain": round(best_gain, 1),
            "cum": cum_pct,
        })

        if iteration % 50 == 0:
            print(f"[{travel_min}min-demand] Iteration {iteration}: "
                  f"+{best_gain:,.0f} demand, cumulative {cum_pct:.1f}%")
            sys.stdout.flush()

    return placements, start_coverage


def merge_result(travel_min, result):
    """Merge result into placements_demand.json using a file lock."""
    output_path = DATA_DIR / "placements_demand.json"
    lock_path = DATA_DIR / "placements_demand.json.lock"

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            time.sleep(0.2)

    try:
        existing = {}
        if output_path.exists():
            with open(output_path, "r") as f:
                existing = json.load(f)

        existing[str(travel_min)] = result

        tmp = output_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(existing, f, separators=(",", ":"))
        tmp.replace(output_path)

        print(f"[{travel_min}min-demand] Saved to placements_demand.json "
              f"({len(existing)} travel times total)")
        sys.stdout.flush()
    finally:
        os.unlink(str(lock_path))


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/precompute_single_demand.py <travel_minutes>")
        sys.exit(1)

    travel_min = int(sys.argv[1])
    t0 = time.time()

    print(f"[{travel_min}min-demand] Loading data...")
    sys.stdout.flush()

    with open(DATA_DIR / "bbox.json", "r") as f:
        bbox_raw = json.load(f)
    with open(DATA_DIR / "centroids.json", "r") as f:
        centroids_raw = json.load(f)

    # Use demand score as weight; fall back to population if demand missing
    centroids_lat = np.array([c["lat"] for c in centroids_raw], dtype=np.float64)
    centroids_lng = np.array([c["lng"] for c in centroids_raw], dtype=np.float64)
    centroids_weight = np.array(
        [c.get("demand", c["pop"]) for c in centroids_raw], dtype=np.float64
    )
    centroids_zone_idx = np.array([ZONE_TO_IDX[c["zone"]] for c in centroids_raw], dtype=np.int32)
    centroids_sc = [c["sc"] for c in centroids_raw]

    bbox_lat = np.array([b["lat"] for b in bbox_raw], dtype=np.float64)
    bbox_lng = np.array([b["lng"] for b in bbox_raw], dtype=np.float64)

    time_multiplier = travel_min / 5

    placements, start_cov = run_greedy(
        centroids_lat, centroids_lng, centroids_weight, centroids_zone_idx,
        bbox_lat, bbox_lng, centroids_sc, time_multiplier, travel_min
    )

    elapsed = time.time() - t0
    final_cov = placements[-1]["cum"] if placements else start_cov
    print(f"[{travel_min}min-demand] Done: {len(placements)} placements, "
          f"{start_cov:.1f}% -> {final_cov:.1f}% in {elapsed:.1f}s")
    sys.stdout.flush()

    merge_result(travel_min, {
        "startCoverage": round(start_cov, 2),
        "placements": placements,
    })


if __name__ == "__main__":
    main()
