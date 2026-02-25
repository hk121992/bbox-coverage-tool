#!/usr/bin/env python3
"""
Parse raw Overpass API response for Belgian supermarkets.
Extracts lat, lon, and name for each element.
For 'way' elements, uses center coordinates from 'out center' output.
"""

import json
import sys

RAW_PATH = "/Users/henry/Desktop/bbox-coverage-tool/data/supermarkets_raw.json"
OUT_PATH = "/Users/henry/Desktop/bbox-coverage-tool/data/supermarkets.json"


def parse_supermarkets(raw_path: str) -> list[dict]:
    with open(raw_path, "r") as f:
        data = json.load(f)

    elements = data.get("elements", [])
    supermarkets = []

    for elem in elements:
        elem_type = elem.get("type")

        # Get coordinates
        if elem_type == "node":
            lat = elem.get("lat")
            lon = elem.get("lon")
        elif elem_type == "way":
            center = elem.get("center", {})
            lat = center.get("lat")
            lon = center.get("lon")
        else:
            continue

        if lat is None or lon is None:
            continue

        # Get name from tags
        tags = elem.get("tags", {})
        name = tags.get("name", None)

        supermarkets.append({
            "lat": lat,
            "lon": lon,
            "name": name,
        })

    return supermarkets


def main():
    supermarkets = parse_supermarkets(RAW_PATH)

    # Save cleaned data
    with open(OUT_PATH, "w") as f:
        json.dump(supermarkets, f, indent=2, ensure_ascii=False)

    # Report statistics
    total = len(supermarkets)
    named = sum(1 for s in supermarkets if s["name"] is not None)
    unnamed = total - named

    print(f"Total supermarkets found: {total}")
    print(f"  With name:    {named}")
    print(f"  Without name: {unnamed}")
    print(f"\nOutput saved to: {OUT_PATH}")

    # Show sample records
    print("\n--- Sample records ---")
    for record in supermarkets[:5]:
        print(f"  {record['name'] or '(unnamed)':<30s}  lat={record['lat']:.6f}  lon={record['lon']:.6f}")


if __name__ == "__main__":
    main()
