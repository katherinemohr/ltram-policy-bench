"""
LtRAM utilization vs time, under candidate migration policies.

For each migration threshold T, simulate the policy and compute, at every
sweep, the number of pages currently sitting on LtRAM:

  A page is on LtRAM at sweep t iff:
    * it's inside a clean epoch (no write events since some recent write), AND
    * that clean epoch has lasted ≥ T sweeps so far (so the policy already
      decided to migrate it)

Pages with no recorded write events at all (Class 1, static-RO) are treated
as on LtRAM the entire run after their first appearance.

Output: dirty_ltram_utilization.png
  Single panel: % of LtRAM capacity used over time, one curve per candidate T.
  - T = 0 baseline (instant migration → maximum theoretical utilization)
  - T values derived from K-means clustering (K=2, 3, 4 top boundaries)
  - 100% horizontal line: LtRAM capacity ceiling — anything above means
    eviction is required because the policy would over-commit LtRAM.

Usage: dirty_ltram_utilization_plot.py <workload> <run_name>
"""

import sys
import re
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

sys.path.insert(0, str(Path(__file__).parent))
from _phase_data import load_pages, load_stability, parse_args_phase

workload, run_name, phase = parse_args_phase(sys.argv)
out_dir = RESULTS_DIR / "runs" / run_name

df = load_pages(out_dir, phase)
total_sweeps  = df.attrs["total_sweeps"]
total_seconds = df.attrs["total_seconds"]
interval_ms   = df.attrs["interval_ms"]
sec_per_sweep = interval_ms / 1000.0
phase_label   = df.attrs["label"]
if "write_events" not in df.columns:
    df["write_events"] = ""
n_pages_total = len(df)
if n_pages_total == 0:
    print("WARNING: no pages")
    sys.exit(0)

LTRAM_PAGES = 65536    # 256 MB / 4 KB


def parse_events(s):
    if not s:
        return []
    return [int(t) for t in s.split(";")]


def compute_ltram_timeline(T_sweeps):
    """Return a length-total_sweeps int array: pages on LtRAM at each sweep.

    Uses delta-encoding + cumulative sum to avoid O(pages × sweeps) work.
    Each interval (start, end) where a page is on LtRAM contributes
    delta[start] += 1, delta[end+1] -= 1; final cumsum gives occupancy.
    """
    delta = np.zeros(total_sweeps + 1, dtype=np.int64)

    for events_str in df["write_events"]:
        events = parse_events(events_str)
        if not events:
            # Static-RO: on LtRAM the whole run
            delta[0] += 1
            delta[total_sweeps] -= 1
            continue

        # Intermediate epochs: between consecutive write events
        # Epoch [prev+1, nxt-1], length = nxt - prev - 1
        prev = events[0]
        for nxt in events[1:]:
            length = nxt - prev - 1
            if length > T_sweeps:
                start = prev + 1 + T_sweeps
                end   = nxt - 1
                delta[start] += 1
                delta[end + 1] -= 1
            prev = nxt
        # Final epoch: [last_event+1, total_sweeps-1]
        if events[-1] + 1 < total_sweeps:
            length = total_sweeps - 1 - events[-1]
            if length > T_sweeps:
                start = events[-1] + 1 + T_sweeps
                end   = total_sweeps - 1
                delta[start] += 1
                delta[end + 1] -= 1

    return np.cumsum(delta[:total_sweeps])


# === K-means in log-stability-time to derive K=2/3/4 cluster Ts ===
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
        inertia = sum(
            (weights[labels == k] * (values[labels == k] - centers[k]) ** 2).sum()
            for k in range(K) if (labels == k).any()
        )
        if inertia < best_inertia:
            best_inertia, best_centers, best_labels = inertia, centers, labels
    order = np.argsort(best_centers)
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    return best_centers[order], rank[best_labels]


cluster_Ts_sweeps = {}
try:
    sf_data = load_stability(out_dir, phase)
    L_arr = sf_data["L"].astype(float)
    C_arr = sf_data["C"].astype(float)
    pos = L_arr > 0
    L_arr, C_arr = L_arr[pos], C_arr[pos]
    log_L = np.log(L_arr)
    for K in [2, 3, 4]:
        if len(L_arr) < K:
            continue
        _, labels = weighted_kmeans_1d(log_L, C_arr, K)
        bottom_mask = labels < (K - 1)
        if bottom_mask.any():
            T_sw = int(L_arr[bottom_mask].max()) + 1
        else:
            T_sw = 0
        cluster_Ts_sweeps[K] = T_sw
except FileNotFoundError:
    pass

# === Compute timelines for several T candidates ===
candidate_Ts = [("T = 0s (instant)", 0)]
for K in [2, 3, 4]:
    if K in cluster_Ts_sweeps:
        Tsw = cluster_Ts_sweeps[K]
        candidate_Ts.append(
            (f"T = {Tsw * sec_per_sweep:.1f}s  (K={K} cluster top-boundary)", Tsw)
        )

print(f"{workload} ({run_name}) — LtRAM utilization simulation")
print(f"  total_sweeps={total_sweeps}  total_seconds={total_seconds:.1f}s  "
      f"n_pages={n_pages_total}")

timelines = {}
for label, Tsw in candidate_Ts:
    tl = compute_ltram_timeline(Tsw)
    timelines[label] = tl
    pct = 100.0 * tl / LTRAM_PAGES
    print(f"  {label}:  peak {pct.max():.1f}% capacity  |  "
          f"mean {pct.mean():.1f}%  |  "
          f"final {pct[-1]:.1f}%")

# === Plot ===
TAB_COLORS = ["#999999", "#1f77b4", "#ff7f0e", "#2ca02c"]   # T=0 grey, K=2/3/4 colors
TAB_STYLES = ["-", "-", "--", "-."]

x_secs = np.arange(total_sweeps) * sec_per_sweep

fig, ax = plt.subplots(figsize=(14, 6.5))

for (label, Tsw), color, style in zip(candidate_Ts, TAB_COLORS, TAB_STYLES):
    tl = timelines[label]
    pct = 100.0 * tl / LTRAM_PAGES
    ax.plot(x_secs, pct, color=color, linewidth=2, linestyle=style, label=label)

ax.axhline(100, color="black", linewidth=1.4, linestyle=":",
           label="LtRAM capacity ceiling (100% = 65,536 pages)")

# Shade above-capacity region in light red — eviction territory
ymax = max(105, max(100.0 * timelines[lbl].max() / LTRAM_PAGES
                    for lbl, _ in candidate_Ts))
if ymax > 100:
    ax.axhspan(100, ymax, color="#d62728", alpha=0.06,
               label="oversubscribed (eviction required)")

ax.set_xlabel("time (seconds since run start)", fontsize=11)
ax.set_ylabel("LtRAM utilization (% of capacity)", fontsize=11)
ax.set_title(
    f"{workload} — LtRAM occupancy over time, by migration threshold\n"
    f"each curve simulates the policy: page joins LtRAM after T sweeps clean, "
    f"leaves on next write\n"
    f"run length = {total_seconds:.0f}s   |   "
    f"workload = {n_pages_total} pages   |   "
    f"LtRAM capacity = {LTRAM_PAGES:,} pages",
    fontsize=11,
)
ax.set_ylim(0, ymax * 1.02)
ax.legend(loc="upper right", fontsize=10, framealpha=0.92)
ax.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig(out_dir / f"dirty_ltram_utilization_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

print(f"\nWrote dirty_ltram_utilization.png to {out_dir}")
