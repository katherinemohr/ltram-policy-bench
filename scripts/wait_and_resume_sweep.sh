#!/bin/bash
# Detached helper: wait for the user's prefetch_pagerank_test to FULLY finish
# (all 4 pr runs done + host idle => script exited + reverted the prefetcher),
# then run the remaining 10% synth-sweep points (50/70/90) and merge+plot.
set -u
cd /scratch/hsshim/ltram-policy-bench
CSV=results/synthsweep/full10pct_260603__085112/synthsweep.csv
PLOT=results/synthsweep/full10pct_260603__085112/synthsweep_10pct.png
LOG=results/synthsweep/full10pct_260603__085112/resume.log
exec >>"$LOG" 2>&1
echo "=== waiter started $(date '+%F %T') ==="

# ---- phase 1: wait for the prefetch test to finish ----
# done = its pagerank.csv has 4 data rows (A/B x on/off all run) AND qemu idle,
# confirmed across two checks 30s apart (so the script's revert has run).
idle_ok=0
for i in $(seq 1 150); do            # cap ~75 min
    PT=$(ls -dt results/perf/prefetch_test_* 2>/dev/null | head -1)
    rows=$(grep -cE '^(A_local|B_remote),' "$PT/pagerank.csv" 2>/dev/null)
    rows=${rows:-0}
    qemu=$(pgrep -xc qemu-system-x86_64 2>/dev/null); qemu=${qemu:-0}
    echo "[$(date '+%T')] wait: rows=$rows qemu=$qemu idle_ok=$idle_ok ($PT)"
    if [ "$rows" -ge 4 ] && [ "$qemu" -eq 0 ]; then
        idle_ok=$((idle_ok + 1))
        [ "$idle_ok" -ge 2 ] && break    # idle confirmed twice => fully done+reverted
    else
        idle_ok=0
    fi
    sleep 30
done

if [ "${idle_ok:-0}" -lt 2 ]; then
    echo "!! gave up waiting (prefetch test not confirmed complete). NOT running sweep."
    echo "=== waiter exit $(date '+%F %T') ==="
    exit 1
fi
echo "=== prefetch test complete; resuming sweep 50/70/90 at $(date '+%T') ==="

# ---- phase 2: run the 3 remaining points, merge, sort, plot ----
for w in 50 70 90; do
    echo "########## synth W=$w% (token off) ##########"
    TOKEN_RATE=0 RATE=10000 WR=$w DUR=300 bash scripts/run-vm.sh synthrun
    D=$(ls -td results/synthrun/*/ | head -1)
    util=$(awk -F, 'NR>1 && $1>120 {s+=$6;n++} END{if(n)printf "%.2f",s/n; else print "NA"}' "$D/synth_timeseries.csv")
    wb=$(awk -F,   'NR>1 && $1>120 {s+=$7;n++} END{if(n)printf "%.2f",s/n; else print "NA"}' "$D/synth_timeseries.csv")
    echo "$w,${util:-NA},${wb:-NA}" >> "$CSV"
    echo "  -> W=$w util=$util wb=$wb"
done
# keep header (2 lines) then numeric-sort the data rows
{ head -2 "$CSV"; tail -n +3 "$CSV" | sort -t, -k1,1n; } > "$CSV.s" && mv "$CSV.s" "$CSV"
echo "=== FINAL 10% CURVE ==="; cat "$CSV"
python3 scripts/plot_synthsweep.py "$CSV" "$PLOT" 2>&1 | tail -1
echo "=== SWEEP COMPLETE $(date '+%F %T') -> $PLOT ==="
