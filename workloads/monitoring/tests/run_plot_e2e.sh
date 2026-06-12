#!/bin/bash
# End-to-end: drive the real sweeper against the synthetic target into a run
# directory, then render the new plots from the *actual* emitted CSVs.
# Proves the T02-output -> T03/T07-input contract on one continuous artifact.
#
#   bash workloads/monitoring/tests/run_plot_e2e.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MON="$(dirname "$HERE")"
ROOT="$(cd "$MON/../.." && pwd)"
RUN="_selftest_lifecycle"
RUN_DIR="$ROOT/results/runs/$RUN"
fail() { echo "FAIL: $*" >&2; exit 1; }

( cd "$MON" && make >/dev/null 2>&1 ) || fail "build dirty_sweep"
gcc -O2 -o "$HERE/lifecycle_target" "$HERE/lifecycle_target.c" || fail "build target"
rm -rf "$RUN_DIR"; mkdir -p "$RUN_DIR"

"$HERE/lifecycle_target" 2>"$RUN_DIR/target.err" &
TPID=$!
sleep 0.2
grep -q rw-p "/proc/$TPID/maps" 2>/dev/null || { wait "$TPID"; fail "target maps unreadable"; }
"$MON/dirty_sweep" "$TPID" "$RUN_DIR/dirty_sweep.csv" 100 2>"$RUN_DIR/sweep.err"
wait "$TPID" 2>/dev/null

[ -f "$RUN_DIR/dirty_sweep_lifecycle.csv" ] || fail "no lifecycle CSV"

python3 "$MON/dirty_lifecycle_plot.py" selftest "$RUN" run || fail "lifecycle plot crashed"
[ -f "$RUN_DIR/dirty_lifecycle_run.png" ] || fail "no dirty_lifecycle_run.png produced"
echo "PASS e2e: rendered dirty_lifecycle_run.png from real sweeper output"

# Eligibility (added once dirty_eligibility_plot.py exists)
if [ -f "$MON/dirty_eligibility_plot.py" ] && [ -f "$RUN_DIR/dirty_sweep_stability.csv" ]; then
    python3 "$MON/dirty_eligibility_plot.py" selftest "$RUN" run || fail "eligibility plot crashed"
    ls "$RUN_DIR"/dirty_eligibility_*_run.png >/dev/null 2>&1 \
        || fail "no dirty_eligibility_*_run.png produced"
    echo "PASS e2e: rendered eligibility figures from real sweeper output"
fi

echo "Run dir: $RUN_DIR"
