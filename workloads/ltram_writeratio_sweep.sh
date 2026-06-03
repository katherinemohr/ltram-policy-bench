#!/bin/sh
# HEADLINE experiment: LtRAM utilization + repatriation vs write ratio.
#
# For each write ratio w (read = 100-w), against ycsbc/redis with the scanning
# hand on redis, we measure:
#   utilization% = redis pages in LtRAM / all redis pages   (numa_maps N1/(N0+N1))
#                  -> "X% of redis's footprint can move to LtRAM and save DRAM"
#   repat%       = migrated_back / migrated_in              (ltram counters)
#                  -> "of what we moved in, how much bounced back out"
#
# Fresh redis per ratio (clean isolation), token cap OFF so each ratio reaches
# its steady-state offload ceiling within the run (the "how much CAN move" view).
# In one boot. Run as root in the guest.
set -u

RATIOS="${RATIOS:-0 5 10 20 30 40 50 60 70 80 90 100}"
N="${SCAN_N:-4096}"            # fixed scan_batch (big enough to find cold fast)
OPC="${OPC:-300000}"          # ops/run -- long enough for each ratio to converge
RECS=100000

STATS=/sys/kernel/debug/ltram/stats
RESET=/sys/kernel/debug/ltram/reset
SCANP=/sys/kernel/debug/ltram/scan_pid
PARAM=/sys/module/ltram/parameters
OUT="${LTRAM_RUN_DIR:-/mnt/results/writeratio/$(date +%y%m%d__%H%M%S)}"; mkdir -p "$OUT"
CSV="$OUT/writeratio.csv"
YBASE="LD_LIBRARY_PATH=/mnt/workloads /mnt/workloads/YCSB-C/ycsbc -db redis -P /tmp/wr.spec"

[ -e "$STATS" ] || mount -t debugfs none /sys/kernel/debug 2>/dev/null
ip link set lo up 2>/dev/null

echo "$N" > "$PARAM/scan_batch" 2>/dev/null
echo 0   > "$PARAM/token_rate" 2>/dev/null      # offload ceiling (converge fully)
trap 'echo 42 > "$PARAM/token_rate" 2>/dev/null; echo -1 > "$SCANP" 2>/dev/null' EXIT INT TERM

RPID=""
fresh_redis() {
    [ -n "$RPID" ] && { kill "$RPID" 2>/dev/null; sleep 1; }
    numactl --cpunodebind=0 --membind=0 redis-server --save "" --daemonize yes
    i=0; while [ $i -lt 50 ]; do redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 0.2; i=$((i+1)); done
    RPID=$(pidof redis-server | awk '{print $1}')
}
mkspec() {  # $1 = write ratio (0-100)
    rp=$(awk "BEGIN{printf \"%.4f\",(100-$1)/100}")
    up=$(awk "BEGIN{printf \"%.4f\",$1/100}")
    cat > /tmp/wr.spec <<EOF
recordcount=$RECS
operationcount=$OPC
workload=com.yahoo.ycsb.workloads.CoreWorkload
readallfields=true
readproportion=$rp
updateproportion=$up
scanproportion=0
insertproportion=0
requestdistribution=uniform
EOF
}
stat_get() { grep "^$1 " "$STATS" 2>/dev/null | awk '{print $2}'; }
redis_numa() {   # echo "ltram_pages total_pages" for redis (node1 = LtRAM)
    awk '{ for(i=1;i<=NF;i++){ if($i~/^N0=/){split($i,a,"=");n0+=a[2]} if($i~/^N1=/){split($i,a,"=");n1+=a[2]} } }
         END{ print n1+0, n0+n1+0 }' "/proc/$RPID/numa_maps" 2>/dev/null
}

cat > "$CSV" <<'HDR'
# LtRAM utilization + repatriation vs write ratio (scan on redis, token off = offload ceiling)
# write_ratio    % of ops that are updates (read = 100 - write)
# utilization_pct redis pages in LtRAM / all redis pages = N1/(N0+N1) from numa_maps  ("% offloadable")
# repat_pct       migrated_back / migrated_in * 100  ("% of moved-in pages that bounced back")
# migrated_in     pages scan moved DRAM->LtRAM (stats, reset per ratio)
# migrated_back   pages repatriated LtRAM->DRAM (stats, reset per ratio)
# redis_ltram_pg  redis pages resident in LtRAM (numa_maps N1)
# redis_total_pg  redis resident pages total (numa_maps N0+N1)
# ktps            ycsbc transaction throughput
write_ratio,utilization_pct,repat_pct,migrated_in,migrated_back,redis_ltram_pg,redis_total_pg,ktps
HDR

for w in $RATIOS; do
    mkspec "$w"
    fresh_redis
    echo 1 > "$RESET" 2>/dev/null
    echo "$RPID" > "$SCANP" 2>/dev/null
    sh -c "$YBASE" 2>"$OUT/.err" >/dev/null
    K=$(tail -1 "$OUT/.err" | awk '{print $NF}')
    echo -1 > "$SCANP" 2>/dev/null
    set -- $(redis_numa); N1=${1:-0}; NT=${2:-0}
    MI=$(stat_get placed_migrated_in); MB=$(stat_get migrated_back_of_migrated)
    UTIL=$(awk -v a="$N1" -v b="$NT" 'BEGIN{ if(b>0) printf "%.2f",a/b*100; else print 0 }')
    REPAT=$(awk -v i="${MI:-0}" -v b="${MB:-0}" 'BEGIN{ if(i>0) printf "%.2f",b/i*100; else print 0 }')
    echo "${w},${UTIL},${REPAT},${MI:-0},${MB:-0},${N1},${NT},${K}" | tee -a "$CSV"
done

echo ""
echo "=== write-ratio sweep saved: ${CSV#/mnt/} (host: results/) ==="
