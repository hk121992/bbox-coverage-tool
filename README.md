# bbox Coverage Modelling Tool

Interactive tool for analysing and optimising bpost bbox (parcel locker) network coverage across Belgium.

## Data Sources
- **bbox locations:** bpost public locker finder (Feb 2026) — 2,379 locations
- **Population:** StatBel statistical sector data (2024) — 19,795 sectors, 11.76M population
- **Supermarkets:** OpenStreetMap (Feb 2026) — 3,372 locations
- **Age demographics:** StatBel population structure (2025) — municipality-level age distribution
- **Fiscal income:** StatBel municipal income data — average net taxable income per capita

## Methodology

### Coverage Modes
The tool supports two coverage weighting modes:

**Population coverage** — each statistical sector is weighted by its resident population. A sector is "covered" if a bbox locker exists within the travel time threshold. Coverage % = covered population / total population.

**Demand-weighted coverage** — each sector is weighted by a demand score combining population, e-commerce age demographics (25–54 age bracket), and relative income level:

```
demand_score = population × ecommerce_age_ratio × income_index
```

where `income_index` = municipality income per capita / national average. This surfaces areas with higher parcel delivery demand, not just raw population.

### Spatial Model
Travel time thresholds are adjusted by area type: walking in urban zones (400m/5min), mixed in suburban zones (600m/5min), and driving in rural zones (4km/5min), based on population density classification.

### Optimisation
The Network Planner uses the Maximal Covering Location Problem (MCLP) framework. The greedy algorithm provides a (1 - 1/e) approximation guarantee for the coverage objective. Placements are precomputed for travel times 1–15 minutes under both population and demand-weighted modes.

Optionally, a supermarket top-up phase uses precomputed results to identify existing supermarket locations that would cover remaining demand after optimal placements.

## Usage

**Live:** https://hk121992.github.io/bbox-coverage-tool/

**Local:** Must be served via HTTP due to `fetch` requirements — opening `file://` directly will not work.
```bash
cd bbox-coverage-tool
python3 -m http.server 8000
# Open http://localhost:8000 in Chrome
```

## Data Pipeline

Run in order when refreshing underlying data:

```bash
# 1. Precompute population-weighted placements (parallelisable by travel time)
python3 scripts/precompute_single.py <travel_minutes>

# 2. Build demand scores (enriches centroids.json with demand, ageRatio, incomeIdx fields)
python3 scripts/build_demand_scores.py

# 3. Precompute demand-weighted placements (parallelisable by travel time)
python3 scripts/precompute_single_demand.py <travel_minutes>
```

Steps 1 and 3 can be parallelised across travel times by running multiple instances simultaneously. Each instance merges its result into the output JSON atomically.

Output files:
- `data/placements.json` — population-weighted precomputed placements
- `data/placements_demand.json` — demand-weighted precomputed placements
- `data/placements_sm.json` — precomputed supermarket top-up placements
- `data/centroids.json` — enriched with demand scores after step 2
