"""
Cluster-based migration threshold selection.

Given the stability-period histogram, run weighted 1D K-means in log space
to discover the workload's natural epoch-length groupings. For each K in
{2, 3, 4} we evaluate ALL K-1 cluster boundaries (not just the top one):

    boundary i (i = 0 .. K-2):
        T_i = max length within cluster i + 1 sweep
        clusters {0..i}    stay on DRAM (epochs do not qualify for migration)
        clusters {i+1..K-1} qualify; pages migrate to LtRAM after T_i sweeps

So K=4 produces 3 candidate T values, K=3 produces 2, K=2 produces 1 — six
candidates in total. Capacity oversubscription (util > 100%) is permitted;
LtRAM eviction is a separate concern handled by the controller.

Output: dirty_stability_clusters.png — one panel per K, with all K-1 boundary
lines drawn on the histogram and a per-boundary metrics table beside it.

Usage: dirty_stability_cluster_plot.py <workload> <run_name>
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
m = re.search(r"total_sweeps=(\d+)\s+total_seconds=([\d.]+)\s+interval_ms=(\d+)", header)
total_sweeps = int(m.group(1))
total_seconds = float(m.group(2))
interval_ms = int(m.group(3))
sec_per_sweep = interval_ms / 1000.0

df = pd.read_csv(csv_path, comment="#")
L = df["stability_period_sweeps"].values.astype(float)
C = df["count"].values.astype(float)
mask_pos = L > 0
L, C = L[mask_pos], C[mask_pos]

LTRAM_PAGES        = 65536
ERASES_PER_CELL    = 100_000
TOTAL_ERASES       = LTRAM_PAGES * ERASES_PER_CELL


def weighted_kmeans_1d(values, weights, K, max_iter=200, n_init=10, seed=0):
    """1D weighted K-means. Returns (centers_sorted_asc, labels_in_sorted_order)."""
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


def metrics_at_T(T_sweeps):
    mask = L > T_sweeps
    migs = float(C[mask].sum())
    pageseconds = float(((L[mask] - T_sweeps) * C[mask]).sum() * sec_per_sweep)
    return migs, pageseconds


# Tier 1 page count for context
sweep_csv = out_dir / "dirty_sweep.csv"
tier1_pages = 0
total_pages = None
if sweep_csv.exists():
    df_pages = pd.read_csv(sweep_csv, comment="#")
    df_pages["writable"] = df_pages["vma_perms"].str[1] == "w"
    tier1_pages = int((~df_pages["writable"]).sum())
    total_pages = len(df_pages)


# === Run K-means for K = 2, 3, 4 ===
log_L = np.log(L)
results = []
for K in [2, 3, 4]:
    if len(L) < K:
        continue
    centers_log, labels = weighted_kmeans_1d(log_L, C, K)
    centers_secs = (np.exp(centers_log) * sec_per_sweep).tolist()

    # All K-1 boundaries: T_i = max(cluster i) + 1 for i in 0..K-2
    boundaries = []
    for i in range(K - 1):
        in_cluster_i = labels == i
        if not in_cluster_i.any():
            continue
        T_sw = int(L[in_cluster_i].max()) + 1
        T_sec = T_sw * sec_per_sweep
        migs, ps = metrics_at_T(T_sw)
        cap_pseconds = LTRAM_PAGES * total_seconds
        util_pct = 100.0 * ps / cap_pseconds
        endurance_pct = 100.0 * migs / TOTAL_ERASES
        boundaries.append({
            "i": i,
            "boundary": f"{i+1}|{i+2}",   # human-readable: clusters i+1 and i+2
            "T_sweeps": T_sw,
            "T_secs": T_sec,
            "migrations": migs,
            "utilization_pct": util_pct,
            "endurance_pct_run": endurance_pct,
        })

    results.append({
        "K": K,
        "centers_secs": centers_secs,
        "labels": labels,
        "boundaries": boundaries,
    })


# === Plot 1: stacked panels per K, in the SAME 1-second linear bins as ===
# dirty_stability_hist.png so the two figures are directly comparable. Each
# bar is split into stacked colored sub-bars, one per cluster contributing
# epochs that fall in that 1-second bucket.
# Okabe-Ito colorblind-safe palette, ordered HOT→COLD by cluster index:
#   cluster 1 (shortest stability = hottest = DRAM-bound) → vermilion
#   cluster K (longest stability  = coldest = LtRAM-friendly) → blue
# Yellow was dropped (too low contrast on white). Hatch patterns below add
# redundant encoding so bars are distinguishable in grayscale / for full
# monochromats. Luminances are deliberately spread (warm→cool also goes
# from light→dark) so the ordering survives a B&W print.
CLUSTER_COLORS = ["#D55E00", "#E69F00", "#009E73", "#0072B2"]   # hot→cold
CLUSTER_HATCHES = ["",       "///",     "...",     "xxx"]      # paired w/ colors
BOUNDARY_LINESTYLES = [(0, (5, 2)), (0, (3, 1, 1, 1)), (0, (1, 1))]  # solid-ish to dotted

# Use the SAME 1-second linear bins as dirty_stability_hist.png so the two
# figures are directly comparable. Tradeoff: when a cluster boundary falls
# *inside* a 1-second bin (e.g. matmul, where K-means centers are sub-second),
# that bin will show stacked colors. That's mathematically honest — it's
# saying "this 1-second bucket of stability times contains epochs from
# multiple K-means groups." Sweep-granularity bins would avoid stacking but
# render the long-stability tail (e.g. redis's 1000s+ epochs) invisibly thin
# at typical figure widths. We prioritize tail visibility.
BIN_WIDTH_SEC = 1.0
length_secs = L * sec_per_sweep
n_bins = int(np.ceil(total_seconds / BIN_WIDTH_SEC)) + 1
bin_idx = np.clip(np.floor(length_secs / BIN_WIDTH_SEC).astype(int),
                  0, n_bins - 1)
bin_centers = (np.arange(n_bins) + 0.5) * BIN_WIDTH_SEC

fig, axes = plt.subplots(len(results), 1, figsize=(14, 4.2 * len(results)),
                         sharex=True)
if len(results) == 1:
    axes = [axes]

for ax, r in zip(axes, results):
    K = r["K"]
    # Bin counts per cluster: sum count[i] into bin[bin_idx[i]] for each cluster
    binned_per_cluster = np.zeros((K, n_bins), dtype=np.float64)
    for k in range(K):
        mk = r["labels"] == k
        np.add.at(binned_per_cluster[k], bin_idx[mk], C[mk])

    # At sweep-granularity binning each bin maps to one cluster, so stacking
    # is a no-op — but we keep the stacking machinery for safety in case a
    # future change widens the bin (then mixed-cluster bins stack honestly).
    bottom = np.zeros(n_bins, dtype=np.float64)
    for k in range(K):
        ax.bar(bin_centers, binned_per_cluster[k], bottom=bottom,
               width=BIN_WIDTH_SEC,
               color=CLUSTER_COLORS[k], edgecolor="black", linewidth=0.0,
               hatch=CLUSTER_HATCHES[k],
               label=f"cluster {k+1}  (center {r['centers_secs'][k]:.2f}s)")
        bottom += binned_per_cluster[k]

    # Vertical line at every cluster boundary (T = max(cluster i) + 1 sweep)
    for bidx, b in enumerate(r["boundaries"]):
        ls = BOUNDARY_LINESTYLES[min(bidx, len(BOUNDARY_LINESTYLES) - 1)]
        ax.axvline(b["T_secs"], color="black", linewidth=2, linestyle=ls,
                   label=(f"T={b['T_secs']:.2f}s  "
                          f"(boundary cluster {b['i']+1}|{b['i']+2}; "
                          f"{int(b['migrations']):,} migs, util={b['utilization_pct']:.0f}%)"))

    ax.set_yscale("log")
    ax.set_ylabel("number of stability periods (log)", fontsize=10)
    ax.set_title(f"K = {K} clusters   |   "
                 f"{len(r['boundaries'])} boundaries evaluated   |   "
                 f"cluster centers (s): "
                 f"[{', '.join(f'{c:.2f}' for c in r['centers_secs'])}]",
                 fontsize=10)
    ax.legend(loc="upper right", fontsize=8.5, framealpha=0.92, ncol=1)
    ax.grid(True, which="both", alpha=0.3)

axes[-1].set_xlabel(
    f"stability period length (seconds, "
    f"{BIN_WIDTH_SEC*1000:.0f}ms bins = sweep granularity)",
    fontsize=11)
fig.suptitle(
    f"{workload} — stability-period histogram colored by K-means cluster\n"
    f"finer bins than dirty_stability_hist.png so each bar = one stability "
    f"length = one cluster (no stacking)\n"
    f"run length = {total_seconds:.0f}s   "
    f"|   workload = {total_pages or '?'} pages, {tier1_pages} static-RO",
    fontsize=12, y=1.00,
)
plt.tight_layout()
plt.savefig(out_dir / "dirty_stability_clusters.png", dpi=120, bbox_inches="tight")
plt.close()


# === Plot 2: page-seconds breakdown per cluster, K = 2/3/4 (pie charts) ===
# This is the cluster-based composition view: of all observed page-seconds,
# how much falls into each cluster? Distinct from the logical Class 1-4
# breakdown — clusters are workload-derived modes, not OS/app annotations.
import matplotlib.patches as mpatches

fig2, axes2 = plt.subplots(1, len(results), figsize=(6.0 * len(results), 6.5))
if len(results) == 1:
    axes2 = [axes2]

for ax, r in zip(axes2, results):
    K = r["K"]
    # page-seconds per cluster = sum(L * count) for epochs in that cluster
    pageseconds = np.zeros(K)
    for k in range(K):
        mk = r["labels"] == k
        pageseconds[k] = float((L[mk] * C[mk]).sum() * sec_per_sweep)
    total_ps = pageseconds.sum() if pageseconds.sum() > 0 else 1.0

    sizes = pageseconds
    colors = CLUSTER_COLORS[:K]
    hatches = CLUSTER_HATCHES[:K]
    labels = [f"Group {k+1}\n(center {r['centers_secs'][k]:.2f}s)"
              for k in range(K)]

    def autopct(pct):
        if pct < 1.5:
            return ""
        return f"{pct:.1f}%\n({pct * total_ps / 100:.0f} p·s)"

    # Pie wedges: solid colors only, no hatching. The wedges are large enough
    # that color alone reads cleanly; the hatching just makes percentages
    # hard to read on a busy background.
    ax.pie(
        sizes, labels=None, colors=colors, autopct=autopct,
        startangle=90, pctdistance=0.72,
        wedgeprops=dict(edgecolor="black", linewidth=0.7),
    )

    legend_labels = [
        f"Group {k+1}: center {r['centers_secs'][k]:>6.2f}s   "
        f"{pageseconds[k]:>10.0f} p·s   ({100*pageseconds[k]/total_ps:>5.1f}%)"
        for k in range(K)
    ]
    patches = [mpatches.Patch(color=colors[k], label=legend_labels[k])
               for k in range(K)]
    ax.legend(handles=patches, loc="center left",
              bbox_to_anchor=(0, -0.15), fontsize=9,
              frameon=True, prop={"family": "monospace"})

    # Top boundary T (most defensible single-T choice for this K)
    top_T = r["boundaries"][-1]["T_secs"] if r["boundaries"] else None
    top_T_str = f"T_top = {top_T:.2f}s" if top_T is not None else ""
    ax.set_title(f"K = {K} clusters   |   {top_T_str}\n"
                 f"page-seconds per cluster", fontsize=11)
    ax.set_aspect("equal")

fig2.suptitle(
    f"{workload} — cluster-group breakdown (by page-seconds)\n"
    f"unsupervised K-means modes — distinct from logical Class 1-4 annotation\n"
    f"run length = {total_seconds:.0f}s   "
    f"|   total page-seconds = {sum((L*C).sum()*sec_per_sweep for _ in [0]):.0f}",
    fontsize=12,
)
plt.tight_layout()
plt.savefig(out_dir / "dirty_stability_cluster_groups.png", dpi=120, bbox_inches="tight")
plt.close()


# === Print summary ===
print(f"\n{workload} ({run_name}):")
print(f"  total_sweeps={total_sweeps}  total_seconds={total_seconds:.1f}s  "
      f"interval={interval_ms}ms")
print(f"  {'K':>3} {'boundary':>10} {'T (s)':>10} {'migrations':>14} "
      f"{'util %':>8} {'NOR-budget % (this run)':>25}")
for r in results:
    for b in r["boundaries"]:
        print(f"  {r['K']:>3} {b['boundary']:>10} {b['T_secs']:>10.2f} "
              f"{int(b['migrations']):>14,} "
              f"{b['utilization_pct']:>7.1f}% "
              f"{b['endurance_pct_run']:>24.4f}%")
print(f"\nWrote dirty_stability_clusters.png to {out_dir}")
