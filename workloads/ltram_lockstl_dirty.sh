#!/bin/sh
# Does YCSB-C's in-process lock_stl store churn its DATA heap on pure reads
# (like redis), or stay clean (like pagerank)? Probe ycsbc's own pages.
set -u
W=$(sed -n 's/.*ltram_wr=\([0-9]\+\).*/\1/p' /proc/cmdline); W=${W:-0}
PROBE_SECS=$(sed -n 's/.*ltram_dur=\([0-9]\+\).*/\1/p' /proc/cmdline); PROBE_SECS=${PROBE_SECS:-60}
RECS=${RECS:-100000}; DIST=${DIST:-zipfian}
SPEC=/tmp/lockstl.spec; PROBE=/mnt/workloads/repat_test/redis_dirty_probe
rp=$(awk "BEGIN{printf \"%.4f\",(100-$W)/100}"); up=$(awk "BEGIN{printf \"%.4f\",$W/100}")
cat > "$SPEC" <<SPECEOF
recordcount=$RECS
operationcount=100000000
workload=com.yahoo.ycsb.workloads.CoreWorkload
readallfields=true
readproportion=$rp
updateproportion=$up
scanproportion=0
insertproportion=0
requestdistribution=$DIST
SPECEOF
OUT=${LTRAM_RUN_DIR:-/mnt/results/lockstldirty/$(date +%y%m%d__%H%M%S)}; mkdir -p "$OUT"
echo "[lockstl] write_ratio=$W%  probe=${PROBE_SECS}s"
# lock_stl lives IN the ycsbc process (no server); probe that pid.
YBASE="LD_LIBRARY_PATH=/mnt/workloads /mnt/workloads/YCSB-C/ycsbc -db lock_stl -P $SPEC -threads 1"
sh -c "$YBASE" 2>"$OUT/.err" >/dev/null &
YPID=$!
i=0; while [ $i -lt 2400 ]; do grep -q "Loading records" "$OUT/.err" 2>/dev/null && break; kill -0 "$YPID" 2>/dev/null || break; sleep 0.2; i=$((i+1)); done
echo "[lockstl] load done; probing ${PROBE_SECS}s of PURE reads on ycsbc pid $YPID"
"$PROBE" "$YPID" "$PROBE_SECS" | tee "$OUT/lockstl_probe.txt"
kill "$YPID" 2>/dev/null
