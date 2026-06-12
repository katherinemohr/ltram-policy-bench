#!/bin/bash
# T09: verify the run-vm.sh wiring block for the new plots — correct phases
# (run-only -> {run}; phase-split -> {run, load, full}), correct guards, and
# silent skip on old runs. Drives the SAME guard logic with a stub for python3.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
fail() { echo "FAIL: $*" >&2; exit 1; }

# Logging stub mimicking `python3 <plot.py> <workload> <run> <phase>`.
LOG=""
PLOT() { LOG+="$(basename "$1"):$4
"; }

# The wiring block under test (mirrors scripts/run-vm.sh), with PLOT for python3.
run_block() {
  local RUN_NAME="$1"; LOG=""
  local RESULTS="$TMP"; local WORKLOADS="x"; local WORKLOAD="w"; local LTRAM_HIST=1
  if [[ "$LTRAM_HIST" = "1" ]]; then
    LIFE_PHASES="run"
    if [[ -f "$RESULTS/runs/$RUN_NAME/dirty_sweep_load_lifecycle.csv" ]]; then
      LIFE_PHASES="run load full"
    fi
    for PHASE in $LIFE_PHASES; do
      if [[ "$PHASE" = "run" || "$PHASE" = "full" ]]; then
        life_csv="$RESULTS/runs/$RUN_NAME/dirty_sweep_lifecycle.csv"
        stab_csv="$RESULTS/runs/$RUN_NAME/dirty_sweep_stability.csv"
      else
        life_csv="$RESULTS/runs/$RUN_NAME/dirty_sweep_${PHASE}_lifecycle.csv"
        stab_csv="$RESULTS/runs/$RUN_NAME/dirty_sweep_${PHASE}_stability.csv"
      fi
      if [[ -f "$life_csv" ]]; then
        PLOT "$WORKLOADS/monitoring/dirty_lifecycle_plot.py" "$WORKLOAD" "$RUN_NAME" "$PHASE"
        if [[ -f "$stab_csv" ]]; then
          PLOT "$WORKLOADS/monitoring/dirty_eligibility_plot.py" "$WORKLOAD" "$RUN_NAME" "$PHASE"
        fi
      fi
    done
  fi
}

# 0) syntax check the real script
bash -n "$ROOT/scripts/run-vm.sh" || fail "run-vm.sh syntax error"
echo "PASS: run-vm.sh parses"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# 1) run-only run dir (lifecycle + stability, no load) -> lifecycle+elig for run only
mkdir -p "$TMP/runs/A"
: > "$TMP/runs/A/dirty_sweep_lifecycle.csv"
: > "$TMP/runs/A/dirty_sweep_stability.csv"
run_block A
exp=$'dirty_lifecycle_plot.py:run\ndirty_eligibility_plot.py:run\n'
[[ "$LOG" == "$exp" ]] || fail "run-only: got [$LOG]"
[[ "$LOG" != *":full"* ]] || fail "must never pass 'full'"
echo "PASS: run-only invokes lifecycle+eligibility for run, never full"

# 2) phase-split run dir (also has load lifecycle+stability) -> run, load, full
mkdir -p "$TMP/runs/B"
: > "$TMP/runs/B/dirty_sweep_lifecycle.csv"
: > "$TMP/runs/B/dirty_sweep_stability.csv"
: > "$TMP/runs/B/dirty_sweep_load_lifecycle.csv"
: > "$TMP/runs/B/dirty_sweep_load_stability.csv"
run_block B
exp=$'dirty_lifecycle_plot.py:run\ndirty_eligibility_plot.py:run\ndirty_lifecycle_plot.py:load\ndirty_eligibility_plot.py:load\ndirty_lifecycle_plot.py:full\ndirty_eligibility_plot.py:full\n'
[[ "$LOG" == "$exp" ]] || fail "phase-split: got [$LOG]"
echo "PASS: phase-split invokes both plots for run, load, AND full"

# 3) eligibility skipped when stability CSV missing (lifecycle still runs)
mkdir -p "$TMP/runs/C"
: > "$TMP/runs/C/dirty_sweep_lifecycle.csv"
run_block C
exp=$'dirty_lifecycle_plot.py:run\n'
[[ "$LOG" == "$exp" ]] || fail "no-stability: got [$LOG]"
echo "PASS: eligibility skipped without stability CSV; lifecycle still runs"

# 4) old run dir (no lifecycle CSV) -> nothing, silently
mkdir -p "$TMP/runs/D"
: > "$TMP/runs/D/dirty_sweep.csv"
run_block D
[[ -z "$LOG" ]] || fail "old run should skip silently, got [$LOG]"
echo "PASS: old run (no lifecycle CSV) skips both plots silently"

echo "ALL WIRING TESTS PASSED"
