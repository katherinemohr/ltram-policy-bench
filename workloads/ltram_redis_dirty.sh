#!/bin/sh
# Root-cause diagnostic: what writes redis's pages? Two soft-dirty probes on the
# SAME loaded redis, back to back (no reload between):
#   1. READS : 30s while pure-read YCSB hammers it    -> read-induced + background
#   2. IDLE  : 30s with the client killed (redis idle) -> background only
# (reads - idle) = the writes actually caused by serving reads. If IDLE is also
# high, it's redis's own housekeeping (dict rehash / cron / jemalloc), not reads.
set -u
RECS=${RECS:-100000}; OPC=${OPC:-5000000}; PROBE_SECS=${PROBE_SECS:-30}
DIST=${DIST:-zipfian}; SPEC=/tmp/ro.spec
PROBE=/mnt/workloads/repat_test/redis_dirty_probe
cat > "$SPEC" <<SPECEOF
recordcount=$RECS
operationcount=$OPC
workload=com.yahoo.ycsb.workloads.CoreWorkload
readallfields=true
readproportion=1.0
updateproportion=0
scanproportion=0
insertproportion=0
requestdistribution=$DIST
SPECEOF
ip link set lo up 2>/dev/null
numactl --cpunodebind=0 --membind=0 redis-server --save "" --daemonize yes
i=0; while [ $i -lt 50 ]; do redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 0.2; i=$((i+1)); done
RPID=$(pidof redis-server | awk '{print $1}')
OUT=${LTRAM_RUN_DIR:-/mnt/results/redisdirty/$(date +%y%m%d__%H%M%S)}; mkdir -p "$OUT"
YBASE="LD_LIBRARY_PATH=/mnt/workloads /mnt/workloads/YCSB-C/ycsbc -db redis -P $SPEC"

sh -c "$YBASE" 2>"$OUT/.err" >/dev/null &
YPID=$!
i=0; while [ $i -lt 2400 ]; do grep -q "Loading records" "$OUT/.err" 2>/dev/null && break; kill -0 "$YPID" 2>/dev/null || break; sleep 0.2; i=$((i+1)); done

echo "[redisdirty] === probe 1: ${PROBE_SECS}s of PURE READS (redis pid $RPID) ==="
"$PROBE" "$RPID" "$PROBE_SECS" | tee "$OUT/probe_reads.txt"

kill "$YPID" 2>/dev/null; sleep 3        # stop the client, let redis quiesce
echo ""
echo "[redisdirty] === probe 2: ${PROBE_SECS}s IDLE (no client) ==="
"$PROBE" "$RPID" "$PROBE_SECS" | tee "$OUT/probe_idle.txt"

echo ""
RD=$(awk '/HARD ceiling/{gsub(/[()%]/,"",$5);print $5}' "$OUT/probe_reads.txt")
ID=$(awk '/HARD ceiling/{gsub(/[()%]/,"",$5);print $5}' "$OUT/probe_idle.txt")
echo "[redisdirty] dirty% under READS=$RD  IDLE=$ID  -> read-induced ≈ (READS - IDLE)"
