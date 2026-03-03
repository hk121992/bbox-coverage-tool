# Decision Log: bbox-coverage-tool

Decisions made during development. Format: DEC-NNN.

See `docs/plan.md` Architecture Decisions for a summary table.

---

## DEC-001: Area-type adjusted coverage thresholds

**Date**: 2026-02
**Status**: Resolved
**Decision**: Use different travel time thresholds by area type (urban walking 400m/5min, suburban walking 600m/5min, rural driving 4km/5min) rather than a single flat threshold.
**Rationale**: Urban residents walk to lockers; rural residents drive. A flat threshold overestimates rural coverage and underestimates urban coverage.

---

## DEC-002: Greedy MCLP algorithm

**Date**: 2026-02
**Status**: Resolved
**Decision**: Use greedy approximation for MCLP rather than exact ILP solver.
**Rationale**: Exact ILP does not scale to 19,795 sectors. Greedy provides (1 - 1/e) ≈ 63% of optimal in polynomial time, which is sufficient for planning purposes.

---

## DEC-003: Demand weighting formula

**Date**: 2026-02
**Status**: Resolved
**Decision**: Demand score = population × e-commerce age ratio (25–54) × income index.
**Rationale**: Parcel locker usage correlates with online shopping behaviour. Age 25–54 is the primary e-commerce demographic. Income is a proxy for purchase volume.

---
