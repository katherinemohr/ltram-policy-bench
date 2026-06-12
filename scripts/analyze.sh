#!/bin/bash
# Run the post-run analysis and plotting scripts against an existing run directory.
#
# Usage:
#   scripts/analyze.sh <workload> [<run_name>]
#
# Arguments:
#   workload  — matmul, gapbs, redis, or duckdb
#   run_name  — path under results/runs/ (default: <today>/<workload>_<LTRAM_CONFIG>)
#
# Respects the same env vars as run-vm.sh:
#   LTRAM_CONFIG — config tag (default: configA)
#   LTRAM_RUN    — override the full run_name
#
# All dirty_*_plot.py scripts are skipped automatically when the corresponding
# CSV files are absent (i.e. the run was not captured with LTRAM_HIST=1).
#
# Examples:
#   scripts/analyze.sh redis
#   scripts/analyze.sh redis 2026-05-07/redis_configA
#   LTRAM_CONFIG=configB scripts/analyze.sh gapbs

if [[ "$#" -lt 1 ]]; then
    echo "Usage: $0 <workload> [<run_name>]"
    echo "  workload — matmul, gapbs, redis, or duckdb"
    echo "  run_name — path under results/runs/ (default: <today>/<workload>_<LTRAM_CONFIG>)"
    exit 1
fi

WORKLOAD=$1
RUN_NAME=${2:-${LTRAM_RUN:-}}
if [[ -z "$RUN_NAME" ]]; then
    echo "Usage: $0 <workload> <run_name>  (or set LTRAM_RUN)"
    echo "Error: run_name not provided and cannot be derived reliably because run directories may use auto-generated config tags."
    exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
WORKLOADS="${BASE_DIR}/workloads"
RESULTS="${BASE_DIR}/results"
RUN_DIR="$RESULTS/runs/$RUN_NAME"

if [[ ! -d "$RUN_DIR" ]]; then
    echo "Error: run directory not found: $RUN_DIR"
    exit 1
fi

echo "Analyzing run: $RUN_NAME"
echo "Run dir: $RUN_DIR"

python3 "$WORKLOADS/monitoring/meminfo_plot.py" "$WORKLOAD" "$RUN_NAME"

PHASES="run"
if [[ -f "$RUN_DIR/dirty_sweep_load.csv" ]]; then
    PHASES="run full"
fi

for PHASE in $PHASES; do
    if [[ -f "$RUN_DIR/dirty_sweep.csv" ]]; then
        python3 "$WORKLOADS/monitoring/dirty_plot.py" "$WORKLOAD" "$RUN_NAME" "$PHASE"
    fi
    if [[ -f "$RUN_DIR/dirty_sweep_stability.csv" ]]; then
        python3 "$WORKLOADS/monitoring/dirty_stability_plot.py"         "$WORKLOAD" "$RUN_NAME" "$PHASE"
        python3 "$WORKLOADS/monitoring/dirty_stability_cluster_plot.py" "$WORKLOAD" "$RUN_NAME" "$PHASE"
        python3 "$WORKLOADS/monitoring/dirty_stability_kelbow_plot.py"  "$WORKLOAD" "$RUN_NAME" "$PHASE"
        python3 "$WORKLOADS/monitoring/dirty_stability_endurance_plot.py" "$WORKLOAD" "$RUN_NAME" "$PHASE" || \
            echo "(WARNING: dirty_stability_endurance_plot.py failed for phase=$PHASE)"
    fi
    if [[ -f "$RUN_DIR/dirty_sweep.csv" ]]; then
        python3 "$WORKLOADS/monitoring/dirty_timeline_plot.py"          "$WORKLOAD" "$RUN_NAME" "$PHASE" || \
            echo "(WARNING: dirty_timeline_plot.py failed for phase=$PHASE)"
        python3 "$WORKLOADS/monitoring/dirty_ltram_utilization_plot.py" "$WORKLOAD" "$RUN_NAME" "$PHASE" || \
            echo "(WARNING: dirty_ltram_utilization_plot.py failed for phase=$PHASE)"
        python3 "$WORKLOADS/monitoring/dirty_cold_threshold_plot.py"    "$WORKLOAD" "$RUN_NAME" "$PHASE" || \
            echo "(WARNING: dirty_cold_threshold_plot.py failed for phase=$PHASE)"
        python3 "$WORKLOADS/monitoring/dirty_vma_breakdown_plot.py"    "$WORKLOAD" "$RUN_NAME" "$PHASE" || \
            echo "(WARNING: dirty_vma_breakdown_plot.py failed for phase=$PHASE)"
    fi
done

if [[ -f "$RUN_DIR/dirty_sweep_stability.csv" ]] \
   && [[ -f "$RUN_DIR/dirty_sweep_load_stability.csv" ]]; then
    python3 "$WORKLOADS/monitoring/dirty_stability_phase_compare_plot.py" "$WORKLOAD" "$RUN_NAME" || \
        echo "(WARNING: dirty_stability_phase_compare_plot.py failed)"
    python3 "$WORKLOADS/monitoring/dirty_phase_categories_plot.py" "$WORKLOAD" "$RUN_NAME" full || \
        echo "(WARNING: dirty_phase_categories_plot.py failed)"
fi

if [[ -f "$RUN_DIR/dirty_sweep.csv" ]] \
   && [[ ! -f "$RUN_DIR/dirty_sweep_load_stability.csv" ]]; then
    python3 "$WORKLOADS/monitoring/dirty_phase_categories_plot.py" "$WORKLOAD" "$RUN_NAME" run || \
        echo "(WARNING: dirty_phase_categories_plot.py failed)"
fi
