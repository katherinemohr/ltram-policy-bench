#!/bin/sh
# redis LtRAM offload TIMESERIES at a given write ratio (redis analog of the
# pagerank run). Scan OFF during load, ON for a fixed window; samples utilization
# + write-back over time. Write ratio from cmdline ltram_wr= (default 10).
set -u
W=$(sed -n 's/.*ltram_wr=\([0-9]\+\).*/\1/p' /proc/cmdline); W=${W:-10}
DUR=$(sed -n 's/.*ltram_dur=\([0-9]\+\).*/\1/p' /proc/cmdline); DUR=${DUR:-600}
RECS=${RECS:-100000}; OPC=${OPC:-100000000}; DIST=${DIST:-zipfian}
STATS=/sys/kernel/debug/ltram/stats
RESET=/sys/kernel/debug/ltram/reset
SCANP=/sys/kernel/debug/ltram/scan_pid
TRATE=/sys/module/ltram/parameters/token_rate
SPEC=/tmp/redis_ts.spec
PROBE_RPID=""

rp=$(awk "BEGIN{printf \"%.4f\",(100-$W)/100}"); up=$(awk "BEGIN{printf \"%.4f\",$W/100}")
cat > "$SPEC" <<SPECEOF
recordcount=$RECS
operationcount=$OPC
workload=com.yahoo.ycsb.workloads.CoreWorkload
readallfields=true
readproportion=$rp
updateproportion=$up
scanproportion=0
insertproportion=0
requestdistribution=$DIST
SPECEOF

[ -e "$STATS" ] || mount -t debugfs none /sys/kernel/debug 2>/dev/null
ip link set lo up 2>/dev/null
numactl --cpunodebind=0 --membind=0 redis-server --save "" --daemonize yes
i=0; while [ $i -lt 50 ]; do redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 0.2; i=$((i+1)); done
RPID=$(pidof redis-server | awk '{print $1}')
OUT=${LTRAM_RUN_DIR:-/mnt/results/redisrun/$(date +%y%m%d__%H%M%S)}; mkdir -p "$OUT"
TSV="$OUT/redis_timeseries.csv"
YBASE="LD_LIBRARY_PATH=/mnt/workloads /mnt/workloads/YCSB-C/ycsbc -db redis -P $SPEC"

echo 0 > "$TRATE" 2>/dev/null                    # offload ceiling (token off)
stat_get(){ grep "^$1 " "$STATS" 2>/dev/null | awk '{print $2}'; }
rss(){ awk '/^VmRSS:/{print int($2/4)}' "/proc/$RPID/status" 2>/dev/null; }

echo "[redisrun] W=$W% dur=${DUR}s redis=$RPID"
echo -1 > "$SCANP" 2>/dev/null
: > "$OUT/.err"
sh -c "$YBASE" 2>"$OUT/.err" >/dev/null &
YPID=$!
i=0; while [ $i -lt 2400 ]; do grep -q "Loading records" "$OUT/.err" 2>/dev/null && break; kill -0 "$YPID" 2>/dev/null || break; sleep 0.2; i=$((i+1)); done
echo 1 > "$RESET" 2>/dev/null
RES_BEFORE=$(stat_get currently_resident_pages)  # LtRAM baseline (after load, scan off)
echo "$RPID" > "$SCANP" 2>/dev/null              # scan ON for the window

echo "t_s,resident_pg,migrated_in,migrated_back,redis_rss_pg,util_pct,repat_pct" > "$TSV"
t0=$(date +%s); peak=0
while [ $(( $(date +%s) - t0 )) -lt "$DUR" ]; do
    kill -0 "$YPID" 2>/dev/null || break
    RES=$(stat_get currently_resident_pages); MI=$(stat_get placed_migrated_in); MB=$(stat_get migrated_back_of_migrated)
    RSS=$(rss); LT=$(( ${RES:-0} - ${RES_BEFORE:-0} ))
    util=$(awk -v a="$LT" -v b="${RSS:-0}" 'BEGIN{ if(b>0) printf "%.2f",a/b*100; else print 0 }')
    repat=$(awk -v i="${MI:-0}" -v b="${MB:-0}" 'BEGIN{ if(i>0) printf "%.2f",b/i*100; else print 0 }')
    echo "$(( $(date +%s) - t0 )),${RES:-0},${MI:-0},${MB:-0},${RSS:-0},$util,$repat" >> "$TSV"
    awk -v u="$util" -v p="$peak" 'BEGIN{exit !(u>p)}' && peak=$util
    sleep 5
done
echo -1 > "$SCANP" 2>/dev/null
kill "$YPID" 2>/dev/null
echo 42 > "$TRATE" 2>/dev/null

MI=$(stat_get placed_migrated_in); MB=$(stat_get migrated_back_of_migrated)
FR=$(awk -v i="${MI:-0}" -v b="${MB:-0}" 'BEGIN{ if(i>0) printf "%.2f",b/i*100; else print 0 }')
echo ""
echo "=== redis W=$W% : peak utilization=${peak}%  final write-back=${FR}% ==="
echo "migrated_in $MI  migrated_back $MB"
echo "timeseries: ${TSV#/mnt/}  (host: results/)"
