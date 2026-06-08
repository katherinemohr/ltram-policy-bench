#!/bin/bash
# Check if at least one argument is provided
if [ "$#" -lt 1 ]; then
    echo "Usage: ./run-vm.sh [workload_name|interactive|all]"
    echo "Available workloads: matmul, gapbs, redis, duckdb, interactive, all"
    exit 1
fi

MODE=$1

# 'all' = run matmul, gapbs, redis sequentially as three separate VM
# invocations. Env vars (LTRAM_CONFIG, LTRAM_RUN) propagate to each child run.
if [ "$MODE" = "all" ]; then
    SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
    for w in matmul gapbs redis duckdb; do
        echo
        echo "================================================================"
        echo "  Running workload: $w"
        echo "================================================================"
        bash "$SCRIPT_PATH" "$w" || echo "WARNING: workload $w failed (continuing)"
    done
    exit 0
fi

if [ "$MODE" = "interactive" ]; then
    INTERACTIVE=1
    WORKLOAD=""
else
    INTERACTIVE=0
    WORKLOAD=$MODE
fi

set -e

# Dynamically find the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Go up one level to find the main ltram-policy-bench root folder
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# Set all paths relative to that root folder
KERNEL="${BASE_DIR}/linux/arch/x86/boot/bzImage"
ROOTFS="${BASE_DIR}/buildroot/output/images/rootfs.ext2"
RESULTS="${BASE_DIR}/results"
WORKLOADS="${BASE_DIR}/workloads"
INPUTS="${BASE_DIR}/inputs"

mkdir -p $RESULTS

# === Run identification ===
# Each run gets its own directory under results/runs/, containing:
#   meminfo.csv           — the time-series from meminfo_log.sh
#   meminfo_summary.txt   — min/avg/max summary from --end
#   numa-topology.txt     — guest's numactl --hardware
#   cmdline.txt           — kernel command line
#   vmstat-{before,after}.txt
#   node{0,1}-meminfo.txt — per-node final state
#   workload-stdout.txt   — stdout/stderr from the workload binary
#   ycsb-{load,run}.txt   — redis only
#   *.png                 — plots produced after the run completes
#
# Override with env vars: LTRAM_CONFIG (default "configA"),
# LTRAM_RUN (default "<timestamp>_<workload>_<config>").
CONFIG_TAG=${LTRAM_CONFIG:-configA}
# Histogram mode (LTRAM_HIST=1): also runs dirty_sweep alongside the workload
# to capture per-page write counts. Use scripts/run-vm-hist.sh as a shorthand.
LTRAM_HIST=${LTRAM_HIST:-0}
LTRAM_NO_ANALYZE=${LTRAM_NO_ANALYZE:-0}
LTRAM_SWEEP_INTERVAL_MS=${LTRAM_SWEEP_INTERVAL_MS:-100}
# Workload duration overrides for longer / steady-state benchmarks. All have
# safe defaults that match the original short-run behavior, so existing
# scripts continue to work unchanged.
LTRAM_REDIS_SPEC=${LTRAM_REDIS_SPEC:-workloadmini.spec}    # alt: workloadlong.spec
LTRAM_MATMUL_ITERS=${LTRAM_MATMUL_ITERS:-}                  # blank = compiled default (10)
LTRAM_GAPBS_GRAPH=${LTRAM_GAPBS_GRAPH:-20}                  # 2^N Kronecker graph vertices
LTRAM_GAPBS_TRIALS=${LTRAM_GAPBS_TRIALS:-}                  # blank = pr binary default
LTRAM_GAPBS_ITERS=${LTRAM_GAPBS_ITERS:-}                    # blank = pr binary default
LTRAM_GAPBS_LOAD_SECS=${LTRAM_GAPBS_LOAD_SECS:-}            # blank = guess from graph size
LTRAM_DUCKDB_RECORDS=${LTRAM_DUCKDB_RECORDS:-}              # blank = bench default (100000)
LTRAM_DUCKDB_OPS=${LTRAM_DUCKDB_OPS:-}                      # blank = bench default (100000)
LTRAM_DUCKDB_READ_RATIO=${LTRAM_DUCKDB_READ_RATIO:-}        # blank = bench default (0.5)
LTRAM_DUCKDB_DIST=${LTRAM_DUCKDB_DIST:-}
# One run dir per day per (workload, config), nested under the date so each
# day groups its workloads together:
#   results/runs/2026-05-07/redis_short_hist/
#   results/runs/2026-05-07/gapbs_short_hist/
# Same day + same workload + same config = overwrite previous run's contents.
# To force a separate dir, set LTRAM_RUN explicitly (e.g. LTRAM_RUN=foo/bar).
RUN_NAME=${LTRAM_RUN:-$(date +%Y-%m-%d)/${MODE}_${CONFIG_TAG}}
RUN_DIR="$RESULTS/runs/$RUN_NAME"
mkdir -p "$RUN_DIR"
echo "Run name: $RUN_NAME"
echo "Run dir : $RUN_DIR"

