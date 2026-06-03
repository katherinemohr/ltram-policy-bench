#!/bin/bash
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; BASE="$(dirname "$HERE")"
NLIST="${NLIST:-0 20 40 60 80 100}"; DUR="${DUR:-300}"
OUT="$BASE/results/synthsweep"; mkdir -p "$OUT"
TS="$(date +%y%m%d__%H%M%S)"; SDIR="$OUT/$TS"; mkdir -p "$SDIR"; CSV="$SDIR/synthsweep.csv"
echo "# synthetic zipfian workload: util + write-back vs write ratio (op-level writes, clean reads)" > "$CSV"
echo "write_ratio,peak_util_pct,final_writeback_pct" >> "$CSV"
for w in $NLIST; do
    echo ""; echo "########## synth W=$w% ##########"
    WR="$w" DUR="$DUR" bash "$HERE/run-vm.sh" synthrun
    D=$(ls -td "$BASE"/results/synthrun/*/ 2>/dev/null | head -1)
    line=$(grep "peak utilization" "$D/run.log" 2>/dev/null | tail -1)
    util=$(echo "$line" | sed -n 's/.*utilization=\([0-9.]*\)%.*/\1/p')
    wb=$(echo "$line" | sed -n 's/.*write-back=\([0-9.]*\)%.*/\1/p')
    echo "$w,${util:-NA},${wb:-NA}" >> "$CSV"; echo "  -> $w,${util:-NA},${wb:-NA}"
done
echo "=== synth sweep done: $CSV ==="; grep -v '^#' "$CSV"
python3 "$HERE/plot_synthsweep.py" "$CSV" "$SDIR/synthsweep.png" 2>/dev/null && echo "plot: $SDIR/synthsweep.png" || true
