#!/bin/sh
# Synthetic clean workload offload timeseries at a write ratio (cmdline ltram_wr=).
set -u
W=$(sed -n 's/.*ltram_wr=\([0-9]\+\).*/\1/p' /proc/cmdline); W=${W:-10}
DUR=$(sed -n 's/.*ltram_dur=\([0-9]\+\).*/\1/p' /proc/cmdline); DUR=${DUR:-300}
MB=${MB:-128}
THETA=${THETA:-0.99}
RATE=$(sed -n 's/.*ltram_rate=\([0-9]*\).*/\1/p' /proc/cmdline); RATE=${RATE:-10000}
STATS=/sys/kernel/debug/ltram/stats; RESET=/sys/kernel/debug/ltram/reset
SCANP=/sys/kernel/debug/ltram/scan_pid; TRATE=/sys/module/ltram/parameters/token_rate
SYNTH=/mnt/workloads/repat_test/ltram_synth
[ -e "$STATS" ] || mount -t debugfs none /sys/kernel/debug 2>/dev/null
OUT=${LTRAM_RUN_DIR:-/mnt/results/synthrun/$(date +%y%m%d__%H%M%S)}; mkdir -p "$OUT"
TSV="$OUT/synth_timeseries.csv"
stat_get(){ grep "^$1 " "$STATS" 2>/dev/null | awk '{print $2}'; }
rss(){ awk '/^VmRSS:/{print int($2/4)}' "/proc/$YPID/status" 2>/dev/null; }
# token_rate comes from the kernel cmdline (run-vm.sh TOKEN_RATE); do not override
echo -1 > "$SCANP" 2>/dev/null
"$SYNTH" "$MB" "$W" "$((DUR + 60))" "$THETA" "$RATE" > "$OUT/.synth" 2>&1 &
YPID=$!
i=0; while [ $i -lt 600 ]; do grep -q "INIT DONE" "$OUT/.synth" 2>/dev/null && break; kill -0 "$YPID" 2>/dev/null || break; sleep 0.2; i=$((i+1)); done
echo "[synth] W=$W% mb=$MB dur=${DUR}s pid=$YPID  ($(cat "$OUT/.synth"))"
echo 1 > "$RESET" 2>/dev/null
RES_BEFORE=$(stat_get currently_resident_pages)
echo "$YPID" > "$SCANP" 2>/dev/null
echo "t_s,resident_pg,migrated_in,migrated_back,rss_pg,util_pct,repat_pct" > "$TSV"
t0=$(date +%s); peak=0
while [ $(( $(date +%s) - t0 )) -lt "$DUR" ]; do
    kill -0 "$YPID" 2>/dev/null || break
    RES=$(stat_get currently_resident_pages); MI=$(stat_get placed_migrated_in); MB=$(stat_get migrated_back_of_migrated)
    RS=$(rss); LT=$(( ${RES:-0} - ${RES_BEFORE:-0} ))
    util=$(awk -v a="$LT" -v b="${RS:-0}" 'BEGIN{ if(b>0) printf "%.2f",a/b*100; else print 0 }')
    repat=$(awk -v i="${MI:-0}" -v b="${MB:-0}" 'BEGIN{ if(i>0) printf "%.2f",b/i*100; else print 0 }')
    echo "$(( $(date +%s) - t0 )),${RES:-0},${MI:-0},${MB:-0},${RS:-0},$util,$repat" >> "$TSV"
    awk -v u="$util" -v p="$peak" 'BEGIN{exit !(u>p)}' && peak=$util
    sleep 5
done
echo -1 > "$SCANP" 2>/dev/null; kill "$YPID" 2>/dev/null;
MI=$(stat_get placed_migrated_in); MB=$(stat_get migrated_back_of_migrated)
FR=$(awk -v i="${MI:-0}" -v b="${MB:-0}" 'BEGIN{ if(i>0) printf "%.2f",b/i*100; else print 0 }')
echo "=== synth W=$W% : peak utilization=${peak}%  final write-back=${FR}% ==="
