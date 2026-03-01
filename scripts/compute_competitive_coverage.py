#!/usr/bin/env python3
"""
Compute per-sector competitive coverage metrics.
For each Belgian statistical sector, counts competitor points within travel radius
and computes a competitive gap score.

Output: data/competitive_coverage.json
"""

import json
import math
from pathlib import Path
from collections import defaultdict

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

ZONE_TO_IDX = {"urban": 0, "suburban": 1, "rural": 2}
RADII_ARRAY = np.array([400, 600, 4000], dtype=np.float64)
CELL_SIZE = 0.01
DEG_TO_RAD = 0.017453293
R_EARTH = 6371000
TRAVEL_MIN = 5  # Reference travel time for precomputed coverage


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


def gap_score(operator_count):
    """Competitive gap score: higher = more opportunity (fewer competitors)."""
    if operator_count == 0:
        return 1.0
    elif operator_count <= 2:
        return 0.7
    elif operator_count <= 4:
        return 0.3
    else:
        return 0.0


def main():
    # Load centroids
    with open(DATA_DIR / "centroids.json") as f:
        centroids = json.load(f)

    # Load competitors
    with open(DATA_DIR / "competitors.json") as f:
        competitors = json.load(f)

    print(f"Centroids: {len(centroids)}")
    print(f"Competitors: {len(competitors)}")

    # Build arrays
    n_cent = len(centroids)
    cent_lat = np.array([c["lat"] for c in centroids])
    cent_lng = np.array([c["lng"] for c in centroids])
    cent_zone_idx = np.array([ZONE_TO_IDX[c["zone"]] for c in centroids])
    cent_sc = [c["sc"] for c in centroids]

    n_comp = len(competitors)
    comp_lat = np.array([c["lat"] for c in competitors])
    comp_lng = np.array([c["lng"] for c in competitors])
    comp_operator = [c["operator"] for c in competitors]

    # Build spatial index for competitors
    comp_index = build_spatial_index(comp_lat, comp_lng)

    # Compute radii for this travel time
    time_multiplier = TRAVEL_MIN / 5.0
    radii = RADII_ARRAY * time_multiplier
    max_radius = radii[2]  # rural radius (largest)
    max_radius_deg = max_radius / 111000

    # For each centroid, find competitors within its zone-appropriate radius
    sectors = {}
    operator_coverage = defaultdict(int)  # operator → number of sectors covered
    covered_by_any = 0

    for i in range(n_cent):
        nearby = get_nearby_indices(comp_index, cent_lat[i], cent_lng[i], max_radius_deg)

        all_operators_present = set()
        competitor_operators = set()  # excludes bpost (own network)
        comp_count = 0

        if nearby:
            nearby_arr = np.array(nearby)
            dists = haversine_vec(cent_lat[i], cent_lng[i],
                                  comp_lat[nearby_arr], comp_lng[nearby_arr])
            zone_radius = radii[cent_zone_idx[i]]
            within = nearby_arr[dists <= zone_radius]

            comp_count = len(within)
            for idx in within:
                op = comp_operator[idx]
                all_operators_present.add(op)
                if op != "bpost":
                    competitor_operators.add(op)

        ops_list = sorted(all_operators_present)
        # Gap score based on actual competitors only (not bpost own network)
        op_count = len(competitor_operators)

        sectors[cent_sc[i]] = {
            "cc": comp_count,
            "oc": op_count,
            "ops": ops_list,
            "gap": gap_score(op_count),
        }

        if comp_count > 0:
            covered_by_any += 1
            for op in ops_list:
                operator_coverage[op] += 1

        if (i + 1) % 5000 == 0:
            print(f"  Processed {i + 1}/{n_cent} sectors...")

    # Build output
    output = {
        "meta": {
            "travelMinutes": TRAVEL_MIN,
            "totalCompetitors": n_comp,
            "totalSectors": n_cent,
        },
        "sectors": sectors,
        "stats": {
            "coveredByAny": covered_by_any,
            "operatorCoverage": dict(sorted(operator_coverage.items(),
                                            key=lambda x: -x[1])),
        },
    }

    out_path = DATA_DIR / "competitive_coverage.json"
    with open(out_path, "w") as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"\nOutput saved to: {out_path}")
    print(f"File size: {out_path.stat().st_size / 1024:.1f} KB")
    print(f"\nSectors covered by any competitor: {covered_by_any}/{n_cent} "
          f"({covered_by_any / n_cent * 100:.1f}%)")
    print(f"\nOperator coverage (sectors):")
    for op, count in sorted(operator_coverage.items(), key=lambda x: -x[1]):
        print(f"  {op}: {count}")

    # Gap score distribution
    from collections import Counter
    gap_dist = Counter(s["gap"] for s in sectors.values())
    print(f"\nGap score distribution:")
    for score in sorted(gap_dist.keys(), reverse=True):
        print(f"  {score}: {gap_dist[score]} sectors")


if __name__ == "__main__":
    main()
