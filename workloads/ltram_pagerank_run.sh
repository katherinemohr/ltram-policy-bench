#!/bin/sh
# Pagerank LtRAM offload run (the clean read-only-data headline).
#
# pr builds a CSR graph once (writes), then runs many PR trials that READ the
# graph and rewrite small score arrays. We:
#   - keep the scan OFF during build (so build writes don't pollute repat),
#   - turn it ON for a LONG trial phase (hundreds of trials) so it converges on
#     the read-only graph while the write-hot score arrays get aged and skipped,
#   - SAMPLE residency over time (pr frees everything on exit, so we must measure
#     while it runs) -> a timeseries showing the graph offloading and STAYING.
#
# Tunables (env): G (graph scale, def 20), TRIALS (def 300), ITERS (def 100).
set -u

G=$(sed -n 's/.*ltram_g=\([0-9]*\).*/\1/p' /proc/cmdline); G=${G:-20}
TRIALS=$(sed -n 's/.*ltram_trials=\([0-9]*\).*/\1/p' /proc/cmdline); TRIALS=${TRIALS:-300}
ITERS=${ITERS:-100}
STATS=/sys/kernel/debug/ltram/stats
RESET=/sys/kernel/debug/ltram/reset
SCANP=/sys/kernel/debug/ltram/scan_pid
PR="numactl --cpunodebind=0 --membind=0 /mnt/workloads/pr -g $G -n $TRIALS -i $ITERS"

[ -e "$STATS" ] || mount -t debugfs none /sys/kernel/debug 2>/dev/null
OUT="${LTRAM_RUN_DIR:-/mnt/results/pagerankrun/$(date +%y%m%d__%H%M%S)}"; mkdir -p "$OUT"
TSV="$OUT/pagerank_timeseries.csv"

stat_get() { grep "^$1 " "$STATS" 2>/dev/null | awk '{print $2}'; }

echo "[pagerank] g=$G trials=$TRIALS iters=$ITERS  (token_rate=$(cat /sys/module/ltram/parameters/token_rate))"

# start pr with the scan OFF; capture stdout so we can detect "Build Time:"
echo -1 > "$SCANP" 2>/dev/null
: > "$OUT/.prout"
sh -c "$PR" > "$OUT/.prout" 2>&1 &
PPID=$!

i=0; while [ $i -lt 600 ]; do
    grep -q "Build Time:" "$OUT/.prout" 2>/dev/null && break
    kill -0 "$PPID" 2>/dev/null || break
    sleep 0.2; i=$((i + 1))
done
echo 1 > "$RESET" 2>/dev/null               # zero counters at the start of trials
echo "$PPID" > "$SCANP" 2>/dev/null         # scan ON for the read trials
echo "[pagerank] build done, scan attached to pid $PPID; sampling..."

echo "t_s,resident_pg,migrated_in,migrated_back,pr_rss_pg,util_pct,repat_pct" > "$TSV"
t0=$(date +%s)
peak_util=0
while kill -0 "$PPID" 2>/dev/null; do
    RES=$(stat_get currently_resident_pages); AT=$(stat_get placed_at_alloc)
    MI=$(stat_get placed_migrated_in);       MB=$(stat_get migrated_back_of_migrated)
    RSS=$(awk '/^VmRSS:/{print int($2/4)}' "/proc/$PPID/status" 2>/dev/null)
    LT=$(( ${RES:-0} - ${AT:-0} ))
    util=$(awk -v a="$LT" -v b="${RSS:-0}" 'BEGIN{ if(b>0) printf "%.2f",a/b*100; else print 0 }')
    repat=$(awk -v i="${MI:-0}" -v b="${MB:-0}" 'BEGIN{ if(i>0) printf "%.2f",b/i*100; else print 0 }')
    echo "$(( $(date +%s) - t0 )),${RES:-0},${MI:-0},${MB:-0},${RSS:-0},$util,$repat" >> "$TSV"
    awk -v u="$util" -v p="$peak_util" 'BEGIN{exit !(u>p)}' && peak_util=$util
    sleep 2
done
echo -1 > "$SCANP" 2>/dev/null

MI=$(stat_get placed_migrated_in); MB=$(stat_get migrated_back_of_migrated)
FINAL_REPAT=$(awk -v i="${MI:-0}" -v b="${MB:-0}" 'BEGIN{ if(i>0) printf "%.2f",b/i*100; else print 0 }')
echo ""
echo "=== pagerank LtRAM offload (read trials only; build excluded) ==="
echo "peak utilization  : ${peak_util}%   (graph pages in LtRAM / pr RSS)"
echo "final repat        : ${FINAL_REPAT}%  (migrated_back/migrated_in over trials)"
echo "migrated_in $MI  migrated_back $MB"
echo "timeseries: ${TSV#/mnt/}  (host: results/)"
