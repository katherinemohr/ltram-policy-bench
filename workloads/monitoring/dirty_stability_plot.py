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
import re
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import pandas as pd

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

workload = sys.argv[1]
run_name = sys.argv[2]
out_dir = RESULTS_DIR / "runs" / run_name
stability_csv = out_dir / "dirty_sweep_stability.csv"

with open(stability_csv) as f:
    header_line = f.readline().strip()
m = re.search(r"total_sweeps=(\d+)\s+total_seconds=([\d.]+)\s+interval_ms=(\d+)", header_line)
total_sweeps = int(m.group(1))
total_seconds = float(m.group(2))
interval_ms = int(m.group(3))
sec_per_sweep = interval_ms / 1000.0

df = pd.read_csv(stability_csv, comment="#")
# Columns: stability_period_sweeps, count[, final_count]
lengths = df["stability_period_sweeps"].values
counts  = df["count"].values
final_counts = (df["final_count"].values if "final_count" in df.columns
                else np.zeros_like(counts))
intermediate_counts = counts - final_counts
total_periods = int(counts.sum())

# Class 1 (static-RO) page count, read from the per-page sweep CSV. Class 1
# pages each contribute one stability period of length ≈ total_sweeps to
# `stability_hist`, but they are NOT migrated by a threshold-based policy —
# the kernel places them on LtRAM at fault time. So we should exclude them
# from the cost (migrations) but keep their utility contribution.
sweep_csv = out_dir / "dirty_sweep.csv"
tier1_pages = 0
tier1_size_mb = 0.0
total_pages = None
final_stabs_sweeps = None  # per-page final-epoch length, for the LtRAM-occupancy curve
if sweep_csv.exists():
    df_pages = pd.read_csv(sweep_csv, comment="#")
    df_pages["writable"] = df_pages["vma_perms"].str[1] == "w"
    tier1_pages = int((~df_pages["writable"]).sum())
    tier1_size_mb = tier1_pages * 4 / 1024
    tier2_pages = int(df_pages["writable"].sum())
    tier2_size_mb = tier2_pages * 4 / 1024
    total_pages = len(df_pages)
    if "final_stab_period" in df_pages.columns:
        # For Class 1 pages (never written), final_stab_period == total_sweeps
        # already from how dirty_sweep finalizes. For writable pages, it's
        # the time since last write. Either way, "page on LtRAM at end of
        # run iff final_stab_period > T".
        final_stabs_sweeps = df_pages["final_stab_period"].astype(int).values
    print(f"Class 1 (static-RO, !VM_WRITE): {tier1_pages} pages "
          f"(~{tier1_size_mb:.1f} MB) — placed at fault time, costs {tier1_pages} "
          f"LtRAM writes one-shot (no speculation, no CoW)")
    print(f"Class 2 (writable, LRW-managed): {tier2_pages} pages "
          f"(~{tier2_size_mb:.1f} MB) — threshold policy decides per stability period")
else:
    print(f"WARNING: dirty_sweep.csv not found, can't separate Class 1 from Class 2")

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
n_bins = int(np.ceil(total_seconds / BIN_WIDTH_SEC)) + 1
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
plt.tight_layout()
plt.savefig(out_dir / "dirty_stability_hist.png", dpi=120, bbox_inches="tight")
plt.close()

# Plot 1b: stacked histogram, intermediate vs final epoch
fig, ax = plt.subplots(figsize=(11, 6))
ax.bar(bin_centers, binned_intermediate,
       width=BIN_WIDTH_SEC * 0.9, color="#0072B2",
       edgecolor="black", linewidth=0.2,
       label="intermediate (terminated by a write)")
ax.bar(bin_centers, binned_final, bottom=binned_intermediate,
       width=BIN_WIDTH_SEC * 0.9, color="#D55E00",
       edgecolor="black", linewidth=0.2,
       label="final (active at end of run)")
ax.set_yscale("log")
ax.set_xlabel(f"stability period length (seconds, {BIN_WIDTH_SEC:.0f}s bins)")
ax.set_ylabel("number of stability periods (log)")
ax.set_title(
    f"{workload} — stability-period histogram, intermediate vs final\n"
    f"{total_periods} total periods, {total_seconds:.1f}s run "
    f"(orange = page's last clean window, blue = ended by subsequent write)"
)
ax.legend(loc="upper right", fontsize=10)
ax.grid(True, which="both", alpha=0.3)
plt.tight_layout()
plt.savefig(out_dir / "dirty_stability_hist_final.png", dpi=120, bbox_inches="tight")
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
plt.savefig(out_dir / "dirty_stability_cdf.png", dpi=120, bbox_inches="tight")
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
            color="#D55E00", linewidth=1.5, linestyle=":",
            label=f"final only (n={total_final})")
