"""
Migration-eligibility raster (page-lifecycle-view, T05/T06/T07).

For a chosen threshold T, simulates the round-trip DRAM<->LtRAM policy (GRILL
C04) over every page incarnation and renders, one row per incarnation, where
each page lives over time:

  dirty_eligibility_K{2,3,4}_{phase}.png   (one figure per cluster-derived T)

Policy (C04):
  * non-writable page  -> placed on LtRAM at first_seen, never evicted
                          (one LtRAM write, an endurance cost).
  * writable page      -> starts on DRAM; after T consecutive clean sweeps it
                          migrates to LtRAM (one LtRAM write); the first write
                          while on LtRAM evicts it back to DRAM (a DRAM write,
                          NOT counted) and it must re-earn T clean sweeps.
  * counters reset at incarnation boundaries.

Endurance (C06): each DRAM->LtRAM migration and each RO placement is one LtRAM
write; evictions are free. Reported per figure and against the chip budget
TOTAL_ERASES = 65536 pages x 100000 erases = 6.4e9.

Default thresholds: one top-boundary T per K in {2,3,4} from the same weighted
1-D K-means over the stability histogram the cluster plots use (GRILL D05).
Override with LTRAM_ELIG_T=<seconds> to render a single explicit-T figure.

RO start-placement uses the recorded (final) perms (GRILL D08/A01 — assumed).

Usage: dirty_eligibility_plot.py <workload> <run_name> [phase] (phase: run|load|full)

The "full" phase simulates the policy over the merged load+run incarnation
timeline (see dirty_lifecycle_plot.load_lifecycle_full) and derives cluster Ts
from the exact merged stability histogram (_phase_data.load_stability "full").
"""

import os
import sys
import re
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from dirty_lifecycle_plot import load_lifecycle, load_lifecycle_full  # safe: __main__-guarded
from _phase_data import parse_args_phase           # argv shape only; loader untouched

# --- C05 state values: ordered for the max-priority downsample.
# NOTE (RK-7): the GRILL specifies a downsample priority only for the lifecycle
# raster (C03), not for C05. This ordering — WRITE_EVICT > ON_LTRAM >
# DRAM_ACCRUING > FREED_GONE > NOT_YET_ALLOCATED — is a PROPOSED default
# (mirroring C03's "show the most active state in a merged bin"), pending
# confirmation. It is not a GRILL contract.
E_NOT_ALLOC    = 0
E_FREED        = 1
E_DRAM         = 2
E_LTRAM        = 3
E_WRITE_EVICT  = 4
_E_COLORS = ["white", "#C8C8C8", "#999999", "#0072B2", "#D55E00"]

MAX_W = 1600
MAX_H = 1600

LTRAM_PAGES     = 65536
ERASES_PER_CELL = 100_000
TOTAL_ERASES    = LTRAM_PAGES * ERASES_PER_CELL   # 6.4e9 (C06)


# === Cluster-T derivation (GRILL D05; logic mirrors dirty_stability_*_plot) ===

def weighted_kmeans_1d(values, weights, K, max_iter=200, n_init=10, seed=0):
    """1-D weighted K-means. Returns (centers_sorted_asc, labels_in_sorted_order).
    Copied from dirty_stability_plot.py to avoid importing that script (which
    runs at import). Parity with the cluster plot is by construction."""
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


def cluster_top_thresholds(stab, Ks=(2, 3, 4)):
    """Return {K: T_sweeps} — the top-boundary threshold per K (GRILL D05).
    T_sec = max stability-second in the bottom (K-1) clusters + 1; in sweeps."""
    L = stab["L"].astype(float)
    C = stab["C"].astype(float)
    sec_per_sweep = stab["sec_per_sweep"]
    pos = L > 0
    L, C = L[pos], C[pos]
    if len(L) == 0:
        return {}
    sweeps_per_sec = int(round(1.0 / sec_per_sweep))
    L_sec_int = np.maximum(1, np.round(L * sec_per_sweep)).astype(int)
    unique_secs, inverse = np.unique(L_sec_int, return_inverse=True)
    C_per_sec_bin = np.bincount(inverse, weights=C).astype(np.float64)
    log_L_secs = np.log(unique_secs.astype(float))
    out = {}
    for K in Ks:
        if len(unique_secs) < K:
            continue
        _, bin_labels = weighted_kmeans_1d(log_L_secs, C_per_sec_bin, K)
        bottom = bin_labels < (K - 1)
        T_sec = int(unique_secs[bottom].max()) + 1 if bottom.any() else 0
        out[K] = T_sec * sweeps_per_sec
    return out


