"""
Cluster-colored per-page write timeline.

Like dirty_timeline.png, but each (page, sweep) cell is colored by which
K-means cluster the *containing epoch* belongs to. Three panels (K=2/3/4)
let you see how clustering granularity changes the picture.

Each cell encodes:
  white       page not present at that sweep (before its first observed write)
  cluster col cluster ID of the epoch this cell falls inside
  black       write event (1-sweep flash; epoch transition)

The cluster colormap goes cool→warm: cluster 1 (shortest stability, "hot",
DRAM-resident) is warm-vermilion; the highest cluster (most-stable, best
LtRAM candidate) is dark-blue.

Usage: dirty_timeline_clusters_plot.py <workload> <run_name>
"""

import sys
import re
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap, BoundaryNorm
import matplotlib.patches as mpatches

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

workload = sys.argv[1]
run_name = sys.argv[2]
out_dir = RESULTS_DIR / "runs" / run_name
sweep_csv     = out_dir / "dirty_sweep.csv"
stability_csv = out_dir / "dirty_sweep_stability.csv"

with open(sweep_csv) as f:
    header = f.readline().strip()
m = re.search(r"total_sweeps=(\d+)\s+total_seconds=([\d.]+)\s+interval_ms=(\d+)", header)
total_sweeps  = int(m.group(1))
total_seconds = float(m.group(2))
interval_ms   = int(m.group(3))
sec_per_sweep = interval_ms / 1000.0

df = pd.read_csv(sweep_csv, comment="#", dtype={"write_events": str})
df["write_events"] = df["write_events"].fillna("")

if "write_events" not in df.columns:
    print("ERROR: dirty_sweep.csv missing write_events column.")
    sys.exit(1)

n_pages = len(df)
if n_pages == 0:
    print("WARNING: no pages")
    sys.exit(0)


# === K-means on stability histogram (same logic as cluster plot) ===
df_stab = pd.read_csv(stability_csv, comment="#")
L = df_stab["stability_period_sweeps"].values.astype(float)
C = df_stab["count"].values.astype(float)
mp = L > 0
L, C = L[mp], C[mp]
log_L = np.log(L)


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
    return best_centers[order]


def epoch_length_to_cluster(length_sweeps, centers_log_sorted):
    """Map an epoch length (in sweeps) to its nearest K-means cluster ID."""
    log_len = np.log(max(1, length_sweeps))
    return int(np.argmin(np.abs(centers_log_sorted - log_len)))


# Compute centers for K = 2, 3, 4
cluster_centers_per_K = {K: weighted_kmeans_1d(log_L, C, K) for K in [2, 3, 4]}


# === Build the per-(page, sweep) cluster matrix for each K ===
# State encoding (per K):
#   0       NOT_PRESENT (white)
#   1..K    cluster id + 1
#   K+1     DIRTY (black, 1-sweep transition marker)
def parse_events(s):
    if not s:
        return []
    return [int(t) for t in s.split(";")]

# We'll precompute per-page (epoch_start, epoch_end_inclusive, length_sweeps) tuples.
def page_epochs(events, total_sweeps):
    """Yield (start, end_inclusive, length_sweeps) for each clean epoch.
    Cells before the FIRST event are NOT_PRESENT, not part of any epoch.
    Cells AT each event are DIRTY transitions.
    """
    if not events:
        # Static-RO: one giant epoch covering the whole run.
        yield (0, total_sweeps - 1, total_sweeps)
        return
    # Between events: clean intermediate epochs
    for prev, nxt in zip(events[:-1], events[1:]):
        s, e = prev + 1, nxt - 1
        if s <= e:
            yield (s, e, e - s + 1)
    # After last event: final epoch
    last = events[-1]
    if last + 1 <= total_sweeps - 1:
        yield (last + 1, total_sweeps - 1, total_sweeps - 1 - last)


def build_state_matrix(K, centers_log):
    NOT_PRESENT = 0
    DIRTY = K + 1
    state = np.full((n_pages, total_sweeps), NOT_PRESENT, dtype=np.uint8)
    final_lengths = np.zeros(n_pages, dtype=np.int32)

    for i, row in enumerate(df.itertuples(index=False)):
        events = parse_events(getattr(row, "write_events"))
        if not events:
            # Static-RO: assign whole run to longest cluster (highest length)
            cid = epoch_length_to_cluster(total_sweeps, centers_log)
            state[i, :] = cid + 1
            final_lengths[i] = total_sweeps
            continue
        for (s, e, L_sw) in page_epochs(events, total_sweeps):
            cid = epoch_length_to_cluster(L_sw, centers_log)
            state[i, s:e + 1] = cid + 1
        # Mark write events as DIRTY (1-sweep markers, override)
        for ev in events:
            if 0 <= ev < total_sweeps:
                state[i, ev] = DIRTY
        # Track final-epoch length for sorting
        last = events[-1]
        final_lengths[i] = total_sweeps - 1 - last
    return state, final_lengths


# === Render: one figure per K, processed sequentially to keep memory low ===
# Large workloads (e.g. redis with 177K pages × 3312 sweeps) blow up if all
# three K state matrices live simultaneously. We build, render, save, and
# release for each K in turn.
import gc

print(f"Workload: {workload} ({n_pages} pages × {total_sweeps} sweeps)")
for K in [2, 3, 4]:
    centers_log = cluster_centers_per_K[K]
    state, final_lengths = build_state_matrix(K, centers_log)
    sort_idx = np.argsort(-final_lengths, kind="stable")
    img = state[sort_idx]

    # Same Okabe-Ito hot→cold ordering as dirty_stability_cluster_plot.py.
    # Yellow swapped out for bluish-green to improve contrast and luminance
    # ordering (warm-light → cool-dark).
    base_colors = ["#D55E00", "#E69F00", "#009E73", "#0072B2"]   # cluster 1..K hot→cold
    cmap_list = ["white"] + base_colors[:K] + ["black"]
    cmap = ListedColormap(cmap_list)
    norm = BoundaryNorm(np.arange(len(cmap_list) + 1) - 0.5, cmap.N)

    fig, ax = plt.subplots(figsize=(14, 6))
    extent = (0, total_seconds, 0, n_pages)
    ax.imshow(img, aspect="auto", interpolation="nearest",
              cmap=cmap, norm=norm, extent=extent, origin="lower")

    centers_sec = np.exp(centers_log) * sec_per_sweep
    legend = [mpatches.Patch(color="white", ec="black", label="not present")]
    for k in range(K):
        legend.append(
            mpatches.Patch(color=base_colors[k],
                           label=f"cluster {k+1}  (center {centers_sec[k]:.2f}s)")
        )
    legend.append(mpatches.Patch(color="black", label="write event"))
    ax.legend(handles=legend, loc="center left",
              bbox_to_anchor=(1.02, 0.5), fontsize=10)

    ax.set_xlabel("time (seconds since run start)", fontsize=11)
    ax.set_ylabel(f"page rows (sorted, longer-final ↓)", fontsize=11)
    ax.set_title(
        f"{workload} — cluster-colored timeline, K = {K}  "
        f"|   centers (s): [{', '.join(f'{c:.2f}' for c in centers_sec)}]\n"
        f"({total_seconds:.1f}s run, {total_sweeps} sweeps × {interval_ms}ms, "
        f"{n_pages} pages)",
        fontsize=11,
    )
    plt.tight_layout()
    out_path = out_dir / f"dirty_timeline_clusters_K{K}.png"
    plt.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    # Free memory before next K
    del state, img, sort_idx, final_lengths
    gc.collect()
    print(f"  K={K}: wrote {out_path.name}  centers={centers_sec.tolist()}")
