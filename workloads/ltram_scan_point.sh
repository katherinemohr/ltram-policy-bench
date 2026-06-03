#!/bin/sh
# ONE clean scan-point per VM boot (for the per-boot scan_batch sweep -- each N
# is an independent VM, so there is zero cross-run carryover in `resident`).
#
# scan_batch and token_rate come from the kernel cmdline (ltram.scan_batch=,
# ltram.token_rate=), set by run-vm.sh. We measure a scan-OFF baseline then a
# scan-ON run against ycsbc/redis and APPEND one row to
# /mnt/results/scanboot/sweep.csv (the host writes the header once).
#
# resident is reported as a DELTA (after-scan minus after-baseline) so it counts
# only what the scan added to LtRAM this run -- not the at-alloc routing baseline
# or any carryover (there is none, fresh boot).
set -u

# Long, pure-read spec so every N converges (fair N isolation); override as arg 1.
SPEC=${1:-/mnt/workloads/YCSB-C/workloads/workload_sweep.spec}
STATS=/sys/kernel/debug/ltram/stats
RESET=/sys/kernel/debug/ltram/reset
SCANP=/sys/kernel/debug/ltram/scan_pid
# Append into THIS sweep's timestamped dir. The host writes scanboot/.sweep_dir
# with the run's YYMMDD__HHMMSS before booting; a standalone run makes its own.
# So re-running a sweep never clobbers a previous one.
SWEEP=$(cat /mnt/results/scanboot/.sweep_dir 2>/dev/null)
[ -n "$SWEEP" ] || SWEEP=$(date +%y%m%d__%H%M%S)
OUT=/mnt/results/scanboot/$SWEEP; mkdir -p "$OUT"; CSV="$OUT/sweep.csv"
YCSBC="LD_LIBRARY_PATH=/mnt/workloads /mnt/workloads/YCSB-C/ycsbc -db redis -P $SPEC"

[ -e "$STATS" ] || mount -t debugfs none /sys/kernel/debug 2>/dev/null
ip link set lo up 2>/dev/null

NBATCH=$(cat /sys/module/ltram/parameters/scan_batch 2>/dev/null)
TRATE=$(cat /sys/module/ltram/parameters/token_rate 2>/dev/null)

# redis up, and WAIT until it actually answers (fixes the start race that left
# scan_pid empty in the single-boot sweep).
pidof redis-server >/dev/null 2>&1 || \
    numactl --cpunodebind=0 --membind=0 redis-server --save "" --daemonize yes
i=0; while [ $i -lt 50 ]; do
    redis-cli ping 2>/dev/null | grep -q PONG && break
    sleep 0.2; i=$((i + 1))
done
RPID=$(pidof redis-server | awk '{print $1}')

SKPID=""
for p in /proc/[0-9]*; do
    [ "$(cat "$p/comm" 2>/dev/null)" = ltram_scan ] && { SKPID=${p#/proc/}; break; }
done
echo "[scanpoint] N=$NBATCH token_rate=$TRATE redis=$RPID scan_kthread=${SKPID:-none}"

stat_get() { grep "^$1 " "$STATS" 2>/dev/null | awk '{print $2}'; }
scan_cpu() { [ -n "$SKPID" ] && awk '{print $14+$15}' "/proc/$SKPID/stat" 2>/dev/null || echo 0; }
run_ktps() { sh -c "$YCSBC" 2>"$OUT/.err" >/dev/null; tail -1 "$OUT/.err" | awk '{print $NF}'; }

# --- baseline: scan OFF ---
echo -1 > "$SCANP" 2>/dev/null
echo 1  > "$RESET" 2>/dev/null
BASE=$(run_ktps)
RES_BEFORE=$(stat_get currently_resident_pages)

# --- scan ON (scan_batch already set from cmdline) ---
echo 1 > "$RESET" 2>/dev/null
C0=$(scan_cpu)
echo "$RPID" > "$SCANP" 2>/dev/null
K=$(run_ktps)
echo -1 > "$SCANP" 2>/dev/null
CPU=$(awk -v a="$C0" -v b="$(scan_cpu)" 'BEGIN{ printf "%.2f",(b-a)/100 }')
RES_AFTER=$(stat_get currently_resident_pages)
MI=$(stat_get placed_migrated_in); MB=$(stat_get migrated_back_of_migrated)
OVER=$(awk -v b="$BASE" -v k="$K" 'BEGIN{ if(b+0>0) printf "%.2f",(b-k)/b*100; else print "NA" }')
RESD=$(( ${RES_AFTER:-0} - ${RES_BEFORE:-0} ))

# Write the self-documenting header once (first boot of the sweep / standalone).
[ -f "$CSV" ] || cat > "$CSV" <<'HDR'
# per-boot scan_batch sweep -- one independent VM per N (no cross-run carryover)
# scan_batch     N: PTEs the scan kthread visits per ~100ms wake (kernel cmdline ltram.scan_batch)
# token_rate     endurance budget in effect this boot (0 = unlimited for the sweep; 42 = realistic)
# base_ktps      scan-OFF ycsbc throughput measured IN THIS boot (per-boot baseline)
# ktps           scan-ON ycsbc throughput
# overhead_pct   (base_ktps-ktps)/base_ktps*100  -- user-visible cost
# scan_cpu_s     ltram_scan kthread CPU seconds during the scan run -- background cost
# migrated_in    pages scan moved DRAM->LtRAM (stats:placed_migrated_in)
# migrated_back  pages bounced LtRAM->DRAM on write (stats:migrated_back_of_migrated; lower=better)
# resident_delta LtRAM frames the scan added = resident(after scan) - resident(after baseline)
scan_batch,token_rate,base_ktps,ktps,overhead_pct,scan_cpu_s,migrated_in,migrated_back,resident_delta
HDR
echo "${NBATCH},${TRATE},${BASE},${K},${OVER},${CPU},${MI:-0},${MB:-0},${RESD}" >> "$CSV"
echo "[scanpoint] ktps=$K base=$BASE over=$OVER% migrated_in=$MI back=$MB resident_delta=$RESD -> $CSV"
