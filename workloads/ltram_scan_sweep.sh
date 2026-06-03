#!/bin/sh
# Sweep the scanning-hand scan_batch (N = PTEs visited per wake) against
# ycsbc/redis at a FIXED read/write mix, measuring BOTH overhead signals:
#   - user-visible : ycsbc transaction throughput (KTPS) vs the scan-off baseline
#   - background   : the ltram_scan kthread's own CPU time during the run
# plus the LtRAM placement outcome (migrated / repatriated / resident) at each N.
#
# The data lives in redis, so we point the scanner at the REDIS pid. Each ycsbc
# invocation reloads the dataset (untimed) -> every iteration starts with the
# data freshly written into DRAM, then the scan migrates the read-cold pages
# during the timed transaction phase.
#
# Usage (guest):  sh /mnt/workloads/ltram_scan_sweep.sh [spec] [batch-list]
set -u

SPEC=${1:-/mnt/workloads/YCSB-C/workloads/workloadb.spec}   # read-heavy default

# N sweep: linear 0..N_MAX by N_STEP (N=0 is the scan-off baseline). Default is
# 0..4096 by 128 = 33 points (~20 min; each N is a full fresh-redis load+run).
# Lower N_STEP for finer resolution at the cost of runtime; or pass an explicit
# space-separated list as arg 2.
N_MAX=${SCAN_N_MAX:-4096}
N_STEP=${SCAN_N_STEP:-128}
BATCHES=${2:-$(seq 0 "$N_STEP" "$N_MAX")}

RESULTS=/mnt/results
# Write into the run's folder (set by run_profile.sh) so sweep.csv sits next to
# run.log/meta.txt; standalone, make our own timestamped dir.
OUT="${LTRAM_RUN_DIR:-$RESULTS/scansweep/$(date +%y%m%d__%H%M%S)}"; mkdir -p "$OUT"
CSV="$OUT/sweep.csv"
PARAM=/sys/module/ltram/parameters/scan_batch
RESET=/sys/kernel/debug/ltram/reset
SCANP=/sys/kernel/debug/ltram/scan_pid
STATS=/sys/kernel/debug/ltram/stats
YCSBC="LD_LIBRARY_PATH=/mnt/workloads /mnt/workloads/YCSB-C/ycsbc -db redis -P $SPEC"

[ -e "$STATS" ] || mount -t debugfs none /sys/kernel/debug 2>/dev/null

ip link set lo up 2>/dev/null
RPID=""

# (re)start redis fresh on the DRAM node so every N begins from an identical,
# uncontaminated state: empty DB -> ycsbc reloads it into DRAM -> the scan
# migrates the read-cold pages during that N's transaction phase only.
fresh_redis() {
    [ -n "$RPID" ] && { kill "$RPID" 2>/dev/null; sleep 1; }
    numactl --cpunodebind=0 --membind=0 redis-server --save "" --daemonize yes
    sleep 1
    RPID=$(pidof redis-server | awk '{print $1}')
}

