"""
True endurance analysis: distinguish one-time placements from recurring churn.

The naive "LtRAM writes per run" metric mixes two fundamentally different
costs together:

  * FINAL-epoch migrations at threshold T: a page reached T seconds of
    stability AND was still stable at end of run. We assume those pages
    stay read-only over the deployment lifetime — so each migration is
    a ONE-TIME placement cost.

  * INTERMEDIATE-epoch migrations at threshold T: a page reached T seconds
    of stability AND was later rewritten during the run. These pages will
    keep being rewritten in production at roughly the observed rate, so
    each one is a RECURRING cost — we have to amortize their migration
    rate over the deployment lifetime.

True NOR-cell endurance consumed for deployment lifetime D:
    one_time_writes(T)  = sum(final_count[L])         for L > T
    recurring_rate(T)   = sum(intermediate_count[L]) / run_seconds  for L > T
    total_writes(T, D)  = one_time(T) + recurring_rate(T) × D
    endurance_pct(T, D) = total_writes(T, D) / TOTAL_ERASES   (chip, wear-leveled)

Three-panel output: dirty_stability_endurance_split.png
  Panel A: final vs intermediate share of qualifying epochs (the user's
           "read-only vs read-heavy" composition for stability windows > T)
  Panel B: one-time writes (fixed) vs recurring-rate × annum (per year)
           — direct head-to-head of the two cost components
  Panel C: total endurance % consumed vs T, plotted for several deployment
           horizons (1y, 5y, 10y) so the reader can pick their target

Usage: dirty_stability_endurance_plot.py <workload> <run_name>
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
csv_path = out_dir / "dirty_sweep_stability.csv"

with open(csv_path) as f:
    header = f.readline().strip()
m = re.search(r"total_sweeps=(\d+)\s+total_seconds=([\d.]+)\s+interval_ms=(\d+)",
              header)
total_sweeps  = int(m.group(1))
total_seconds = float(m.group(2))
interval_ms   = int(m.group(3))
sec_per_sweep = interval_ms / 1000.0

df = pd.read_csv(csv_path, comment="#")
L = df["stability_period_sweeps"].values.astype(np.int64)
C = df["count"].values.astype(np.float64)
F = (df["final_count"].values.astype(np.float64)
     if "final_count" in df.columns else np.zeros_like(C))
I = C - F   # intermediate (epoch terminated by a subsequent write)

# === Constants ===
LTRAM_PAGES        = 65536
ERASES_PER_CELL    = 100_000
TOTAL_ERASES       = LTRAM_PAGES * ERASES_PER_CELL    # chip-level, perfect WL
SECS_PER_YEAR      = 365.25 * 86400

# Threshold grid (geometric, plus 0)
thresholds = np.unique(np.concatenate([
    np.array([0]),
    np.geomspace(1, max(total_sweeps, 2), 200).astype(int),
])).astype(int)
thresholds = thresholds[thresholds <= total_sweeps]
thresh_secs = thresholds * sec_per_sweep

# Per-T accumulators
final_migs = np.zeros_like(thresholds, dtype=float)
inter_migs = np.zeros_like(thresholds, dtype=float)
total_migs = np.zeros_like(thresholds, dtype=float)
for i, T in enumerate(thresholds):
    mask = L > T
    final_migs[i] = F[mask].sum()
    inter_migs[i] = I[mask].sum()
    total_migs[i] = C[mask].sum()

# Static-RO pages: each contributes ONE one-time placement at fault. They
# show up in the stability histogram as final epochs of length total_sweeps,
# so they're already counted in `final_migs` — no double-counting needed.

# Recurring rate (writes per second of observed run)
recurring_rate = inter_migs / total_seconds

# Endurance % over various deployment horizons
HORIZONS = [1, 5, 10]    # years
endurance_pct = {}
for D_yr in HORIZONS:
    D_sec = D_yr * SECS_PER_YEAR
    total_w = final_migs + recurring_rate * D_sec
    endurance_pct[D_yr] = 100.0 * total_w / TOTAL_ERASES

# Composition: % of qualifying epochs that are final vs intermediate
with np.errstate(invalid="ignore", divide="ignore"):
    final_share_pct = np.where(total_migs > 0, 100.0 * final_migs / total_migs, np.nan)
    inter_share_pct = np.where(total_migs > 0, 100.0 * inter_migs / total_migs, np.nan)

# === Plot ===
TAB_BLUE   = "#1f77b4"
TAB_ORANGE = "#ff7f0e"
TAB_GREEN  = "#2ca02c"
TAB_RED    = "#d62728"

fig, (axA, axB, axC) = plt.subplots(3, 1, figsize=(14, 12), sharex=True)

# --- Panel A: composition (final vs intermediate share of qualifying epochs) ---
# Color convention matches timeline / hist_final / cdf_final figures:
#   FINAL        = vermilion (#D55E00)
#   INTERMEDIATE = blue      (#0072B2)
axA.fill_between(thresh_secs, 0, final_share_pct,
                 color="#D55E00", alpha=0.85,
                 label="FINAL — read-only at end-of-run (one-time cost)")
axA.fill_between(thresh_secs, final_share_pct, 100.0,
                 color="#0072B2", alpha=0.85,
                 label="INTERMEDIATE — terminated by a later write (recurring cost)")
axA.set_ylim(0, 100)
axA.set_ylabel("% of epochs with length > T", fontsize=11)
axA.set_title(
    f"{workload} — endurance cost decomposition by epoch type\n"
    f"run = {total_seconds:.0f}s   |   "
    f"NOR chip endurance = {TOTAL_ERASES:.1e} writes (perfect wear leveling)",
    fontsize=12,
)
axA.legend(loc="upper left", fontsize=10, framealpha=0.92)
axA.grid(True, alpha=0.3)

# --- Panel B: writes per run, split into one-time vs recurring ---
# Same color convention: FINAL=vermilion, INTERMEDIATE=blue.
axB.plot(thresh_secs, final_migs, color="#D55E00", linewidth=2,
         marker="o", markersize=3,
         label="one-time placement writes (final epochs)")
axB.plot(thresh_secs, inter_migs, color="#0072B2", linewidth=2,
         marker="s", markersize=3, linestyle="--",
         label="recurring writes during this run (intermediate epochs)")
axB.plot(thresh_secs, total_migs, color="black", linewidth=1.2,
         alpha=0.5, linestyle=":",
         label="total (sum)")
axB.set_yscale("log")
axB.set_ylabel("LtRAM writes during this run (log)", fontsize=11)
axB.legend(loc="upper right", fontsize=10, framealpha=0.92)
axB.grid(True, which="both", alpha=0.3)

# --- Panel C: endurance % consumed at deployment horizons ---
# Linear y-axis with 0-100% range to match Panel A. Anything that would
# exceed 100% clips at the top — that's the "device burned out" region.
HORIZON_COLORS = {1: TAB_GREEN, 5: TAB_ORANGE, 10: TAB_RED}
HORIZON_STYLES = {1: "-",        5: "--",       10: ":"}
for D_yr in HORIZONS:
    axC.plot(thresh_secs, endurance_pct[D_yr],
             color=HORIZON_COLORS[D_yr], linewidth=2,
             linestyle=HORIZON_STYLES[D_yr],
             label=f"{D_yr}-year deployment")
# 100% line: device burns out at this T
axC.axhline(100, color="black", linewidth=1.4, linestyle="-",
            alpha=0.7, label="100% (NOR worn out)")
# Red-shaded "over budget" band — for any horizon whose curve exits the
# visible range (clips at top), this region marks the burnout zone.
axC.axhspan(100, 110, color=TAB_RED, alpha=0.08)
axC.set_ylim(0, 110)
axC.set_ylabel("% of NOR endurance budget consumed (linear)\n"
               "= one-time placements + (rate × deployment)",
               fontsize=11)
axC.set_xlabel("migration threshold T (seconds of clean before migrating)",
               fontsize=11)
axC.legend(loc="upper right", fontsize=10, framealpha=0.92)
axC.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig(out_dir / "dirty_stability_endurance_split.png",
            dpi=120, bbox_inches="tight")
plt.close()

# === Print summary at a few interesting T values ===
print(f"\n{workload} ({run_name}) — endurance decomposition")
print(f"  total_sweeps={total_sweeps}  total_seconds={total_seconds:.1f}s")
print()
print(f"  {'T (s)':>8} {'final %':>10} {'inter %':>10} "
      f"{'1-time writes':>14} {'recur/y':>14} "
      f"{'5y endur %':>12} {'10y endur %':>12}")
for T_sec in [1, 5, 10, 30, 60, 120, 300]:
    if T_sec > total_seconds:
        continue
    idx = int(np.argmin(np.abs(thresh_secs - T_sec)))
    f_pct = final_share_pct[idx]
    i_pct = inter_share_pct[idx]
    one_time = final_migs[idx]
    rec_per_y = recurring_rate[idx] * SECS_PER_YEAR
    e5 = endurance_pct[5][idx]
    e10 = endurance_pct[10][idx]
    print(f"  {T_sec:>8.1f} {f_pct:>9.1f}% {i_pct:>9.1f}% "
          f"{int(one_time):>14,} {int(rec_per_y):>14,} "
          f"{e5:>11.4f}% {e10:>11.4f}%")
print(f"\nWrote dirty_stability_endurance_split.png to {out_dir}")
