"""
Plot stability period distribution from dirty_sweep_stability.csv.

A "stability period" = a maximal run of consecutive sweeps where a single
page stays present and clean (soft-dirty bit unset). Each (page, period)
pair is one data point. A page can produce many periods over its lifetime.

Three plots produced:

  dirty_stability_hist.png   Histogram of stability-period lengths (X = length, Y = count, log y).
  dirty_stability_cdf.png    CDF of stability-period lengths (X = length, Y = cumulative fraction).
  dirty_stability_costben.png  Cost/benefit at varying thresholds — the policy curve.

Cost/benefit interpretation: pick a threshold T (in sweeps).
  - For each period of length L > T: page would be migrated to LtRAM at the
    T-th sweep of the period; provides L-T sweeps of LtRAM service before the
    next write. Cost: 1 LtRAM write per migration.
  - For each period L <= T: never reaches threshold, no migration.

Usage: dirty_stability_plot.py <workload> <run_name>
"""

import sys
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from _phase_data import (
    load_stability, load_pages, parse_args_phase,
    has_load, classify_pages, _load_stability_one,
)

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

workload, run_name, phase = parse_args_phase(sys.argv)
out_dir = RESULTS_DIR / "runs" / run_name

data = load_stability(out_dir, phase)
total_sweeps        = data["total_sweeps"]
total_seconds       = data["total_seconds"]
interval_ms         = data["interval_ms"]
sec_per_sweep       = data["sec_per_sweep"]
lengths             = data["L"]
counts              = data["C"]
final_counts        = data["F"]
intermediate_counts = data["I"]
total_periods       = int(counts.sum())
phase_label         = data["label"]

# Class 1 (static-RO) page count, read from the per-page sweep CSV. Class 1
# pages each contribute one stability period of length ≈ total_sweeps to
# `stability_hist`, but they are NOT migrated by a threshold-based policy —
# the kernel places them on LtRAM at fault time. So we should exclude them
# from the cost (migrations) but keep their utility contribution.
tier1_pages = 0
tier1_size_mb = 0.0
total_pages = None
max_stabs_sweeps = None    # per-page LONGEST clean epoch, for the "ever-on-LtRAM" curve
try:
    df_pages = load_pages(out_dir, phase)
    df_pages["writable"] = df_pages["vma_perms"].str[1] == "w"
    tier1_pages = int((~df_pages["writable"]).sum())
    tier1_size_mb = tier1_pages * 4 / 1024
    tier2_pages = int(df_pages["writable"].sum())
    tier2_size_mb = tier2_pages * 4 / 1024
    total_pages = len(df_pages)
    # Use max_stab_period (LONGEST clean epoch over the run) rather than
    # final_stab_period (last clean epoch only). max_stab > T → page was
    # migrated to LtRAM at some point during the run; final_stab > T only
    # counts pages still on LtRAM at end-of-run, undercounting pages that
    # were migrated, then evicted. For the "% pages ever on LtRAM" curve
    # max_stab is the correct semantic.
    if "max_stab_period" in df_pages.columns:
        max_stabs_sweeps = df_pages["max_stab_period"].astype(int).values
    print(f"Class 1 (static-RO, !VM_WRITE): {tier1_pages} pages "
          f"(~{tier1_size_mb:.1f} MB) — placed at fault time, costs {tier1_pages} "
          f"LtRAM writes one-shot (no speculation, no CoW)")
    print(f"Class 2 (writable, LRW-managed): {tier2_pages} pages "
          f"(~{tier2_size_mb:.1f} MB) — threshold policy decides per stability period")
except FileNotFoundError:
    print(f"WARNING: per-page CSV for phase={phase} not found, "
          f"can't separate Class 1 from Class 2")

# Convert sweep counts to seconds for human-readable axes
length_secs = lengths * sec_per_sweep

# Weighted statistics
mean_len_sweeps = (lengths * counts).sum() / total_periods
median_len_sweeps = np.percentile(np.repeat(lengths, np.minimum(counts, 1000)), 50)
total_sweep_time = (lengths * counts).sum()

print(f"Run: {run_name}")
print(f"  total_sweeps   = {total_sweeps}   ({total_seconds:.1f}s)")
print(f"  total periods  = {total_periods}")
print(f"  mean length    = {mean_len_sweeps:.1f} sweeps "
      f"({mean_len_sweeps * sec_per_sweep:.2f}s)")
print(f"  total RO-time  = {total_sweep_time} sweeps × pages "
      f"({total_sweep_time * sec_per_sweep:.0f} page-seconds of LtRAM-eligible time)")

# ---------------------------------------------------------------------------
# Plot 1: stability-period length histogram, aggregated into 1-second bins
# ---------------------------------------------------------------------------
# At sweep granularity bars are too thin to read. Bucket into integer-second
# bins so the multi-modal structure is visible.
BIN_WIDTH_SEC = 1.0
# Cap n_bins at actual data extent (+5%) — see cluster_plot for rationale.
# Previously n_bins=ceil(total_seconds) padded thousands of empty trailing
# bins and made rendering 12+ minutes per cluster figure for long runs.
n_bins = int(np.ceil(length_secs.max() * 1.05 / BIN_WIDTH_SEC)) + 1 if length_secs.size else 1
bin_idx = np.floor(length_secs / BIN_WIDTH_SEC).astype(int)
bin_idx = np.clip(bin_idx, 0, n_bins - 1)
binned_intermediate = np.zeros(n_bins, dtype=np.int64)
binned_final        = np.zeros(n_bins, dtype=np.int64)
np.add.at(binned_intermediate, bin_idx, intermediate_counts)
np.add.at(binned_final,        bin_idx, final_counts)
bin_centers = (np.arange(n_bins) + 0.5) * BIN_WIDTH_SEC

binned_total = binned_intermediate + binned_final

# Plot 1a: all-stability histogram (no intermediate/final split)
fig, ax = plt.subplots(figsize=(11, 6))
ax.bar(bin_centers, binned_total,
       width=BIN_WIDTH_SEC * 0.9, color="#0072B2",
       edgecolor="black", linewidth=0.2)
