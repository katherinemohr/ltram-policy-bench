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

# Per-run output directory: results/<label>/<YYMMDD__HHMMSS>/ so nothing is
# overwritten and every run is kept. run_profile.sh sets $LTRAM_RUN_DIR (so the
# .ltram/run.log it writes land in the same folder); standalone we make our own.
RUN_DIR="${LTRAM_RUN_DIR:-/mnt/results/${LABEL}/$(date +%y%m%d__%H%M%S 2>/dev/null || echo unknown)}"
mkdir -p "$RUN_DIR" 2>/dev/null

OUT="$RUN_DIR/profile_${LABEL}.csv"

# Extract the counters we care about from /proc/vmstat. Print one CSV row.
snap() {
    awk '
    BEGIN {
        order = "pgpgin pgpgout pgfault pgmajfault pgalloc_normal pgalloc_movable nr_file_pages nr_anon_pages nr_dirty nr_writeback workingset_refault_file nr_active_file nr_inactive_file nr_dirtied"
        n = split(order, fields, " ")
    }
    /^[a-z_]+ [0-9]+/ {
        vals[$1] = $2
    }
    END {
        for (i = 1; i <= n; i++) {
            v = (vals[fields[i]] ? vals[fields[i]] : 0)
            # The kernel counts pgpgin/pgpgout in 512-byte sectors, not KB:
            # blk-core.c submit_bio() does count_vm_events(PGPGIN/PGPGOUT,
            # bio_sectors(bio)), and bio_sectors = bi_size >> 9. Everything else
            # in this list is already in 4 KB pages. 8 sectors = 1 page, so
            # divide by 8 to put every CSV column in the same unit (4 KB pages).
            if (fields[i] == "pgpgin" || fields[i] == "pgpgout")
                v = int(v / 8)
            printf "%s%s", (i==1 ? "" : ","), v
        }
    }
    ' /proc/vmstat
}

# --- LtRAM placement via node stats -----------------------------------------
# LtRAM lives entirely on one NUMA node (default node 1), and that node has ONLY
# ZONE_LTRAM. So the node's own stats ARE the LtRAM stats -- no missing kernel
# counter required. node_snap() emits "key value" lines from that node's meminfo
# (residency + file/anon/dirty split, in kB) and numastat (cumulative count of
# allocations satisfied from the node = allocations that landed in LtRAM).
LTRAM_NODE=${LTRAM_NODE:-1}
NODE_DIR="/sys/devices/system/node/node${LTRAM_NODE}"

node_snap() {
    if [ -r "$NODE_DIR/meminfo" ]; then
        # lines look like: "Node 1 FilePages:   1234 kB" -> "FilePages 1234"
        awk '{ for (i=1;i<=NF;i++) if ($i ~ /:$/) { k=$i; sub(/:$/,"",k); print k, $(i+1) } }' \
            "$NODE_DIR/meminfo"
    fi
    [ -r "$NODE_DIR/numastat" ] && cat "$NODE_DIR/numastat"
}

# Make sure /mnt/results exists (the virtfs mount creates it, but be defensive)
mkdir -p /mnt/results

# Headers if new file
if [ ! -f "$OUT" ]; then
    cat > "$OUT" <<'EOF'
label,phase,t,pgpgin,pgpgout,pgfault,pgmajfault,pgalloc_normal,pgalloc_movable,nr_file_pages,nr_anon_pages,nr_dirty,nr_writeback,workingset_refault_file,nr_active_file,nr_inactive_file,nr_dirtied
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
NODE_BEFORE="/tmp/.node_before_$$"; node_snap > "$NODE_BEFORE"
# Snapshot the kernel LtRAM counters before the workload so the bottom line can
# report PER-RUN deltas (this also cancels the boot residue automatically).
LSTATS=/sys/kernel/debug/ltram/stats
LT_BEFORE="/tmp/.lt_before_$$"; [ -r "$LSTATS" ] && cp "$LSTATS" "$LT_BEFORE" 2>/dev/null

echo "=== Profiling ${LABEL}: $* ==="
echo "Starting at t=${T0}"

# Run the workload (timed). With LTRAM_SCAN=1 the workload runs in the
# background and the kernel scanning hand is pointed at its pid, so the
# autonomous placement policy migrates its write-cold pages *during* the run.
SCAN_PID_FILE=/sys/kernel/debug/ltram/scan_pid
if [ "${LTRAM_SCAN:-0}" = 1 ] && [ -w "$SCAN_PID_FILE" ]; then
    "$@" &
    WPID=$!
    echo "$WPID" > "$SCAN_PID_FILE"
    echo "[scan] scanning hand attached to pid $WPID"
    wait "$WPID"; RC=$?
    echo -1 > "$SCAN_PID_FILE"