# === Host NUMA pinning ===
# Active (Configuration A): pin everything to host node 0.
# Both guest tiers (Node 0 = DRAM, Node 1 = LtRAM) are backed by the same
# host socket, so there is no real latency asymmetry between them — only
# the SLIT distance=20 hint exposed to the guest kernel. Clean baseline
# for policy/correctness work.
NUMACTL="numactl --cpunodebind=0 --membind=0"
M0_HOST_OPTS=""
M1_HOST_OPTS=""

# Configuration B (uncomment when policy validation is done, and comment
# out Configuration A above):
# vCPUs stay on host node 0, m0 is bound to host node 0 (local), m1 is
# bound to host node 1 (remote). Accesses to the guest's LtRAM tier now
# incur real cross-socket DRAM latency. Useful for memory-sensitivity
# sweeps. Do NOT combine with --membind=0 — that would fight the
# per-backend host-nodes pinning.
# NUMACTL="numactl --cpunodebind=0"
# M0_HOST_OPTS=",host-nodes=0,policy=bind"
# M1_HOST_OPTS=",host-nodes=1,policy=bind"

$NUMACTL qemu-system-x86_64 \
  -enable-kvm \
  -cpu host \
  -smp 4 \
  -m 8G \
  \
  -object memory-backend-ram,id=m0,size=7936M${M0_HOST_OPTS} \
  -object memory-backend-ram,id=m1,size=256M${M1_HOST_OPTS} \
  -numa node,nodeid=0,memdev=m0,cpus=0-3 \
  -numa node,nodeid=1,memdev=m1 \
  -numa dist,src=0,dst=1,val=20 \
  -numa dist,src=1,dst=0,val=20 \
  -kernel "$KERNEL" \
  -drive file="$ROOTFS",format=raw,if=virtio \
  -append "root=/dev/vda rw console=ttyS0 nokaslr numa=on ltram_workload=$WORKLOAD interactive=$INTERACTIVE ltram_run=$RUN_NAME ltram_hist=$LTRAM_HIST ltram_sweep_interval_ms=$LTRAM_SWEEP_INTERVAL_MS ltram_redis_spec=$LTRAM_REDIS_SPEC ltram_matmul_iters=$LTRAM_MATMUL_ITERS ltram_gapbs_graph=$LTRAM_GAPBS_GRAPH ltram_gapbs_trials=$LTRAM_GAPBS_TRIALS ltram_gapbs_iters=$LTRAM_GAPBS_ITERS ltram_gapbs_load_secs=$LTRAM_GAPBS_LOAD_SECS ltram_duckdb_records=$LTRAM_DUCKDB_RECORDS ltram_duckdb_ops=$LTRAM_DUCKDB_OPS ltram_duckdb_read_ratio=$LTRAM_DUCKDB_READ_RATIO ltram_duckdb_dist=$LTRAM_DUCKDB_DIST" \
  \
  -virtfs local,path="$WORKLOADS",mount_tag=workloads,security_model=passthrough \
  -virtfs local,path="$RESULTS",mount_tag=results,security_model=none \
  -virtfs local,path="$INPUTS",mount_tag=inputs,security_model=passthrough \
  \
  -nographic \
  -serial mon:stdio \
  -no-reboot # -s -S

if [[ $INTERACTIVE -eq 0 ]] && [[ $LTRAM_NO_ANALYZE -eq 0 ]]; then
  bash "$SCRIPT_DIR/analyze.sh" "$WORKLOAD" "$RUN_NAME"
fi
