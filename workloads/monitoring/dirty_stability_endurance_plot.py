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
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from _phase_data import (
    load_stability, parse_args_phase,
    has_load, classify_pages, _load_stability_one,
)

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

workload, run_name, phase = parse_args_phase(sys.argv)
out_dir = RESULTS_DIR / "runs" / run_name

data = load_stability(out_dir, phase)
total_sweeps  = data["total_sweeps"]
total_seconds = data["total_seconds"]
interval_ms   = data["interval_ms"]
sec_per_sweep = data["sec_per_sweep"]
L = data["L"]
C = data["C"].astype(np.float64)
F = data["F"].astype(np.float64)
I = C - F
phase_label = data["label"]

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

# === DEPLOYMENT-SCENARIO formula (replaces final/recurring split) ===
# Old: total = final_migs + (inter_migs/run_seconds) * D_sec
#   - Treated final-at-end-of-run epochs as one-time. Wrong: in steady-state
#     deployment those pages get touched again every run cycle, so they are
#     recurring at the full per-second rate, not one-time.
#
# New: total = load_total_migs + (run_total_migs - non_recur_pages) * (D_sec / run_seconds)
#   - load_total_migs is the cold-start cost (paid once per deployment)
#   - non_recur_pages = C1 + C2 + C3 (kernel can't write OR has no run events)
#     gets subtracted because those pages migrate at most once over the
#     deployment, not once per run cycle.
#   - Everything else (C4: writable + has run events) recurs every cycle.
#
# Same C1+C2+C3 logic the costben_normalized bottom panel uses; this plot's
# Panel C now agrees with the costben_normalized 5-year line.
out_dir_local = out_dir
pcat = classify_pages(out_dir_local)
non_recur_pages = pcat["class1"] + pcat["class2"] + pcat["class3"]
run_total_sweeps_pcat = pcat["run_total_sweeps"]
non_recur_at_T = np.where(thresholds < run_total_sweeps_pcat, non_recur_pages, 0)

# total_migs already computed above is for whichever phase was loaded (run
# or full). For the deployment formula we want the run phase's total_migs.
# When phase=full, the helper merges histograms; the "run" component is
# what we need separately. Re-load run-phase stability if not already.
if phase == "full" and has_load(out_dir_local):
    run_stab_only = _load_stability_one(out_dir_local / "dirty_sweep_stability.csv", "run only")
    L_run = run_stab_only["L"]; C_run = run_stab_only["C"].astype(float)
    run_total_migs = np.array([C_run[L_run > T].sum() for T in thresholds], dtype=float)
    run_seconds_for_N = run_stab_only["total_seconds"]
    load_stab_only = _load_stability_one(out_dir_local / "dirty_sweep_load_stability.csv", "load only")
    L_load = load_stab_only["L"]; C_load = load_stab_only["C"].astype(float)
    load_total_migs = np.array([C_load[L_load > T].sum() for T in thresholds], dtype=float)
else:
    # phase=run (or no load): use the data we already loaded
    run_total_migs = total_migs.copy()
    run_seconds_for_N = total_seconds
    load_total_migs = np.zeros_like(thresholds, dtype=float)

recurring_at_T = np.maximum(run_total_migs - non_recur_at_T, 0)

# Endurance % over various deployment horizons
HORIZONS = [1, 5, 10]    # years
endurance_pct = {}
for D_yr in HORIZONS:
    D_sec = D_yr * SECS_PER_YEAR
    N = D_sec / run_seconds_for_N
    total_w = load_total_migs + recurring_at_T * N
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
#   FINAL        = dark blue  (#0072B2)  — settled, blue=clean
#   INTERMEDIATE = light blue (#56B4E9)  — active, lighter blue=more active
axA.fill_between(thresh_secs, 0, final_share_pct,
                 color="#0072B2", alpha=0.85,
                 label="FINAL — read-only at end-of-run (one-time cost)")
axA.fill_between(thresh_secs, final_share_pct, 100.0,
                 color="#56B4E9", alpha=0.85,
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
# Same color convention: FINAL=dark blue, INTERMEDIATE=light blue.
axB.plot(thresh_secs, final_migs, color="#0072B2", linewidth=2,
         marker="o", markersize=3,
         label="one-time placement writes (final epochs)")
axB.plot(thresh_secs, inter_migs, color="#56B4E9", linewidth=2,
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
plt.savefig(out_dir / f"dirty_stability_endurance_split_{phase}.png",
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
    rec_per_y = (recurring_at_T[idx] / run_seconds_for_N) * SECS_PER_YEAR
    e5 = endurance_pct[5][idx]
    e10 = endurance_pct[10][idx]
    print(f"  {T_sec:>8.1f} {f_pct:>9.1f}% {i_pct:>9.1f}% "
          f"{int(one_time):>14,} {int(rec_per_y):>14,} "
          f"{e5:>11.4f}% {e10:>11.4f}%")
print(f"\nWrote dirty_stability_endurance_split.png to {out_dir}")
