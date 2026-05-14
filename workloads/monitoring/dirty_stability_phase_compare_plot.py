"""
Compare load vs run vs full-deployment endurance/utility curves.

Rhetorical question this answers: how much of the full-deployment endurance
cost is the unavoidable load-phase misplacement (cold start — no historical
info available for the OS to make a placement decision), vs the steady-state
run phase that a migration policy can actually optimize?

Reads:
  dirty_sweep_load_stability.csv   load phase epoch histogram (required)
  dirty_sweep_stability.csv        run  phase epoch histogram (required)
  dirty_sweep_load.csv             load phase per-page (for top panel)
  dirty_sweep.csv                  run  phase per-page (for top panel)

If load_stability.csv is missing (matmul, or pre-split runs) the plot is
skipped silently — there's nothing to compare against.

Output: dirty_stability_phase_compare.png  (mirrors costben_normalized layout)
  Top    % pages on LtRAM at end-of-phase, run-only vs load-only
  Bottom 5y endurance %, full (load+run merged) vs load-only vs run-only

The "full" stability histogram is computed by element-wise summing the load
and run histograms. This is exact for non-boundary epochs; boundary epochs
(a page that was clean at end-of-load and rewritten during run) get
approximated — bounded by ~n_pages epochs, doesn't change the qualitative
shape of the comparison.

Usage: dirty_stability_phase_compare_plot.py <workload> <run_name>
"""

import sys
import re
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

workload = sys.argv[1]
run_name = sys.argv[2]
out_dir = RESULTS_DIR / "runs" / run_name

run_stab_csv   = out_dir / "dirty_sweep_stability.csv"
load_stab_csv  = out_dir / "dirty_sweep_load_stability.csv"
run_pages_csv  = out_dir / "dirty_sweep.csv"
load_pages_csv = out_dir / "dirty_sweep_load.csv"

if not load_stab_csv.exists() or not run_stab_csv.exists():
    print(f"phase_compare: skipping — need both {load_stab_csv.name} and "
          f"{run_stab_csv.name} (run wasn't phase-split, or load phase exited "
          f"early — no comparison to make)")
    sys.exit(0)


def load_stability(path):
    with open(path) as f:
        header = f.readline().strip()
    m = re.search(r"total_sweeps=(\d+)\s+total_seconds=([\d.]+)\s+interval_ms=(\d+)",
                  header)
    df = pd.read_csv(path, comment="#")
    L = df["stability_period_sweeps"].values.astype(np.int64)
    C = df["count"].values.astype(np.float64)
    F = (df["final_count"].values.astype(np.float64)
         if "final_count" in df.columns else np.zeros_like(C))
    return {
        "total_sweeps":  int(m.group(1)),
        "total_seconds": float(m.group(2)),
        "interval_ms":   int(m.group(3)),
        "sec_per_sweep": int(m.group(3)) / 1000.0,
        "L": L, "C": C, "F": F, "I": C - F,
    }


load_d = load_stability(load_stab_csv)
run_d  = load_stability(run_stab_csv)

# === Build "full" combined stability data by merging histograms ===
all_L = np.union1d(load_d["L"], run_d["L"])

def align(L, vals):
    arr = np.zeros_like(all_L, dtype=np.float64)
    idx = np.searchsorted(all_L, L)
    arr[idx] = vals
    return arr

full_C = align(load_d["L"], load_d["C"]) + align(run_d["L"], run_d["C"])
full_F = align(load_d["L"], load_d["F"]) + align(run_d["L"], run_d["F"])
full_d = {
    "total_sweeps":  load_d["total_sweeps"] + run_d["total_sweeps"],
    "total_seconds": load_d["total_seconds"] + run_d["total_seconds"],
    "sec_per_sweep": run_d["sec_per_sweep"],
    "L": all_L, "C": full_C, "F": full_F, "I": full_C - full_F,
}

