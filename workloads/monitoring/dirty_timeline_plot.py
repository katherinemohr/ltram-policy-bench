"""
Per-page write-history timeline plots.

Reads dirty_sweep.csv and renders:
  dirty_timeline.png         — raster, each row is one page in original order,
                               colored by per-sweep state.
  dirty_timeline_sorted.png  — same raster, but rows sorted in descending
                               order of final-epoch length (top rows = pages
                               whose data has been quiet the longest).

Color encoding per cell (one cell = one (page, sweep)):
  white         page not present at that sweep
  light blue    clean during an intermediate epoch
  vermilion     clean during the final epoch (page's current state)
  dark blue     written that sweep (the dirty-bit observation)

For large workloads (would-be matrix > MAX_W × MAX_H cells) the timeline is
downsampled by integer stride in BOTH dimensions, never materializing the
full state matrix in memory. Each downsampled cell takes the max-priority
state (DIRTY > FINAL > INTERMEDIATE > NOT_PRESENT) over the cells it
represents — i.e. write events are preserved through downsampling rather
than being averaged away.

Usage: dirty_timeline_plot.py <workload> <run_name>
"""

import sys
import re
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap, BoundaryNorm
import matplotlib.patches as mpatches
import pandas as pd

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

workload = sys.argv[1]
run_name = sys.argv[2]
out_dir = RESULTS_DIR / "runs" / run_name
csv_path = out_dir / "dirty_sweep.csv"

with open(csv_path) as f:
    header_line = f.readline().strip()
m = re.search(r"total_sweeps=(\d+)\s+total_seconds=([\d.]+)\s+interval_ms=(\d+)",
              header_line)
total_sweeps  = int(m.group(1))
total_seconds = float(m.group(2))
interval_ms   = int(m.group(3))
sec_per_sweep = interval_ms / 1000.0

df = pd.read_csv(csv_path, comment="#", dtype={"write_events": str})
df["write_events"] = df["write_events"].fillna("")

if "write_events" not in df.columns:
    print("ERROR: dirty_sweep.csv has no write_events column. "
          "Re-run dirty_sweep with the updated binary.")
    sys.exit(1)

n_pages_total = len(df)
if n_pages_total == 0:
    print("WARNING: no pages in dirty_sweep.csv")
    sys.exit(0)

# Encoding for per-(page, sweep) state
STATE_NOT_PRESENT  = 0
STATE_INTERMEDIATE = 1
STATE_FINAL        = 2
STATE_DIRTY        = 3
COLORMAP = ListedColormap(["white", "#56B4E9", "#D55E00", "#0072B2"])

# === Downsampling parameters ===
# Target image resolution caps. Anything larger than these gets downsampled
# by integer stride. For small workloads the strides are 1 (no downsample).
MAX_W = 1600    # time-axis cells (≈ pixels at 14" × 120 dpi)
MAX_H = 1600    # page-axis cells

