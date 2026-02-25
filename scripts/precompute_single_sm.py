#!/usr/bin/env python3
"""
Precompute supermarket top-up placements for a single travel time.

For each target A% (in 5% increments from bbox start coverage up to 95%):
1. Use bbox + first N optimal placements to reach A% as baseline.
2. For each uncovered sector, find its nearest supermarket.
3. Greedily pick supermarkets by total uncovered weight to build SM sequence.
4. Backwards optimisation: check which of the N optimal placements are
   redundant because the SM placements can cover their unique sectors.

Writes all A% results for this travel time to placements_sm.json atomically.

Usage: python3 scripts/precompute_single_sm.py <travel_minutes> [pop|demand]
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
A_STEP = 5  # target A% increment


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


def haversine_single(lat1, lng1, lat2, lng2):
    d_lat = (lat2 - lat1) * DEG_TO_RAD
    d_lng = (lng2 - lng1) * DEG_TO_RAD
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(lat1 * DEG_TO_RAD) * math.cos(lat2 * DEG_TO_RAD) *
         math.sin(d_lng / 2) ** 2)
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compute_coverage(lats, lngs, centroids_lat, centroids_lng, centroids_zone_idx,
                     centroid_index, radii, n):
    """Compute which centroids are covered by a set of locations."""
    max_radius_deg = float(radii[2]) / 111000
    covered = np.zeros(n, dtype=bool)
    for bi in range(len(lats)):
        nearby = get_nearby_indices(centroid_index, lats[bi], lngs[bi], max_radius_deg)
        if not nearby:
            continue
        nearby = np.array(nearby)
        uncov_mask = ~covered[nearby]
        if not uncov_mask.any():
            continue
        nearby_uncov = nearby[uncov_mask]
        dists = haversine_vec(lats[bi], lngs[bi],
                              centroids_lat[nearby_uncov], centroids_lng[nearby_uncov])
        zone_radii = radii[centroids_zone_idx[nearby_uncov]]
        covered[nearby_uncov[dists <= zone_radii]] = True
    return covered


def run_sm_topup(covered, centroids_lat, centroids_lng, centroids_weight, centroids_zone_idx,
                 sm_lat, sm_lng, sm_names, centroid_index, sm_index, radii,
                 total_weight, label):
    """Build greedy SM sequence from the given covered state."""
    n = len(centroids_lat)
    covered = covered.copy()  # don't mutate caller's array
    max_radius_deg = float(radii[2]) / 111000

    covered_weight = float(centroids_weight[covered].sum())
    start_coverage = covered_weight / total_weight * 100

    # For each uncovered sector, find nearest supermarket
    uncovered_indices = np.where((~covered) & (centroids_weight > 0))[0]

    sm_to_centroids = defaultdict(list)
    for ci in uncovered_indices:
        clat, clng = centroids_lat[ci], centroids_lng[ci]
        nearby_sm = get_nearby_indices(sm_index, clat, clng, max_radius_deg)
        if not nearby_sm:
            nearby_sm = get_nearby_indices(sm_index, clat, clng, max_radius_deg * 3)
        if not nearby_sm:
            continue

        best_sm = -1
        best_dist = float('inf')
        for si in nearby_sm:
            d = haversine_single(clat, clng, sm_lat[si], sm_lng[si])
            if d < best_dist:
                best_dist = d
                best_sm = si
        if best_sm >= 0:
            sm_to_centroids[best_sm].append(ci)

    # Greedy: pick SM with most uncovered weight
    placements = []
    coverage_cap = 0.995 * total_weight

    while covered_weight < coverage_cap and sm_to_centroids:
        best_sm = -1
        best_gain = 0.0
        best_centroid_list = None

        for si, centroid_list in sm_to_centroids.items():
            still_uncov = [ci for ci in centroid_list if not covered[ci]]
            if not still_uncov:
                continue
            sm_to_centroids[si] = still_uncov
            gain = float(centroids_weight[still_uncov].sum())
            if gain > best_gain:
                best_gain = gain
                best_sm = si
                best_centroid_list = still_uncov

        if best_sm == -1 or best_gain == 0:
            break

        for ci in best_centroid_list:
            covered[ci] = True
        covered_weight += best_gain
        del sm_to_centroids[best_sm]

        cum_pct = round(covered_weight / total_weight * 100, 2)
        placements.append({
            "name": sm_names[best_sm],
            "lat": round(float(sm_lat[best_sm]), 5),
            "lng": round(float(sm_lng[best_sm]), 5),
            "gain": round(best_gain, 1),
            "cum": cum_pct,
        })

    return placements, start_coverage


def backwards_check(bbox_covered, opt_placements_used, sm_placements,
                    centroids_lat, centroids_lng, centroids_weight, centroids_zone_idx,
                    centroid_index, radii, label):
    """Check which of the used optimal placements are redundant given SM coverage."""
    n = len(centroids_lat)
    max_radius_deg = float(radii[2]) / 111000
    n_opt = len(opt_placements_used)
    if n_opt == 0 or not sm_placements:
        return []

    opt_lat = np.array([p["lat"] for p in opt_placements_used], dtype=np.float64)
    opt_lng = np.array([p["lng"] for p in opt_placements_used], dtype=np.float64)
    sm_lat_arr = np.array([p["lat"] for p in sm_placements], dtype=np.float64)
    sm_lng_arr = np.array([p["lng"] for p in sm_placements], dtype=np.float64)

    sm_place_index = build_spatial_index(sm_lat_arr, sm_lng_arr)

    # For each optimal placement, find which sectors it covers
    opt_covers = []
    for oi in range(n_opt):
        nearby = get_nearby_indices(centroid_index, opt_lat[oi], opt_lng[oi], max_radius_deg)
        if not nearby:
            opt_covers.append(np.array([], dtype=int))
            continue
        nearby = np.array(nearby)
        dists = haversine_vec(opt_lat[oi], opt_lng[oi],
                              centroids_lat[nearby], centroids_lng[nearby])
        zone_radii = radii[centroids_zone_idx[nearby]]
        opt_covers.append(nearby[dists <= zone_radii])

    bbox_set = set(np.where(bbox_covered)[0].tolist())

    redundant = []
    for oi in range(n_opt):
        my_sectors = set(opt_covers[oi].tolist())
        if not my_sectors:
            continue

        # Unique = not covered by bbox or other optimal placements
        my_unique = my_sectors - bbox_set
        if not my_unique:
            redundant.append(oi)
            continue

        for oj in range(n_opt):
            if oj == oi:
                continue
            my_unique -= set(opt_covers[oj].tolist())
            if not my_unique:
                break

        if not my_unique:
            redundant.append(oi)
            continue

        # Check if all unique sectors are coverable by a SM placement
        all_covered_by_sm = True
        for ci in my_unique:
            if centroids_weight[ci] == 0:
                continue
            clat, clng = centroids_lat[ci], centroids_lng[ci]
            sector_radius = float(radii[centroids_zone_idx[ci]])
            sector_radius_deg = sector_radius / 111000

            nearby_sm = get_nearby_indices(sm_place_index, clat, clng, sector_radius_deg)
            if not nearby_sm:
                all_covered_by_sm = False
                break

            found = False
            for si in nearby_sm:
                d = haversine_single(clat, clng, float(sm_lat_arr[si]), float(sm_lng_arr[si]))
                if d <= sector_radius:
                    found = True
                    break
            if not found:
                all_covered_by_sm = False
                break

        if all_covered_by_sm:
            redundant.append(oi)

    # Build result list
    redundant_list = []
    for oi in redundant:
        my_sectors = set(opt_covers[oi].tolist()) - bbox_set
        for oj in range(n_opt):
            if oj == oi:
                continue
            my_sectors -= set(opt_covers[oj].tolist())
        unique_weight = float(centroids_weight[list(my_sectors)].sum()) if my_sectors else 0.0

        entry = {
            "idx": oi,
            "lat": round(float(opt_lat[oi]), 5),
            "lng": round(float(opt_lng[oi]), 5),
            "uniqueWeight": round(unique_weight, 1),
        }
        if "sc" in opt_placements_used[oi]:
            entry["sc"] = opt_placements_used[oi]["sc"]
        redundant_list.append(entry)

    return redundant_list


def merge_results(travel_min, mode, results):
    """Merge all A% results for this travel time into placements_sm.json."""
    output_path = DATA_DIR / "placements_sm.json"
    lock_path = DATA_DIR / "placements_sm.json.lock"

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

        for target_a, result in results.items():
            key = f"{travel_min}_{target_a}_{mode}"
            existing[key] = result

        tmp = output_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(existing, f, separators=(",", ":"))
        tmp.replace(output_path)

        print(f"[{travel_min}min-sm-{mode}] Saved {len(results)} entries to placements_sm.json "
              f"({len(existing)} total)")
        sys.stdout.flush()
    finally:
        os.unlink(str(lock_path))


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/precompute_single_sm.py <travel_minutes> [pop|demand]")
        sys.exit(1)

    travel_min = int(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "pop"
    t0 = time.time()

    print(f"[{travel_min}min-sm-{mode}] Loading data...")
    sys.stdout.flush()

    with open(DATA_DIR / "bbox.json", "r") as f:
        bbox_raw = json.load(f)
    with open(DATA_DIR / "centroids.json", "r") as f:
        centroids_raw = json.load(f)
    with open(DATA_DIR / "supermarkets.json", "r") as f:
        sm_raw = json.load(f)

    if mode == "demand":
        placements_path = DATA_DIR / "placements_demand.json"
    else:
        placements_path = DATA_DIR / "placements.json"

    with open(placements_path, "r") as f:
        all_placements = json.load(f)

    opt_data = all_placements.get(str(travel_min))
    if not opt_data:
        print(f"[{travel_min}min-sm-{mode}] ERROR: no optimal placements for {travel_min}min")
        sys.exit(1)

    # Build arrays
    n = len(centroids_raw)
    centroids_lat = np.array([c["lat"] for c in centroids_raw], dtype=np.float64)
    centroids_lng = np.array([c["lng"] for c in centroids_raw], dtype=np.float64)
    centroids_zone_idx = np.array([ZONE_TO_IDX[c["zone"]] for c in centroids_raw], dtype=np.int32)

    if mode == "demand":
        centroids_weight = np.array(
            [c.get("demand", c["pop"]) for c in centroids_raw], dtype=np.float64
        )
    else:
        centroids_weight = np.array([c["pop"] for c in centroids_raw], dtype=np.float64)

    total_weight = centroids_weight.sum()
    time_multiplier = travel_min / 5
    radii = RADII_ARRAY * time_multiplier

    bbox_lat = np.array([b["lat"] for b in bbox_raw], dtype=np.float64)
    bbox_lng = np.array([b["lng"] for b in bbox_raw], dtype=np.float64)

    sm_lat = np.array([s["lat"] for s in sm_raw], dtype=np.float64)
    sm_lng = np.array([s["lng"] for s in sm_raw], dtype=np.float64)
    sm_names = [s.get("name", "Unknown") for s in sm_raw]

    centroid_index = build_spatial_index(centroids_lat, centroids_lng)
    sm_index = build_spatial_index(sm_lat, sm_lng)

    # Step 1: Compute bbox-only coverage (shared across all A% levels)
    bbox_covered = compute_coverage(bbox_lat, bbox_lng, centroids_lat, centroids_lng,
                                    centroids_zone_idx, centroid_index, radii, n)
    bbox_cov_pct = float(centroids_weight[bbox_covered].sum()) / total_weight * 100

    print(f"[{travel_min}min-sm-{mode}] bbox coverage: {bbox_cov_pct:.1f}%")
    sys.stdout.flush()

    # Step 2: Determine A% levels to compute
    # Start from bbox coverage rounded up to next 5%, go up to 95%
    start_a = int(math.ceil(bbox_cov_pct / A_STEP)) * A_STEP
    a_levels = list(range(start_a, 100, A_STEP))

    opt_placements = opt_data["placements"]

    print(f"[{travel_min}min-sm-{mode}] Computing SM top-up for A% levels: {a_levels}")
    sys.stdout.flush()

    # Step 3: For each A% level, find how many optimal placements are needed,
    # then compute SM top-up from that baseline
    results = {}

    for target_a in a_levels:
        label = f"{travel_min}min-sm-{mode}-A{target_a}"

        # Find how many optimal placements needed to reach target_a%
        # The placements list has cumulative coverage in 'cum' field
        n_opt_needed = 0
        for i, p in enumerate(opt_placements):
            if p["cum"] >= target_a:
                n_opt_needed = i + 1
                break
        else:
            # All placements don't reach target_a — use all of them
            n_opt_needed = len(opt_placements)

        opt_used = opt_placements[:n_opt_needed]

        # Build baseline coverage: bbox + first n_opt_needed optimal placements
        if n_opt_needed > 0:
            opt_lats = np.array([p["lat"] for p in opt_used], dtype=np.float64)
            opt_lngs = np.array([p["lng"] for p in opt_used], dtype=np.float64)
            baseline_lat = np.concatenate([bbox_lat, opt_lats])
            baseline_lng = np.concatenate([bbox_lng, opt_lngs])
        else:
            baseline_lat = bbox_lat
            baseline_lng = bbox_lng

        covered = compute_coverage(baseline_lat, baseline_lng, centroids_lat, centroids_lng,
                                   centroids_zone_idx, centroid_index, radii, n)
        actual_a = float(centroids_weight[covered].sum()) / total_weight * 100

        print(f"[{label}] {n_opt_needed} optimal placements -> {actual_a:.1f}% coverage")
        sys.stdout.flush()

        # SM top-up from this baseline
        sm_placements, sm_start = run_sm_topup(
            covered, centroids_lat, centroids_lng, centroids_weight, centroids_zone_idx,
            sm_lat, sm_lng, sm_names, centroid_index, sm_index, radii,
            total_weight, label
        )

        # Backwards check
        redundant = backwards_check(
            bbox_covered, opt_used, sm_placements,
            centroids_lat, centroids_lng, centroids_weight, centroids_zone_idx,
            centroid_index, radii, label
        )

        final_cov = sm_placements[-1]["cum"] if sm_placements else actual_a
        print(f"[{label}] {len(sm_placements)} SM placements -> {final_cov:.1f}%, "
              f"{len(redundant)} redundant optimal")
        sys.stdout.flush()

        results[target_a] = {
            "optUsed": n_opt_needed,
            "startCoverage": round(actual_a, 2),
            "placements": sm_placements,
            "redundant": redundant,
        }

    elapsed = time.time() - t0
    print(f"[{travel_min}min-sm-{mode}] All done: {len(results)} A% levels in {elapsed:.1f}s")
    sys.stdout.flush()

    merge_results(travel_min, mode, results)


if __name__ == "__main__":
    main()
