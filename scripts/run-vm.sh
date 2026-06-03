#!/bin/bash
# Check if at least one argument is provided
if [ "$#" -lt 1 ]; then
    echo "Usage: ./run-vm.sh [workload_name|all|interactive]"
    echo "Available workloads: matmul, pagerank, ycsbc, filewrite, rwstress, all, interactive"
    echo "  e.g. ./run-vm.sh all   (profiles every workload, prints bottom lines, powers off)"
    exit 1
fi

MODE=$1

if [ "$MODE" = "interactive" ]; then
    INTERACTIVE=1
    WORKLOAD=""
else
    INTERACTIVE=0
    WORKLOAD=$MODE
fi

# Optional per-boot LtRAM knobs passed through to the kernel cmdline as module
# params (ltram.scan_batch / ltram.token_rate). Used by the per-boot sweep so
# each N is an independent VM (no cross-run carryover).
EXTRA_APPEND=""
[ -n "${SCAN_BATCH:-}" ] && EXTRA_APPEND="$EXTRA_APPEND ltram.scan_batch=$SCAN_BATCH"
[ -n "${TOKEN_RATE:-}" ] && EXTRA_APPEND="$EXTRA_APPEND ltram.token_rate=$TOKEN_RATE"
[ -n "${WR:-}" ]         && EXTRA_APPEND="$EXTRA_APPEND ltram_wr=$WR"   # write ratio for the per-boot write-ratio sweep
[ -n "${DUR:-}" ]        && EXTRA_APPEND="$EXTRA_APPEND ltram_dur=$DUR" # scan/sample window seconds (redis timeseries)
[ -n "${RATE:-}" ]       && EXTRA_APPEND="$EXTRA_APPEND ltram_rate=$RATE"  # synth op-rate throttle
[ -n "${G:-}" ]          && EXTRA_APPEND="$EXTRA_APPEND ltram_g=$G"        # pagerank graph scale (2^G vertices)
[ -n "${TRIALS:-}" ]     && EXTRA_APPEND="$EXTRA_APPEND ltram_trials=$TRIALS"

set -e

# Dynamically find the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Go up one level to find the main ltram-policy-bench root folder
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# Set all paths relative to that root folder
KERNEL="${BASE_DIR}/linux/arch/x86/boot/bzImage"
# Optional: boot a different kernel (e.g. the vanilla 6.8 baseline worktree) for
# overhead comparisons, without rebuilding. e.g.
#   KERNEL_OVERRIDE=/scratch/hsshim/ltram-policy-bench-linux-base/arch/x86/boot/bzImage
KERNEL="${KERNEL_OVERRIDE:-$KERNEL}"
ROOTFS="${BASE_DIR}/buildroot/output/images/rootfs.ext2"
RESULTS="${BASE_DIR}/results"
WORKLOADS="${BASE_DIR}/workloads"
INPUTS="${BASE_DIR}/inputs"

mkdir -p $RESULTS

# === Host NUMA pinning ===
# Active (Configuration A): pin everything to host node 0.
# Both guest tiers (Node 0 = DRAM, Node 1 = LtRAM) are backed by the same
# host socket, so there is no real latency asymmetry between them — only
# the SLIT distance=20 hint exposed to the guest kernel. Clean baseline
# for policy/correctness work.
# Set CONFIG_B=1 to put the guest LtRAM tier (node 1) on host node 1 = REAL
# cross-socket latency (memory-sensitivity / latency-overhead runs). Default
# (Configuration A) backs both guest tiers on host node 0 = no asymmetry.
if [ "${CONFIG_B:-0}" = "1" ]; then
    # vCPUs + DRAM (m0) on host node 0 (local); LtRAM (m1) on host node 1 (remote).
    # Do NOT --membind=0 — that would fight the per-backend host-nodes pinning.
    NUMACTL="numactl --cpunodebind=0"
    M0_HOST_OPTS=",host-nodes=0,policy=bind"
    M1_HOST_OPTS=",host-nodes=1,policy=bind"
    echo "[run-vm] Configuration B: LtRAM(node1) -> host node 1 (remote, real latency)"
else
    NUMACTL="numactl --cpunodebind=0 --membind=0"
    M0_HOST_OPTS=""
    M1_HOST_OPTS=""
fi

$NUMACTL qemu-system-x86_64 \
  -enable-kvm \
  -cpu host \
  -smp 4 \
  -m 8G \
  -rtc base=localtime \
  \
  -object memory-backend-ram,id=m0,size=7936M${M0_HOST_OPTS} \
  -object memory-backend-ram,id=m1,size=256M${M1_HOST_OPTS} \
  -numa node,nodeid=0,memdev=m0,cpus=0-3 \
  -numa node,nodeid=1,memdev=m1 \
  -numa dist,src=0,dst=1,val=20 \
  -numa dist,src=1,dst=0,val=20 \
  -kernel "$KERNEL" \
  -drive file="$ROOTFS",format=raw,if=virtio \
  -append "root=/dev/vda rw console=ttyS0 nokaslr numa=on ltram_workload=$WORKLOAD interactive=$INTERACTIVE$EXTRA_APPEND" \
  \
  -virtfs local,path="$WORKLOADS",mount_tag=workloads,security_model=passthrough \
  -virtfs local,path="$RESULTS",mount_tag=results,security_model=none \
  -virtfs local,path="$INPUTS",mount_tag=inputs,security_model=passthrough \
  \
  -nographic \
  -serial mon:stdio \
  -no-reboot # -s -S

if [[ $INTERACTIVE -eq 0 ]]; then
  # also try to plot the monitoring graphs
  python3 $WORKLOADS/monitoring/meminfo_plot.py $WORKLOAD
fi