# === C04 policy simulation (factored out; unit-tested) =======================

def simulate_state_row(first_seen, last_seen, events, writable, T_sweeps,
                       total_sweeps):
    """Return (state_row, migrations_to_ltram, sweeps_on_ltram) for one
    incarnation under threshold T_sweeps (GRILL C04)."""
    state = np.full(total_sweeps, E_NOT_ALLOC, dtype=np.uint8)
    migrations = 0
    on_ltram = 0
    f, l = first_seen, min(last_seen, total_sweeps - 1)

    if not writable:
        # RO: placed on LtRAM at fault, never evicted; one placement write.
        if l >= f:
            state[f:l + 1] = E_LTRAM
            on_ltram = l - f + 1
            migrations = 1
    else:
        evset = set(events)
        cur = E_DRAM
        clean = 0
        for s in range(f, l + 1):
            if s in evset:
                cur = E_DRAM            # evict if on LtRAM (DRAM write, not counted)
                clean = 0
                state[s] = E_WRITE_EVICT
            else:
                clean += 1
                if cur == E_DRAM and clean >= T_sweeps:
                    cur = E_LTRAM
                    migrations += 1     # DRAM->LtRAM migration = one LtRAM write
                state[s] = cur          # E_DRAM (accruing) or E_LTRAM
            if cur == E_LTRAM:
                on_ltram += 1
    if last_seen + 1 < total_sweeps:
        state[last_seen + 1:] = E_FREED
    return state, migrations, on_ltram


# === Render ==================================================================