# === Threshold grid (in sweeps) ===
sec_per_sweep = run_d["sec_per_sweep"]
max_sweeps = max(load_d["total_sweeps"], run_d["total_sweeps"])
thresholds = np.unique(np.concatenate([
    np.array([0]),
    np.geomspace(1, max(max_sweeps, 2), 200).astype(int),
])).astype(int)
thresholds = thresholds[thresholds <= max_sweeps]
thresh_secs = thresholds * sec_per_sweep

# === Constants ===
LTRAM_PAGES     = 65536
ERASES_PER_CELL = 100_000
TOTAL_ERASES    = LTRAM_PAGES * ERASES_PER_CELL
SECS_PER_YEAR   = 365.25 * 86400
HORIZON_SEC     = 5 * SECS_PER_YEAR


def total_migs_curve(d):
    out = np.zeros_like(thresholds, dtype=float)
    for i, T in enumerate(thresholds):
        out[i] = d["C"][d["L"] > T].sum()
    return out

# DEPLOYMENT-SCENARIO formula (matches costben_normalized + endurance_split):
# Old (final + inter × N) treated final-at-end-of-run as one-time, which
# undercounts churn in steady-state. New: subtract C1+C2+C3 (truly stays-RO)
# from total run migrations, multiply by N for cycles, add load_total once.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))
from _phase_data import classify_pages
pcat = classify_pages(out_dir)
non_recur = pcat["class1"] + pcat["class2"] + pcat["class3"]
non_recur_at_T = np.where(thresholds < pcat["run_total_sweeps"], non_recur, 0)

run_total_migs  = total_migs_curve(run_d)
load_total_migs = total_migs_curve(load_d)
N = HORIZON_SEC / run_d["total_seconds"]
recurring = np.maximum(run_total_migs - non_recur_at_T, 0)

# Three deployment views (clear physical interpretation per curve):
#   load-only: cold-start cost ALONE (no extrapolation, one-time)
#   run-only:  steady-state ONLY (recurring × N, no cold-start)
#   full:     cold-start + steady-state (load + recurring × N) — REALISTIC
load_5y = 100.0 * load_total_migs              / TOTAL_ERASES
run_5y  = 100.0 * (recurring * N)              / TOTAL_ERASES
full_5y = 100.0 * (load_total_migs + recurring * N) / TOTAL_ERASES


def pages_pct_curve(pages_csv):
    if not pages_csv.exists():
        return None, None
    df = pd.read_csv(pages_csv, comment="#")
    if "final_stab_period" not in df.columns:
        return None, None
    finals = df["final_stab_period"].astype(int).values
    total = len(df)
    pct = np.array([100.0 * (finals > T).sum() / total for T in thresholds])
    return pct, total


run_pct,  run_total  = pages_pct_curve(run_pages_csv)
load_pct, load_total = pages_pct_curve(load_pages_csv)

# === Plot ===
TAB_BLUE = "#0072B2"
VERMIL   = "#D55E00"

fig, (ax_top, ax_bot) = plt.subplots(
    2, 1, figsize=(13, 9), sharex=True,
    gridspec_kw={"height_ratios": [1, 1]},
)

# Top: % pages on LtRAM at end-of-phase
if run_pct is not None:
    ax_top.plot(thresh_secs, run_pct, color=TAB_BLUE, linewidth=2.5,
                marker="o", markersize=4,
                label=f"run only  (end-of-run snapshot, n={run_total})")
if load_pct is not None:
    ax_top.plot(thresh_secs, load_pct, color=VERMIL, linewidth=2.0,
                linestyle="--", marker="s", markersize=4,
                label=f"load only (end-of-load snapshot, n={load_total})")
n_total = max(run_total or 1, load_total or 1)
ceiling = 100.0 * LTRAM_PAGES / n_total
ax_top.axhline(ceiling, color="gray", linestyle=":", linewidth=1.5,
               label=f"LtRAM capacity ceiling = {ceiling:.1f}% "
                     f"({LTRAM_PAGES * 4 / 1024:.0f} MB)")
