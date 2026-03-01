#!/usr/bin/env bash
# Fetch competitor parcel locker and post office data from OpenStreetMap Overpass API
# Belgium bounding box: 49.5,2.4,51.6,6.4

set -euo pipefail

DATA_DIR="$(cd "$(dirname "$0")/../data" && pwd)"

echo "=== Fetching parcel lockers from Overpass API ==="
curl -s -o "$DATA_DIR/competitors_lockers_raw.json" \
  --data-urlencode 'data=[out:json][timeout:90];(node["amenity"="parcel_locker"](49.5,2.4,51.6,6.4);way["amenity"="parcel_locker"](49.5,2.4,51.6,6.4););out center tags;' \
  'https://overpass-api.de/api/interpreter'
echo "  Saved to $DATA_DIR/competitors_lockers_raw.json"

echo "=== Fetching post offices from Overpass API ==="
curl -s -o "$DATA_DIR/competitors_postoffices_raw.json" \
  --data-urlencode 'data=[out:json][timeout:90];(node["amenity"="post_office"](49.5,2.4,51.6,6.4);way["amenity"="post_office"](49.5,2.4,51.6,6.4););out center tags;' \
  'https://overpass-api.de/api/interpreter'
echo "  Saved to $DATA_DIR/competitors_postoffices_raw.json"

echo ""
echo "=== Done ==="
echo "Locker elements: $(python3 -c "import json; print(len(json.load(open('$DATA_DIR/competitors_lockers_raw.json'))['elements']))")"
echo "Post office elements: $(python3 -c "import json; print(len(json.load(open('$DATA_DIR/competitors_postoffices_raw.json'))['elements']))")"
