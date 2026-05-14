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

n_pages = len(df)
if n_pages == 0:
    print("WARNING: no pages")
    sys.exit(0)


# === K-means on stability histogram (same logic as cluster plot) ===
sf_data = load_stability(out_dir, phase)
L = sf_data["L"].astype(float)
C = sf_data["C"].astype(float)
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


# === Downsampling parameters (mirrors dirty_timeline_plot.py) ===
# For huge workloads (e.g. gapbs xlong: 565k pages × 6000 sweeps = 3.4G cells)
# we never materialize the full matrix. Downsample by integer stride in BOTH
# axes; each output cell takes max-priority over the cells it represents
# (DIRTY > clusters > NOT_PRESENT) so write events survive downsampling.
MAX_W = 1600
MAX_H = 1600
t_stride = max(1, (total_sweeps + MAX_W - 1) // MAX_W)
p_stride = max(1, (n_pages       + MAX_H - 1) // MAX_H)
n_t_bins = (total_sweeps + t_stride - 1) // t_stride
n_p_bins = (n_pages       + p_stride - 1) // p_stride
if t_stride > 1 or p_stride > 1:
    print(f"[downsample] {n_pages} pages × {total_sweeps} sweeps → "
          f"{n_p_bins} × {n_t_bins}  (p_stride={p_stride}, t_stride={t_stride})")
else:
    print(f"[full-res] {n_pages} pages × {total_sweeps} sweeps")


def _build_row_state(K, centers_log, events):
    """Build a per-page 1D state row of length total_sweeps (uint8)."""
    NOT_PRESENT, DIRTY = 0, K + 1
    state = np.full(total_sweeps, NOT_PRESENT, dtype=np.uint8)
    if not events:
        cid = epoch_length_to_cluster(total_sweeps, centers_log)
        state[:] = cid + 1
        return state, total_sweeps
    for (s, e, L_sw) in page_epochs(events, total_sweeps):
        cid = epoch_length_to_cluster(L_sw, centers_log)
        state[s:e + 1] = cid + 1
    for ev in events:
        if 0 <= ev < total_sweeps:
            state[ev] = DIRTY
    last = events[-1]
    return state, total_sweeps - 1 - last


def _downsample_time(state, t_stride, n_t_bins):
    pad = n_t_bins * t_stride - len(state)
    if pad > 0:
        state = np.concatenate([state, np.zeros(pad, dtype=np.uint8)])
    return state.reshape(n_t_bins, t_stride).max(axis=1)


def build_state_matrix(K, centers_log, page_order):
    """Return downsampled grid for the given page ordering. Streams pages
    in `page_order` and time-downsamples each row into the output grid.
    Memory: O(n_p_bins × n_t_bins) plus O(total_sweeps) per row —
    never the full O(n_pages × total_sweeps).
    """
    grid = np.zeros((n_p_bins, n_t_bins), dtype=np.uint8)
    write_events_col = df["write_events"].values
    for new_idx, old_idx in enumerate(page_order):
        events = parse_events(write_events_col[old_idx])
        row_state, _ = _build_row_state(K, centers_log, events)
        row_ds = _downsample_time(row_state, t_stride, n_t_bins)
        p_bin = new_idx // p_stride
        np.maximum(grid[p_bin], row_ds, out=grid[p_bin])
    return grid


# Pre-compute per-page final-epoch length once (cheap; doesn't depend on K)
# so we can sort pages BEFORE rendering, instead of sorting an already-
# downsampled grid (which would mix unrelated pages within a p_bin).
_we_col = df["write_events"].values
_final_lengths = np.zeros(n_pages, dtype=np.int32)
for _i in range(n_pages):
    _evs = parse_events(_we_col[_i])
    _final_lengths[_i] = (total_sweeps if not _evs
                          else total_sweeps - 1 - _evs[-1])
sort_idx = np.argsort(-_final_lengths, kind="stable")


# === Render: one figure per K, processed sequentially to keep memory low ===
# Large workloads (e.g. redis with 177K pages × 3312 sweeps) blow up if all
# three K state matrices live simultaneously. We build, render, save, and
# release for each K in turn.
import gc

print(f"Workload: {workload} ({n_pages} pages × {total_sweeps} sweeps)")
for K in [2, 3, 4]:
    centers_log = cluster_centers_per_K[K]
    img = build_state_matrix(K, centers_log, sort_idx)

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
    if t_stride > 1 or p_stride > 1:
        ax.text(0.99, 0.02,
                f"downsampled p_stride={p_stride}, t_stride={t_stride}",
                transform=ax.transAxes, fontsize=8, ha="right", va="bottom",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                          edgecolor="gray", alpha=0.8))

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
    out_path = out_dir / f"dirty_timeline_clusters_K{K}_{phase}.png"
    plt.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    # Free memory before next K
    del img
    gc.collect()
    print(f"  K={K}: wrote {out_path.name}  centers={centers_sec.tolist()}")
