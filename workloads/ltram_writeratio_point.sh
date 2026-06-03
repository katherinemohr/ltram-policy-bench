#!/bin/sh
# ONE write-ratio point per VM boot (clean-boot isolation, matching the per-boot
# scan_batch sweep). The write ratio arrives on the kernel cmdline as ltram_wr=<w>
# and scan_batch as ltram.scan_batch=<N> (set by run-vm.sh). Measures, for this w:
#   utilization% = workload LtRAM pages / total resident (ltram resident delta / VmRSS)
#   repat%       = migrated_back / migrated_in              (ltram counters)
# Scan runs only during the TRANSACTION phase (off during the load/prefill writes).
# Appends one row to the sweep's timestamped dir (host writes writeratio/.sweep_dir).
set -u

W=$(sed -n 's/.*ltram_wr=\([0-9]\+\).*/\1/p' /proc/cmdline); W=${W:-50}
# OPC huge so the transaction phase outlasts the fixed scan window at EVERY ratio
# (we kill ycsbc after the window; it never needs to finish). SCAN_SECS gives the
# scan the SAME wall-clock time at every write ratio -> de-confounds utilization.
OPC=${OPC:-5000000}; RECS=100000
SCAN_SECS=${SCAN_SECS:-100}
N=$(cat /sys/module/ltram/parameters/scan_batch 2>/dev/null)

STATS=/sys/kernel/debug/ltram/stats
RESET=/sys/kernel/debug/ltram/reset
SCANP=/sys/kernel/debug/ltram/scan_pid
YBASE="LD_LIBRARY_PATH=/mnt/workloads /mnt/workloads/YCSB-C/ycsbc -db redis -P /tmp/wr.spec"

[ -e "$STATS" ] || mount -t debugfs none /sys/kernel/debug 2>/dev/null
ip link set lo up 2>/dev/null

SWEEP=$(cat /mnt/results/writeratio/.sweep_dir 2>/dev/null)
[ -n "$SWEEP" ] || SWEEP=$(date +%y%m%d__%H%M%S)
OUT=/mnt/results/writeratio/$SWEEP; mkdir -p "$OUT"; CSV="$OUT/writeratio.csv"

# spec for this write ratio (read = 100 - W)
DIST=${DIST:-zipfian}
rp=$(awk "BEGIN{printf \"%.4f\",(100-$W)/100}"); up=$(awk "BEGIN{printf \"%.4f\",$W/100}")
cat > /tmp/wr.spec <<EOF
recordcount=$RECS
operationcount=$OPC
workload=com.yahoo.ycsb.workloads.CoreWorkload
readallfields=true
readproportion=$rp
updateproportion=$up
scanproportion=0
insertproportion=0
requestdistribution=$DIST
EOF

# fresh redis on the DRAM node, wait until it answers
numactl --cpunodebind=0 --membind=0 redis-server --save "" --daemonize yes
i=0; while [ $i -lt 50 ]; do redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 0.2; i=$((i+1)); done
RPID=$(pidof redis-server | awk '{print $1}')
echo "[wr-point] write_ratio=$W scan_batch=$N redis=$RPID"

stat_get()  { grep "^$1 " "$STATS" 2>/dev/null | awk '{print $2}'; }
# NOTE: /proc/<pid>/numa_maps CANNOT see LtRAM -- node 1 was removed from
# N_MEMORY, and show_numa_map only prints nodes for_each_node_state(N_MEMORY),
# so N1= never appears. We instead use the reliable ltram resident counter:
# the scan only migrates THIS workload, so (currently_resident - at_alloc) is
# the workload's LtRAM page count; VmRSS is its total resident footprint.
rss_pages() { awk '/^VmRSS:/{print int($2/4)}' "/proc/$RPID/status" 2>/dev/null; }

# Scan OFF during the LOAD (prefill = all writes), then scan for a FIXED window
# during the transaction phase -- same wall-clock scan time at every write ratio.
# Otherwise utilization just tracks how long the slow high-write runs happen to
# take (redis KTPS collapses at high write -> longer run -> more scan time).
# ycsbc prints "# Loading records:" to stderr exactly when load finishes.
cmds() { redis-cli info stats 2>/dev/null | awk -F: '/total_commands_processed/{print $2+0}'; }
echo -1 > "$SCANP" 2>/dev/null
: > "$OUT/.err"
sh -c "$YBASE" 2>"$OUT/.err" >/dev/null &
YPID=$!
i=0; while [ $i -lt 2400 ]; do
    grep -q "Loading records" "$OUT/.err" 2>/dev/null && break
    kill -0 "$YPID" 2>/dev/null || break          # ycsbc exited early (failsafe)
    sleep 0.2; i=$((i + 1))
done
echo 1 > "$RESET" 2>/dev/null                     # zero counters at txn start
RES_BEFORE=$(stat_get currently_resident_pages)   # LtRAM baseline after load
CMD0=$(cmds)
echo "$RPID" > "$SCANP" 2>/dev/null               # scan ON for a fixed window
sleep "$SCAN_SECS"                                # equal scan time at every ratio
RES_AFTER=$(stat_get currently_resident_pages)
NT=$(rss_pages); NT=${NT:-0}                       # RSS while redis is still alive
CMD1=$(cmds)
echo -1 > "$SCANP" 2>/dev/null
kill "$YPID" 2>/dev/null                           # stop ycsbc; window is over

N1=$(( ${RES_AFTER:-0} - ${RES_BEFORE:-0} ))      # LtRAM pages added during txns
MI=$(stat_get placed_migrated_in); MB=$(stat_get migrated_back_of_migrated)
# throughput over the window, from redis's own command counter (ycsbc didn't finish)
K=$(awk -v a="${CMD0:-0}" -v b="${CMD1:-0}" -v t="$SCAN_SECS" 'BEGIN{ if(t>0) printf "%.4f",(b-a)/t/1000; else print 0 }')
UTIL=$(awk -v a="$N1" -v b="$NT" 'BEGIN{ if(b>0) printf "%.2f",a/b*100; else print 0 }')
REPAT=$(awk -v i="${MI:-0}" -v b="${MB:-0}" 'BEGIN{ if(i>0) printf "%.2f",b/i*100; else print 0 }')

[ -f "$CSV" ] || cat > "$CSV" <<'HDR'
# per-boot write-ratio sweep -- one independent VM per write ratio
# write_ratio     % of ops that are updates (read = 100 - write)
# utilization_pct workload pages in LtRAM / total resident = (resident delta during txns) / VmRSS
#                 (numa_maps can't see LtRAM: node 1 is out of N_MEMORY, so we use the ltram counter)
# repat_pct       migrated_back / migrated_in * 100
# (each ratio scanned for the SAME fixed wall-clock window so utilization is comparable)
# migrated_in/back ltram counters over the fixed scan window (scan off during prefill/load)
# redis_ltram_pg  resident-in-LtRAM delta over the window; redis_total_pg VmRSS pages
# ktps           throughput over the window (from redis total_commands_processed)
write_ratio,utilization_pct,repat_pct,migrated_in,migrated_back,redis_ltram_pg,redis_total_pg,ktps
HDR
echo "${W},${UTIL},${REPAT},${MI:-0},${MB:-0},${N1},${NT},${K}" >> "$CSV"
echo "[wr-point] util=$UTIL% repat=$REPAT% migrated_in=$MI back=$MB ktps=$K -> $CSV"