def _render_one(recs, T_sweeps, meta, out_dir, workload, run_name, phase, tag):
    total_sweeps = meta["total_sweeps"]
    total_seconds = meta["total_seconds"]
    sec_per_sweep = meta["sec_per_sweep"]

    rows = []
    total_migs = 0
    pages_ever = 0
    for r in recs:
        writable = len(r["vma_perms"]) > 1 and r["vma_perms"][1] == "w"
        st, migs, on = simulate_state_row(r["first_seen"], r["last_seen"],
                                          r["events"], writable, T_sweeps,
                                          total_sweeps)
        rows.append((on, st))
        total_migs += migs
        if on > 0:
            pages_ever += 1

    # D09 eligibility sort: total time-on-LtRAM descending.
    order = sorted(range(len(rows)), key=lambda i: -rows[i][0])

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap, BoundaryNorm
    import matplotlib.patches as mpatches

    n = len(rows)
    t_stride = max(1, (total_sweeps + MAX_W - 1) // MAX_W)
    p_stride = max(1, (n + MAX_H - 1) // MAX_H)
    n_t_bins = (total_sweeps + t_stride - 1) // t_stride
    n_p_bins = (n + p_stride - 1) // p_stride
    grid = np.zeros((n_p_bins, n_t_bins), dtype=np.uint8)
    for new_idx, ri in enumerate(order):
        st = rows[ri][1]
        pad = n_t_bins * t_stride - len(st)
        if pad > 0:
            st = np.concatenate([st, np.zeros(pad, dtype=np.uint8)])
        st_t = st.reshape(n_t_bins, t_stride).max(axis=1)
        np.maximum(grid[new_idx // p_stride], st_t, out=grid[new_idx // p_stride])

    cmap = ListedColormap(_E_COLORS)
    norm = BoundaryNorm([0, 1, 2, 3, 4, 5], cmap.N)
    fig, ax = plt.subplots(figsize=(14, 8))
    ax.imshow(grid, aspect="auto", interpolation="nearest", cmap=cmap, norm=norm,
              extent=(0, total_seconds, 0, n), origin="lower")
    T_sec = T_sweeps * sec_per_sweep
    endur_pct = 100.0 * total_migs / TOTAL_ERASES
    ax.set_xlabel("time (seconds since run start)")
    ax.set_ylabel(f"incarnation (sorted by time-on-LtRAM ↓)  [{n:,}]")
    ax.set_title(
        f"{workload} — migration eligibility, T={T_sec:.1f}s ({T_sweeps} sweeps)  "
        f"[{tag}]\n"
        f"{pages_ever:,}/{n:,} incarnations reach LtRAM   |   "
        f"LtRAM writes (migrations) = {total_migs:,}  "
        f"= {endur_pct:.4f}% of {TOTAL_ERASES:.1e} chip budget")
    legend = [
        mpatches.Patch(color="#D55E00", label="write (evicts LtRAM→DRAM)"),
        mpatches.Patch(color="#0072B2", label="on LtRAM"),
        mpatches.Patch(color="#999999", label="on DRAM (accruing clean)"),
        mpatches.Patch(color="#C8C8C8", label="freed / gone"),
        mpatches.Patch(color="white", label="not yet allocated", ec="black"),
    ]
    ax.legend(handles=legend, loc="center left", bbox_to_anchor=(1.02, 0.5),
              fontsize=10)
    plt.tight_layout()
    out_png = out_dir / f"dirty_eligibility_{tag}_{phase}.png"
    plt.savefig(out_png, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"Wrote {out_png}  (T={T_sec:.1f}s, {total_migs:,} LtRAM writes, "
          f"{pages_ever}/{n} on LtRAM)")


def _main():
    workload, run_name, phase = parse_args_phase(sys.argv)
    top = Path(__file__).parents[2]
    out_dir = top / "results" / "runs" / run_name

    if phase == "full":
        run_csv = out_dir / "dirty_sweep_lifecycle.csv"
        load_csv = out_dir / "dirty_sweep_load_lifecycle.csv"
        if not (run_csv.exists() and load_csv.exists()):
            print("eligibility: need both run and load lifecycle CSVs for the "
                  "full view — skipping (phase=full)")
            return
        recs, meta = load_lifecycle_full(out_dir)
        src = "full (load + run)"
    else:
        life_name = ("dirty_sweep_lifecycle.csv" if phase == "run"
                     else f"dirty_sweep_{phase}_lifecycle.csv")
        life_path = out_dir / life_name
        if not life_path.exists():
            print(f"eligibility: {life_path} not found — skipping (phase={phase})")
            return
        recs, meta = load_lifecycle(life_path)
        src = str(life_path)
    if not recs or meta["total_sweeps"] == 0:
        print(f"eligibility: no incarnations in {src}")
        return

    override = os.environ.get("LTRAM_ELIG_T")
    if override:
        T_sweeps = max(0, int(round(float(override) / meta["sec_per_sweep"])))
        _render_one(recs, T_sweeps, meta, out_dir, workload, run_name, phase,
                    tag=f"T{float(override):g}s")
        return

    if phase == "full":
        # Exact merged histogram (load + run), same as the cluster plots' full view.
        if not ((out_dir / "dirty_sweep.csv").exists()
                and (out_dir / "dirty_sweep_load.csv").exists()):
            print("eligibility: need dirty_sweep.csv and dirty_sweep_load.csv to "
                  "build the full stability histogram; set LTRAM_ELIG_T=<seconds> "
                  "for an explicit threshold")
            return
        from _phase_data import load_stability
        stab = load_stability(out_dir, "full")
    else:
        stab_name = ("dirty_sweep_stability.csv" if phase == "run"
                     else f"dirty_sweep_{phase}_stability.csv")
        stab_path = out_dir / stab_name
        if not stab_path.exists():
            print(f"eligibility: {stab_path} not found — cannot derive cluster Ts; "
                  f"set LTRAM_ELIG_T=<seconds> for an explicit threshold")
            return
        from _phase_data import _load_stability_one
        stab = _load_stability_one(stab_path, f"{phase} only")
    thresholds = cluster_top_thresholds(stab, Ks=(2, 3, 4))
    if not thresholds:
        print("eligibility: stability histogram too small for clustering")
        return
    for K, T_sweeps in thresholds.items():
        _render_one(recs, T_sweeps, meta, out_dir, workload, run_name, phase,
                    tag=f"K{K}")


if __name__ == "__main__":
    _main()