t_stride = max(1, (total_sweeps + MAX_W - 1) // MAX_W)
p_stride = max(1, (n_pages_total + MAX_H - 1) // MAX_H)
n_t_bins = (total_sweeps + t_stride - 1) // t_stride
n_p_bins = (n_pages_total + p_stride - 1) // p_stride

if t_stride > 1 or p_stride > 1:
    print(f"[downsample] {n_pages_total} pages × {total_sweeps} sweeps → "
          f"{n_p_bins} × {n_t_bins}  "
          f"(p_stride={p_stride}, t_stride={t_stride})")
else:
    print(f"[full-res] {n_pages_total} pages × {total_sweeps} sweeps")


def parse_events(s):
    if not s:
        return []
    return [int(t) for t in s.split(";")]


def build_state_row(events):
    """Per-page 1D state array. Memory: total_sweeps bytes."""
    state = np.full(total_sweeps, STATE_NOT_PRESENT, dtype=np.uint8)
    if not events:
        # Static-RO: entire run is the FINAL state.
        state[:] = STATE_FINAL
        return state, total_sweeps
    first_event = events[0]
    last_event  = events[-1]
    if first_event < last_event:
        state[first_event:last_event + 1] = STATE_INTERMEDIATE
    if last_event + 1 < total_sweeps:
        state[last_event + 1:] = STATE_FINAL
    for e in events:
        if 0 <= e < total_sweeps:
            state[e] = STATE_DIRTY
    final_length = total_sweeps - 1 - last_event
    return state, final_length


def downsample_time(state, t_stride, n_t_bins):
    """Reduce 1D state from total_sweeps → n_t_bins by max-priority within
    each t_stride window. Padding (if any) is NOT_PRESENT."""
    pad = n_t_bins * t_stride - len(state)
    if pad > 0:
        state = np.concatenate([state, np.zeros(pad, dtype=np.uint8)])
    return state.reshape(n_t_bins, t_stride).max(axis=1)


def build_grid(page_order):
    """Build the downsampled (n_p_bins × n_t_bins) state grid by streaming
    over pages in `page_order`. Rows are merged with max-priority when
    p_stride > 1 (i.e. several pages collapse into one output row)."""
    grid = np.zeros((n_p_bins, n_t_bins), dtype=np.uint8)
    final_lens = np.zeros(n_pages_total, dtype=np.int32)
    write_events_col = df["write_events"].values
    for new_idx, old_idx in enumerate(page_order):
        events = parse_events(write_events_col[old_idx])
        state, fl = build_state_row(events)
        final_lens[old_idx] = fl
        state_t = downsample_time(state, t_stride, n_t_bins)
        p_bin = new_idx // p_stride
        np.maximum(grid[p_bin], state_t, out=grid[p_bin])
    return grid, final_lens


# Pass 1: original page order (also yields final_lengths for sorting)
grid_unsorted, final_lengths = build_grid(np.arange(n_pages_total))


def render(grid, fname, title_suffix, ylabel):
    fig, ax = plt.subplots(figsize=(14, 8))
    extent = (0, total_seconds, 0, n_pages_total)
    norm = BoundaryNorm([0, 1, 2, 3, 4], COLORMAP.N)
    ax.imshow(grid, aspect="auto", interpolation="nearest",
              cmap=COLORMAP, norm=norm, extent=extent, origin="lower")
    ax.set_xlabel("time (seconds since run start)")
    ax.set_ylabel(ylabel)
    ds_str = (f"  |  downsampled p_stride={p_stride}, t_stride={t_stride}"
              if (p_stride > 1 or t_stride > 1) else "")
    ax.set_title(f"{workload} — per-page write timeline {title_suffix}\n"
                 f"{total_seconds:.1f}s run, {total_sweeps} sweeps × "
                 f"{interval_ms}ms{ds_str}")

    legend = [
        mpatches.Patch(color="#0072B2", label="dirty (write observed)"),
        mpatches.Patch(color="#56B4E9", label="clean — intermediate epoch"),
        mpatches.Patch(color="#D55E00", label="clean — final epoch"),
        mpatches.Patch(color="white",   label="not present", ec="black"),
    ]
    ax.legend(handles=legend, loc="center left", bbox_to_anchor=(1.02, 0.5),
              fontsize=10)
    plt.tight_layout()
    plt.savefig(out_dir / fname, dpi=120, bbox_inches="tight")
    plt.close()


# Plot 1: original order. Low addresses at bottom (origin='lower' + ascending /proc/maps).
render(grid_unsorted, "dirty_timeline.png",
       "(original page order, low addresses at bottom)",
       ylabel=f"virtual page index ({n_pages_total} pages, addr ↑)")

# Pass 2: sorted DESCENDING by final-epoch length so longest is at the BOTTOM.
sort_idx = np.argsort(-final_lengths, kind="stable")
grid_sorted, _ = build_grid(sort_idx)

render(grid_sorted, "dirty_timeline_sorted.png",
       "(sorted by final-epoch length, longest/best at bottom)",
       ylabel=f"virtual page index, sorted ({n_pages_total} pages, longer-final ↓)")

print(f"Wrote dirty_timeline.png and dirty_timeline_sorted.png to {out_dir}")
print(f"  pages plotted: {n_pages_total}  "
      f"(image grid {n_p_bins} × {n_t_bins})")
print(f"  pages whose data has been quiet for ≥50% of run: "
      f"{int((final_lengths >= total_sweeps * 0.5).sum())}")
