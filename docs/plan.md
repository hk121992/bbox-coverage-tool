# Project Plan: bbox-coverage-tool

## Vision

A publicly accessible web tool that lets bpost analysts and stakeholders explore the current bbox parcel locker network coverage across Belgium, simulate optimal placement of new lockers, and understand coverage gaps — all through an interactive map interface with no installation required.

## Milestones

### Milestone 1: Core Coverage Tool

- **Goal**: Functional interactive map with population and demand-weighted coverage analysis
- **Status**: complete
- **Estimated effort**: L
- **Tasks**:
  - [x] Build Leaflet map with bbox locker locations
  - [x] Implement population-weighted coverage model (StatBel sectors)
  - [x] Implement demand-weighted coverage (population × e-commerce age ratio × income index)
  - [x] MCLP greedy algorithm for optimal locker placement
  - [x] Coverage charts (Chart.js)
  - [x] Guided walkthrough/tutorial (walkthrough.js)
  - [x] Precomputed placements for travel times 1–15 min (population + demand modes)
  - [x] Supermarket top-up placement phase
  - [x] Deploy to GitHub Pages
- **Decisions Needed**: None
- **Verification**: Live at https://hk121992.github.io/bbox-coverage-tool/

### Milestone 2: Refinements & Improvements

- **Goal**: Polish UX, address any gaps found after initial deployment
- **Status**: not-started
- **Estimated effort**: M
- **Tasks**:
  - [ ] TBD — awaiting PM direction
- **Decisions Needed**: What improvements are highest priority?
- **Verification**: TBD

## Architecture Decisions

| # | Decision | Options Considered | Chosen | Rationale | Date |
|---|----------|--------------------|--------|-----------|------|
| 1 | Coverage model | Single threshold, area-type adjusted | Area-type adjusted (urban/suburban/rural) | More realistic travel behaviour | 2026-02 |
| 2 | Optimisation algorithm | Exact MCLP (ILP), greedy MCLP | Greedy MCLP | (1-1/e) approximation, scales to 20k sectors | 2026-02 |
| 3 | Demand weighting | Population only, population+age+income | Combined demand score | Better proxy for actual parcel locker usage | 2026-02 |
| 4 | Frontend framework | React, Vue, vanilla JS | Vanilla JS | No build step, simple deployment, fast load | 2026-02 |
| 5 | Data storage | Database, flat files, precomputed JSON | Precomputed JSON | Static hosting, instant load, no backend needed | 2026-02 |

## Risk Register

| Risk | Impact (H/M/L) | Likelihood (H/M/L) | Mitigation | Owner |
|------|-----------------|---------------------|------------|-------|
| StatBel data becomes stale | M | M | Re-run pipeline when new StatBel data released | PM |
| Large JSON files slow initial load | M | L | Files already served via GitHub Pages CDN; monitor if issues arise | Dev |
| OSM supermarket data drift | L | M | Re-run parse_supermarkets.py + precompute_single_sm.py | Dev |

## Dependencies

| Dependency | Type | Status | Impact if Missing |
|------------|------|--------|-------------------|
| Leaflet 1.9.4 | External CDN | Active | Map non-functional |
| Chart.js 4.4.7 | External CDN | Active | Charts non-functional |
| StatBel statistical sectors | External data | 2024 vintage | Coverage model stale |
| bpost bbox locations | External data | Feb 2026 vintage | Locations stale |
| OSM supermarkets | External data | Feb 2026 vintage | SM top-up stale |

## Change Log

| Date | Change | Reason | Approved By |
|------|--------|--------|-------------|
| 2026-02-25 | Refactored ui.js + walkthrough.js, removed ~170 lines | Code quality | PM |
| 2026-03-03 | Added CLAUDE.md, claude-progress.txt, docs/ | Middle Management onboarding | PM |
