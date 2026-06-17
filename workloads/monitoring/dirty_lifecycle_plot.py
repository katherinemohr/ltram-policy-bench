"""
Per-incarnation lifecycle raster (page-lifecycle-view, T03/T04).

Reads the dirty_sweep_lifecycle.csv sidecar (one row per page *incarnation*,
produced by dirty_sweep.c) and renders a full-population raster:

  dirty_lifecycle_{phase}.png   one row per incarnation, X = time.

Unlike dirty_timeline_*.png (which infers state from write events and so can't
tell "not allocated" from "freed"), this uses the incarnation's real
first_seen/last_seen to draw true allocation and deallocation boundaries.

Cell-state encoding (GRILL C03):
  NOT_YET_ALLOCATED  before first_seen                       -> white
  CLEAN_INTERMEDIATE present, clean, before the last clean run -> light blue
  CLEAN_FINAL        present, in the last clean run to last_seen -> dark blue
  WRITTEN            sweep in write_events                    -> vermilion
  FREED_GONE         after last_seen                          -> light grey

Downsample priority (max wins, GRILL C03):
  WRITTEN > CLEAN_INTERMEDIATE > FREED_GONE > NOT_YET_ALLOCATED > CLEAN_FINAL

Row order (GRILL D09): first_seen ascending; within a same-first_seen cohort,
longest-clean-run descending; final tie-break (vma_start, vpage_idx).

Usage: dirty_lifecycle_plot.py <workload> <run_name> [phase] (phase: run|load|full)

The "full" phase merges the load and run lifecycle sidecars into one timeline:
run sweep indices are offset by load_total_sweeps, and an incarnation present at
the last load sweep AND the first run sweep (same page) is stitched into a single
boundary-spanning incarnation rather than double-counted.
"""

import sys
import re
from pathlib import Path

import numpy as np

# --- C03 state values: ordered so np.max() yields the C03 downsample priority.
# WRITTEN highest; CLEAN_FINAL lowest (a merged bin reads "final/settled" only
# when every constituent cell is in its final clean run).
STATE_CLEAN_FINAL  = 0
STATE_NOT_ALLOC    = 1
STATE_FREED        = 2
STATE_CLEAN_INTER  = 3
STATE_WRITTEN      = 4

# COLORMAP index == state value.
_COLORS = ["#0072B2", "white", "#C8C8C8", "#56B4E9", "#D55E00"]

# Downsample caps — same as dirty_timeline_plot.py for comparable figures (R11).
MAX_W = 1600
MAX_H = 1600


# === Lifecycle CSV → records =================================================

def _parse_events(s):
    if not s or (isinstance(s, float)):
        return []
    s = str(s)
    if not s:
        return []
    return [int(t) for t in s.split(";")]


def load_lifecycle(path):
    """Parse a dirty_sweep_lifecycle.csv. Returns (records, meta).

    records: list of dicts with first_seen, last_seen, events (sorted list),
             vma_start (int), vpage_idx (int), vma_perms, vma_path.
    meta:    total_sweeps, total_seconds, interval_ms, sec_per_sweep.
    """
    path = Path(path)
    with open(path) as f:
        header = f.readline()
    m = re.search(r"total_sweeps=(\d+)\s+total_seconds=([\d.]+)\s+interval_ms=(\d+)",
                  header)
    if not m:
        raise ValueError(f"{path}: bad metadata header line")
    total_sweeps = int(m.group(1))
    total_seconds = float(m.group(2))
    interval_ms = int(m.group(3))

    recs = []
    with open(path) as f:
        next(f)  # metadata comment
        cols = next(f).rstrip("\n").split(",")
        idx = {name: i for i, name in enumerate(cols)}
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            # write_events is the last field and contains no commas internally
            # (it's ';'-joined), so a plain split is safe.
            parts = line.split(",")
            ev = parts[idx["write_events"]] if len(parts) > idx["write_events"] else ""
            recs.append({
                "vma_start": int(parts[idx["vma_start"]], 16),
                "vma_perms": parts[idx["vma_perms"]],
                "vma_path":  parts[idx["vma_path"]],
                "vpage_idx": int(parts[idx["vpage_idx"]]),
                "first_seen": int(parts[idx["first_seen"]]),
                "last_seen":  int(parts[idx["last_seen"]]),
                "events": sorted(_parse_events(ev)),
            })
    meta = dict(total_sweeps=total_sweeps, total_seconds=total_seconds,
                interval_ms=interval_ms, sec_per_sweep=interval_ms / 1000.0)
    return recs, meta


# === "full" merge: load + run on one timeline ================================

