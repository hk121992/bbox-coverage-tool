#!/usr/bin/env python3
"""
Preprocess StatBel statistical sectors for the bbox coverage tool.

Steps:
1. Load sector boundary GeoJSON (EPSG:31370)
2. Reproject to WGS84 (EPSG:4326)
3. Load population XLSX and merge
4. Calculate centroids, population density, urban/rural classification
5. Simplify geometry for browser performance
6. Export as compact GeoJSON
"""

import json
import sys
import time
import geopandas as gpd
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path("/Users/henry/Desktop/bbox-coverage-tool/data")

# Density thresholds (inhabitants/km²)
URBAN_THRESHOLD = 600
SUBURBAN_THRESHOLD = 150

def main():
    t0 = time.time()

    # --- Step 1: Load sector boundaries ---
    print("Loading sector boundaries (210 MB, this takes a moment)...")
    geojson_path = DATA_DIR / "sh_statbel_statistical_sectors_31370_20220101.geojson" / "sh_statbel_statistical_sectors_31370_20220101.geojson"
    gdf = gpd.read_file(geojson_path)
    print(f"  Loaded {len(gdf)} sectors in {time.time()-t0:.1f}s")
    print(f"  CRS: {gdf.crs}")

    # --- Step 2: Reproject to WGS84 ---
    print("Reprojecting to WGS84 (EPSG:4326)...")
    gdf = gdf.to_crs(epsg=4326)
    print(f"  Done in {time.time()-t0:.1f}s")

    # --- Step 3: Load population data ---
    print("Loading population data...")
    pop_df = pd.read_excel(
        DATA_DIR / "statbel_population.xlsx",
        engine="openpyxl"
    )
    print(f"  Loaded {len(pop_df)} rows")
    print(f"  Columns: {list(pop_df.columns)}")

    # Rename for clarity
    pop_df = pop_df.rename(columns={
        "CD_SECTOR": "cd_sector",
        "TOTAL": "population",
        "OPPERVLAKKTE IN HM²": "area_hm2"
    })

    # Keep only active sectors (no stop date or stop in future)
    # Some sectors have DT_STOP_SECTOR = 9999-12-31
    pop_df = pop_df[["cd_sector", "population", "area_hm2"]].copy()

    # Handle duplicates - some sectors appear multiple times, keep the one with latest data
    pop_df = pop_df.groupby("cd_sector").agg({
        "population": "sum",
        "area_hm2": "first"
    }).reset_index()

    print(f"  Unique sectors with population: {len(pop_df)}")
    print(f"  Total population: {pop_df['population'].sum():,.0f}")

    # --- Step 4: Merge population into geodataframe ---
    print("Merging population data...")
    gdf = gdf.merge(pop_df, on="cd_sector", how="left")

    # Fill missing population with 0
    gdf["population"] = gdf["population"].fillna(0).astype(int)

    matched = (gdf["population"] > 0).sum()
    print(f"  Matched: {matched} / {len(gdf)} sectors have population > 0")
    print(f"  Total population in merged data: {gdf['population'].sum():,.0f}")

    # --- Step 5: Calculate centroids and density ---
    print("Calculating centroids and density...")

    # Centroid in WGS84
    centroids = gdf.geometry.centroid
    gdf["centroid_lat"] = centroids.y.round(5)
    gdf["centroid_lng"] = centroids.x.round(5)

    # Area in km² (from hm² in population data, or from geometry)
    # 1 hm² = 0.01 km²
    if "area_hm2" in gdf.columns:
        gdf["area_km2"] = (gdf["area_hm2"] * 0.01).round(4)
    else:
        # Fallback: use ms_area_ha from geojson (1 ha = 0.01 km²)
        gdf["area_km2"] = (gdf["ms_area_ha"] * 0.01).round(4)

    # Population density (inhabitants per km²)
    gdf["pop_density"] = np.where(
        gdf["area_km2"] > 0,
        (gdf["population"] / gdf["area_km2"]).round(1),
        0
    )

    # Urban/suburban/rural classification
    gdf["zone_type"] = np.where(
        gdf["pop_density"] >= URBAN_THRESHOLD, "urban",
        np.where(gdf["pop_density"] >= SUBURBAN_THRESHOLD, "suburban", "rural")
    )

    zone_counts = gdf["zone_type"].value_counts()
    print(f"  Zone classification:")
    for zone, count in zone_counts.items():
        pop = gdf[gdf["zone_type"] == zone]["population"].sum()
        print(f"    {zone}: {count} sectors, {pop:,.0f} population")

    # --- Step 6: Simplify geometry ---
    print("Simplifying geometry...")
    # Tolerance 0.0002 degrees (~20m at Belgian latitudes) — preserves shape well
    gdf["geometry"] = gdf.geometry.simplify(tolerance=0.0002, preserve_topology=True)

    # --- Step 6b: Fix double-UTF-8 encoding in string columns ---
    print("Fixing character encoding...")
    def fix_double_utf8(s):
        """Fix strings that were double-encoded as UTF-8."""
        if not isinstance(s, str):
            return s
        try:
            return s.encode('latin-1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s

    str_cols = gdf.select_dtypes(include=['object']).columns
    for col in str_cols:
        gdf[col] = gdf[col].apply(fix_double_utf8)

    # --- Step 7: Select and rename columns for export ---
    print("Preparing export...")
    export_cols = [
        "cd_sector",
        "tx_sector_descr_nl",
        "tx_munty_descr_nl",
        "tx_munty_descr_fr",
        "cd_munty_refnis",
        "tx_prov_descr_nl",
        "cd_rgn_refnis",
        "tx_rgn_descr_nl",
        "population",
        "area_km2",
        "pop_density",
        "zone_type",
        "centroid_lat",
        "centroid_lng",
        "geometry"
    ]

    # Only keep columns that exist
    export_cols = [c for c in export_cols if c in gdf.columns]
    gdf_export = gdf[export_cols].copy()

    # Rename for compactness
    rename_map = {
        "cd_sector": "sc",
        "tx_sector_descr_nl": "sn",
        "tx_munty_descr_nl": "mun",
        "tx_munty_descr_fr": "mun_fr",
        "cd_munty_refnis": "mun_id",
        "tx_prov_descr_nl": "prov",
        "cd_rgn_refnis": "rgn",
        "tx_rgn_descr_nl": "rgn_nl",
        "population": "pop",
        "area_km2": "area",
        "pop_density": "dens",
        "zone_type": "zone",
        "centroid_lat": "clat",
        "centroid_lng": "clng",
    }
    gdf_export = gdf_export.rename(columns=rename_map)

    # --- Step 8: Export ---
    # Drop unused columns (rgn_nl, mun_id not needed in browser)
    for col in ["rgn_nl", "mun_id"]:
        if col in gdf_export.columns:
            gdf_export = gdf_export.drop(columns=[col])

    output_path = DATA_DIR / "sectors.json"
    print(f"Exporting to {output_path}...")

    # Export as GeoJSON with proper UTF-8 and compact coordinates
    def strip_z(coords):
        """Recursively strip Z coordinates from coordinate arrays."""
        if isinstance(coords, (list, tuple)):
            if len(coords) > 0 and isinstance(coords[0], (int, float)):
                return [round(coords[0], 5), round(coords[1], 5)]
            return [strip_z(c) for c in coords]
        return coords

    features = []
    for _, row in gdf_export.iterrows():
        geom = row.geometry.__geo_interface__
        geom["coordinates"] = strip_z(geom["coordinates"])
        props = {k: v for k, v in row.items() if k != "geometry"}
        # Ensure proper string encoding
        for k, v in props.items():
            if isinstance(v, str):
                props[k] = v
        features.append({"type": "Feature", "properties": props, "geometry": geom})

    geojson = {"type": "FeatureCollection", "features": features}

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, separators=(",", ":"), ensure_ascii=False)

    file_size = output_path.stat().st_size / (1024 * 1024)
    print(f"  File size: {file_size:.1f} MB")

    if file_size > 25:
        print("  WARNING: File exceeds 25 MB. Applying moderate simplification...")
        gdf_export["geometry"] = gdf_export.geometry.simplify(tolerance=0.0005, preserve_topology=True)
        # Re-export
        features = []
        for _, row in gdf_export.iterrows():
            geom = row.geometry.__geo_interface__
            geom["coordinates"] = strip_z(geom["coordinates"])
            props = {k: v for k, v in row.items() if k != "geometry"}
            features.append({"type": "Feature", "properties": props, "geometry": geom})
        geojson = {"type": "FeatureCollection", "features": features}
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(geojson, f, separators=(",", ":"), ensure_ascii=False)
        file_size = output_path.stat().st_size / (1024 * 1024)
        print(f"  New file size: {file_size:.1f} MB")

    # Also export centroids as a lightweight separate file for the algorithm
    print("Exporting centroids file...")
    centroids_data = gdf_export[["sc", "pop", "dens", "zone", "clat", "clng"]].copy()
    centroids_data = centroids_data.rename(columns={"clat": "lat", "clng": "lng"})
    # Convert to dict for JSON export (no geometry)
    centroids_list = centroids_data.to_dict(orient="records")
    centroids_path = DATA_DIR / "centroids.json"
    with open(centroids_path, "w", encoding="utf-8") as f:
        json.dump(centroids_list, f, separators=(",", ":"), ensure_ascii=False)

    centroids_size = centroids_path.stat().st_size / (1024 * 1024)
    print(f"  Centroids file: {centroids_size:.1f} MB")

    # --- Summary ---
    elapsed = time.time() - t0
    print(f"\n=== DONE in {elapsed:.1f}s ===")
    print(f"Sectors: {len(gdf_export)}")
    print(f"Total population: {gdf_export['pop'].sum():,.0f}")
    print(f"sectors.json: {file_size:.1f} MB")
    print(f"centroids.json: {centroids_size:.1f} MB")

    # Regional breakdown
    print(f"\nRegional breakdown:")
    for rgn_code, rgn_name in [("2000", "Flanders"), ("3000", "Wallonia"), ("4000", "Brussels")]:
        mask = gdf_export["rgn"] == int(rgn_code) if gdf_export["rgn"].dtype != object else gdf_export["rgn"] == rgn_code
        pop = gdf_export.loc[mask, "pop"].sum()
        count = mask.sum()
        print(f"  {rgn_name}: {count} sectors, {pop:,.0f} population")


if __name__ == "__main__":
    main()