ax_top.set_ylabel("% of pages on LtRAM at end-of-phase", fontsize=11)
ax_top.set_title(
    f"{workload} — load-phase vs run-phase deployment cost\n"
    f"load = {load_d['total_seconds']:.0f}s   |   "
    f"run = {run_d['total_seconds']:.0f}s   |   "
    f"full (merged) = {full_d['total_seconds']:.0f}s",
    fontsize=12,
)
top_max = max(
    run_pct.max() if run_pct is not None else 0,
    load_pct.max() if load_pct is not None else 0,
)
ax_top.set_ylim(0, max(105, top_max * 1.1))
ax_top.legend(loc="upper right", fontsize=10)
ax_top.grid(True, alpha=0.3)

# Bottom: 5y endurance % consumed
ax_bot.plot(thresh_secs, full_5y, color="black", linewidth=2.8,
            marker="D", markersize=4,
            label="full (load + run repeating 5y)  — naive deployment")
ax_bot.plot(thresh_secs, load_5y, color=VERMIL, linewidth=2.0,
            linestyle="--", marker="s", markersize=4,
            label="load only (repeating 5y)  — cold-start cost")
ax_bot.plot(thresh_secs, run_5y, color=TAB_BLUE, linewidth=2.5,
            marker="o", markersize=4,
            label="run only (repeating 5y)  — what the policy can optimize")
ax_bot.set_xlabel("migration threshold T (seconds of clean before migrating)",
                  fontsize=11)
ax_bot.set_ylabel("% of NOR endurance consumed (5-year deployment)",
                  fontsize=11)
bot_max = max(full_5y.max(), load_5y.max(), run_5y.max())
ax_bot.set_ylim(0, max(110, bot_max * 1.1))

# Budget crossover: smallest T where run-only 5y endurance ≤ 100%.
# This is the most aggressive policy that fits chip endurance during steady-state.
under = run_5y <= 100.0
if under.any():
    idx_b = int(np.argmax(under))
    T_b   = thresh_secs[idx_b]
    rb    = run_5y[idx_b]
    pages_b = run_pct[idx_b] if run_pct is not None else None
    blabel = (f"5y-budget crossover (run): T={T_b:.1f}s  "
              f"(run endurance={rb:.1f}%"
              + (f", {pages_b:.0f}% pages" if pages_b is not None else "")
              + ")")
    for ax in (ax_top, ax_bot):
        ax.axvline(T_b, color="red", linewidth=2.6, alpha=0.85,
                   linestyle="-",
                   label=blabel if ax is ax_bot else None)
ax_bot.legend(loc="upper right", fontsize=10)
ax_bot.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig(out_dir / "dirty_stability_phase_compare.png",
            dpi=120, bbox_inches="tight")
plt.close()

# === Summary table ===
print(f"\n{workload} ({run_name}) — phase comparison")
print(f"  load: {load_d['total_seconds']:>6.1f}s   run: {run_d['total_seconds']:>6.1f}s")
print()
print(f"  {'T (s)':>8} {'load 5y%':>12} {'run 5y%':>12} {'full 5y%':>12} "
      f"{'load %pages':>14} {'run %pages':>14}")
for T_sec in [1, 5, 10, 30, 60, 120, 300]:
    if T_sec > max(load_d["total_seconds"], run_d["total_seconds"]):
        continue
    idx = int(np.argmin(np.abs(thresh_secs - T_sec)))
    lp = f"{load_pct[idx]:>12.1f}%" if load_pct is not None else "         n/a"
    rp = f"{run_pct[idx]:>12.1f}%"  if run_pct  is not None else "         n/a"
    print(f"  {T_sec:>8.1f} {load_5y[idx]:>11.4f}% {run_5y[idx]:>11.4f}% "
          f"{full_5y[idx]:>11.4f}% {lp} {rp}")

print(f"\nWrote dirty_stability_phase_compare.png to {out_dir}")
