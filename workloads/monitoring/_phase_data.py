"""
Shared loaders for run-only vs load+run-merged dirty_sweep data.

Used by every dirty_*_plot.py script to produce paired _run / _full output
figures, so the team can compare what the policy view looks like for the
steady-state run phase alone vs the whole deployment (load + run).

Two main entry points:

  iter_phases(out_dir) -> ["run"] or ["run", "full"]
      ['run']         if no dirty_sweep_load.csv (matmul, or pre-split runs)
      ['run','full']  if both phase CSVs exist

  load_stability(out_dir, phase) -> dict
      Per-epoch stability histogram. Use for any dirty_stability_*_plot.py
      that reads dirty_sweep_stability.csv. The 'full' phase merges by
      summing histograms element-wise (boundary epochs approximated;
      bounded by ~n_pages epochs).

  load_pages(out_dir, phase) -> DataFrame (with .attrs)
      Per-page dirty_sweep table. Use for any plotter that reads
      dirty_sweep.csv. The 'full' phase outer-merges on
      (vma_start, vma_end, vpage_idx) and recomputes final_stab_period and
      max_stab_period across the load/run boundary.

All returned dicts/DataFrames carry total_sweeps, total_seconds, interval_ms
and a human-readable label (used in plot titles).
"""
from pathlib import Path
import re

import numpy as np
import pandas as pd


def _parse_header(line):
    m = re.search(
        r"total_sweeps=(\d+)\s+total_seconds=([\d.]+)\s+interval_ms=(\d+)", line
    )
    return int(m.group(1)), float(m.group(2)), int(m.group(3))


def has_load(out_dir):
    return (Path(out_dir) / "dirty_sweep_load.csv").exists()


def iter_phases(out_dir):
    return ["run", "full"] if has_load(out_dir) else ["run"]


# === Stability-histogram loaders ===

def _load_stability_one(path, label):
    with open(path) as f:
        header = f.readline().strip()
    ts, sec, ims = _parse_header(header)
    df = pd.read_csv(path, comment="#")
    L = df["stability_period_sweeps"].values.astype(np.int64)
    C = df["count"].values.astype(np.int64)
    F = (df["final_count"].values.astype(np.int64)
         if "final_count" in df.columns else np.zeros_like(C))
    return dict(L=L, C=C, F=F, I=C - F,
                total_sweeps=ts, total_seconds=sec, interval_ms=ims,
                sec_per_sweep=ims / 1000.0, label=label)


def load_stability(out_dir, phase):
    out_dir = Path(out_dir)
    if phase == "run":
        return _load_stability_one(out_dir / "dirty_sweep_stability.csv",
                                   "run only")
    if phase == "load":
        return _load_stability_one(out_dir / "dirty_sweep_load_stability.csv",
                                   "load only")
    if phase == "full":
        return _build_full_stability_exact(out_dir)
    raise ValueError(f"Unknown phase: {phase}")