else
    "$@"
    RC=$?
fi

T1=$(date +%s)
sync   # flush any pending writes so writeback counters update
AFTER=$(snap)
echo "${LABEL},after,${T1},${AFTER}" >> "$OUT"
NODE_AFTER="/tmp/.node_after_$$"; node_snap > "$NODE_AFTER"
LT_AFTER="/tmp/.lt_after_$$"; [ -r "$LSTATS" ] && cp "$LSTATS" "$LT_AFTER" 2>/dev/null

DUR=$((T1 - T0))
echo "Workload exit=${RC}, duration=${DUR}s"

# Methodology legend: what each counter means and exactly when the kernel
# increments it. Printed BEFORE the deltas so it reads as a lookup table for
# the numbers that follow. All values are in 4 KB pages (pgpgin/pgpgout were
# converted from KB in snap()). Counter sites are in mm/ as of the zone_ltram
# kernel; "delta" = after - before across the workload.
cat <<'EOF'

=== Counter legend (units: 4 KB pages; delta = after - before) ===

  -- Disk I/O (block layer; only counts traffic that actually hits the device) --
  pgpgin    Pages read IN from the backing block device (page cache misses
            that did real I/O + readahead). Counted in blk-core.c submit_bio()
            for REQ_OP_READ, in 512-byte sectors. Does NOT count cache hits.
            Converted sectors/8 -> pages here.
  pgpgout   Pages written OUT to the block device (REQ_OP_WRITE), in 512-byte
            sectors, same submit_bio() site. Dirty page-cache flushed to disk.
            Converted sectors/8 -> pages here.

  -- Faults (per-PTE; counts the work the fault handler did) --
  pgfault   ALL minor+major page faults (handle_mm_fault). Includes anonymous
            first-touch, COW write-protect faults, and file-page faults. This
            is the dominant signal for anonymous/in-RAM workloads.
  pgmajfault  Subset of pgfault that had to block on I/O (e.g. fault that
            triggered a disk read). High ratio => working set exceeds RAM.

  -- Allocation (per-zone; where new pages physically came from) --
  pgalloc_normal   Pages handed out from ZONE_NORMAL (node 0 DRAM tier).
  pgalloc_movable  Pages handed out from ZONE_MOVABLE.
            NOTE: /proc/vmstat has no pgalloc_ltram (the zone was never added to
            the vmstat label macros), so LtRAM allocs are absent from the
            columns above. They are captured separately below via node stats,
            since the LtRAM node hosts only ZONE_LTRAM.

  -- Page-cache residency (instantaneous gauge, not a rate) --
  nr_file_pages   Net change in total file-backed pages resident in the cache.
            Snapshot delta, so a small number can hide large churn (equal
            add+evict cancels out). For churn use the bpftrace tracer.

  -- Dirty / writeback state (the read-only-vs-writeable signal) --
  nr_dirty        Net change in pages currently flagged dirty (awaiting
            writeback) at snapshot time. A gauge, not cumulative.
  nr_writeback    Net change in pages currently under active writeback.
  workingset_refault_file   File pages that were evicted and then faulted
            back in (thrash indicator). 0 => the file working set fit in RAM.
  nr_active_file / nr_inactive_file   Net movement between the file LRU lists.
  nr_dirtied      CUMULATIVE count of dirtying events (account_page_dirtied):
            every time a clean page is first made dirty. Unlike nr_dirty this
            only goes up. This is the closest vmstat proxy for "a page that
            was read-only got written" -- but it is process/zone-blind, so it
            cannot tell you whether the dirtied page was one we put in LtRAM.

EOF

# Print human-readable deltas. The CSV keeps full history (and lives on the
# host-shared results dir, so it survives VM restarts), but we only ever report
# the LATEST before/after pair -- accumulate and print at END, no stale blocks.
echo "=== Deltas for ${LABEL} ==="
awk -F, -v L="$LABEL" '
    NR==1 { next }                             # header
    $1==L && $2=="before" { for (i=4; i<=NF; i++) b[i]=$i }
    $1==L && $2=="after"  { for (i=4; i<=NF; i++) d[i]=$i-b[i]; nf=NF; seen=1 }
    END {
        if (!seen) { print "  (no completed run for " L " yet)"; exit }
        split("pgpgin pgpgout pgfault pgmajfault pgalloc_normal pgalloc_movable nr_file_pages nr_anon_pages nr_dirty nr_writeback workingset_refault_file nr_active_file nr_inactive_file nr_dirtied", names, " ")
        for (i=4; i<=nf; i++)
            printf "  %-30s %+d\n", names[i-3], d[i]
    }