ax.legend(loc="lower right", fontsize=9)
ax.set_xlabel("stability period length (seconds)")
ax.set_ylabel("cumulative fraction of periods with length ≤ x")
ax.set_title(
    f"{workload} — stability period CDF, intermediate vs final\n"
    f"orange = page's last clean window, blue dashed = ended by subsequent write"
)
ax.set_ylim(0, 1.02)
ax.grid(True, which="both", alpha=0.3)
annotate_thresholds(ax, cum_counts, total_periods)
plt.tight_layout()
plt.savefig(out_dir / "dirty_stability_cdf_final.png", dpi=120, bbox_inches="tight")
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

# Keep only positive-length entries for clustering.
_pos = lengths > 0
log_L = np.log(lengths[_pos].astype(float))
weights_for_km = counts[_pos].astype(float)

cluster_results = []  # list of dicts: K, T_secs, T_sweeps, centers_secs
for K in [2, 3, 4]:
    if len(log_L) < K:
        continue
    centers_log, labels_km = weighted_kmeans_1d(log_L, weights_for_km, K)
    bottom_mask = labels_km < (K - 1)
    if bottom_mask.any():
        T_sweeps_K = int(lengths[_pos][bottom_mask].max()) + 1
    else:
        T_sweeps_K = 0
    cluster_results.append({
        "K": K,
        "T_sweeps": T_sweeps_K,
        "T_secs":   T_sweeps_K * sec_per_sweep,
        "centers_secs": (np.exp(centers_log) * sec_per_sweep).tolist(),
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

# ---------------------------------------------------------------------------
# Plot 3a: original two-panel cost/benefit, absolute units
# ---------------------------------------------------------------------------
fig, (ax_cost, ax_benefit) = plt.subplots(
    2, 1, figsize=(11, 8), sharex=True,
    gridspec_kw={"height_ratios": [1, 1]},
)

ax_cost.plot(thresh_secs, migrations, color="#0072B2", linewidth=2,
             marker="o", markersize=3, label="total LtRAM writes (Class 1 + Class 2)")
ax_cost.axhline(tier1_writes, color="green", linestyle=":", linewidth=1.5,
                label=f"Class 1 fixed cost = {int(tier1_writes)} writes (one-time, at fault)")
ax_cost.set_yscale("log")
ax_cost.set_ylabel("LtRAM writes per run (log)")
ax_cost.set_title(
    f"{workload} — migration cost/benefit vs threshold\n"
    f"Class 1 (static-RO): {tier1_pages} pages, {tier1_size_mb:.1f} MB — "
    f"placed at fault time, costs {int(tier1_writes)} LtRAM writes (one-shot)\n"
    f"top: total LtRAM writes  |  bottom: total LtRAM utilization (Class 1 + Class 2)"
)
ax_cost.legend(loc="upper right", fontsize=9)
ax_cost.grid(True, which="both", alpha=0.3)

ax_benefit.plot(thresh_secs, benefit_page_seconds, color="#D55E00", linewidth=2,
                marker="s", markersize=3, label="combined utility")
# Show Class 1's standalone contribution as a horizontal baseline. At any
# threshold, Class 1 alone provides this much utility (zero-cost, since
# the kernel places these pages on LtRAM at fault time).
tier1_pageseconds_baseline = tier1_pages * total_seconds
ax_benefit.axhline(
    tier1_pageseconds_baseline, color="green", linestyle=":", linewidth=1.5,
    label=f"Class 1 alone (free): {tier1_pageseconds_baseline:.2e} page-sec"
)
ax_benefit.axhline(
    ltram_capacity_pageseconds, color="gray", linestyle="--", linewidth=1.5,
    label=f"LtRAM capacity × run time = {ltram_capacity_pageseconds:.2e} "
          f"page-sec ({LTRAM_PAGES} pages × {total_seconds:.0f}s)"
)
ax_benefit.set_xlabel("migration threshold (seconds of clean before migrating)")
ax_benefit.set_ylabel("total LtRAM utilization (page-seconds)")
ax_benefit.legend(loc="upper right", fontsize=9)
ax_benefit.grid(True, which="both", alpha=0.3)

plt.tight_layout()
plt.savefig(out_dir / "dirty_stability_costben.png", dpi=120, bbox_inches="tight")
plt.close()

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
if final_stabs_sweeps is not None and total_pages is not None and total_pages > 0:
    pages_on_ltram_pct = np.array([
        100.0 * (final_stabs_sweeps > T).sum() / total_pages
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

fig, (ax_util, ax_life) = plt.subplots(
    2, 1, figsize=(13, 9), sharex=True,
    gridspec_kw={"height_ratios": [1, 1]},
)

# ---- Top: % of pages on LtRAM ----
ax_util.plot(thresh_secs, pages_on_ltram_pct, color=TAB_BLUE, linewidth=2.5,
             marker="o", markersize=4,
             label="% of pages with final epoch > T (LtRAM-eligible)")
ax_util.axhline(capacity_ceiling_pct, color=TAB_BLUE, linestyle=":",
                linewidth=1.8, alpha=0.7,
                label=f"LtRAM capacity ceiling = {capacity_ceiling_pct:.1f}% "
                      f"({LTRAM_PAGES * 4 / 1024:.0f} MB / total)")
if pages_on_ltram_pct.max() > capacity_ceiling_pct:
    ax_util.axhspan(capacity_ceiling_pct,
                    max(105, pages_on_ltram_pct.max() * 1.05),
                    color=TAB_RED, alpha=0.08,
                    label="capacity-limited (more pages want LtRAM than fit)")
ax_util.set_ylabel("LtRAM-eligible pages\n(% of total workload pages)",
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

# ---- Bottom: % of NOR chip endurance consumed during this run, split ----
# Convention: vermilion = FINAL (one-time placement), blue = INTERMEDIATE
# (recurring during run). y-axis is % of TOTAL_ERASES (chip-level, perfect
# wear leveling) — i.e. "this run consumes X% of the chip's lifetime budget".
# Log scale because values can range from 0.001% (gapbs cold workload) up to
# 100%+ (redis xlong burns through chip in one run at low T).
final_endurance_pct = 100.0 * final_migs / TOTAL_ERASES
inter_endurance_pct = 100.0 * inter_migs / TOTAL_ERASES
total_endurance_pct = 100.0 * migrations / TOTAL_ERASES

# Bottom panel: total % of NOR chip endurance consumed over a 5-year
# deployment vs migration threshold. Single line, no split, no fill.
# Y-axis auto-scales to the max value so all curves are visible regardless
# of workload (matmul ≈0.001%, redis ≈17000% at low T).
SECS_PER_YEAR = 365.25 * 86400
HORIZON_SEC   = 5 * SECS_PER_YEAR

total_5y_writes = final_migs + inter_migs * (HORIZON_SEC / total_seconds)
total_5y_pct    = 100.0 * total_5y_writes / TOTAL_ERASES

ax_life.plot(thresh_secs, total_5y_pct, color=TAB_ORANGE,
             linewidth=2.5, marker="s", markersize=4,
             label="total endurance consumed (5y deployment)")
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
    for ax in (ax_util, ax_life):
        ax.axvline(Ts, color=color, linewidth=2.2, alpha=0.85,
                   linestyle=(0, (5, 2 + K)),  # different dash pattern per K
                   label=label if ax is ax_life else None)

# (Capacity-only T_optimal annotation deliberately dropped: for oversubscribed
# workloads it produces a degenerate "refuse to migrate" answer. Cluster Ts
# above are the policy recommendations; eviction handles capacity overflow.)

# Re-draw legend on bottom panel with cluster Ts included
ax_life.legend(loc="upper right", fontsize=9, framealpha=0.92)

plt.tight_layout()
plt.savefig(out_dir / "dirty_stability_costben_normalized.png",
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
sweep_csv = out_dir / "dirty_sweep.csv"
tier1_pagesw = 0
tier1_pages_count = 0
if sweep_csv.exists():
    df_pages = pd.read_csv(sweep_csv, comment="#")
    df_pages["writable"] = df_pages["vma_perms"].str[1] == "w"
    tier1_pages_count = int((~df_pages["writable"]).sum())
    # Class 1 pages each contribute one epoch of length=total_sweeps to stability_hist.
    # Subtract that from tier2_pagesw to isolate "writable, long-epoch" pages.
    tier1_pagesw = tier1_pages_count * total_sweeps
    tier2_pagesw_writable = max(tier2_pagesw - tier1_pagesw, 0)
    t1_psec = tier1_pagesw * to_sec
    t2w_psec = tier2_pagesw_writable * to_sec
else:
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
plt.savefig(out_dir / "dirty_tiers_perepoch.png", dpi=120, bbox_inches="tight")
plt.close()

print(f"  capacity-feasible threshold (page-seconds, ref only):  T = {T_cap:.1f}s" if T_cap is not None else "  capacity always feasible")
print(f"  cluster-derived T (K-means in log-stability):")
for r in cluster_results:
    centers_str = "  ".join(f"{c:.2f}" for c in r["centers_secs"])
    print(f"    K={r['K']}: T = {r['T_secs']:>6.2f}s   "
          f"cluster centers (s) = [{centers_str}]")

print(f"Wrote dirty_stability_{{hist,cdf,costben*}}.png and dirty_tiers_perepoch.png to {out_dir}")
