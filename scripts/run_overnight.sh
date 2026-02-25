#!/bin/bash
# Overnight batch precompute for bbox Coverage Tool
#
# Current state:
#   Population:  has 3,4,5,6,9,12       — missing 1,2,7,8,10,11,13,14,15
#   Demand:      has 5,6,9,12           — missing 1,2,3,4,7,8,10,11,13,14,15
#   Supermarket: none                   — needs all 1-15 for both pop and demand
#
# Phase 1: Optimal placements (greedy MCLP) — ~3.5h at 4 parallel
# Phase 2: SM top-up (all A% levels per travel time) — ~20min at 4 parallel
#
# Each SM job computes all A% levels (5% increments) internally,
# plus backwards optimisation to find redundant optimal placements.
#
# Runs 4 jobs in parallel at a time.
# Logs go to /tmp/precompute_<time>_<mode>.txt
#
# Usage: bash scripts/run_overnight.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="python3"

echo "=== bbox Overnight Precompute ==="
echo "Started: $(date)"
echo ""

# --- Helper: run up to N jobs in parallel ---
run_batch() {
    local label="$1"
    shift
    local pids=()
    local logs=()

    echo "--- $label ---"

    for arg in "$@"; do
        local mode="${arg%%:*}"
        local t="${arg##*:}"
        local log="/tmp/precompute_${t}_${mode}.txt"

        if [ "$mode" = "pop" ]; then
            $PY "$SCRIPT_DIR/precompute_single.py" "$t" > "$log" 2>&1 &
        elif [ "$mode" = "dem" ]; then
            $PY "$SCRIPT_DIR/precompute_single_demand.py" "$t" > "$log" 2>&1 &
        elif [ "$mode" = "smpop" ]; then
            $PY "$SCRIPT_DIR/precompute_single_sm.py" "$t" "pop" > "$log" 2>&1 &
        elif [ "$mode" = "smdem" ]; then
            $PY "$SCRIPT_DIR/precompute_single_sm.py" "$t" "demand" > "$log" 2>&1 &
        fi

        echo "  Launched ${mode} ${t}min → $log (PID $!)"
        pids+=($!)
        logs+=("$log")
    done

    # Wait for all in this batch
    local i=0
    for pid in "${pids[@]}"; do
        wait "$pid"
        local exit_code=$?
        if [ $exit_code -eq 0 ]; then
            echo "  ✓ Done: ${logs[$i]}"
        else
            echo "  ✗ FAILED (exit $exit_code): ${logs[$i]}"
        fi
        i=$((i + 1))
    done
    echo ""
}

# ============================================================
# Phase 1: Optimal placements (population + demand)
# ============================================================

# Population: missing 1,2,7,8,10,11,13,14,15
run_batch "Population — batch 1 (1, 2, 7, 8)"     pop:1  pop:2  pop:7  pop:8
run_batch "Population — batch 2 (10, 11, 13, 14)"  pop:10 pop:11 pop:13 pop:14
run_batch "Population — batch 3 (15)"               pop:15

# Demand: missing 1,2,3,4,7,8,10,11,13,14,15
run_batch "Demand — batch 1 (1, 2, 3, 4)"          dem:1  dem:2  dem:3  dem:4
run_batch "Demand — batch 2 (7, 8, 10, 11)"        dem:7  dem:8  dem:10 dem:11
run_batch "Demand — batch 3 (13, 14, 15)"          dem:13 dem:14 dem:15

echo "=== Phase 1 complete (optimal placements): $(date) ==="
echo ""

# ============================================================
# Phase 2: Supermarket top-up + backwards optimisation
#           (depends on Phase 1 being done)
#           Each job computes all A% levels (5% increments)
# ============================================================

run_batch "SM Pop — batch 1 (1, 2, 3, 4)"      smpop:1  smpop:2  smpop:3  smpop:4
run_batch "SM Pop — batch 2 (5, 6, 7, 8)"      smpop:5  smpop:6  smpop:7  smpop:8
run_batch "SM Pop — batch 3 (9, 10, 11, 12)"   smpop:9  smpop:10 smpop:11 smpop:12
run_batch "SM Pop — batch 4 (13, 14, 15)"      smpop:13 smpop:14 smpop:15

run_batch "SM Demand — batch 1 (1, 2, 3, 4)"   smdem:1  smdem:2  smdem:3  smdem:4
run_batch "SM Demand — batch 2 (5, 6, 7, 8)"   smdem:5  smdem:6  smdem:7  smdem:8
run_batch "SM Demand — batch 3 (9, 10, 11, 12)" smdem:9  smdem:10 smdem:11 smdem:12
run_batch "SM Demand — batch 4 (13, 14, 15)"   smdem:13 smdem:14 smdem:15

echo "=== All done: $(date) ==="

# ============================================================
# Summary
# ============================================================
echo ""
echo "--- placements.json travel times ---"
python3 -c "
import json
with open('$(dirname "$SCRIPT_DIR")/data/placements.json') as f:
    d = json.load(f)
for k in sorted(d.keys(), key=int):
    p = d[k]
    n = len(p['placements'])
    sc = p['startCoverage']
    fc = p['placements'][-1]['cum'] if p['placements'] else sc
    print(f'  {k}min: {sc:.1f}% -> {fc:.1f}%  ({n} placements)')
"

echo ""
echo "--- placements_demand.json travel times ---"
python3 -c "
import json, os
path = '$(dirname "$SCRIPT_DIR")/data/placements_demand.json'
if not os.path.exists(path):
    print('  (file not found)')
else:
    with open(path) as f:
        d = json.load(f)
    for k in sorted(d.keys(), key=int):
        p = d[k]
        n = len(p['placements'])
        sc = p['startCoverage']
        fc = p['placements'][-1]['cum'] if p['placements'] else sc
        print(f'  {k}min: {sc:.1f}% -> {fc:.1f}%  ({n} placements)')
"

echo ""
echo "--- placements_sm.json entries ---"
python3 -c "
import json, os
path = '$(dirname "$SCRIPT_DIR")/data/placements_sm.json'
if not os.path.exists(path):
    print('  (file not found)')
else:
    with open(path) as f:
        d = json.load(f)
    for k in sorted(d.keys()):
        p = d[k]
        n = len(p['placements'])
        r = len(p.get('redundant', []))
        sc = p['startCoverage']
        fc = p['placements'][-1]['cum'] if p['placements'] else sc
        print(f'  {k}: {sc:.1f}% -> {fc:.1f}%  ({n} SM placements, {r} redundant optimal)')
"