def _build_full_stability_exact(out_dir):
    """Build a TRUE merged stability histogram by reconstructing epochs from
    concatenated per-page write_events.

    Replaces the earlier histogram-sum approximation, which artificially
    capped each merged epoch at max(load_total_sweeps, run_total_sweeps)
    instead of the true full-timeline duration. The sum approximation also
    double-counted boundary epochs: a page that was clean at end of load
    AND throughout run would contribute one 'final' epoch in both load.csv
    and run.csv → 2 epochs of lengths load_total and run_total — but in the
    full view it should contribute ONE epoch of length (load_total + run_total).

    This function uses the same concatenated write_events that load_pages('full')
    builds, then derives per-page epochs (zero events ⇒ one epoch of length
    total_sweeps; n events ⇒ n+1 epochs: gaps before/between/after each event)
    and tallies them into a histogram with final/intermediate split.

    Each page contributes:
      - n_events = 0 → 1 final epoch of length total_sweeps
      - n_events ≥ 1:
          * for each consecutive pair (e_i, e_{i+1}): one intermediate epoch
            of length (e_{i+1} - e_i - 1)
          * one final epoch of length (total_sweeps - 1 - last_event)
        (the pre-first-event window is NOT counted as an epoch — that's
        before the page entered its first observable epoch.)
    """
    out_dir = Path(out_dir)
    df = load_pages(out_dir, "full")
    total_sweeps = df.attrs["total_sweeps"]
    interval_ms  = df.attrs["interval_ms"]

    # Tally epoch lengths with separate final/intermediate counts.
    # Use dicts then convert to sorted arrays for compactness.
    final_counts = {}
    inter_counts = {}

    we_col = df["write_events"].values
    for s in we_col:
        if not s:
            # Page has no events anywhere → one final epoch covering the
            # entire merged timeline. This is the case the histogram-sum
            # approximation got wrong.
            L = total_sweeps
            final_counts[L] = final_counts.get(L, 0) + 1
            continue
        events = [int(t) for t in s.split(";")]
        # Intermediate epochs: between consecutive events
        for i in range(len(events) - 1):
            L = events[i+1] - events[i] - 1
            if L > 0:
                inter_counts[L] = inter_counts.get(L, 0) + 1
        # Final epoch: after last event to end of merged timeline
        last = events[-1]
        L = total_sweeps - 1 - last
        if L > 0:
            final_counts[L] = final_counts.get(L, 0) + 1

    all_L_set = set(final_counts) | set(inter_counts)
    L = np.array(sorted(all_L_set), dtype=np.int64)
    F = np.array([final_counts.get(int(l), 0) for l in L], dtype=np.int64)
    I = np.array([inter_counts.get(int(l), 0) for l in L], dtype=np.int64)
    C = F + I

    return dict(
        L=L, C=C, F=F, I=I,
        total_sweeps=total_sweeps,
        total_seconds=df.attrs["total_seconds"],
        interval_ms=interval_ms,
        sec_per_sweep=interval_ms / 1000.0,
        label="full (load + run, exact merge)",
    )


# === Per-page loaders ===

def _load_pages_one(path, label):
    with open(path) as f:
        header = f.readline().strip()
    ts, sec, ims = _parse_header(header)
    df = pd.read_csv(path, comment="#", dtype={"write_events": str})
    if "write_events" in df.columns:
        df["write_events"] = df["write_events"].fillna("")
    df.attrs["total_sweeps"]  = ts
    df.attrs["total_seconds"] = sec
    df.attrs["interval_ms"]   = ims
    df.attrs["label"]         = label
    return df


