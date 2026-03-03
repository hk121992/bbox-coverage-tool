# Project: bbox-coverage-tool

## Purpose

Interactive tool for analysing and optimising bpost bbox (parcel locker) network coverage across Belgium. It models population and demand-weighted coverage using geospatial data, and runs a Maximal Covering Location Problem (MCLP) algorithm to suggest optimal placement of new lockers. Deployed as a static site on GitHub Pages.

## Tech Stack

- Language: JavaScript (frontend), Python (data pipeline)
- Framework: None — vanilla JS + Leaflet 1.9.4 + Chart.js 4.4.7
- Database: None — precomputed JSON data files (~80MB in `data/`)
- Key dependencies: Leaflet (maps), Chart.js (charts), NumPy (Python pipeline)

## Project Structure

```
bbox-coverage-tool/
├── index.html          — main web interface
├── js/
│   ├── app.js          — core application logic
│   ├── ui.js           — UI controls and interactions
│   ├── map.js          — Leaflet map integration
│   ├── walkthrough.js  — guided walkthrough/tutorial
│   └── chart.js        — Chart.js visualizations
├── css/
│   └── style.css
├── scripts/            — Python data pipeline
│   ├── precompute_single.py        — population-weighted placements
│   ├── precompute_single_demand.py — demand-weighted placements
│   ├── precompute_single_sm.py     — supermarket top-up placements
│   ├── precompute_parallel.py      — parallelization orchestration
│   ├── precompute_placements.py    — shared placement logic
│   ├── build_demand_scores.py      — demand weighting calculations
│   ├── preprocess_sectors.py       — statistical sector preprocessing
│   ├── parse_supermarkets.py       — OSM supermarket parsing
│   └── run_overnight.sh            — data refresh automation
└── data/               — precomputed geospatial data (~80MB)
    ├── bbox.json           — 2,379 bbox locker locations
    ├── centroids.json      — statistical sector centroids with demand scores
    ├── sectors.json        — StatBel sector boundaries (GeoJSON, 18MB)
    ├── supermarkets.json   — 3,372 supermarket locations (OSM)
    ├── placements.json     — population-weighted placements
    ├── placements_demand.json — demand-weighted placements
    └── placements_sm.json  — supermarket top-up placements (45MB)
```

## Workflows

- **Dev server**: `python3 -m http.server 8000` (open http://localhost:8000 in Chrome)
- **Data pipeline**:
  ```bash
  python3 scripts/precompute_single.py <travel_minutes>
  python3 scripts/build_demand_scores.py
  python3 scripts/precompute_single_demand.py <travel_minutes>
  ```
- **Build**: None — no build step, static files served directly
- **Test**: None — manual browser testing
- **Deploy**: Automatic via GitHub Pages on push to main

## Rules

- Always read `claude-progress.txt` first to understand current state
- Always update `claude-progress.txt` after completing a task
- Always update `docs/progress.md` with a session entry when done
- Never modify files outside this project's directory
- Ask before deleting files or making breaking changes
- Ask before adding new dependencies
- Commit work with clear messages referencing the task from the session brief
- Raw data sources (StatBel, OSM) are excluded from git — do not add them
- Large data files in `data/` are in git — be careful modifying them

## Context Files

- `claude-progress.txt` — Quick-resume: current state, last session, next steps
- `docs/plan.md` — Living project plan with milestones and tasks
- `docs/decisions.md` — All decisions made for this project
- `docs/progress.md` — Session-by-session progress log

## Managed By

This project is managed by the Middle Management system at `~/projects/Middle-management/`.
Session briefs and coordination come from there. If you need a decision that isn't covered
by existing decisions, create a note in the progress update — the management agent will
route it to the product manager.