' "$OUT"

# --- LtRAM placement deltas (node-stat based) -------------------------------
# Memory fields are kB in the source; shown here as 4 KB pages (kB/4) to match
# the rest of the report. numa_hit/local_node/other_node are cumulative
# allocation counts (the per-LtRAM-node "pgalloc" that vmstat lacks).
# Persist the LtRAM placement report to a sidecar so it survives the VM exit
# (the CSV holds only vmstat counters; this is the node-stat side).
NODE_OUT="$RUN_DIR/profile_${LABEL}.node"
{
    echo "# LtRAM placement for ${LABEL} (node ${LTRAM_NODE} = ZONE_LTRAM only)"
    echo "# memory deltas in 4 KB pages; numa_* are cumulative alloc counts"
    if [ -s "$NODE_BEFORE" ] && [ -s "$NODE_AFTER" ]; then
        awk '
            NR==FNR { b[$1]=$2; next }
            {
                key=$1; d=$2-b[key]
                if (key=="numa_hit"||key=="local_node"||key=="other_node"||key=="numa_miss"||key=="numa_foreign") {
                    printf "%-18s %+d  (allocs into LtRAM node)\n", key, d
                } else if (key=="MemUsed"||key=="FilePages"||key=="AnonPages"||key=="Active(file)"||key=="Inactive(file)"||key=="Dirty"||key=="Writeback"||key=="Mapped"||key=="Shmem") {
                    printf "%-18s %+d pages\n", key, int(d/4)
                }
            }
        ' "$NODE_BEFORE" "$NODE_AFTER"
        echo "---"
        echo "FilePages delta = read-only/file data placed in LtRAM (the target case)"
        echo "AnonPages delta = anonymous pages in LtRAM (read-only anon routing)"
        echo "Dirty/Writeback = LtRAM pages that got WRITTEN -> should stay ~0 if"
        echo "                  read-only placement is safe for this workload"
    else
        echo "(node ${LTRAM_NODE} stats unavailable -- no LtRAM node or sysfs not mounted)"
    fi
} | tee "$NODE_OUT" | sed 's/^/  /'
echo "  [saved $NODE_OUT]"
rm -f "$NODE_BEFORE" "$NODE_AFTER"

# Quick classification (file-page based; meaningless for anonymous/in-RAM
# workloads like redis where the action is in pgfault/pgalloc, not file I/O).
echo ""
echo "=== Quick read/write classification (file pages only) ==="
echo "  read-heavy : pgpgin >> pgpgout and nr_dirtied small"
echo "  write-heavy: pgpgout and nr_dirtied both grow"
echo "  See the counter legend above for what each number actually measures."

# ===================== BOTTOM LINE (presentation summary) ===================
# Combines the vmstat deltas (DRAM anon/file), the kernel LtRAM counters, and
# the wear histogram into the handful of derived numbers that tell the story:
# how much went to LtRAM, how much stayed read-only, DRAM footprint reduction,
# device utilization, and wear distribution. Printed AND saved as a key,value
# CSV to profile_<label>.summary for slides/plots.
SUMMARY="$RUN_DIR/profile_${LABEL}.summary"

# DRAM deltas for the latest run (col 8 pgalloc_normal, 10 nr_file_pages,
# 11 nr_anon_pages).
eval "$(awk -F, -v L="$LABEL" '
    $1==L && $2=="before" { a=$8; f=$10; n=$11 }
    $1==L && $2=="after"  { da=$8-a; df=$10-f; dn=$11-n }
    END { printf "D_ALLOC=%d D_FILE=%d D_ANON=%d", da, df, dn }
' "$OUT")"

# getd: per-run DELTA of a cumulative LtRAM counter (after - before).
# getn: current value from the after snapshot (for gauges + lifetime wear).
getd() {
    [ -s "$LT_AFTER" ] || { echo 0; return; }
    _a=$(awk -v k="$1" '$1==k{print $2}' "$LT_AFTER"); _b=0
    [ -s "$LT_BEFORE" ] && _b=$(awk -v k="$1" '$1==k{print $2}' "$LT_BEFORE")
    echo $(( ${_a:-0} - ${_b:-0} ))
}
getn() { [ -s "$LT_AFTER" ] && awk -v k="$1" '$1==k{print $2}' "$LT_AFTER" || echo 0; }

