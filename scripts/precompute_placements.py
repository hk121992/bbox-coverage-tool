#!/usr/bin/env python3
"""
Precompute greedy MCLP placement sequences for the bbox coverage tool.

Uses numpy for vectorized haversine distance computation.
Runs the greedy algorithm for travel times 1-15 minutes.
Output: data/placements.json
"""

import json
import math
import time
import sys
from pathlib import Path
from collections import defaultdict

import numpy as np

DATA_DIR = Path("/Users/henry/Desktop/bbox-coverage-tool/data")

BASE_RADII = {"urban": 400, "suburban": 600, "rural": 4000}
ZONE_TO_IDX = {"urban": 0, "suburban": 1, "rural": 2}
RADII_ARRAY = np.array([400, 600, 4000], dtype=np.float64)
CELL_SIZE = 0.01
DEG_TO_RAD = 0.017453293
R_EARTH = 6371000


def build_spatial_index(lats, lngs):
    """Build grid-based spatial index from coordinate arrays."""
    index = defaultdict(list)
    cell_lats = (lats // CELL_SIZE).astype(int)
    cell_lngs = (lngs // CELL_SIZE).astype(int)
    for i in range(len(lats)):
        key = (cell_lats[i], cell_lngs[i])
        index[key].append(i)
    return index


def get_nearby_indices(index, lat, lng, radius_deg):
    """Get indices of all points within radius_deg grid cells of (lat, lng)."""
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
    """Vectorized haversine: distance from (lat1, lng1) to arrays of points. Returns meters."""
    d_lat = (lats2 - lat1) * DEG_TO_RAD
    d_lng = (lngs2 - lng1) * DEG_TO_RAD
    a = (np.sin(d_lat / 2) ** 2 +
         math.cos(lat1 * DEG_TO_RAD) * np.cos(lats2 * DEG_TO_RAD) *
         np.sin(d_lng / 2) ** 2)
    return R_EARTH * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def run_greedy(centroids_lat, centroids_lng, centroids_pop, centroids_zone_idx,
               bbox_lat, bbox_lng, centroids_sc, time_multiplier):
    """
    Run greedy MCLP to exhaustion using vectorized operations.
    Returns (placements, start_coverage_pct).
    """
    n = len(centroids_lat)
    total_pop = centroids_pop.sum()
    if total_pop == 0:
        return [], 0.0

    # Zone-specific radii for this travel time
    radii = RADII_ARRAY * time_multiplier  # [urban_r, suburban_r, rural_r]
    max_radius = radii[2]  # rural is largest
    max_radius_deg = max_radius / 111000

    # Build spatial index of centroids
    centroid_index = build_spatial_index(centroids_lat, centroids_lng)

    # Compute existing bbox coverage
    covered = np.zeros(n, dtype=bool)
    for bi in range(len(bbox_lat)):
        nearby = get_nearby_indices(centroid_index, bbox_lat[bi], bbox_lng[bi], max_radius_deg)
        if not nearby:
            continue
        nearby = np.array(nearby)
        # Filter to uncovered only
        uncov_mask = ~covered[nearby]
        if not uncov_mask.any():
            continue
        nearby_uncov = nearby[uncov_mask]

        dists = haversine_vec(bbox_lat[bi], bbox_lng[bi],
                              centroids_lat[nearby_uncov], centroids_lng[nearby_uncov])
        zone_radii = radii[centroids_zone_idx[nearby_uncov]]
        newly_covered = nearby_uncov[dists <= zone_radii]
        covered[newly_covered] = True

    covered_pop = centroids_pop[covered].sum()
    start_coverage = float(covered_pop / total_pop * 100)

    print(f"  Start coverage: {start_coverage:.1f}% ({int(covered_pop):,} / {int(total_pop):,})")
    sys.stdout.flush()

    # Greedy placement loop
    placements = []
    iteration = 0

    # Pre-filter: only centroids with pop > 0 are candidates
    has_pop = centroids_pop > 0

    # Cap at 99% — the tail adds thousands of low-value placements
    coverage_cap = 0.99 * total_pop

    while covered_pop < coverage_cap:
        # Candidate mask: uncovered centroids with pop > 0
        candidate_mask = has_pop & ~covered
        candidate_indices = np.where(candidate_mask)[0]

        if len(candidate_indices) == 0:
            break

        best_idx = -1
        best_gain = 0
        best_newly = None

        for ci in candidate_indices:
            clat, clng = centroids_lat[ci], centroids_lng[ci]

            # Get nearby centroids
            nearby = get_nearby_indices(centroid_index, clat, clng, max_radius_deg)
            if not nearby:
                continue
            nearby = np.array(nearby)

            # Filter to uncovered
            uncov_mask = ~covered[nearby]
            if not uncov_mask.any():
                continue
            nearby_uncov = nearby[uncov_mask]

            # Vectorized distance
            dists = haversine_vec(clat, clng,
                                  centroids_lat[nearby_uncov], centroids_lng[nearby_uncov])
            zone_radii = radii[centroids_zone_idx[nearby_uncov]]
            within = dists <= zone_radii

            if not within.any():
                continue

            newly = nearby_uncov[within]
            gain = int(centroids_pop[newly].sum())

            if gain > best_gain:
                best_gain = gain
                best_idx = ci
                best_newly = newly

        if best_idx == -1 or best_gain == 0:
            break

        # Place the best candidate
        covered[best_newly] = True
        covered_pop += best_gain
        iteration += 1

        cum_pct = round(float(covered_pop / total_pop * 100), 2)
        placements.append({
            "sc": centroids_sc[best_idx],
            "lat": round(float(centroids_lat[best_idx]), 5),
            "lng": round(float(centroids_lng[best_idx]), 5),
            "gain": best_gain,
            "cum": cum_pct,
        })

        if iteration % 50 == 0:
            print(f"    Iteration {iteration}: +{best_gain} people, "
                  f"cumulative {cum_pct:.1f}%")
            sys.stdout.flush()

    return placements, start_coverage


def main():
    t0 = time.time()

    # Load data
    print("Loading data...")
    sys.stdout.flush()
    with open(DATA_DIR / "bbox.json", "r") as f:
        bbox_raw = json.load(f)
    with open(DATA_DIR / "centroids.json", "r") as f:
        centroids_raw = json.load(f)

    # Convert to numpy arrays
    n_centroids = len(centroids_raw)
    centroids_lat = np.array([c["lat"] for c in centroids_raw], dtype=np.float64)
    centroids_lng = np.array([c["lng"] for c in centroids_raw], dtype=np.float64)
    centroids_pop = np.array([c["pop"] for c in centroids_raw], dtype=np.float64)
    centroids_zone_idx = np.array([ZONE_TO_IDX[c["zone"]] for c in centroids_raw], dtype=np.int32)
    centroids_sc = [c["sc"] for c in centroids_raw]

    bbox_lat = np.array([b["lat"] for b in bbox_raw], dtype=np.float64)
    bbox_lng = np.array([b["lng"] for b in bbox_raw], dtype=np.float64)

    total_pop = centroids_pop.sum()
    pop_count = (centroids_pop > 0).sum()
    print(f"  bbox lockers: {len(bbox_raw)}")
    print(f"  centroids: {n_centroids} ({pop_count} with pop > 0)")
    print(f"  total population: {int(total_pop):,}")
    sys.stdout.flush()

    # Resume from existing results if available
    output_path = DATA_DIR / "placements.json"
    results = {}
    if output_path.exists():
        with open(output_path, "r") as f:
            results = json.load(f)
        completed = sorted(int(k) for k in results.keys())
        print(f"  Resuming — already have travel times: {completed}")
        sys.stdout.flush()

    # Run for each travel time (start from 3 min)
    for travel_min in range(3, 16):
        key = str(travel_min)
        if key in results:
            print(f"\n=== Travel time: {travel_min} min — SKIPPING (already computed) ===")
            sys.stdout.flush()
            continue

        time_multiplier = travel_min / 5
        print(f"\n=== Travel time: {travel_min} min (multiplier: {time_multiplier:.2f}) ===")
        sys.stdout.flush()
        t1 = time.time()

        placements, start_cov = run_greedy(
            centroids_lat, centroids_lng, centroids_pop, centroids_zone_idx,
            bbox_lat, bbox_lng, centroids_sc, time_multiplier
        )

        elapsed = time.time() - t1
        final_cov = placements[-1]["cum"] if placements else start_cov
        print(f"  Result: {len(placements)} placements, "
              f"{start_cov:.1f}% -> {final_cov:.1f}% in {elapsed:.1f}s")
        sys.stdout.flush()

        results[key] = {
            "startCoverage": round(start_cov, 2),
            "placements": placements,
        }

        # Save after each travel time so we don't lose progress
        print(f"  Saving progress ({len(results)}/13 travel times)...")
        sys.stdout.flush()
        with open(output_path, "w") as f:
            json.dump(results, f, separators=(",", ":"))

    file_size = output_path.stat().st_size / (1024 * 1024)
    print(f"\nFinal file size: {file_size:.1f} MB")

    total_elapsed = time.time() - t0
    print(f"\nDone in {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")


if __name__ == "__main__":
    main()
