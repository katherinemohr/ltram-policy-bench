#!/bin/sh
# Run profile_workload.sh across one, several, or all workloads.
#
# Workloads are defined by profiles/<name>.work files (sourced shell snippets
# that set LABEL, optional PREP, and RUN). Adding a workload = drop a new
# .work file in profiles/. Nothing here or in the rootfs needs editing.
#
# Usage (inside the guest):
#   sh /mnt/workloads/run_profile.sh                # all workloads
#   sh /mnt/workloads/run_profile.sh all            # all workloads
#   sh /mnt/workloads/run_profile.sh matmul ycsbc   # just these
#
# Output: one CSV per workload at /mnt/results/profile_<LABEL>.csv (shared back
# to the host's results/ dir), plus profile_<LABEL>.ltram if the kernel exposes
# /sys/kernel/debug/ltram/stats.

set -u

HERE=/mnt/workloads
PROFILES="$HERE/profiles"
PROFILER="$HERE/profile_workload.sh"
RESULTS=/mnt/results

# Ensure debugfs is mounted so we can read/capture /sys/kernel/debug/ltram/*
# (minimal rootfs does not auto-mount it; harmless if already mounted).
[ -e /sys/kernel/debug/ltram/stats ] || mount -t debugfs none /sys/kernel/debug 2>/dev/null || true

list_all() {
    for f in "$PROFILES"/*.work; do
        [ -e "$f" ] || continue
        b=${f##*/}
        echo "${b%.work}"
    done
}

# Default to every workload when no args are given.
if [ "$#" -eq 0 ] || [ "$1" = all ]; then
    set -- $(list_all)
fi

if [ "$#" -eq 0 ]; then
    echo "No workloads found in $PROFILES (expected <name>.work files)." >&2
    exit 1
fi

mkdir -p "$RESULTS"
ran=""

for name in "$@"; do
    f="$PROFILES/$name.work"
    if [ ! -f "$f" ]; then
        echo "!! unknown workload '$name'. Available: $(list_all | tr '\n' ' ')" >&2
        continue
    fi

    # Reset then load this workload's definition.
    LABEL="$name"; PREP=""; RUN=""
    . "$f"

    # Per-run output dir: results/<test>/<YYMMDD__HHMMSS>/ -- nothing is
    # overwritten, every run is kept. profile_workload.sh honors $LTRAM_RUN_DIR
    # so its csv/node/summary land in the same folder as the run.log + .ltram.
    TS=$(date +%y%m%d__%H%M%S 2>/dev/null || echo unknown)
    RUN_DIR="$RESULTS/$LABEL/$TS"
    mkdir -p "$RUN_DIR"
    export LTRAM_RUN_DIR="$RUN_DIR"

    # Record which kernel this run used (build number from uname + optional
    # feature notes from workloads/KERNEL_INFO.txt) so a saved folder is
    # self-explanatory without re-running.
    {
        echo "run:      $TS"
        echo "workload: $LABEL"
        echo "command:  $RUN"
        echo "build:    $(uname -v 2>/dev/null | grep -o '#[0-9]*' | head -1)"
        echo "kernel:   $(uname -srm 2>/dev/null) $(uname -v 2>/dev/null)"
        if [ -r "$HERE/KERNEL_INFO.txt" ]; then
            echo "notes:"
            sed 's/^/  /' "$HERE/KERNEL_INFO.txt"
        fi
    } > "$RUN_DIR/meta.txt"

    echo ""
    echo "############################################################"
    echo "# workload: $LABEL    ->  results/$LABEL/$TS/"
    echo "############################################################"

    if [ -n "$PREP" ]; then
        echo "[prep] (unmeasured) $PREP"
        sh -c "$PREP"
    fi

    # Measured window. Tee the whole console output to run.log so the run is
    # fully reproducible from its saved folder.
    sh "$PROFILER" "$LABEL" sh -c "$RUN" 2>&1 | tee "$RUN_DIR/run.log"

    # Snapshot the kernel LtRAM counters into the same folder.
    if [ -r /sys/kernel/debug/ltram/stats ]; then
        cp /sys/kernel/debug/ltram/stats "$RUN_DIR/profile_${LABEL}.ltram"
    fi

    unset LTRAM_RUN_DIR
    ran="$ran $LABEL/$TS"
done

echo ""
echo "=== done. saved runs (host: results/<test>/<timestamp>/): ==="
for r in $ran; do
    echo "  results/$r/   (run.log, profile_*.{csv,node,ltram,summary})"
done