def _stitch_full(load_recs, load_ts, run_recs):
    """Merge per-phase incarnation records onto a single merged timeline.

    Run sweep indices are shifted by load_ts (so run sweep s -> load_ts + s).
    A load incarnation present at the LAST load sweep (last_seen == load_ts - 1)
    is the same continuous incarnation as the run incarnation for that page that
    is present at the FIRST run sweep (first_seen == 0): the workload process
    persists across the two separate dirty_sweep invocations, so such a page was
    never actually freed. Those two are stitched into one incarnation; everyone
    else is kept verbatim (load side) or offset by load_ts (run side).

    Page identity is (vma_start, vpage_idx) — the lifecycle CSV does not carry
    vma_end, and vma_start uniquely identifies the mapping within a run.
    """
    def key(r):
        return (r["vma_start"], r["vpage_idx"])

    # First run incarnation per page that opens at run sweep 0 (boundary side).
    run_boundary = {}
    for i, r in enumerate(run_recs):
        if r["first_seen"] == 0:
            run_boundary.setdefault(key(r), i)

    merged = []
    used_run = set()
    for lr in load_recs:
        ri = run_boundary.get(key(lr)) if lr["last_seen"] == load_ts - 1 else None
        if ri is not None and ri not in used_run:
            rr = run_recs[ri]
            used_run.add(ri)
            # events stay sorted: every offset run event > every load event.
            merged.append({**lr,
                           "last_seen": rr["last_seen"] + load_ts,
                           "events": lr["events"] + [e + load_ts for e in rr["events"]],
                           # prefer run-side (final) perms/path, as _phase_data 'full' does
                           "vma_perms": rr["vma_perms"],
                           "vma_path":  rr["vma_path"]})
        else:
            merged.append(dict(lr))
    for i, rr in enumerate(run_recs):
        if i in used_run:
            continue
        merged.append({**rr,
                       "first_seen": rr["first_seen"] + load_ts,
                       "last_seen":  rr["last_seen"] + load_ts,
                       "events":     [e + load_ts for e in rr["events"]]})
    return merged


def load_lifecycle_full(out_dir):
    """Load + run merged lifecycle records. Returns (records, meta) shaped like
    load_lifecycle, with total_sweeps = load_total + run_total."""
    out_dir = Path(out_dir)
    run_recs, run_meta = load_lifecycle(out_dir / "dirty_sweep_lifecycle.csv")
    load_recs, load_meta = load_lifecycle(out_dir / "dirty_sweep_load_lifecycle.csv")
    load_ts = load_meta["total_sweeps"]
    recs = _stitch_full(load_recs, load_ts, run_recs)
    meta = dict(total_sweeps=load_ts + run_meta["total_sweeps"],
                total_seconds=load_meta["total_seconds"] + run_meta["total_seconds"],
                interval_ms=run_meta["interval_ms"],
                sec_per_sweep=run_meta["interval_ms"] / 1000.0)
    return recs, meta


# === C03 state row + D09 sort key (factored out so they are unit-testable) ===

def build_state_row(first_seen, last_seen, events, total_sweeps):
    """Per-incarnation 1-D state array of length total_sweeps, encoding C03."""
    state = np.full(total_sweeps, STATE_NOT_ALLOC, dtype=np.uint8)
    f = max(0, first_seen)
    l = min(total_sweeps - 1, last_seen)
    if l >= f:
        # Within the incarnation, default to intermediate-clean ...
        state[f:l + 1] = STATE_CLEAN_INTER
        if events:
            e_last = events[-1]
            # final clean run = sweeps strictly after the last write, up to l.
            # If the incarnation ends on a write (e_last == l) there is no
            # final clean run, so the CLEAN_FINAL region is empty (RK-3).
            if e_last < l:
                state[e_last + 1:l + 1] = STATE_CLEAN_FINAL
            for e in events:
                if f <= e <= l:
                    state[e] = STATE_WRITTEN
        else:
            # No writes in this incarnation -> the whole span is its final
            # (and only) clean run.
            state[f:l + 1] = STATE_CLEAN_FINAL
    # after last_seen: freed / gone
    if last_seen + 1 < total_sweeps:
        state[last_seen + 1:] = STATE_FREED
    return state


def longest_clean_run(first_seen, last_seen, events):
    """Max consecutive clean (present, non-written) sweeps in the incarnation."""
    if not events:
        return last_seen - first_seen + 1
    bounds = [first_seen - 1] + list(events) + [last_seen + 1]
    return max(b - a - 1 for a, b in zip(bounds, bounds[1:]))


def sort_key(rec):
    # D09: first_seen asc, longest-clean-run desc, then (vma_start, vpage_idx).
    return (rec["first_seen"],
            -longest_clean_run(rec["first_seen"], rec["last_seen"], rec["events"]),
            rec["vma_start"], rec["vpage_idx"])


