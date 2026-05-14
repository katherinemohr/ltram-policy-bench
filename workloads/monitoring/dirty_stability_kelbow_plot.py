"""
K-sweep profiling for stability-period clustering.

Sweeps K from 2 to K_MAX (default 10) and plots metrics that help pick the
"natural" number of clusters for this workload:

  Panel A (top):    Inertia + silhouette vs K
                    Inertia (within-cluster sum of squared log-distances) always
                    decreases — look for the elbow. Silhouette score peaks at the
                    workload's natural K and is the more reliable signal.
  Panel B (middle): Cluster centers ladder — for each K, plot the K cluster centers.
                    Lets you see at which K the workload "runs out" of structure
                    (centers stop spreading or start collapsing).
  Panel C (bottom): Top-boundary T (= max length of bottom K-1 clusters + 1 sweep)
                    plus migrations and LtRAM utilization at that T. Shows how the
                    policy choice moves as you add clusters.

Output: dirty_stability_kelbow.png

Usage: dirty_stability_kelbow_plot.py <workload> <run_name>
"""

import sys
import re
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

sys.path.insert(0, str(Path(__file__).parent))
from _phase_data import load_stability, parse_args_phase

workload, run_name, phase = parse_args_phase(sys.argv)
out_dir = RESULTS_DIR / "runs" / run_name

data = load_stability(out_dir, phase)
total_sweeps  = data["total_sweeps"]
total_seconds = data["total_seconds"]
interval_ms   = data["interval_ms"]
sec_per_sweep = data["sec_per_sweep"]
phase_label   = data["label"]
L = data["L"].astype(float)
C = data["C"].astype(float)
mask_pos = L > 0
L, C = L[mask_pos], C[mask_pos]

K_MIN = 2
K_MAX = min(10, len(L) - 1)   # can't have more clusters than data points
K_RANGE = list(range(K_MIN, K_MAX + 1))

LTRAM_PAGES  = 65536
TOTAL_ERASES = 6.4e9

log_L = np.log(L).reshape(-1, 1)

# Sweep K and collect metrics
inertias       = []
silhouettes    = []
top_T_secs     = []
migrations_top = []
util_top       = []
centers_per_K  = {}

for K in K_RANGE:
    km = KMeans(n_clusters=K, random_state=0, n_init=10)
    km.fit(log_L, sample_weight=C)
    inertias.append(km.inertia_)

    labels = km.labels_
    centers = km.cluster_centers_.flatten()
    order = np.argsort(centers)
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    labels_sorted = rank[labels]
    centers_sorted = centers[order]
    centers_per_K[K] = (np.exp(centers_sorted) * sec_per_sweep).tolist()

    # Silhouette: sklearn doesn't support sample_weight here, so we down-sample
    # by drawing weighted samples (each unique length sampled in proportion to
    # its count). Bounded sample size keeps runtime sane.
    if len(set(labels)) > 1:
        rng = np.random.RandomState(0)
        n_sample = min(int(C.sum()), 4000)
        probs = C / C.sum()
        sample_idx = rng.choice(len(L), size=n_sample, replace=True, p=probs)
        try:
            sil = silhouette_score(log_L[sample_idx], labels[sample_idx])
        except Exception:
            sil = float("nan")
    else:
        sil = float("nan")
    silhouettes.append(sil)

    # Top-boundary T: max length of bottom (K-1) clusters + 1 sweep
    bottom_mask = labels_sorted < (K - 1)
    T_sw = (int(L[bottom_mask].max()) + 1) if bottom_mask.any() else 0
    T_sec = T_sw * sec_per_sweep
    top_T_secs.append(T_sec)

    mask = L > T_sw
    migs = float(C[mask].sum())
    ps   = float(((L[mask] - T_sw) * C[mask]).sum() * sec_per_sweep)
    util = 100.0 * ps / (LTRAM_PAGES * total_seconds)
    migrations_top.append(migs)
    util_top.append(util)

# Pick suggested K = argmax(silhouette)
sil_arr = np.array(silhouettes, dtype=float)
if np.all(np.isnan(sil_arr)):
    K_suggest = None
else:
    K_suggest = K_RANGE[int(np.nanargmax(sil_arr))]

# === Plot: 3 stacked panels ===
fig, (axA, axB, axC) = plt.subplots(3, 1, figsize=(13, 12), sharex=True)

