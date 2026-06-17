"""
Unit tests for dirty_lifecycle_plot's C03 state grid and D09 sort (T03/T04).
Run: python3 workloads/monitoring/tests/test_lifecycle_plot.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import dirty_lifecycle_plot as L

F, NA, FR, CI, CF, W = (L.STATE_CLEAN_FINAL, L.STATE_NOT_ALLOC, L.STATE_FREED,
                        L.STATE_CLEAN_INTER, L.STATE_CLEAN_FINAL, L.STATE_WRITTEN)
NA, FR, CI, CF, W = (L.STATE_NOT_ALLOC, L.STATE_FREED, L.STATE_CLEAN_INTER,
                     L.STATE_CLEAN_FINAL, L.STATE_WRITTEN)


def eq(got, exp, msg):
    got = list(int(x) for x in got)
    assert got == exp, f"{msg}\n  got={got}\n  exp={exp}"


# Case A: alloc at 2, freed after 7, writes at 2 and 5 -> final clean run 6..7
eq(L.build_state_row(2, 7, [2, 5], 10),
   [NA, NA, W, CI, CI, W, CF, CF, FR, FR],
   "A: alloc/free boundaries + intermediate vs final clean")

# Case B: incarnation ends on a write -> no final clean run (RK-3)
eq(L.build_state_row(0, 4, [4], 6),
   [CI, CI, CI, CI, W, FR],
   "B: ends-on-write has empty CLEAN_FINAL region")

# Case C: no writes -> whole span is the final clean run
eq(L.build_state_row(1, 3, [], 5),
   [NA, CF, CF, CF, FR],
   "C: never-written incarnation is all final-clean")

# Case D: present to the very end -> no FREED cells
eq(L.build_state_row(0, 9, [], 10),
   [CF] * 10,
   "D: resident-through-end has no freed region")

# longest_clean_run
assert L.longest_clean_run(2, 7, [2, 5]) == 2, "lcr A"
assert L.longest_clean_run(1, 3, []) == 3, "lcr none"
assert L.longest_clean_run(0, 4, [4]) == 4, "lcr trailing-write (pre-write run)"

# D09 sort: same first_seen -> longer clean run first; then vma_start, vpage
recs = [
    {"first_seen": 0, "last_seen": 9, "events": [0, 5], "vma_start": 0x10, "vpage_idx": 0},  # lcr=4
    {"first_seen": 0, "last_seen": 9, "events": [],      "vma_start": 0x20, "vpage_idx": 0},  # lcr=10
    {"first_seen": 3, "last_seen": 9, "events": [],      "vma_start": 0x10, "vpage_idx": 0},  # later alloc
    {"first_seen": 0, "last_seen": 9, "events": [0, 5], "vma_start": 0x08, "vpage_idx": 0},  # lcr=4, lower addr
]
order = sorted(range(len(recs)), key=lambda i: L.sort_key(recs[i]))
# expected: rec1 (fs0,lcr10), then rec3 (fs0,lcr4,addr0x08), then rec0 (fs0,lcr4,addr0x10), then rec2 (fs3)
assert order == [1, 3, 0, 2], f"D09 sort order wrong: {order}"

print("PASS test_lifecycle_plot: C03 grid + D09 sort")


# === "full" merge / boundary stitching (load + run on one timeline) ==========

def rec(vma_start, vpage, first_seen, last_seen, events, perms="rw-p", path=""):
    return {"vma_start": vma_start, "vpage_idx": vpage, "vma_perms": perms,
            "vma_path": path, "first_seen": first_seen, "last_seen": last_seen,
            "events": events}


def find(merged, vma_start, vpage, first_seen):
    for m in merged:
        if (m["vma_start"], m["vpage_idx"], m["first_seen"]) == (vma_start, vpage, first_seen):
            return m
    raise AssertionError(f"no merged rec for {vma_start:#x}/{vpage}/fs={first_seen}")


load_ts = 4  # load timeline is sweeps 0..3

# Page P1: present throughout both phases -> ONE stitched incarnation.
#   load: present 0..3 (last load sweep), write at 1
#   run : present 0..5, write at 2     (offset -> first_seen 4, write at 6)
# Page P2: load-only, freed mid-load (last_seen 2 != load_ts-1) -> stays load-only.
# Page P3: run-only, appears at run sweep 1 -> offset to first_seen 5.
load_recs = [rec(0x10, 0, 0, 3, [1]), rec(0x20, 0, 0, 2, [])]
run_recs  = [rec(0x10, 0, 0, 5, [2], perms="r--p", path="/lib/x"),
             rec(0x30, 0, 1, 5, [3])]

merged = L._stitch_full(load_recs, load_ts, run_recs)
assert len(merged) == 3, f"expected 3 merged incarnations, got {len(merged)}"

p1 = find(merged, 0x10, 0, 0)
assert p1["last_seen"] == 5 + load_ts, f"P1 stitched last_seen wrong: {p1['last_seen']}"
assert p1["events"] == [1, 4 + 2], f"P1 stitched events wrong: {p1['events']}"
assert p1["vma_perms"] == "r--p", "P1 should take run-side (final) perms"
assert p1["vma_path"] == "/lib/x", "P1 should take run-side (final) path"

p2 = find(merged, 0x20, 0, 0)
assert p2["last_seen"] == 2 and p2["events"] == [], "P2 load-only unchanged"

p3 = find(merged, 0x30, 0, 5)  # 1 + load_ts
assert p3["last_seen"] == 5 + load_ts and p3["events"] == [3 + load_ts], "P3 offset"

# load_lifecycle_full meta math (no stitch dependence): total_sweeps adds up.
total = load_ts + 6  # run timeline length 6
assert max(m["last_seen"] for m in merged) < total, "merged stays within full timeline"

print("PASS test_lifecycle_plot: full-merge boundary stitching")
