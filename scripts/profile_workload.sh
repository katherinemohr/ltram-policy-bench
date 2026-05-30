#!/bin/sh
# Profile a workload's file-page activity using /proc/vmstat snapshots.
# Outputs a CSV row to results/profile_<workload>.csv with deltas of the
# key counters.
#
# Usage:   profile_workload.sh <workload_label> <command...>
# Example: profile_workload.sh ycsbc /workloads/YCSB-C/ycsbc -P workloada
#
# Run inside the QEMU guest. The /mnt/results directory is shared via virtfs
# back to the host so the CSV survives the VM exit.

set -u

LABEL="$1"
shift
if [ -z "$LABEL" ] || [ $# -eq 0 ]; then
    echo "Usage: $0 <label> <workload command...>" >&2
    exit 1
fi

OUT="/mnt/results/profile_${LABEL}.csv"

# Extract the counters we care about from /proc/vmstat. Print one CSV row.
snap() {
    awk '
    BEGIN {
        order = "pgpgin pgpgout pgfault pgmajfault pgalloc_normal pgalloc_movable nr_file_pages nr_dirty nr_writeback workingset_refault_file nr_active_file nr_inactive_file nr_dirtied"
        n = split(order, fields, " ")
    }
    /^[a-z_]+ [0-9]+/ {
        vals[$1] = $2
    }
    END {
        for (i = 1; i <= n; i++) {
            printf "%s%s", (i==1 ? "" : ","), (vals[fields[i]] ? vals[fields[i]] : 0)
        }
    }
    ' /proc/vmstat
}

# Make sure /mnt/results exists (the virtfs mount creates it, but be defensive)
mkdir -p /mnt/results

# Headers if new file
if [ ! -f "$OUT" ]; then
    cat > "$OUT" <<'EOF'
label,phase,t,pgpgin,pgpgout,pgfault,pgmajfault,pgalloc_normal,pgalloc_movable,nr_file_pages,nr_dirty,nr_writeback,workingset_refault_file,nr_active_file,nr_inactive_file,nr_dirtied
EOF
fi

# Clean baseline: sync dirty data, drop file caches
sync
echo 3 > /proc/sys/vm/drop_caches
sleep 1

# Snapshot before
T0=$(date +%s)
BEFORE=$(snap)
echo "${LABEL},before,${T0},${BEFORE}" >> "$OUT"

echo "=== Profiling ${LABEL}: $* ==="
echo "Starting at t=${T0}"

# Run the workload (timed)
"$@"
RC=$?

T1=$(date +%s)
sync   # flush any pending writes so writeback counters update
AFTER=$(snap)
echo "${LABEL},after,${T1},${AFTER}" >> "$OUT"

DUR=$((T1 - T0))
echo "Workload exit=${RC}, duration=${DUR}s"

# Print human-readable deltas
echo "=== Deltas for ${LABEL} ==="
awk -F, '
    NR==1 { next }                             # header
    $1=="'"$LABEL"'" && $2=="before" { for (i=4; i<=NF; i++) b[i]=$i; names_set=0 }
    $1=="'"$LABEL"'" && $2=="after"  {
        # field name lookup
        split("pgpgin pgpgout pgfault pgmajfault pgalloc_normal pgalloc_movable nr_file_pages nr_dirty nr_writeback workingset_refault_file nr_active_file nr_inactive_file nr_dirtied", names, " ")
        for (i=4; i<=NF; i++) {
            d = $i - b[i]
            printf "  %-30s %+d\n", names[i-3], d
        }
    }
' "$OUT" | tail -20

# Inline interpretation
echo ""
echo "=== Interpretation hints ==="
echo "  pgpgin  = file pages read from disk (KB / 4)"
echo "  pgpgout = dirty pages written to disk (KB / 4)"
echo "  nr_dirtied = cumulative dirty events (cumulative)"
echo "  nr_file_pages delta = net change in cached file pages"
echo ""
echo "  read-heavy: pgpgin >> pgpgout, nr_dirtied small"
echo "  write-heavy: pgpgout grows, nr_dirtied grows"
echo "  fraction of pgpgin'd pages that got dirty ~ nr_dirtied / pgpgin"

exit $RC
