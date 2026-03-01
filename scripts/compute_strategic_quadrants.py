#!/usr/bin/env python3
"""
Compute strategic opportunity quadrants for each Belgian statistical sector.

Categorizes sectors on two axes:
  - Demand level (high/low relative to median)
  - Competitive intensity (greenfield vs. contested)

Output quadrants:
  blue_ocean    — High demand + low competition (best opportunities)
  battleground  — High demand + high competition (proven market, tough fight)
  frontier      — Low demand + low competition (expansion play)
  crowded_niche — Low demand + high competition (avoid)

Output: data/strategic_quadrants.json
"""

import json
import math
import statistics
from pathlib import Path
from collections import Counter

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Competition threshold: gap >= this → "low competition" (greenfield/light)
COMPETITION_THRESHOLD = 0.7

ZONE_TO_IDX = {"urban": 0, "suburban": 1, "rural": 2}
RADII_ARRAY = np.array([400, 600, 4000], dtype=np.float64)
CELL_SIZE = 0.01
DEG_TO_RAD = 0.017453293
R_EARTH = 6371000
TRAVEL_MIN = 5


def haversine_vec(lat1, lng1, lats2, lngs2):
    d_lat = (lats2 - lat1) * DEG_TO_RAD
    d_lng = (lngs2 - lng1) * DEG_TO_RAD
    a = (np.sin(d_lat / 2) ** 2 +
         math.cos(lat1 * DEG_TO_RAD) * np.cos(lats2 * DEG_TO_RAD) *
         np.sin(d_lng / 2) ** 2)
    return R_EARTH * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def build_spatial_index(lats, lngs):
    from collections import defaultdict
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


def main():
    # Load data
    with open(DATA_DIR / "centroids.json") as f:
        centroids = json.load(f)

    with open(DATA_DIR / "competitive_coverage.json") as f:
        comp_cov = json.load(f)

    with open(DATA_DIR / "bbox.json") as f:
        bbox_data = json.load(f)

    comp_sectors = comp_cov.get("sectors", {})

    print(f"Centroids: {len(centroids)}")
    print(f"Competitive sectors: {len(comp_sectors)}")

    # Compute demand threshold: median of sectors with pop > 0
    demands = [c["demand"] for c in centroids if c.get("demand", 0) > 0 and c["pop"] > 0]
    demand_threshold = statistics.median(demands)
    print(f"Demand threshold (median): {demand_threshold:.1f}")

    # Compute current bbox coverage to determine uncovered population
    n = len(centroids)
    cent_lat = np.array([c["lat"] for c in centroids], dtype=np.float64)
    cent_lng = np.array([c["lng"] for c in centroids], dtype=np.float64)
    cent_pop = np.array([c["pop"] for c in centroids], dtype=np.float64)
    cent_zone = np.array([ZONE_TO_IDX[c["zone"]] for c in centroids], dtype=np.int32)
    cent_sc = [c["sc"] for c in centroids]

    bbox_lat = np.array([b["lat"] for b in bbox_data], dtype=np.float64)
    bbox_lng = np.array([b["lng"] for b in bbox_data], dtype=np.float64)

    # Build spatial index for centroids
    cent_index = build_spatial_index(cent_lat, cent_lng)

    # Compute coverage at reference travel time
    time_multiplier = TRAVEL_MIN / 5.0
    radii = RADII_ARRAY * time_multiplier
    max_radius = radii[2]
    max_radius_deg = max_radius / 111000

    covered = np.zeros(n, dtype=bool)
    for bi in range(len(bbox_lat)):
        nearby = get_nearby_indices(cent_index, bbox_lat[bi], bbox_lng[bi], max_radius_deg)
        if not nearby:
            continue
        nearby = np.array(nearby)
        uncov = nearby[~covered[nearby]]
        if len(uncov) == 0:
            continue
        dists = haversine_vec(bbox_lat[bi], bbox_lng[bi], cent_lat[uncov], cent_lng[uncov])
        zone_radii = radii[cent_zone[uncov]]
        covered[uncov[dists <= zone_radii]] = True

    # Assign quadrants
    sector_quadrants = {}
    summary = {
        "blue_ocean": {"count": 0, "pop": 0, "uncoveredPop": 0, "demand": 0},
        "battleground": {"count": 0, "pop": 0, "uncoveredPop": 0, "demand": 0},
        "frontier": {"count": 0, "pop": 0, "uncoveredPop": 0, "demand": 0},
        "crowded_niche": {"count": 0, "pop": 0, "uncoveredPop": 0, "demand": 0},
    }

    for i, c in enumerate(centroids):
        sc = c["sc"]
        pop = c["pop"]
        demand = c.get("demand", 0)

        if pop == 0:
            sector_quadrants[sc] = "frontier"  # empty sectors → frontier
            continue

        # Get competition level
        comp_data = comp_sectors.get(sc, {})
        gap = comp_data.get("gap", 1.0)

        # Classify
        high_demand = demand >= demand_threshold
        low_competition = gap >= COMPETITION_THRESHOLD

        if high_demand and low_competition:
            quadrant = "blue_ocean"
        elif high_demand and not low_competition:
            quadrant = "battleground"
        elif not high_demand and low_competition:
            quadrant = "frontier"
        else:
            quadrant = "crowded_niche"

        sector_quadrants[sc] = quadrant
        summary[quadrant]["count"] += 1
        summary[quadrant]["pop"] += pop
        summary[quadrant]["demand"] += round(demand)
        if not covered[i]:
            summary[quadrant]["uncoveredPop"] += pop

    # Build output
    output = {
        "meta": {
            "demandThreshold": round(demand_threshold, 1),
            "competitionThreshold": COMPETITION_THRESHOLD,
            "travelMinutes": TRAVEL_MIN,
            "totalSectors": len(centroids),
        },
        "summary": summary,
        "sectors": sector_quadrants,
    }

    out_path = DATA_DIR / "strategic_quadrants.json"
    with open(out_path, "w") as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"\nOutput saved to: {out_path}")
    print(f"File size: {out_path.stat().st_size / 1024:.1f} KB")

    total_pop = sum(c["pop"] for c in centroids)
    total_uncov = sum(s["uncoveredPop"] for s in summary.values())

    print(f"\n{'Quadrant':<20} {'Sectors':>8} {'Pop':>12} {'Uncovered':>12} {'% of Uncov':>10}")
    print("-" * 64)
    for q in ["blue_ocean", "battleground", "frontier", "crowded_niche"]:
        s = summary[q]
        pct = s["uncoveredPop"] / total_uncov * 100 if total_uncov > 0 else 0
        print(f"{q:<20} {s['count']:>8,} {s['pop']:>12,} {s['uncoveredPop']:>12,} {pct:>9.1f}%")
    print(f"{'TOTAL':<20} {sum(s['count'] for s in summary.values()):>8,} "
          f"{total_pop:>12,} {total_uncov:>12,}")


if __name__ == "__main__":
    main()
