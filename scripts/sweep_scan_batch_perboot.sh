#!/bin/bash
# Per-boot scan_batch sweep: one INDEPENDENT VM per N, so there is zero
# cross-run carryover in `resident` and redis always comes up clean. Slower than
# the in-guest sweep, but bulletproof -- the right way to get defensible per-N
# numbers. Each boot runs ltram_scan_point.sh, which appends one row to
# results/scanboot/sweep.csv.
#
# Usage:    scripts/sweep_scan_batch_perboot.sh
# Tune:     NLIST="256 512 1024 2048 4096"  scripts/sweep_scan_batch_perboot.sh
#           TOKEN_RATE=42 scripts/sweep_scan_batch_perboot.sh   # realistic endurance
set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(dirname "$HERE")"
# Default N sweep is COARSE. With the long pure-read spec every N converges to
# the same placement, so fine resolution adds hours for no extra signal (the
# differentiator is overhead, which is smooth). Each boot now runs ~2x90s ycsbc
# (~4 min/boot), so 7 points ~= 30 min. For a fine sweep: N_STEP=128 (overnight).
N_MIN="${N_MIN:-128}"; N_MAX="${N_MAX:-4096}"; N_STEP="${N_STEP:-}"
if [ -n "$N_STEP" ]; then
    NLIST="${NLIST:-$(seq "$N_MIN" "$N_STEP" "$N_MAX")}"
else
    NLIST="${NLIST:-128 256 512 1024 2048 3072 4096}"
fi
TOKEN_RATE="${TOKEN_RATE:-0}"          # 0 = isolate scan_batch; 42 = realistic
OUT="$BASE/results/scanboot"; mkdir -p "$OUT"
# Each sweep run gets its own timestamped dir so re-running never clobbers a
# prior run. The guest reads scanboot/.sweep_dir to append into this run's dir;
# the header is written by the first boot. Marker is cleared on exit.
TS="$(date +%y%m%d__%H%M%S)"
SWEEP_DIR="$OUT/$TS"; mkdir -p "$SWEEP_DIR"
CSV="$SWEEP_DIR/sweep.csv"
echo "$TS" > "$OUT/.sweep_dir"
trap 'rm -f "$OUT/.sweep_dir"' EXIT

echo "per-boot sweep: NLIST=[$NLIST]  token_rate=$TOKEN_RATE  -> $CSV"
for N in $NLIST; do
    echo ""
    echo "############ boot: scan_batch=$N (token_rate=$TOKEN_RATE) ############"
    SCAN_BATCH="$N" TOKEN_RATE="$TOKEN_RATE" bash "$HERE/run-vm.sh" scanpoint
done

echo ""
echo "=== per-boot sweep complete: $CSV ==="
grep -v '^#' "$CSV" 2>/dev/null
python3 "$HERE/plot_scansweep.py" "$CSV" "$SWEEP_DIR/scansweep.png" 2>/dev/null \
    && echo "plot: $SWEEP_DIR/scansweep.png" || true