# === Grid build (downsampled, max-priority — same approach as the timeline) ===

def _build_grid(recs, order, total_sweeps, t_stride, n_t_bins, p_stride, n_p_bins):
    grid = np.zeros((n_p_bins, n_t_bins), dtype=np.uint8)
    for new_idx, ri in enumerate(order):
        r = recs[ri]
        state = build_state_row(r["first_seen"], r["last_seen"], r["events"],
                                total_sweeps)
        pad = n_t_bins * t_stride - len(state)
        if pad > 0:
            state = np.concatenate([state, np.zeros(pad, dtype=np.uint8)])
        state_t = state.reshape(n_t_bins, t_stride).max(axis=1)
        p_bin = new_idx // p_stride
        np.maximum(grid[p_bin], state_t, out=grid[p_bin])
    return grid


def _main():
    sys.path.insert(0, str(Path(__file__).parent))
    from _phase_data import parse_args_phase  # argv shape only; loader untouched

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap, BoundaryNorm
    import matplotlib.patches as mpatches

    workload, run_name, phase = parse_args_phase(sys.argv)
    top = Path(__file__).parents[2]
    out_dir = top / "results" / "runs" / run_name

    if phase == "full":
        run_csv = out_dir / "dirty_sweep_lifecycle.csv"
        load_csv = out_dir / "dirty_sweep_load_lifecycle.csv"
        if not (run_csv.exists() and load_csv.exists()):
            print(f"lifecycle: need both run and load lifecycle CSVs for the "
                  f"full view — skipping (phase=full)")
            return
        recs, meta = load_lifecycle_full(out_dir)
    else:
        fname = ("dirty_sweep_lifecycle.csv" if phase == "run"
                 else f"dirty_sweep_{phase}_lifecycle.csv")
        csv_path = out_dir / fname
        if not csv_path.exists():
            print(f"lifecycle: {csv_path} not found — skipping (phase={phase})")
            return
        recs, meta = load_lifecycle(csv_path)
    total_sweeps = meta["total_sweeps"]
    total_seconds = meta["total_seconds"]
    interval_ms = meta["interval_ms"]
    n = len(recs)
    if n == 0 or total_sweeps == 0:
        print(f"lifecycle: no incarnations in {csv_path} — nothing to plot")
        return

    order = sorted(range(n), key=lambda i: sort_key(recs[i]))

    t_stride = max(1, (total_sweeps + MAX_W - 1) // MAX_W)
    p_stride = max(1, (n + MAX_H - 1) // MAX_H)
    n_t_bins = (total_sweeps + t_stride - 1) // t_stride
    n_p_bins = (n + p_stride - 1) // p_stride

    grid = _build_grid(recs, order, total_sweeps,
                       t_stride, n_t_bins, p_stride, n_p_bins)

    cmap = ListedColormap(_COLORS)
    norm = BoundaryNorm([0, 1, 2, 3, 4, 5], cmap.N)
    fig, ax = plt.subplots(figsize=(14, 8))
    ax.imshow(grid, aspect="auto", interpolation="nearest", cmap=cmap, norm=norm,
              extent=(0, total_seconds, 0, n), origin="lower")
    ds = (f"  |  downsampled p_stride={p_stride}, t_stride={t_stride}"
          if (p_stride > 1 or t_stride > 1) else "")
    ax.set_xlabel("time (seconds since run start)")
    ax.set_ylabel(f"incarnation (sorted: first_seen ↑, longest-clean ↓)  "
                  f"[{n:,} incarnations]")
    ax.set_title(f"{workload} — page lifecycle ({meta_label(phase)})\n"
                 f"{total_seconds:.1f}s, {total_sweeps} sweeps × {interval_ms}ms{ds}")
    legend = [
        mpatches.Patch(color="#D55E00", label="written (this sweep)"),
        mpatches.Patch(color="#56B4E9", label="clean — intermediate"),
        mpatches.Patch(color="#0072B2", label="clean — final run"),
        mpatches.Patch(color="#C8C8C8", label="freed / gone"),
        mpatches.Patch(color="white", label="not yet allocated", ec="black"),
    ]
    ax.legend(handles=legend, loc="center left", bbox_to_anchor=(1.02, 0.5),
              fontsize=10)
    plt.tight_layout()
    out_png = out_dir / f"dirty_lifecycle_{phase}.png"
    plt.savefig(out_png, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"Wrote {out_png}  ({n:,} incarnations, phase={phase})")


def meta_label(phase):
    return {"run": "run phase", "load": "load phase",
            "full": "full: load + run"}.get(phase, phase)


if __name__ == "__main__":
    _main()
