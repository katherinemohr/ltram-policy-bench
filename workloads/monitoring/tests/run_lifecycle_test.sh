#!/bin/bash
# Runtime test for the dirty_sweep incarnation instrumentation (T01/T02).
#
# Asserts:
#   * the target's reused anonymous region yields >=2 incarnations at the same
#     vpage, with a residency gap (inc2.first_seen > inc1.last_seen + 1)      [C02]
#   * every emitted incarnation is internally gap-free (last>=first)          [I01]
#   * dirty_sweep_lifecycle.csv header/schema is exactly C01
#   * dirty_sweep.csv schema (column header) is byte-identical to the
#     pre-change binary built from git HEAD                                   [R03/I02]
#
# Run from anywhere: bash workloads/monitoring/tests/run_lifecycle_test.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MON="$(dirname "$HERE")"
ROOT="$(cd "$MON/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

# --- build post-change sweeper, pre-change sweeper (HEAD), and the target ---
( cd "$MON" && make >/dev/null 2>&1 ) || fail "build post-change dirty_sweep"
gcc -O2 -o "$TMP/lifecycle_target" "$HERE/lifecycle_target.c" || fail "build target"
git -C "$ROOT" show HEAD:workloads/monitoring/dirty_sweep.c > "$TMP/orig.c" \
    || fail "fetch HEAD dirty_sweep.c"
gcc -O2 -o "$TMP/dirty_sweep_orig" "$TMP/orig.c" 2>/dev/null \
    || fail "build pre-change dirty_sweep"

run_sweep() {  # $1 = sweeper binary, $2 = output csv
    "$TMP/lifecycle_target" 2>"$TMP/target.err" &
    local tpid=$!
    # confirm we have the real target (its maps must contain the anon region)
    sleep 0.2
    grep -q rw-p "/proc/$tpid/maps" 2>/dev/null || { wait "$tpid"; fail "target $tpid maps unreadable"; }
    "$1" "$tpid" "$2" 100 2>>"$TMP/sweep.err"
    wait "$tpid" 2>/dev/null
}

# --- post-change run (the one we assert incarnations on) ---
run_sweep "$MON/dirty_sweep" "$TMP/post.csv"
LIFE="$TMP/post_lifecycle.csv"
[ -f "$LIFE" ] || fail "no lifecycle CSV produced"

# --- pre-change run (schema baseline only) ---
run_sweep "$TMP/dirty_sweep_orig" "$TMP/pre.csv"

# --- schema check: dirty_sweep.csv column header byte-identical (R03/I02) ---
post_hdr="$(sed -n 2p "$TMP/post.csv")"
pre_hdr="$(sed -n 2p "$TMP/pre.csv")"
[ "$post_hdr" = "$pre_hdr" ] || fail "dirty_sweep.csv column header changed:
  pre : $pre_hdr
  post: $post_hdr"
echo "PASS schema: dirty_sweep.csv column header unchanged vs HEAD"

# --- C01 lifecycle header check ---
exp_cols="vma_start,vma_end,vma_perms,vma_path,vpage_idx,incarnation_idx,first_seen,last_seen,present_count,dirty_count,write_events"
[ "$(sed -n 2p "$LIFE")" = "$exp_cols" ] || fail "lifecycle column header != C01"
sed -n 1p "$LIFE" | grep -qE '^# total_sweeps=[0-9]+ total_seconds=[0-9.]+ interval_ms=[0-9]+ pid=[0-9]+$' \
    || fail "lifecycle metadata header != C01"
echo "PASS schema: lifecycle CSV header matches C01"

# --- incarnation structure assertions (C02 / I01) ---
python3 - "$LIFE" <<'PY' || exit 1
import sys, csv
from collections import defaultdict
path=sys.argv[1]
rows=defaultdict(list)
with open(path) as f:
    r=csv.reader(f)
    for c in r:
        if not c or c[0].startswith('#') or c[0]=='vma_start': continue
        rows[(c[0],int(c[4]))].append(c)
# I01: every incarnation contiguous (last_seen >= first_seen) and present_count>=1
for k,v in rows.items():
    for c in v:
        fs,ls,pc=int(c[6]),int(c[7]),int(c[8])
        assert ls>=fs, f"I01 violated: last<first for {k}: {c}"
        assert pc>=1, f"present_count<1 emitted for {k}: {c}"
# C02: at least one writable page has >=2 incarnations with a real gap
multi=[(k,v) for k,v in rows.items() if len(v)>=2 and v[0][2][1]=='w']
assert multi, "no writable page produced >=2 incarnations (expected from MAP_FIXED remap)"
gap_ok=0
for k,v in multi:
    v=sorted(v,key=lambda c:int(c[5]))  # by incarnation_idx
    for a,b in zip(v,v[1:]):
        a_last=int(a[7]); b_first=int(b[6])
        # incarnation_idx strictly increasing, and a real residency gap
        assert int(b[5])==int(a[5])+1, f"incarnation_idx not contiguous for {k}"
        if b_first > a_last+1: gap_ok+=1
assert gap_ok>0, "two incarnations found but no residency gap between them"
print(f"PASS incarnation: {len(multi)} writable pages with >=2 incarnations, "
      f"{gap_ok} gap-separated boundaries (C02), all contiguous (I01)")
PY

echo "ALL LIFECYCLE TESTS PASSED"
