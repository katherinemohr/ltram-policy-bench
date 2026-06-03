#!/bin/bash
# Per-boot write-ratio sweep: one INDEPENDENT VM per write ratio (matches the
# per-boot scan_batch methodology). Fixed large N so coverage is never the
# bottleneck; token off = offload ceiling. Each boot appends one row.
#
# Usage:  scripts/sweep_writeratio_perboot.sh
# Tune:   RATIOS="0 25 50 75 100"  SCAN_N=4096  scripts/sweep_writeratio_perboot.sh
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; BASE="$(dirname "$HERE")"
RATIOS="${RATIOS:-0 5 10 25 50 100}"
SCAN_N="${SCAN_N:-4096}"          # large -> coverage not a confound
OUT="$BASE/results/writeratio"; mkdir -p "$OUT"
TS="$(date +%y%m%d__%H%M%S)"; SWEEP_DIR="$OUT/$TS"; mkdir -p "$SWEEP_DIR"
CSV="$SWEEP_DIR/writeratio.csv"
echo "$TS" > "$OUT/.sweep_dir"
trap 'rm -f "$OUT/.sweep_dir"' EXIT

echo "per-boot write-ratio sweep: RATIOS=[$RATIOS] scan_batch=$SCAN_N -> $CSV"
for w in $RATIOS; do
    echo ""; echo "########### boot: write_ratio=$w (scan_batch=$SCAN_N) ###########"
    WR="$w" SCAN_BATCH="$SCAN_N" TOKEN_RATE=0 bash "$HERE/run-vm.sh" writeratiopoint
done
echo ""; echo "=== per-boot write-ratio sweep complete: $CSV ==="
grep -v '^#' "$CSV" 2>/dev/null
python3 "$HERE/plot_writeratio.py" "$CSV" "$SWEEP_DIR/writeratio.png" 2>/dev/null \
    && echo "plot: $SWEEP_DIR/writeratio.png" || true