awk -v label="$LABEL" -v summary="$SUMMARY" \
    -v d_alloc="${D_ALLOC:-0}" -v d_file="${D_FILE:-0}" -v d_anon="${D_ANON:-0}" \
    -v p_alloc="$(getd placed_at_alloc)" -v p_mig="$(getd placed_migrated_in)" \
    -v wf_a="$(getd write_faulted_of_alloc)" -v wf_m="$(getd write_faulted_of_migrated)" \
    -v mb="$(getd migrated_back_of_alloc)" -v mb2="$(getd migrated_back_of_migrated)" \
    -v freed="$(getd freed)" -v resid_now="$(getn currently_resident_pages)" \
    -v frtot="$(getn frames_total)" -v frused="$(getn frames_ever_programmed)" \
    -v emin="$(getn erase_count_min)" -v emean="$(getn erase_count_mean_x1000)" \
    -v emed="$(getn erase_count_median)" -v emode="$(getn erase_count_mode)" \
    -v emax="$(getn erase_count_max)" -v eskew="$(getn skew_max_over_mean_x1000)" '
function mb_(p){ return p*4.0/1024 }
function pct(a,b){ return b>0 ? a*100.0/b : 0 }
BEGIN {
    placed  = p_alloc + p_mig          # placed this run (gross)
    net     = placed - freed           # net retained in LtRAM this run
    wf      = wf_a + wf_m
    backout = mb + mb2
    ro      = placed - wf
    dram    = d_alloc                  # DRAM pages allocated this run
    foot    = dram + placed            # pages allocated this run (DRAM + LtRAM)

    printf "\n=== BOTTOM LINE: %s  (per-run deltas; wear is lifetime) ===\n", label
    printf "DRAM allocated     : %d pages (%.1f MB)   [anon %d, file-cache %d]\n",
           dram, mb_(dram), d_anon, d_file
    printf "LtRAM placed       : %d pages (%.1f MB)   [at-alloc %d, migrated %d]\n",
           placed, mb_(placed), p_alloc, p_mig
    printf "  freed %d, net retained %d (%.1f MB)  <- churn\n", freed, net, mb_(net)
    printf "  LtRAM share of alloc       : %.2f%%  (offload / DRAM footprint reduction)\n", pct(placed, foot)
    printf "LtRAM utilization  : %d / %d frames resident now = %.2f%% of device\n",
           resid_now, frtot, pct(resid_now, frtot)
    printf "Read-only vs RW    : read-only %d (%.1f%%), write-faulted %d, repatriated %d\n",
           ro, pct(ro, placed), wf, backout
    printf "Placement perf     : write-back rate = %.2f%%  (lower is better)\n", pct(wf, placed)
    printf "Wear (programs/frame, lifetime, used frames): min %d  mean %.3f  median %d  mode %d  max %d  skew %.2fx\n",
           emin, emean/1000.0, emed, emode, emax, eskew/1000.0
    printf "  frames touched   : %d / %d = %.2f%% of device\n", frused, frtot, pct(frused, frtot)

    printf "metric,value\n"                                   > summary
    printf "dram_pages,%d\n", dram                            >> summary
    printf "dram_anon_pages,%d\n", d_anon                     >> summary
    printf "dram_file_pages,%d\n", d_file                     >> summary
    printf "ltram_placed_pages,%d\n", placed                  >> summary
    printf "ltram_placed_at_alloc,%d\n", p_alloc              >> summary
    printf "ltram_placed_migrated,%d\n", p_mig                >> summary
    printf "ltram_freed_pages,%d\n", freed                    >> summary
    printf "ltram_net_retained_pages,%d\n", net               >> summary
    printf "ltram_share_of_alloc_pct,%.4f\n", pct(placed, foot) >> summary
    printf "ltram_resident_now_pages,%d\n", resid_now         >> summary
    printf "ltram_util_pct,%.4f\n", pct(resid_now, frtot)     >> summary
    printf "ltram_readonly_pages,%d\n", ro                    >> summary
    printf "ltram_write_faulted_pages,%d\n", wf               >> summary
    printf "ltram_repatriated_pages,%d\n", backout            >> summary
    printf "writeback_rate_pct,%.4f\n", pct(wf, placed)       >> summary
    printf "wear_min,%d\n", emin                              >> summary
    printf "wear_mean,%.4f\n", emean/1000.0                   >> summary
    printf "wear_median,%d\n", emed                           >> summary
    printf "wear_mode,%d\n", emode                            >> summary
    printf "wear_max,%d\n", emax                              >> summary
    printf "wear_skew_max_over_mean,%.4f\n", eskew/1000.0     >> summary
    printf "frames_touched,%d\n", frused                      >> summary
    printf "frames_total,%d\n", frtot                         >> summary
}'
echo "  [saved $SUMMARY]"
rm -f "$LT_BEFORE" "$LT_AFTER"

exit $RC