def load_pages(out_dir, phase):
    out_dir = Path(out_dir)
    if phase == "run":
        return _load_pages_one(out_dir / "dirty_sweep.csv", "run only")
    if phase == "load":
        return _load_pages_one(out_dir / "dirty_sweep_load.csv", "load only")
    if phase == "full":
        df_run  = _load_pages_one(out_dir / "dirty_sweep.csv",      "run only")
        df_load = _load_pages_one(out_dir / "dirty_sweep_load.csv", "load only")
        run_ts  = df_run.attrs["total_sweeps"]
        load_ts = df_load.attrs["total_sweeps"]

        key = ["vma_start", "vma_end", "vpage_idx"]
        cols_keep = ["vma_perms", "vma_path", "dirty_count",
                     "max_stab_period", "final_stab_period", "write_events"]
        keep_l = key + [c for c in cols_keep if c in df_load.columns]
        keep_r = key + [c for c in cols_keep if c in df_run.columns]

        rename_load = {
            "dirty_count":       "_load_count",
            "final_stab_period": "_load_final",
            "max_stab_period":   "_load_max",
            "write_events":      "_load_events",
        }
        rename_run = {
            "dirty_count":       "_run_count",
            "final_stab_period": "_run_final",
            "max_stab_period":   "_run_max",
            "write_events":      "_run_events",
        }
        merged = pd.merge(
            df_load[keep_l].rename(columns=rename_load),
            df_run[keep_r].rename(columns=rename_run),
            on=key, how="outer", suffixes=("_l", "_r"),
        )

        for col in ["_load_count", "_run_count",
                    "_load_final", "_run_final",
                    "_load_max",   "_run_max"]:
            if col in merged.columns:
                merged[col] = merged[col].fillna(0)

        # Prefer run-side metadata; fall back to load-side for pages that
        # disappeared between phases.
        for c in ["vma_perms", "vma_path"]:
            r = f"{c}_r" if f"{c}_r" in merged.columns else c
            l = f"{c}_l" if f"{c}_l" in merged.columns else c
            merged[c] = merged[r].fillna(merged[l]) if r != l else merged[r]

        full_count = (merged["_load_count"] + merged["_run_count"]).astype(int)
        # final_stab in the full-phase view:
        #   run_count > 0  → run_final (page was rewritten in run; load-side
        #                   final_stab is irrelevant — that epoch already ended)
        #   run_count == 0 → load_final + run_total_sweeps (page stayed clean
        #                   from its last load-phase write through end of run)
        full_final = np.where(
            merged["_run_count"] > 0,
            merged["_run_final"],
            merged["_load_final"] + run_ts,
        ).astype(int)

        # max_stab: max of (load.max, run.max). Plus, if the page was clean at
        # end-of-load AND throughout run, it has a boundary-spanning epoch of
        # length load_final + run_total_sweeps — take the max with that.
        full_max = np.maximum(merged["_load_max"], merged["_run_max"]).astype(int)
        boundary = (merged["_run_count"] == 0)
        full_max = np.where(
            boundary,
            np.maximum(full_max, merged["_load_final"] + run_ts),
            full_max,
        ).astype(int)

        out = pd.DataFrame({
            "vma_start": merged["vma_start"],
            "vma_end":   merged["vma_end"],
            "vma_perms": merged["vma_perms"],
            "vma_path":  merged["vma_path"],
            "vpage_idx": merged["vpage_idx"],
            "dirty_count":       full_count,
            "final_stab_period": full_final,
            "max_stab_period":   full_max,
        })

        # write_events: concatenate load events as-is, run events with sweep
        # ids offset by load_total_sweeps. Empty strings become "" everywhere.
        if "_load_events" in merged.columns or "_run_events" in merged.columns:
            load_ev = merged.get("_load_events",
                                 pd.Series([""] * len(merged))).fillna("")
            run_ev  = merged.get("_run_events",
                                 pd.Series([""] * len(merged))).fillna("")

            def offset_run_events(s):
                if not s:
                    return ""
                return ";".join(str(int(x) + load_ts) for x in s.split(";"))
            run_ev_off = run_ev.map(offset_run_events)

            def join_evs(le, re_):
                if le and re_:    return f"{le};{re_}"
                if le:            return le
                return re_
            out["write_events"] = [join_evs(le, re_)
                                   for le, re_ in zip(load_ev, run_ev_off)]
        out.attrs["total_sweeps"]  = run_ts + load_ts
        out.attrs["total_seconds"] = (df_run.attrs["total_seconds"]
                                      + df_load.attrs["total_seconds"])
        out.attrs["interval_ms"]   = df_run.attrs["interval_ms"]
        out.attrs["label"]         = "full (load + run)"
        return out
    raise ValueError(f"Unknown phase: {phase}")


# === Convenience for plotter scripts ===

def parse_args_phase(argv, default="run"):
    """Standard argv shape: <script> <workload> <run_name> [phase].
    Phase defaults to 'run' for backward compatibility."""
    workload = argv[1]
    run_name = argv[2]
    phase    = argv[3] if len(argv) > 3 else default
    return workload, run_name, phase


