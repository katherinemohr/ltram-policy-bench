#!/bin/sh
# Time-series sampler for the CRAM tier's debugfs counters. Sibling of
# meminfo_log.sh / ltram_stats_log.sh: same collect/--end shape and the same
# /proc/uptime timestamp, so rows align with meminfo.csv.
#
# CRAM (unlike LtRAM) has no single "stats" file. Each sample reads the five
# scalar counter files under /sys/kernel/debug/cram/ plus the first CRAM node's
# row of the `nodes` table (per-node wear/tokens/residency).
#
# Output: a wide CSV, one column per field, prefixed with ts_s.
#
# Usage:
#   ./cram_stats_log.sh > cram_stats.csv          # start collecting (blocks)
#   ./cram_stats_log.sh --end cram_stats.csv      # stop + print summary
#
# Degrades cleanly on a non-CRAM kernel: prints the header and exits 0 so the
# run harness (S51cramrun) and downstream plotting do not choke.

INTERVAL=0.1
PIDFILE=/tmp/cram_stats_log.pid
DBG=/sys/kernel/debug
CRAM="$DBG/cram"

# Scalar counter files (each holds a single unsigned long). Fixed order so the
# CSV header is stable even if the kernel adds/removes one; missing -> 0.
COUNTERS="demote_count promote_count promote_fail wear_programs wear_denied"

# Per-node columns pulled by POSITION from the first data row of $CRAM/nodes.
# nodes header (mm/cram.c):
#   1 node  2 zratio  3 current_ratio  4 perceived  5 balloon  6 target
#   7 blocked  8 allowed  9 free  10 present  11 programs  12 erase_min
#   13 erase_max  14 erase_mean_x1000  15 tokens  16 rate
# We keep the columns that matter for the read-only wear tier.
NODE_FIELDS="node current_ratio balloon free present programs erase_min erase_max erase_mean_x1000 tokens rate"

# Mount debugfs if the cram dir is not yet visible. Returns 0 only if present.
ensure_cram() {
    [ -d "$CRAM" ] && return 0
    mount | grep -q "debugfs" || mount -t debugfs none "$DBG" 2>/dev/null
    [ -d "$CRAM" ]
}

header() {
    printf "ts_s"
    for f in $COUNTERS; do printf ",%s" "$f"; done
    for f in $NODE_FIELDS; do printf ",%s" "$f"; done
    printf "\n"
}

# One CSV row (no ts): the scalar counters in COUNTERS order, then the first
# CRAM node row's selected columns. Any missing value -> 0.
snap() {
    row=""
    for c in $COUNTERS; do
        v=$(cat "$CRAM/$c" 2>/dev/null)
        row="${row}${row:+,}${v:-0}"
    done
    # First data row of nodes (NR>1), selected columns matching NODE_FIELDS.
    nrow=$(awk 'NR>1 && NF>=16 {
        print $1","$3","$5","$9","$10","$11","$12","$13","$14","$15","$16; exit
    }' "$CRAM/nodes" 2>/dev/null)
    [ -z "$nrow" ] && nrow="0,0,0,0,0,0,0,0,0,0,0"
    printf "%s,%s" "$row" "$nrow"
}

collect() {
    echo $$ > "$PIDFILE"
    header

    if ! ensure_cram; then
        # No CRAM on this kernel: header only, nothing to sample.
        rm -f "$PIDFILE"
        exit 0
    fi

    trap 'rm -f "$PIDFILE"; exit 0' TERM INT

    while true; do
        TS=$(awk '{print $1; exit}' /proc/uptime)
        echo "$TS,$(snap)"
        sleep "$INTERVAL"
    done
}

# min / avg / max / final per column. "final" is the last sampled value (the
# running total for the cumulative counters).
summarize() {
    awk -F',' '
    NR==1 {
        for (i=2; i<=NF; i++) cols[i]=$i
        ncols=NF; next
    }
    NF < 2 { next }
    NR==2  { ts_first=$1 }
    {
        ts_last=$1; nrows++
        for (i=2; i<=ncols; i++) {
            v=$i+0
            if (nrows==1 || v < mins[i]) mins[i]=v
            if (nrows==1 || v > maxs[i]) maxs[i]=v
            avgs[i]+=v
            finals[i]=v
        }
    }
    END {
        if (nrows==0) { print "(no samples)"; exit }
        dur=ts_last-ts_first
        printf "Duration : %.1fs   (%d samples)\n\n", dur, nrows
        printf "%-28s %12s %12s %12s %12s\n", "metric","min","avg","max","final"
        printf "%s\n", "----------------------------------------------------------------------------------"
        for (i=2; i<=ncols; i++)
            printf "%-28s %12d %12d %12d %12d\n", cols[i], mins[i], avgs[i]/nrows, maxs[i], finals[i]
    }' "$1"
}

case "$1" in
    --end)
        kill "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
        sleep 0.2
        summarize "$2"
        ;;
    *)
        collect
        ;;
esac