# pid of the ltram_scan kthread (for its CPU time)
SKPID=""
for p in /proc/[0-9]*; do
    [ "$(cat "$p/comm" 2>/dev/null)" = ltram_scan ] && { SKPID=${p#/proc/}; break; }
done
echo "scan kthread pid=${SKPID:-none}  spec=$SPEC  (fresh redis per N)"

# Lift the endurance token cap for THIS experiment so scan_batch (not the 42/s
# bucket) is what limits migration -- otherwise every N migrates at the same
# capped rate and the sensitivity is masked. The token bucket is its own knob to
# sweep separately. Restored on exit.
TRATE=/sys/module/ltram/parameters/token_rate
echo 0 > "$TRATE" 2>/dev/null
trap 'echo 42 > "$TRATE" 2>/dev/null; echo -1 > "$SCANP" 2>/dev/null' EXIT INT TERM

stat_get()  { grep "^$1 " "$STATS" 2>/dev/null | awk '{print $2}'; }
scan_cpu()  { [ -n "$SKPID" ] && awk '{print $14+$15}' "/proc/$SKPID/stat" 2>/dev/null || echo 0; }
run_ktps()  {           # run ycsbc; echo transaction KTPS (last field, last line)
    sh -c "$YCSBC" 2>"$OUT/.err" >/dev/null
    tail -1 "$OUT/.err" | awk '{print $NF}'
}

cat > "$CSV" <<'HDR'
# LtRAM scanning-hand scan_batch sweep -- column definitions
# (one row per N; row N=0 is the scan-OFF baseline. ltram counters are reset
#  before each N via /sys/kernel/debug/ltram/reset, except where noted.)
#
# scan_batch     N: PTEs the scan kthread visits per ~100ms wake. Set via
#                /sys/module/ltram/parameters/scan_batch. 0 = scanning off (baseline).
# ktps           ycsbc transaction throughput, kilo-ops/sec (higher=better). YCSB-C
#                times the transaction phase and prints total_ops/duration/1000 to
#                stderr; the harness takes the last field of the last line.
# overhead_pct   user-visible cost vs the N=0 baseline: (base_ktps-ktps)/base_ktps*100.
#                >0 = slower than baseline. Captures CPU steal + per-sweep TLB flush +
#                mmap/PTE lock contention + faults induced by migration/repatriation.
# scan_cpu_s     CPU seconds the ltram_scan kthread itself burned during this run
#                (background cost): delta of (utime+stime) from /proc/<scan_kthread>/stat
#                across the run, converted ticks->s (/100).
# migrated_in    pages the scan moved DRAM->LtRAM during this run (placement benefit).
#                From stats:placed_migrated_in (reset before each N).
# migrated_back  pages bounced LtRAM->DRAM during this run (write-back churn: a migrated
#                page got written and repatriated). stats:migrated_back_of_migrated
#                (reset before each N). Lower = better placement quality.
# resident_pages LtRAM frames occupied at the END of this run (instantaneous gauge,
#                4 KB each). stats:currently_resident_pages -- live zone residency, NOT
#                a reset-able counter.
# scan_passes    CUMULATIVE scan-thread wakes since boot (NOT reset per N -- subtract
#                consecutive rows for the per-N count). ~one wake per scan_interval_ms.
#
scan_batch,ktps,overhead_pct,scan_cpu_s,migrated_in,migrated_back,resident_pages,scan_passes
HDR
BASE=""
for N in $BATCHES; do
    fresh_redis                                 # clean, identical start per N
    echo 1 > "$RESET" 2>/dev/null
    C0=$(scan_cpu)
    if [ "$N" = 0 ]; then
        echo -1 > "$SCANP" 2>/dev/null          # scan OFF (baseline)
        K=$(run_ktps); BASE=$K; OVER=0
    else
        echo "$N" > "$PARAM" 2>/dev/null
        echo "$RPID" > "$SCANP" 2>/dev/null      # scan redis (data lives there)
        K=$(run_ktps)
        echo -1 > "$SCANP" 2>/dev/null
        OVER=$(awk -v b="$BASE" -v k="$K" 'BEGIN{ if(b+0>0) printf "%.2f",(b-k)/b*100; else print "NA" }')
    fi
    CPU=$(awk -v a="$C0" -v b="$(scan_cpu)" 'BEGIN{ printf "%.2f",(b-a)/100 }')   # ticks->s
    MI=$(stat_get placed_migrated_in);          MB=$(stat_get migrated_back_of_migrated)
    RES=$(stat_get currently_resident_pages);   SP=$(stat_get scan_passes)
    echo "${N},${K},${OVER},${CPU},${MI:-0},${MB:-0},${RES:-0},${SP:-0}" | tee -a "$CSV"
done

echo ""
echo "=== scan_batch sweep saved: ${CSV#"$RESULTS"/}  (host: results/) ==="