# --- Panel A: inertia (left) + silhouette (right) ---
axA.plot(K_RANGE, inertias, "o-", color="#0072B2", linewidth=2,
         label="inertia (WSS, log-space)")
axA.set_ylabel("inertia (within-cluster SS)", color="#0072B2", fontsize=11)
axA.tick_params(axis="y", labelcolor="#0072B2")
axA.grid(True, alpha=0.3)
axA2 = axA.twinx()
axA2.plot(K_RANGE, silhouettes, "s--", color="#D55E00", linewidth=2,
          label="silhouette score")
axA2.set_ylabel("silhouette (higher = cleaner)", color="#D55E00", fontsize=11)
axA2.tick_params(axis="y", labelcolor="#D55E00")
if K_suggest is not None:
    axA.axvline(K_suggest, color="#009E73", linewidth=2.5, alpha=0.6,
                label=f"silhouette-best K = {K_suggest}")
axA.legend(loc="upper left", fontsize=10)
axA2.legend(loc="upper right", fontsize=10)
axA.set_title(f"{workload} — K-sweep cluster-quality profiling   "
              f"|   K-MAX = {K_MAX}",
              fontsize=12)

# --- Panel B: cluster centers ladder ---
for K in K_RANGE:
    centers = centers_per_K[K]
    axB.scatter([K] * len(centers), centers, s=70,
                c=range(len(centers)), cmap="viridis",
                edgecolor="black", linewidth=0.5)
axB.set_yscale("log")
axB.set_ylabel("cluster center (seconds, log)", fontsize=11)
axB.grid(True, which="both", alpha=0.3)
if K_suggest is not None:
    axB.axvline(K_suggest, color="#009E73", linewidth=2.5, alpha=0.6)

# --- Panel C: top-boundary T + cost & utility ---
axC.plot(K_RANGE, top_T_secs, "o-", color="#0072B2", linewidth=2,
         label="top-boundary T (seconds)")
axC.set_ylabel("top-boundary T (s)", color="#0072B2", fontsize=11)
axC.tick_params(axis="y", labelcolor="#0072B2")
axC.grid(True, alpha=0.3)
axC2 = axC.twinx()
axC2.plot(K_RANGE, migrations_top, "v--", color="#D55E00", linewidth=2,
          label="migrations at top T")
axC2.set_yscale("log")
axC2.set_ylabel("migrations (log)", color="#D55E00", fontsize=11)
axC2.tick_params(axis="y", labelcolor="#D55E00")
if K_suggest is not None:
    axC.axvline(K_suggest, color="#009E73", linewidth=2.5, alpha=0.6)
axC.set_xlabel("K (number of clusters)", fontsize=12)
axC.set_xticks(K_RANGE)
axC.legend(loc="upper left", fontsize=10)
axC2.legend(loc="upper right", fontsize=10)

# Annotate utility at each K
for k, (T, u) in enumerate(zip(top_T_secs, util_top)):
    axC.annotate(f"util={u:.0f}%", xy=(K_RANGE[k], T),
                 xytext=(0, 8), textcoords="offset points",
                 ha="center", fontsize=8, color="#0072B2")

fig.suptitle(
    f"{workload} — K-sweep profiling   "
    f"|   run length = {total_seconds:.0f}s   "
    f"|   silhouette suggests K = {K_suggest}",
    fontsize=12, y=1.00,
)
plt.tight_layout()
plt.savefig(out_dir / f"dirty_stability_kelbow_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

# === Print summary ===
print(f"\n{workload} ({run_name}) — K-sweep profiling")
print(f"  total_sweeps={total_sweeps}  total_seconds={total_seconds:.1f}s  "
      f"interval={interval_ms}ms")
print(f"  silhouette-best K = {K_suggest}")
print(f"  {'K':>3} {'inertia':>14} {'silhouette':>11} {'top T (s)':>10} "
      f"{'migrations':>12} {'util %':>8}")
for K, ine, sil, T, m, u in zip(K_RANGE, inertias, silhouettes, top_T_secs,
                                 migrations_top, util_top):
    flag = "  ← best" if K == K_suggest else ""
    print(f"  {K:>3} {ine:>14.2f} {sil:>11.4f} {T:>10.2f} "
          f"{int(m):>12,} {u:>7.1f}%{flag}")
print(f"\nWrote dirty_stability_kelbow.png to {out_dir}")
