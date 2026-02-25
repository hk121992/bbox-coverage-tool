#!/usr/bin/env python3
"""
Parallel precompute: runs multiple travel times concurrently.
Each travel time runs in a separate process.
Results are merged into data/placements.json.
"""

import json
import sys
import time
from pathlib import Path
from multiprocessing import Pool, cpu_count

# Import the greedy engine from the main script
sys.path.insert(0, str(Path(__file__).parent))
from precompute_placements import (
    run_greedy, DATA_DIR, ZONE_TO_IDX
)
import numpy as np


def compute_one(travel_min):
    """Run greedy for a single travel time. Returns (travel_min, result_dict)."""
    t0 = time.time()
    time_multiplier = travel_min / 5

    # Load data fresh per process
    with open(DATA_DIR / "bbox.json", "r") as f:
        bbox_raw = json.load(f)
    with open(DATA_DIR / "centroids.json", "r") as f:
        centroids_raw = json.load(f)

    centroids_lat = np.array([c["lat"] for c in centroids_raw], dtype=np.float64)
    centroids_lng = np.array([c["lng"] for c in centroids_raw], dtype=np.float64)
    centroids_pop = np.array([c["pop"] for c in centroids_raw], dtype=np.float64)
    centroids_zone_idx = np.array([ZONE_TO_IDX[c["zone"]] for c in centroids_raw], dtype=np.int32)
    centroids_sc = [c["sc"] for c in centroids_raw]
    bbox_lat = np.array([b["lat"] for b in bbox_raw], dtype=np.float64)
    bbox_lng = np.array([b["lng"] for b in bbox_raw], dtype=np.float64)

    print(f"[T={travel_min}] Starting (multiplier: {time_multiplier:.2f})")
    sys.stdout.flush()

    placements, start_cov = run_greedy(
        centroids_lat, centroids_lng, centroids_pop, centroids_zone_idx,
        bbox_lat, bbox_lng, centroids_sc, time_multiplier
    )

    elapsed = time.time() - t0
    final_cov = placements[-1]["cum"] if placements else start_cov
    print(f"[T={travel_min}] Done: {len(placements)} placements, "
          f"{start_cov:.1f}% -> {final_cov:.1f}% in {elapsed:.1f}s")
    sys.stdout.flush()

    return travel_min, {
        "startCoverage": round(start_cov, 2),
        "placements": placements,
    }


def main():
    travel_times = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 else [6, 9, 12, 15]

    # Load existing results to preserve them
    output_path = DATA_DIR / "placements.json"
    results = {}
    if output_path.exists():
        with open(output_path, "r") as f:
            results = json.load(f)
        existing = sorted(int(k) for k in results.keys())
        print(f"Existing travel times: {existing}")

    # Filter out already-computed ones
    todo = [t for t in travel_times if str(t) not in results]
    if not todo:
        print("All requested travel times already computed!")
        return

    n_workers = min(len(todo), 5)  # cap at 5 to leave cores free
    print(f"Computing travel times {todo} using {n_workers} parallel workers")
    sys.stdout.flush()

    t0 = time.time()

    with Pool(n_workers) as pool:
        for travel_min, result in pool.imap_unordered(compute_one, todo):
            results[str(travel_min)] = result
            # Save after each completes
            with open(output_path, "w") as f:
                json.dump(results, f, separators=(",", ":"))
            print(f"Saved travel time {travel_min} ({len(results)} total)")
            sys.stdout.flush()

    file_size = output_path.stat().st_size / (1024 * 1024)
    elapsed = time.time() - t0
    print(f"\nAll done in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"File size: {file_size:.1f} MB")
    print(f"Travel times in file: {sorted(int(k) for k in results.keys())}")


if __name__ == "__main__":
    main()