ax.set_yscale("log")
ax.set_xlabel(f"stability period length (seconds, {BIN_WIDTH_SEC:.0f}s bins)")
ax.set_ylabel("number of stability periods (log)")
ax.set_title(
    f"{workload} — distribution of stability period lengths (all epochs)\n"
    f"{total_periods} periods across all readable pages "
    f"(includes static-RO Class 1), {total_seconds:.1f}s total run"
)
ax.grid(True, which="both", alpha=0.3)
ax.set_xlim(0, float(length_secs.max()) * 1.05 if length_secs.size else None)
plt.tight_layout()
plt.savefig(out_dir / f"dirty_stability_hist_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

# Plot 1b: stacked histogram, intermediate vs final epoch.
# Color convention (blue = clean, red = dirty, lighter = more active):
#   light blue  → INTERMEDIATE (active, between writes)
#   dark blue   → FINAL        (settled, no more writes coming)
fig, ax = plt.subplots(figsize=(11, 6))
ax.bar(bin_centers, binned_intermediate,
       width=BIN_WIDTH_SEC * 0.9, color="#56B4E9",
       edgecolor="black", linewidth=0.2,
       label="intermediate (terminated by a write)")
# IMPORTANT: clamp bottom to a tiny positive value. On log-y scale,
# matplotlib silently DROPS bars whose bottom=0 (because log(0) is
# undefined), so for bins where binned_intermediate==0 the final bar
# would be invisible. Clamping to 0.5 puts the bar bottom just below the
# log-y axis floor (which is at 1 since counts are integers ≥ 1) — same
# visual result as bottom=0 would give if matplotlib handled it correctly.
ax.bar(bin_centers, binned_final,
       bottom=np.maximum(binned_intermediate, 0.5),
       width=BIN_WIDTH_SEC * 0.9, color="#0072B2",
       edgecolor="black", linewidth=0.2,
       label="final (active at end of run)")
ax.set_yscale("log")
ax.set_xlabel(f"stability period length (seconds, {BIN_WIDTH_SEC:.0f}s bins)")
ax.set_ylabel("number of stability periods (log)")
ax.set_title(
    f"{workload} — stability-period histogram, intermediate vs final\n"
    f"{total_periods} total periods, {total_seconds:.1f}s run "
    f"(dark blue = page's last clean window, light blue = ended by subsequent write)"
)
ax.legend(loc="upper right", fontsize=10)
ax.grid(True, which="both", alpha=0.3)
ax.set_xlim(0, float(length_secs.max()) * 1.05 if length_secs.size else None)
plt.tight_layout()
plt.savefig(out_dir / f"dirty_stability_hist_final_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

# ---------------------------------------------------------------------------
# Plot 2: stability-period length CDF
# ---------------------------------------------------------------------------
# To get an empirical CDF without expanding to per-period rows (which would
# explode memory for large counts), do it analytically: cumulative sum of
# counts, normalized.
sort_idx = np.argsort(lengths)
sorted_lengths = lengths[sort_idx]
sorted_counts  = counts[sort_idx]
sorted_intermediate = intermediate_counts[sort_idx]
sorted_final        = final_counts[sort_idx]
cum_counts = np.cumsum(sorted_counts)
cdf = cum_counts / total_periods

# Separate CDFs for intermediate and final epochs
total_intermediate = int(sorted_intermediate.sum())
total_final = int(sorted_final.sum())
cdf_intermediate = (np.cumsum(sorted_intermediate) / total_intermediate
                   if total_intermediate > 0 else np.zeros_like(cdf))
cdf_final = (np.cumsum(sorted_final) / total_final
             if total_final > 0 else np.zeros_like(cdf))

# Threshold annotations shared between both plots.
threshold_secs = [1.0, 10.0, 30.0, 60.0, 90.0]
threshold_secs = [t for t in threshold_secs if t <= total_seconds]
label_y_offsets = [0.05, 0.10, 0.15, 0.20, 0.25]  # stagger labels vertically

def annotate_thresholds(ax, cdf_arr, total):
    for t_sec, dy in zip(threshold_secs, label_y_offsets):
        t_sweeps = int(t_sec / sec_per_sweep)
        if t_sweeps > sorted_lengths.max():
            continue
        frac = cdf_arr[sorted_lengths <= t_sweeps].max() / total
        ax.axvline(t_sec, color="gray", linestyle=":", alpha=0.5)
        ax.annotate(
            f"{100*frac:.1f}% ≤ {t_sec:.0f}s",
            xy=(t_sec, frac),
            xytext=(t_sec + total_seconds * 0.02, 0.55 + dy),
            fontsize=9, va="center",
            arrowprops=dict(arrowstyle="-", color="gray", alpha=0.5),
        )

# Plot 2a: all-epochs CDF only
fig, ax = plt.subplots(figsize=(11, 6))
ax.step(sorted_lengths * sec_per_sweep, cdf, where="post",
        color="#0072B2", linewidth=2, label=f"all epochs (n={total_periods})")
ax.legend(loc="lower right", fontsize=9)
ax.set_xlabel("stability period length (seconds)")
ax.set_ylabel("cumulative fraction of periods with length ≤ x")
ax.set_title(
    f"{workload} — stability period CDF\n"
    f"reads as: 'fraction of periods shorter than X seconds'"
)
ax.set_ylim(0, 1.02)
ax.grid(True, which="both", alpha=0.3)
annotate_thresholds(ax, cum_counts, total_periods)
plt.tight_layout()
plt.savefig(out_dir / f"dirty_stability_cdf_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

# Plot 2b: same CDF with intermediate vs final breakdown overlaid
fig, ax = plt.subplots(figsize=(11, 6))
ax.step(sorted_lengths * sec_per_sweep, cdf, where="post",
        color="#0072B2", linewidth=2, label=f"all epochs (n={total_periods})")
if total_intermediate > 0:
    ax.step(sorted_lengths * sec_per_sweep, cdf_intermediate, where="post",
            color="#56B4E9", linewidth=1.5, linestyle="--",
            label=f"intermediate only (n={total_intermediate})")
if total_final > 0:
    ax.step(sorted_lengths * sec_per_sweep, cdf_final, where="post",
            color="#0072B2", linewidth=1.5, linestyle=":",
            label=f"final only (n={total_final})")
ax.legend(loc="lower right", fontsize=9)
ax.set_xlabel("stability period length (seconds)")
ax.set_ylabel("cumulative fraction of periods with length ≤ x")
ax.set_title(
    f"{workload} — stability period CDF, intermediate vs final\n"
    f"dark blue = page's last clean window, light blue dashed = ended by subsequent write"
)
ax.set_ylim(0, 1.02)
ax.grid(True, which="both", alpha=0.3)
annotate_thresholds(ax, cum_counts, total_periods)
plt.tight_layout()
plt.savefig(out_dir / f"dirty_stability_cdf_final_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

# ---------------------------------------------------------------------------
# Cluster-based T selection (K = 2, 3, 4)
# ---------------------------------------------------------------------------
# 1D weighted K-means in log-stability-time. T = max length of bottom (K-1)
# clusters + 1 sweep — i.e., everything except the top cluster stays on DRAM.
# Capacity oversubscription (util > 100%) is allowed: it just signals an
# eviction policy is needed when more pages want LtRAM than fit.
def weighted_kmeans_1d(values, weights, K, max_iter=200, n_init=10, seed=0):
    rng = np.random.RandomState(seed)
    best_inertia = np.inf
    best_centers, best_labels = None, None
    for _ in range(n_init):
        probs = weights / weights.sum()
        try:
            idx = rng.choice(len(values), size=K, replace=False, p=probs)
        except ValueError:
            idx = np.arange(min(K, len(values)))
        centers = values[idx].astype(float).copy()
        for _it in range(max_iter):
            dist = np.abs(values[:, None] - centers[None, :])
            labels = dist.argmin(axis=1)
            new_centers = centers.copy()
            for k in range(K):
                mk = labels == k
                if mk.any():
                    new_centers[k] = (values[mk] * weights[mk]).sum() / weights[mk].sum()
            if np.allclose(centers, new_centers, rtol=1e-8, atol=1e-10):
                break
            centers = new_centers
        inertia = 0.0
        for k in range(K):
            mk = labels == k
            if mk.any():
                inertia += (weights[mk] * (values[mk] - centers[k]) ** 2).sum()
        if inertia < best_inertia:
            best_inertia, best_centers, best_labels = inertia, centers, labels
    order = np.argsort(best_centers)
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    return best_centers[order], rank[best_labels]

# Cluster on 1-second-binned data so the threshold T lands on the same 1s
# grid as dirty_stability_cluster_plot.py (otherwise the costben_normalized
# figure shows a slightly-different T from the cluster histogram, which is
# confusing — the two should always agree for the same K).
_pos = lengths > 0
sweeps_per_sec = int(round(1.0 / sec_per_sweep))   # 10 at 100ms cadence
L_sec_int = np.maximum(1, np.round(lengths[_pos] * sec_per_sweep)).astype(int)
unique_secs, inverse = np.unique(L_sec_int, return_inverse=True)
C_per_sec_bin = np.bincount(inverse, weights=counts[_pos]).astype(np.float64)
log_L_secs = np.log(unique_secs.astype(float))

cluster_results = []  # list of dicts: K, T_secs, T_sweeps, centers_secs
for K in [2, 3, 4]:
    if len(unique_secs) < K:
        continue
    centers_log, bin_labels = weighted_kmeans_1d(log_L_secs, C_per_sec_bin, K)
    bottom_mask = bin_labels < (K - 1)
    if bottom_mask.any():
        T_sec = int(unique_secs[bottom_mask].max()) + 1
    else:
        T_sec = 0
    T_sweeps_K = T_sec * sweeps_per_sec
    cluster_results.append({
        "K": K,
        "T_sweeps": T_sweeps_K,
        "T_secs":   float(T_sec),
        "centers_secs": np.exp(centers_log).tolist(),
    })

# ---------------------------------------------------------------------------
# Plot 3: cost/benefit at varying threshold
# ---------------------------------------------------------------------------
# At threshold T (in sweeps):
#   migrations(T)    = sum of count[L] for L > T
#   benefit(T)       = sum of count[L] * (L - T) for L > T   (LtRAM page-sweep-cycles served)
#   amortization(T)  = benefit(T) / migrations(T)              (avg LtRAM lifetime per migration)
#
# Plot dual-axis: migrations(T) on left y, benefit(T) on right y, x = T.
thresholds = np.unique(np.concatenate([
    np.array([0]),
    np.geomspace(1, max(total_sweeps, 2), 200).astype(int),
])).astype(int)
thresholds = thresholds[thresholds <= total_sweeps]

migrations  = np.zeros_like(thresholds, dtype=float)
benefit     = np.zeros_like(thresholds, dtype=float)  # total page-sweeps on LtRAM
final_migs  = np.zeros_like(thresholds, dtype=float)  # one-time placements
inter_migs  = np.zeros_like(thresholds, dtype=float)  # recurring (intermediate)
for i, T in enumerate(thresholds):
    mask = lengths > T
    migrations[i] = counts[mask].sum()
    benefit[i]    = (counts[mask] * (lengths[mask] - T)).sum()
    final_migs[i] = final_counts[mask].sum()
    inter_migs[i] = intermediate_counts[mask].sum()

# Class 1 pages also burn one LtRAM write each (at fault time, not at
# migration time, but a write is a write — the cell endurance is consumed).
# So they ARE part of the wear budget; we don't subtract them.
#
# The Class 1 / Class 2 distinction matters for *amortization*, not cost:
#   - Class 1: 1 LtRAM write buys utility for the whole page lifetime
#             (until munmap), no speculation needed, no CoW churn risk.
#   - Class 2: 1 LtRAM write buys utility for at most one stable streak;
#             speculation may waste writes if the page churns.
# So Class 1 is *more efficient* per write, but not free.
#
# Class 2-only migration count (for reporting only — total wear uses `migrations`):
migrations_tier2 = np.maximum(migrations - tier1_pages, 0.0)
tier1_writes    = float(tier1_pages)  # one write per Class 1 page placed

# Convert benefit from page-sweeps to page-seconds (the integrated capacity ×
# time metric). Each unit = "1 page on LtRAM for 1 second".
benefit_page_seconds = benefit * sec_per_sweep
thresh_secs = thresholds * sec_per_sweep

# === LtRAM physical model ===
# Capacity:    256 MB / 4 KB = 65,536 pages.
# Endurance:   100,000 erases per cell × 65,536 pages = 6.4×10⁹ total page
#              erases over the device lifetime.
LTRAM_PAGES        = 65536            # 256 MB / 4 KB
ERASES_PER_CELL    = 100_000          # NOR write/erase endurance
TOTAL_ERASES       = LTRAM_PAGES * ERASES_PER_CELL
TARGET_LIFETIME_YR = 5                # device lifetime target

# Per-second sustainable migration rate, and per-run budget for this run.
target_lifetime_sec = TARGET_LIFETIME_YR * 365.25 * 86400
sustainable_mig_per_sec = TOTAL_ERASES / target_lifetime_sec
endurance_budget_run = sustainable_mig_per_sec * total_seconds

# Normalized ratios: 1.0 = right at the constraint, > 1.0 = infeasible.
# Both Class 1 and Class 2 placements consume endurance (one LtRAM write each),
# so total wear uses the full `migrations` count.
ltram_capacity_pageseconds = LTRAM_PAGES * total_seconds
utility_ratio        = benefit_page_seconds / ltram_capacity_pageseconds
endurance_ratio      = migrations           / endurance_budget_run
# Class 1 alone — fixed contribution, useful as a baseline reference.
tier1_endurance_ratio = tier1_writes / endurance_budget_run

# Find smallest threshold where the capacity constraint is satisfied
# (utility_ratio ≤ 1 → no oversubscription of LtRAM page-second budget).
# We deliberately drop the endurance/5y-extrapolation constraint here:
# extrapolating a short-run migration rate to 5 years is unphysical for
# batch workloads (the rate isn't a steady state) and lets the optimizer
# pick degenerate T values that satisfy endurance by refusing to migrate.
# Endurance is reported descriptively in the lifetime plot below; readers
# scale to their own deployment length.
def find_first_le(arr, limit):
    idx = np.where(arr <= limit)[0]
    return int(idx[0]) if len(idx) > 0 else None

cap_idx = find_first_le(utility_ratio, 1.0)
T_cap = thresh_secs[cap_idx] if cap_idx is not None else None
T_opt = T_cap

# Plot 3a (the older 2-panel absolute-units costben) was replaced by a new
# 3-panel costben that mirrors costben_normalized's layout but uses
# page-seconds (integrated LtRAM utilization) instead of page-count
# (snapshot) on the top panel. The new plot is generated AFTER normalized
# so it can reuse the same total_5y_pct, eff_ratio, etc.

# ---------------------------------------------------------------------------
# Plot 3b: normalized combined view, single panel
# ---------------------------------------------------------------------------
# Two-panel threshold-decision view in real-world units.
#
# Top:    Fraction of the workload's pages whose data ends up on LtRAM at
#         end-of-run, computed as "pages with final_stab_period > T". Includes
#         a horizontal line for the LtRAM capacity ceiling (= LTRAM_PAGES /
#         total_pages) — when the curve is above the ceiling, the workload
#         is capacity-limited (more pages want LtRAM than fit).
# Bottom: Expected NOR device lifetime in years (LINEAR scale), given the
#         observed migration rate during this run.
#
# A reader can answer at a glance: "at threshold T, X% of my pages live on
# LtRAM, and my NOR will last Y years."

TAB_BLUE   = "#1f77b4"
TAB_ORANGE = "#ff7f0e"
TAB_GREEN  = "#2ca02c"
TAB_RED    = "#d62728"

# --- Top metric: % of pages on LtRAM (final_stab > T) ---
# Capacity-blind: counts every page that would qualify, even if more pages
# qualify than fit in LtRAM. The ceiling line shows where capacity binds.
if max_stabs_sweeps is not None and total_pages is not None and total_pages > 0:
    # "Pages ever on LtRAM at threshold T": page was migrated at least once
    # during the run iff its longest clean epoch (max_stab_period) > T.
    pages_on_ltram_pct = np.array([
        100.0 * (max_stabs_sweeps > T).sum() / total_pages
        for T in thresholds
    ])
    capacity_ceiling_pct = 100.0 * LTRAM_PAGES / total_pages
else:
    # Fallback: use the existing utility ratio (capacity × runtime weighting)
    pages_on_ltram_pct = utility_ratio * 100.0
    capacity_ceiling_pct = 100.0

# --- Bottom metric: expected NOR lifetime in years (linear scale) ---
# Two curves bracket reality:
#   pessimistic — observed migration rate continues for the device's life.
#                 lifetime = TOTAL_ERASES / (rate × seconds_per_year).
#   optimistic  — migrations(T) is a one-time placement cost amortized over
#                 the target deployment lifetime (5y). After that, no churn.
#                 lifetime = TARGET_LIFETIME / (migrations / TOTAL_ERASES).
# Truth lies between. If both curves are above the 5y target line, the
# threshold is safe under any reasonable churn assumption.
SECONDS_PER_YEAR = 365.25 * 86400
observed_rate_per_sec = np.where(migrations > 0,
                                 migrations / total_seconds, 0.0)
with np.errstate(divide="ignore", invalid="ignore"):
    lifetime_years_pessimistic = np.where(
        observed_rate_per_sec > 0,
        TOTAL_ERASES / (observed_rate_per_sec * SECONDS_PER_YEAR),
        np.inf,
    )
    lifetime_years_optimistic = np.where(
        migrations > 0,
        TARGET_LIFETIME_YR * TOTAL_ERASES / migrations,
        np.inf,
    )
# Backwards-compatible alias used by the optimal-T finder below.
lifetime_years = lifetime_years_pessimistic

# NOTE: we deliberately do NOT compute a "smallest T that fits in capacity"
# recommendation here. For oversubscribed workloads (e.g. gapbs xlong with
# graph=22, where ~350K pages want LtRAM but only 65K fit) the only T that
# satisfies the page-count constraint is one where ZERO pages migrate —
# i.e., the optimizer satisfies capacity by refusing to use LtRAM. That's
# a degenerate non-policy. Capacity > 100% is acceptable; eviction handles
# the overflow at runtime. The cluster-based T values (K=2/3/4 lines below)
# are the real policy recommendations.
T_optimal    = None
opt_pages    = None
opt_migrations = None

fig, (ax_util, ax_life, ax_eff) = plt.subplots(
    3, 1, figsize=(13, 12), sharex=True,
    gridspec_kw={"height_ratios": [1, 1, 1]},
)

# ---- Top: % of pages on LtRAM ----
ax_util.plot(thresh_secs, pages_on_ltram_pct, color=TAB_BLUE, linewidth=2.5,
             marker="o", markersize=4,
             label="% of pages with max stable epoch > T  (ever on LtRAM during run)")
ax_util.axhline(capacity_ceiling_pct, color=TAB_BLUE, linestyle=":",
                linewidth=1.8, alpha=0.7,
                label=f"LtRAM capacity ceiling = {capacity_ceiling_pct:.1f}% "
                      f"({LTRAM_PAGES * 4 / 1024:.0f} MB / total)")
if pages_on_ltram_pct.max() > capacity_ceiling_pct:
    ax_util.axhspan(capacity_ceiling_pct,
                    max(105, pages_on_ltram_pct.max() * 1.05),
                    color=TAB_RED, alpha=0.08,
                    label="capacity-limited (more pages want LtRAM than fit)")
ax_util.set_ylabel("Pages ever on LtRAM\n(% of total workload pages,\nmax_stab > T)",
                   fontsize=11)
ax_util.set_title(
    f"{workload} — migration threshold decision\n"
    f"LtRAM = {LTRAM_PAGES * 4 / 1024:.0f} MB capacity   |   "
    f"workload = {total_pages or 'unknown'} pages   |   "
    f"NOR = {TOTAL_ERASES:.2e} total writes over device lifetime",
    fontsize=12,
)
ax_util.set_ylim(0, max(105, pages_on_ltram_pct.max() * 1.1))
ax_util.legend(loc="upper right", fontsize=10)
ax_util.grid(True, alpha=0.3)

# ---- Bottom: % of NOR chip endurance consumed over a 5-year deployment ----
# DEPLOYMENT-SCENARIO formula (replaces the older final/intermediate split,
# which was overly optimistic — it counted FINAL-at-end-of-run epochs as
# one-time when in steady-state deployment they actually recur every cycle).
#
# Model: app boots once → load phase happens once (cold-start, all migrations
# counted) → run phase repeats N = HORIZON / run_seconds times in steady
# state. Pages that we know stay read-only across cycles — Class 1
# (non-writable, kernel can't write), Class 2 (writable, no events anywhere),
# Class 3 (writable, events in load only) — are migrated once at cold-start
# and don't re-migrate. Everything else (Class 4: writable + has run events)
# is treated as recurring.
#
#   total_5y_writes(T) = load_total_migs(T)                          [cold-start, optional]
#                      + (run_total_migs(T) − non_recur_pages) × N   [recurring writable]
#
# load_total_migs is added only for phase=full (or when load CSV exists);
# phase=run shows the steady-state-only curve, ignoring cold-start.
#
# This avoids the old formula's failure mode: the OBSERVED final/inter split
# was an artifact of where we stopped observing, not a real distinction in
# the workload. In a deployment that runs run-phase forever, EVERY epoch >T
# (final or intermediate) is a recurring event.
SECS_PER_YEAR = 365.25 * 86400
HORIZON_SEC   = 5 * SECS_PER_YEAR


def _total_migs_curve(L_arr, C_arr, thresh):
    out = np.zeros_like(thresh, dtype=float)
    for i, T in enumerate(thresh):
        out[i] = C_arr[L_arr > T].sum()
    return out


# Always load both phases independently (regardless of `phase` argv) — the
# bottom panel is the deployment view and combines them per-phase.
_run_stab_path = out_dir / "dirty_sweep_stability.csv"
run_stab = _load_stability_one(_run_stab_path, "run only") if _run_stab_path.exists() else None
load_stab = (_load_stability_one(out_dir / "dirty_sweep_load_stability.csv", "load only")
             if has_load(out_dir) else None)

pcat = classify_pages(out_dir)
non_recur_pages = pcat["class1"] + pcat["class2"] + pcat["class3"]
run_total_sweeps_pcat = pcat["run_total_sweeps"]
non_recur_at_T = np.where(thresholds < run_total_sweeps_pcat, non_recur_pages, 0)

if run_stab is not None:
    run_total_migs_arr = _total_migs_curve(run_stab["L"], run_stab["C"], thresholds)
else:
    run_total_migs_arr = np.zeros_like(thresholds, dtype=float)
recurring_at_T = np.maximum(run_total_migs_arr - non_recur_at_T, 0)
N_repeats = HORIZON_SEC / run_stab["total_seconds"] if run_stab is not None else 0.0

if phase == "full" and load_stab is not None:
    load_total_migs_arr = _total_migs_curve(load_stab["L"], load_stab["C"], thresholds)
    total_5y_writes = load_total_migs_arr + recurring_at_T * N_repeats
    bottom_curve_label = ("total endurance, 5y deployment "
                          "= cold-start + N·recurring")
else:
    total_5y_writes = recurring_at_T * N_repeats
    bottom_curve_label = ("steady-state endurance, 5y "
                          "= N·recurring (excludes cold-start)")
total_5y_pct = 100.0 * total_5y_writes / TOTAL_ERASES

ax_life.plot(thresh_secs, total_5y_pct, color=TAB_ORANGE,
             linewidth=2.5, marker="s", markersize=4,
             label=bottom_curve_label)
ax_life.set_ylim(0, max(110, total_5y_pct.max() * 1.1))
ax_life.set_xlabel("migration threshold T (seconds of clean before migrating)",
                   fontsize=11)
ax_life.set_ylabel("% of NOR chip endurance consumed (5-year deployment)",
                   fontsize=11)
ax_life.legend(loc="upper right", fontsize=10, framealpha=0.92)
ax_life.grid(True, alpha=0.3)

# Mark cluster-derived T values for K = 2, 3, 4 on both panels.
# Each K gets a distinct color/linestyle. Capacity oversubscription
# (util > 100%) is allowed — eviction is a separate question.
CLUSTER_COLORS = {2: "#1b9e77", 3: "#7570b3", 4: "#d95f02"}  # ColorBrewer Dark2

def _interp(arr, T_sweeps):
    """Get value at the threshold sweep (closest gridded T)."""
    idx = int(np.argmin(np.abs(thresholds - T_sweeps)))
    return arr[idx], idx

for r in cluster_results:
    K = r["K"]
    color = CLUSTER_COLORS[K]
    Ts = r["T_secs"]
    Tsweeps = r["T_sweeps"]
    pages_pct, idx = _interp(pages_on_ltram_pct, Tsweeps)
    migs_at_T = migrations[idx]
    label = f"K={K}: T={Ts:.1f}s  ({int(migs_at_T):,} migs, {pages_pct:.0f}% pages)"
    for ax in (ax_util, ax_life, ax_eff):
        ax.axvline(Ts, color=color, linewidth=2.2, alpha=0.85,
                   linestyle=(0, (5, 2 + K)),  # different dash pattern per K
                   label=label if ax is ax_life else None)

# Endurance-budget crossover line: smallest T such that the 5-year endurance
# curve is at or below 100% of TOTAL_ERASES. Most aggressive policy that
# still fits within chip endurance — anything to the LEFT of this line burns
# the chip in <5 years.
under_budget = total_5y_pct <= 100.0
if under_budget.any():
    idx_budget = int(np.argmax(under_budget))   # first True (smallest T)
    T_budget        = thresh_secs[idx_budget]
    migs_at_budget  = int(migrations[idx_budget])
    pages_at_budget = pages_on_ltram_pct[idx_budget]
    end_at_budget   = total_5y_pct[idx_budget]
    budget_label = (f"5y-endurance budget: T={T_budget:.1f}s  "
                    f"({migs_at_budget:,} migs, "
                    f"{pages_at_budget:.0f}% pages, "
                    f"endurance={end_at_budget:.1f}%)")
    for ax in (ax_util, ax_life, ax_eff):
        ax.axvline(T_budget, color="red", linewidth=2.6, alpha=0.85,
                   linestyle="-",
                   label=budget_label if ax is ax_life else None)
    print(f"  endurance-budget crossover (5y, ≤100%): "
          f"T={T_budget:.2f}s  migs={migs_at_budget:,}  "
          f"pages={pages_at_budget:.1f}%  endurance={end_at_budget:.2f}%")
else:
    print(f"  endurance-budget crossover: NEVER ≤100% in tested T range "
          f"(workload exceeds budget at every T)")

# Efficiency-optimal T: argmax of (utility / migration) where utility is
# the LtRAM page-seconds bought, capped by LtRAM physical capacity. This
# answers "which T maximizes the ratio of page-time-on-LtRAM purchased per
# LtRAM cell write" — the policy's per-write ROI. The cap stops the metric
# from running away to T=∞ (where on a Pareto tail the uncapped efficiency
# would keep growing); the cap forces argmax to the knee where adding more
# T no longer improves coverage but only reduces migration count.
utility_capped_at_T = np.minimum(benefit_page_seconds, ltram_capacity_pageseconds)
with np.errstate(invalid="ignore", divide="ignore"):
    efficiency = np.where(migrations > 0, utility_capped_at_T / migrations, 0.0)
# Restrict to T values that don't push capacity to zero (eviction would
# kick in, breaking the LtRAM-coverage assumption). If pages_on_ltram_pct
# drops to 0% before the argmax, that's a degenerate "no migration" case.
valid_eff = (migrations > 0) & (pages_on_ltram_pct > 0)
if valid_eff.any():
    T_eff_idx = int(np.argmax(np.where(valid_eff, efficiency, -np.inf)))
    T_eff           = thresh_secs[T_eff_idx]
    migs_at_eff     = int(migrations[T_eff_idx])
    pages_at_eff    = pages_on_ltram_pct[T_eff_idx]
    eff_pageseconds = utility_capped_at_T[T_eff_idx]
    end_at_eff      = total_5y_pct[T_eff_idx]
    eff_label = (f"efficiency-optimal: T={T_eff:.1f}s  "
                 f"({migs_at_eff:,} migs, "
                 f"{pages_at_eff:.0f}% pages, "
                 f"endurance={end_at_eff:.1f}%)")
    for ax in (ax_util, ax_life, ax_eff):
        ax.axvline(T_eff, color="darkorange", linewidth=2.6, alpha=0.85,
                   linestyle="-",
                   label=eff_label if ax is ax_life else None)
    print(f"  efficiency-optimal T (max page-secs per migration): "
          f"T={T_eff:.2f}s  migs={migs_at_eff:,}  "
          f"pages={pages_at_eff:.1f}%  endurance={end_at_eff:.2f}%  "
          f"({eff_pageseconds/migs_at_eff:.0f} page-sec/write)")

# (Capacity-only T_optimal annotation deliberately dropped: for oversubscribed
# workloads it produces a degenerate "refuse to migrate" answer. Cluster Ts
# above are the policy recommendations; eviction handles capacity overflow.)

# Re-draw legend on bottom panel with cluster Ts included
ax_life.legend(loc="upper right", fontsize=9, framealpha=0.92)

# === Panel 3: efficiency = (% pages on LtRAM) / (% NOR endurance over 5y) ===
# A clean policy decision metric:
#   "how many percent of pages does this T put on LtRAM, per 1% of chip
#   endurance burned over a 5y deployment?"
# Higher = better. argmax = the most efficient T (best LtRAM coverage per
# unit endurance cost). When this peak is to the right of the endurance-
# crossover red line, it gives a "comfortably-within-budget" recommendation.
# When to the left, the budget binds and you should pick the red line.
with np.errstate(invalid="ignore", divide="ignore"):
    eff_ratio = np.where(total_5y_pct > 0,
                         pages_on_ltram_pct / total_5y_pct,
                         np.inf)
# The full curve is plotted everywhere it's defined. argmax restriction
# prevents the degenerate "zero-endurance" tail (where ratio → ∞ because
# the denominator collapses) from being picked as 'optimal'.
plottable = (pages_on_ltram_pct > 0) & np.isfinite(eff_ratio)
ax_eff.plot(thresh_secs[plottable], eff_ratio[plottable], color="#9467bd",
            linewidth=2.5, marker="^", markersize=4,
            label="efficiency = %pages_on_LtRAM / %endurance_consumed")

# argmax constrained to "meaningful" range:
#   endurance must fit 5y budget (≤ 100%) AND policy must be doing real
#   work (≥ 1% endurance), to rule out the degenerate "T so high that
#   nothing migrates" point where the ratio explodes meaninglessly.
# Two tiers of fallback for workloads that don't reach 1% endurance:
useful = plottable & (total_5y_pct >= 1.0) & (total_5y_pct <= 100.0)
if not useful.any():
    # Workload barely uses endurance; relax to >= 0.01% but stay ≤ 100%.
    useful = plottable & (total_5y_pct >= 0.01) & (total_5y_pct <= 100.0)
if not useful.any():
    # Workload never fits in budget at all; pick the smallest T in budget.
    # (Should match the red endurance-budget line in this case.)
    useful = plottable & (total_5y_pct <= 100.0)
if useful.any():
    eff_argmax_idx = int(np.argmax(np.where(useful, eff_ratio, -np.inf)))
    T_eff_pct      = thresh_secs[eff_argmax_idx]
    eff_max        = eff_ratio[eff_argmax_idx]
    eff_pages      = pages_on_ltram_pct[eff_argmax_idx]
    eff_end        = total_5y_pct[eff_argmax_idx]
    eff_pct_label = (f"%/% optimal: T={T_eff_pct:.1f}s  "
                     f"({eff_pages:.0f}% pages / {eff_end:.2f}% endurance "
                     f"= {eff_max:.2f}× ratio)")
    for ax in (ax_util, ax_life, ax_eff):
        ax.axvline(T_eff_pct, color="#9467bd", linewidth=2.6, alpha=0.85,
                   linestyle="-",
                   label=eff_pct_label if ax is ax_eff else None)
    print(f"  %/% efficiency-optimal T: T={T_eff_pct:.2f}s  "
          f"pages={eff_pages:.1f}%  endurance={eff_end:.2f}%  "
          f"ratio={eff_max:.3f}")

ax_eff.set_yscale("log")    # ratio can span many orders of magnitude
ax_eff.set_xlabel("migration threshold T (seconds of clean before migrating)",
                  fontsize=11)
ax_eff.set_ylabel("efficiency ratio\n(%pages on LtRAM / %5y endurance)",
                  fontsize=11)
ax_eff.legend(loc="upper right", fontsize=9, framealpha=0.92)
ax_eff.grid(True, which="both", alpha=0.3)

# x-label moves down to ax_eff; clear the old one on ax_life so the panels
# share x cleanly.
ax_life.set_xlabel("")

plt.tight_layout()
plt.savefig(out_dir / f"dirty_stability_costben_normalized_{phase}.png",
            dpi=120, bbox_inches="tight")
plt.close()

# ---------------------------------------------------------------------------
# Plot 3a (NEW): costben — same 3-panel layout as costben_normalized, but
# top panel shows TIME-INTEGRATED LtRAM utilization (page-seconds % of
# capacity × time) instead of the page-count snapshot. Answers:
#   "given threshold T, what fraction of the LtRAM-capacity × time budget
#    is consumed by qualifying epochs?"
# Middle and bottom panels are identical to costben_normalized (5y endurance
# %, and pages/endurance efficiency ratio) so the policy lines (cluster Ts,
# red, orange, purple) on top remain comparable.
# ---------------------------------------------------------------------------
fig2, (ax2_util, ax2_life, ax2_eff) = plt.subplots(
    3, 1, figsize=(13, 12), sharex=True,
    gridspec_kw={"height_ratios": [1, 1, 1]},
)

# --- Top: page-seconds utilization (% of LTRAM_PAGES × run_seconds) ---
util_ratio_pct = utility_ratio * 100.0   # 0% = no use, 100% = exactly fill capacity for full run
ax2_util.plot(thresh_secs, util_ratio_pct, color=TAB_BLUE, linewidth=2.5,
              marker="o", markersize=4,
              label=f"page-seconds utilization (Σ(L−T)·count for L>T,\n"
                    f"normalized to LTRAM_PAGES × run_seconds = "
                    f"{ltram_capacity_pageseconds:.2e})")
ax2_util.axhline(100.0, color="gray", linestyle=":", linewidth=1.8, alpha=0.7,
                 label="100% = exactly fill LtRAM for the full run "
                       "(above = oversubscription)")
if util_ratio_pct.max() > 100.0:
    ax2_util.axhspan(100.0, max(110, util_ratio_pct.max() * 1.05),
                     color=TAB_RED, alpha=0.08,
                     label="oversubscribed (more page-seconds than fit)")
ax2_util.set_ylabel("page-seconds on LtRAM\n(% of capacity × time budget)",
                    fontsize=11)
ax2_util.set_title(
    f"{workload} — migration threshold decision (page-seconds view)\n"
    f"LtRAM = {LTRAM_PAGES * 4 / 1024:.0f} MB capacity × {total_seconds:.0f}s = "
    f"{ltram_capacity_pageseconds:.2e} page-sec budget   |   "
    f"NOR = {TOTAL_ERASES:.2e} writes over device lifetime",
    fontsize=12,
)
ax2_util.set_ylim(0, max(105, util_ratio_pct.max() * 1.1))
ax2_util.legend(loc="upper right", fontsize=10)
ax2_util.grid(True, alpha=0.3)

# --- Middle: identical to costben_normalized middle panel (5y endurance %)
ax2_life.plot(thresh_secs, total_5y_pct, color=TAB_ORANGE,
              linewidth=2.5, marker="s", markersize=4,
              label="total endurance consumed (5y deployment)")
ax2_life.set_ylim(0, max(110, total_5y_pct.max() * 1.1))
ax2_life.set_ylabel("% of NOR chip endurance consumed (5-year deployment)",
                    fontsize=11)
ax2_life.legend(loc="upper right", fontsize=10, framealpha=0.92)
ax2_life.grid(True, alpha=0.3)

# --- Bottom: identical to costben_normalized bottom panel (efficiency) ---
ax2_eff.plot(thresh_secs[plottable], eff_ratio[plottable], color="#9467bd",
             linewidth=2.5, marker="^", markersize=4,
             label="efficiency = %pages_on_LtRAM / %endurance_consumed")
ax2_eff.set_yscale("log")
ax2_eff.set_xlabel("migration threshold T (seconds of clean before migrating)",
                   fontsize=11)
ax2_eff.set_ylabel("efficiency ratio\n(%pages on LtRAM / %5y endurance)",
                   fontsize=11)
ax2_eff.grid(True, which="both", alpha=0.3)

# Re-draw the same vertical T-marker lines on all three panels.
for r in cluster_results:
    K = r["K"]
    color = CLUSTER_COLORS[K]
    Ts = r["T_secs"]
    Tsweeps = r["T_sweeps"]
    pages_pct, idx = _interp(pages_on_ltram_pct, Tsweeps)
    migs_at_T = migrations[idx]
    label2 = f"K={K}: T={Ts:.1f}s  ({int(migs_at_T):,} migs, {pages_pct:.0f}% pages)"
    for ax in (ax2_util, ax2_life, ax2_eff):
        ax.axvline(Ts, color=color, linewidth=2.2, alpha=0.85,
                   linestyle=(0, (5, 2 + K)),
                   label=label2 if ax is ax2_life else None)
if under_budget.any():
    for ax in (ax2_util, ax2_life, ax2_eff):
        ax.axvline(T_budget, color="red", linewidth=2.6, alpha=0.85,
                   linestyle="-",
                   label=budget_label if ax is ax2_life else None)
if valid_eff.any():
    for ax in (ax2_util, ax2_life, ax2_eff):
        ax.axvline(T_eff, color="darkorange", linewidth=2.6, alpha=0.85,
                   linestyle="-",
                   label=eff_label if ax is ax2_life else None)
if useful.any():
    for ax in (ax2_util, ax2_life, ax2_eff):
        ax.axvline(T_eff_pct, color="#9467bd", linewidth=2.6, alpha=0.85,
                   linestyle="-",
                   label=eff_pct_label if ax is ax2_eff else None)
ax2_life.legend(loc="upper right", fontsize=9, framealpha=0.92)
ax2_eff.legend(loc="upper right", fontsize=9, framealpha=0.92)

plt.tight_layout()
plt.savefig(out_dir / f"dirty_stability_costben_{phase}.png",
            dpi=120, bbox_inches="tight")
plt.close()

# Plot 3c (dirty_stability_costben_lifetime.png) was retired. Its
# optimistic-vs-pessimistic framing is strictly subsumed by
# dirty_stability_endurance_split.png, which uses the data-driven
# FINAL/INTERMEDIATE epoch split instead of two heuristic curves and
# also reports the per-deployment-horizon endurance %.

# ---------------------------------------------------------------------------
# Plot 4: Per-epoch tier breakdown (page-sweep-time)
# ---------------------------------------------------------------------------
# Each (page, epoch) is one data point. Classify epochs by length and report
# fraction of total page-sweep-time spent in each tier.
#
#   Long epochs   (length ≥ 50% total_sweeps): contribute to "LtRAM-confident"
#   Medium epochs (5%–50%):                     "LtRAM-eligible w/ re-migrate"
#   Short epochs  (< 5%):                       "stays on DRAM"
#
# Class 1 (static-RO, by VMA flag) appears as long epochs of length=total_sweeps
# but is functionally identical to "writable but never written" — both are
# maximally LtRAM-friendly. We mark them separately when we have the per-page
# CSV available, otherwise lump them in.
TIER2_MIN_FRAC_EPOCH  = 0.50
TIER34_MIN_FRAC_EPOCH = 0.05
t2_min_sw = TIER2_MIN_FRAC_EPOCH * total_sweeps
t34_min_sw = TIER34_MIN_FRAC_EPOCH * total_sweeps

tier2_pagesw = sum(L * c for L, c in zip(lengths, counts) if L >= t2_min_sw)
tier3_pagesw = sum(L * c for L, c in zip(lengths, counts) if t34_min_sw <= L < t2_min_sw)
tier4_pagesw = sum(L * c for L, c in zip(lengths, counts) if L < t34_min_sw)
total_pagesw = tier2_pagesw + tier3_pagesw + tier4_pagesw

# Convert to seconds for readability
to_sec = sec_per_sweep
t2_psec = tier2_pagesw * to_sec
t3_psec = tier3_pagesw * to_sec
t4_psec = tier4_pagesw * to_sec
total_psec = total_pagesw * to_sec

# Read per-page CSV to subtract Class 1 (static-RO) contribution from the long-epoch bucket
tier1_pagesw = 0
tier1_pages_count = 0
try:
    df_pages = load_pages(out_dir, phase)
    df_pages["writable"] = df_pages["vma_perms"].str[1] == "w"
    tier1_pages_count = int((~df_pages["writable"]).sum())
    # Class 1 pages each contribute one epoch of length=total_sweeps to stability_hist.
    # Subtract that from tier2_pagesw to isolate "writable, long-epoch" pages.
    tier1_pagesw = tier1_pages_count * total_sweeps
    tier2_pagesw_writable = max(tier2_pagesw - tier1_pagesw, 0)
    t1_psec = tier1_pagesw * to_sec
    t2w_psec = tier2_pagesw_writable * to_sec
except FileNotFoundError:
    t1_psec = 0
    t2w_psec = t2_psec

epoch_specs = [
    ("Class 1\nOS-known",              t1_psec,  "#009E73"),
    ("Class 2\nApp-known\nwrite-once", t2w_psec, "#0072B2"),
    ("Class 3\nRead-heavy",            t3_psec,  "#F0E442"),
    ("Class 4\nOthers",                t4_psec,  "#D55E00"),
]
e_nonzero = [(l, sec, c) for (l, sec, c) in epoch_specs if sec > 0]
e_labels = [t[0] for t in e_nonzero]
e_sizes  = [t[1] for t in e_nonzero]
e_colors = [t[2] for t in e_nonzero]

import matplotlib.patches as mpatches

def autopct_fmt_e(pct):
    if pct < 1.5:
        return ""
    pseconds = pct * total_psec / 100
    return f"{pct:.1f}%\n({pseconds:.0f} p·s)"

e_legend_labels = [
    f"Class 1 (OS-known, static-RO):        {t1_psec:>10.0f} page-sec  ({100*t1_psec/total_psec:>5.1f}%)",
    f"Class 2 (App-known, write-once data): {t2w_psec:>10.0f} page-sec  ({100*t2w_psec/total_psec:>5.1f}%)",
    f"Class 3 (read-heavy):                 {t3_psec:>10.0f} page-sec  ({100*t3_psec/total_psec:>5.1f}%)",
    f"Class 4 (others):                     {t4_psec:>10.0f} page-sec  ({100*t4_psec/total_psec:>5.1f}%)",
]
legend_colors_e = ["#009E73", "#0072B2", "#F0E442", "#D55E00"]

fig, ax = plt.subplots(figsize=(11, 7))
wedges, _, autotexts = ax.pie(
    e_sizes, labels=None, colors=e_colors,
    autopct=autopct_fmt_e, startangle=90, pctdistance=0.72,
    wedgeprops=dict(edgecolor="black", linewidth=0.7),
)
for at in autotexts:
    at.set_fontsize(10)

patches = [mpatches.Patch(color=c, label=l) for c, l in zip(legend_colors_e, e_legend_labels)]
ax.legend(handles=patches, loc="center left", bbox_to_anchor=(1.02, 0.5),
          fontsize=10, frameon=True, prop={"family": "monospace"})

ltram_psec = t1_psec + t2w_psec + t3_psec
ax.set_title(
    f"{workload} — per-EPOCH class breakdown (by page-sweep-time)\n"
    f"each epoch counted once weighted by its length × 1 page  |  "
    f"total page-time observed: {total_psec:.0f} page-sec\n"
    f"LtRAM-eligible (C1+C2+C3): {100*ltram_psec/total_psec:.1f}%   |   "
    f"DRAM-required (C4): {100*t4_psec/total_psec:.1f}%",
    fontsize=11,
)
ax.set_aspect("equal")
plt.tight_layout()
plt.savefig(out_dir / f"dirty_tiers_perepoch_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

print(f"  capacity-feasible threshold (page-seconds, ref only):  T = {T_cap:.1f}s" if T_cap is not None else "  capacity always feasible")
print(f"  cluster-derived T (K-means in log-stability):")
for r in cluster_results:
    centers_str = "  ".join(f"{c:.2f}" for c in r["centers_secs"])
    print(f"    K={r['K']}: T = {r['T_secs']:>6.2f}s   "
          f"cluster centers (s) = [{centers_str}]")

print(f"Wrote dirty_stability_{{hist,cdf,costben*}}.png and dirty_tiers_perepoch.png to {out_dir}")