# === Page-level C1/C2/C3/C4 classification (load-vs-run aware) ===
#
# Distinguishes pages by whether they have write events in the load and run
# phases of an observed deployment. Drives the deployment-scenario endurance
# formula:
#
#   total_5y_writes(T) = load_total_migs(T)                       (cold-start, all pages)
#                      + recurring_migs(T) × (HORIZON / run_seconds)   (C4 pages, recurring)
#
# where recurring_migs(T) is run_total_migs(T) MINUS the C1+C2+C3 pages that
# qualify at T (those don't actually re-migrate cycle-after-cycle).
#
# Categories:
#   C1: vma not writable                          → kernel can't write; truly RO
#   C2: writable, NO events in load AND NO in run → never observed touched
#   C3: writable, events in load, NO in run       → init-once (touched only during load)
#   C4: writable, events in run                   → recurring (will re-migrate every cycle)
#
# All of C1, C2, C3 contribute exactly one stability epoch of length =
# run_total_sweeps to the run histogram, so they all qualify at any
# T < run_total_sweeps. C4 pages contribute one or more shorter epochs
# (between writes) plus one final epoch.

def classify_pages(out_dir):
    """Per-page C1/C2/C3/C4 classification for a phase-split run.

    Reads dirty_sweep.csv (run-phase per-page) and, if present,
    dirty_sweep_load.csv (load-phase per-page). Returns a dict with:
        'class1', 'class2', 'class3', 'class4' → page counts (int)
        'has_load' → whether load CSV existed (else C3 is undefined and
                     all writable pages collapse into C2 ∪ C4)
        'run_total_sweeps', 'load_total_sweeps' (or None)
    """
    out_dir = Path(out_dir)
    run_df = _load_pages_one(out_dir / "dirty_sweep.csv", "run only")
    run_df["writable"] = run_df["vma_perms"].str[1] == "w"
    has_load = (out_dir / "dirty_sweep_load.csv").exists()

    run_writable = run_df["writable"].values
    run_count    = run_df["dirty_count"].values
    n_class1 = int((~run_writable).sum())

    if has_load:
        load_df = _load_pages_one(out_dir / "dirty_sweep_load.csv", "load only")
        # Outer-merge on (vma_start, vma_end, vpage_idx) so we have
        # load_count per page; pages absent from load → load_count = 0.
        key = ["vma_start", "vma_end", "vpage_idx"]
        merged = pd.merge(
            load_df[key + ["dirty_count"]].rename(columns={"dirty_count": "load_count"}),
            run_df [key + ["dirty_count", "writable"]].rename(columns={"dirty_count": "run_count"}),
            on=key, how="outer",
        )
        merged["load_count"]  = merged["load_count"].fillna(0)
        merged["run_count"]   = merged["run_count"].fillna(0)
        merged["writable"]    = merged["writable"].fillna(True)  # load-only pages: assume writable
        # We classify pages PRESENT IN RUN (since the run histogram is what
        # we're decomposing). Pages only in load are part of cold-start cost.
        in_run = merged["run_count"].notna() | (merged["writable"].astype(bool))
        wm = merged["writable"].astype(bool).values
        lc = merged["load_count"].astype(int).values
        rc = merged["run_count"].astype(int).values
        n_class2 = int((wm & (lc == 0) & (rc == 0)).sum())
        n_class3 = int((wm & (lc >  0) & (rc == 0)).sum())
        n_class4 = int((wm & (rc >  0)).sum())
        load_total = int(load_df.attrs["total_sweeps"])
    else:
        # No load CSV → can't separate C2 (never touched) from C3 (init-once).
        # Treat both as "no run events" combined into C2.
        n_class2 = int((run_writable & (run_count == 0)).sum())
        n_class3 = 0
        n_class4 = int((run_writable & (run_count >  0)).sum())
        load_total = None

    return {
        "class1":            n_class1,
        "class2":            n_class2,
        "class3":            n_class3,
        "class4":            n_class4,
        "total":             n_class1 + n_class2 + n_class3 + n_class4,
        "has_load":          has_load,
        "run_total_sweeps":  int(run_df.attrs["total_sweeps"]),
        "load_total_sweeps": load_total,
    }
