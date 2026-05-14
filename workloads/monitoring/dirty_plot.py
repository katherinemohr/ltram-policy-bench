"""
Plot soft-dirty sweep output (dirty_sweep CSV) as:

  dirty_hist_{phase}.png         per-page write-rate histogram (Plot A)
  dirty_cdf_{phase}.png          per-page write-rate CDF       (Plot B)
  dirty_tiers_{phase}.png        four-class breakdown pie (max_stab tier)
  dirty_tiers_final_{phase}.png  four-class breakdown pie (final_stab tier)

phase ∈ {run, full}. run reads dirty_sweep.csv as-is; full outer-merges
dirty_sweep.csv + dirty_sweep_load.csv on (vma_start, vma_end, vpage_idx)
and recomputes final_stab_period / max_stab_period across the boundary.

Per-page write rate = dirty_count / total_seconds, in writes/sec.
Saturates at 1 / (interval_ms / 1000) — at 100 ms intervals, max 10 writes/sec.

Usage: dirty_plot.py <workload> <run_name> [phase]   (phase default 'run')
"""

import sys
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from _phase_data import load_pages, parse_args_phase

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

workload, run_name, phase = parse_args_phase(sys.argv)
out_dir = RESULTS_DIR / "runs" / run_name

df_all = load_pages(out_dir, phase)
total_sweeps    = df_all.attrs["total_sweeps"]
total_seconds   = df_all.attrs["total_seconds"]
interval_ms     = df_all.attrs["interval_ms"]
phase_label     = df_all.attrs["label"]
saturation_rate = 1000.0 / interval_ms

# Four-class classification, using max_stab_period (longest quiet window) per page.
# This correctly distinguishes "written in a long burst then idle" (Class 2)
# from "written occasionally throughout the run" (Class 3) — dirty_count alone
# can't tell them apart.
#
#   Class 1: static-RO (perms[1] != 'w'). Placed at fault, never migrates.
#   Class 2: writable, max_stab_period >= TIER2_MIN_FRACTION of run — page had
#           one large quiet window covering most of the run. Effectively
#           read-only after a one-time initial burst.
#   Class 3: writable, has SOME stable window but not a dominant one — periodic
#           writes / sparse updates. LRW queue with re-migration.
#   Class 4: writable, no meaningful stable window — actively written.
TIER2_MIN_FRACTION  = 0.50   # max stability ≥ 50% of run = Class 2 (write-once-ish)
TIER34_MIN_FRACTION = 0.05   # max stability ≥ 5% of run = Class 3, else Class 4
tier2_min_sweeps  = int(TIER2_MIN_FRACTION  * total_sweeps)
tier34_min_sweeps = int(TIER34_MIN_FRACTION * total_sweeps)
tier2_min_secs    = tier2_min_sweeps  * interval_ms / 1000.0
tier34_min_secs   = tier34_min_sweeps * interval_ms / 1000.0

df_all["writable"] = df_all["vma_perms"].str[1] == "w"
df_all["tier"] = "?"
df_all.loc[~df_all["writable"], "tier"] = "1"

# Backward compatibility: older CSVs without max_stab_period column
if "max_stab_period" in df_all.columns:
    df_all.loc[df_all["writable"] & (df_all["max_stab_period"] >= tier2_min_sweeps), "tier"] = "2"
    df_all.loc[df_all["writable"] & (df_all["max_stab_period"] <  tier2_min_sweeps) &
                                    (df_all["max_stab_period"] >= tier34_min_sweeps), "tier"] = "3"
    df_all.loc[df_all["writable"] & (df_all["max_stab_period"] <  tier34_min_sweeps), "tier"] = "4"
else:
    print("WARNING: this CSV is from before max_stab_period was added — "
          "falling back to dirty_count classification (less accurate)")
    df_all.loc[df_all["writable"] & (df_all["dirty_count"] <= 1), "tier"] = "2"
    df_all.loc[df_all["writable"] & (df_all["dirty_count"] > 1) &
              (df_all["dirty_count"] <= int(0.05 * total_sweeps)), "tier"] = "3"
    df_all.loc[df_all["writable"] & (df_all["dirty_count"] > int(0.05 * total_sweeps)), "tier"] = "4"

df = df_all.copy()
df["dirty_rate"] = df["dirty_count"] / total_seconds      # writes/sec
df["dirty_fraction"] = df["dirty_count"] / total_sweeps   # 0..1

n_pages = len(df)
if n_pages == 0:
    print(f"WARNING: dirty_sweep.csv for {run_name} has no page data — re-run with histogram capture")
    sys.exit(0)
n_zero = int((df["dirty_count"] == 0).sum())
n_nonzero = n_pages - n_zero

# Per-class summary
def tier_stats(t):
    sub = df_all[df_all["tier"] == t]
    return len(sub), len(sub) * 4 / 1024  # pages, MB

t1_pages, t1_mb = tier_stats("1")
t2_pages, t2_mb = tier_stats("2")
t3_pages, t3_mb = tier_stats("3")
t4_pages, t4_mb = tier_stats("4")
total_mb = (t1_pages + t2_pages + t3_pages + t4_pages) * 4 / 1024
ro_pages = t1_pages         # alias retained for plot titles below
ro_size_mb = t1_mb

print(f"Run: {run_name}")
print(f"  total_sweeps={total_sweeps} duration={total_seconds:.2f}s interval={interval_ms}ms")
print(f"  saturation rate: {saturation_rate:.1f} writes/sec")
print(f"  Class 2 cut:  max_stab ≥ {tier2_min_sweeps}  ({100*TIER2_MIN_FRACTION:.0f}% of run)")
print(f"  Class 3/4 cut: max_stab ≥ {tier34_min_sweeps}  ({100*TIER34_MIN_FRACTION:.0f}% of run)")
print(f"")
print(f"Four-class breakdown ({total_mb:.1f} MB total):")
print(f"  Class 1 (static-RO):       {t1_pages:>7} pages  {t1_mb:>7.1f} MB  ({100*t1_pages/n_pages:>5.1f}%)  — place at fault, no tracking")
print(f"  Class 2 (write-once):      {t2_pages:>7} pages  {t2_mb:>7.1f} MB  ({100*t2_pages/n_pages:>5.1f}%)  — effectively static-RO after init, easy migrate")
print(f"  Class 3 (read-heavy):      {t3_pages:>7} pages  {t3_mb:>7.1f} MB  ({100*t3_pages/n_pages:>5.1f}%)  — LRW queue with occasional re-migrate")
print(f"  Class 4 (write-heavy):     {t4_pages:>7} pages  {t4_mb:>7.1f} MB  ({100*t4_pages/n_pages:>5.1f}%)  — stays on DRAM")
print(f"")
ltram_eligible_mb  = t1_mb + t2_mb + t3_mb
ltram_eligible_pct = 100 * (t1_pages + t2_pages + t3_pages) / n_pages
strict_ltram_mb    = t1_mb + t2_mb
strict_ltram_pct   = 100 * (t1_pages + t2_pages) / n_pages
print(f"  LtRAM-confident (Class 1 + 2):       {strict_ltram_mb:>7.1f} MB ({strict_ltram_pct:>5.1f}% of workload)")
print(f"  LtRAM-eligible  (Class 1 + 2 + 3):   {ltram_eligible_mb:>7.1f} MB ({ltram_eligible_pct:>5.1f}% of workload)")
print(f"  DRAM-required   (Class 4):           {t4_mb:>7.1f} MB ({100*t4_pages/n_pages:>5.1f}% of workload)")
print(f"  → with sufficient LtRAM, the LtRAM-eligible fraction could be displaced from DRAM")
print(f"")

# Top static-RO VMAs (Class 1 detail)
df_t1 = df_all[df_all["tier"] == "1"]
if len(df_t1) > 0:
    print(f"Class 1 top VMAs:")
    for path, n in df_t1["vma_path"].value_counts().head(6).items():
        print(f"  {n:>6} pages  {path}")
    print(f"")

# ---------------------------------------------------------------------------
# Plot A: Per-page write-rate histogram (phase-aware)
# ---------------------------------------------------------------------------
# X = write rate (writes/sec). Each bar = one discrete sweep count (0..N).
# Bar width = 1/total_seconds because adjacent counts differ by one sweep.
counts = df["dirty_count"].value_counts().sort_index()
rates  = counts.index.values / total_seconds
bar_width = 1.0 / total_seconds

fig, ax = plt.subplots(figsize=(11, 6))
ax.bar(rates, counts.values,
       width=bar_width, color="#0072B2", edgecolor="black", linewidth=0.2)
ax.set_yscale("log")
ax.set_xlabel("write rate (writes/sec)")
ax.set_ylabel("number of pages (log)")
ax.set_title(
    f"{workload} — write-rate distribution, {phase_label}\n"
    f"{total_sweeps} sweeps × {interval_ms} ms = {total_seconds:.1f} s   "
    f"|   of which {ro_pages} pages ({ro_size_mb:.1f} MB) are static-RO (Class 1)"
)
ax.set_xlim(-saturation_rate * 0.02, saturation_rate * 1.05)
ax.grid(True, which="both", axis="y", alpha=0.3)
ax.annotate(
    f"never-written\n{n_zero} pages ({100*n_zero/n_pages:.1f}%)",
    xy=(0, n_zero), xytext=(saturation_rate * 0.15, n_zero),
    fontsize=10, ha="left", va="center",
    arrowprops=dict(arrowstyle="->", color="gray"),
)
plt.tight_layout()
plt.savefig(out_dir / f"dirty_hist_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

# ---------------------------------------------------------------------------
# Plot B: CDF of per-page write rate
# ---------------------------------------------------------------------------
# Step CDF: y = fraction of pages with write rate <= x.
# At x=0 this reads as the "read-only fraction" — directly the LtRAM-
# eligible population if you placed the threshold at 0.
sorted_rates = np.sort(df["dirty_rate"].values)
cdf = np.arange(1, len(sorted_rates) + 1) / len(sorted_rates)
read_only_frac = n_zero / n_pages

fig, ax = plt.subplots(figsize=(11, 6))

# CDF as a stepped curve (post-step matches the "≤ x" semantics)
ax.step(sorted_rates, cdf, where="post", color="#0072B2", linewidth=2)

# Shade the "below threshold" region for a default threshold
default_threshold = 0.0
ax.axhspan(0, read_only_frac, xmin=0, xmax=default_threshold/saturation_rate
           if default_threshold > 0 else 0.005,
           color="#56B4E9", alpha=0.18,
           label=f"LtRAM-eligible at threshold={default_threshold:g}/s "
                 f"({100*read_only_frac:.1f}%)")

# Mark the read-only fraction (the big jump at x=0)
ax.axhline(read_only_frac, color="gray", linestyle=":", alpha=0.6)
ax.text(saturation_rate * 0.5, read_only_frac + 0.015,
        f"read-only fraction: {100*read_only_frac:.1f}% "
        f"({n_zero} of {n_pages} pages)",
        fontsize=10, ha="center", va="bottom")

ax.set_xlabel("write rate (writes/sec)")
ax.set_ylabel("cumulative fraction of pages with rate ≤ x")
ax.set_title(
    f"{workload} — write-rate CDF (all readable pages)\n"
    f"placing threshold at x → fraction at top is LtRAM-eligible   "
    f"|   of which {ro_pages} pages are statically RO (rate=0)"
)
ax.set_xlim(-saturation_rate * 0.02, saturation_rate * 1.05)
ax.set_ylim(0, 1.02)
ax.grid(True, alpha=0.3)
ax.legend(loc="center right")

# Compact stats box in the empty middle
saturated_frac = (df["dirty_count"] == total_sweeps).sum() / n_pages
middle_frac = 1 - read_only_frac - saturated_frac
stats_text = (
    f"Population breakdown:\n"
    f"  rate = 0       : {100*read_only_frac:5.1f}%   (read-only)\n"
    f"  0 < rate < sat : {100*middle_frac:5.1f}%   (intermediate)\n"
    f"  rate = sat     : {100*saturated_frac:5.1f}%   (saturating)"
)
ax.text(0.45, 0.45, stats_text, transform=ax.transAxes,
        fontsize=9, family="monospace", va="center",
        bbox=dict(boxstyle="round,pad=0.5", facecolor="white",
                  edgecolor="gray", alpha=0.9))

plt.tight_layout()
plt.savefig(out_dir / f"dirty_cdf_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

# ---------------------------------------------------------------------------
# Plot C: Four-class breakdown — pie chart
# ---------------------------------------------------------------------------
ltram_mb = t1_mb + t2_mb + t3_mb
dram_mb  = t4_mb

tier_specs = [
    ("Class 1\nOS-known",            t1_mb, "#009E73"),  # bluish green
    ("Class 2\nApp-known\nwrite-once", t2_mb, "#0072B2"),  # blue
    ("Class 3\nRead-heavy",          t3_mb, "#F0E442"),  # yellow
    ("Class 4\nOthers",              t4_mb, "#D55E00"),  # vermilion
]
nonzero = [(label, mb, color) for (label, mb, color) in tier_specs if mb > 0]
labels  = [t[0] for t in nonzero]
sizes   = [t[1] for t in nonzero]
colors  = [t[2] for t in nonzero]

def autopct_fmt(pct):
    if pct < 1.5:
        return ""              # too small to label inside; legend covers it
    mb = pct * total_mb / 100
    return f"{pct:.1f}%\n({mb:.0f} MB)"

# Build legend entries with full labels
legend_labels = [
    f"Class 1 (OS-known, static-RO):                          {t1_mb:>6.1f} MB  ({100*t1_mb/total_mb:>4.1f}%)",
    f"Class 2 (App-known write-once,  max-stab ≥ {tier2_min_secs:>5.1f}s):     {t2_mb:>6.1f} MB  ({100*t2_mb/total_mb:>4.1f}%)",
    f"Class 3 (read-heavy,            {tier34_min_secs:>5.1f}s ≤ max-stab < {tier2_min_secs:>5.1f}s): {t3_mb:>6.1f} MB  ({100*t3_mb/total_mb:>4.1f}%)",
    f"Class 4 (others,                max-stab <  {tier34_min_secs:>5.1f}s):    {t4_mb:>6.1f} MB  ({100*t4_mb/total_mb:>4.1f}%)",
]
legend_colors = ["#009E73", "#0072B2", "#F0E442", "#D55E00"]

fig, ax = plt.subplots(figsize=(10, 7))
wedges, _, autotexts = ax.pie(
    sizes, labels=None, colors=colors,
    autopct=autopct_fmt,
    startangle=90, pctdistance=0.72,
    wedgeprops=dict(edgecolor="black", linewidth=0.7),
)
for at in autotexts:
    at.set_fontsize(10)

# Legend with monospace for tabular alignment
import matplotlib.patches as mpatches
patches = [mpatches.Patch(color=c, label=l) for c, l in zip(legend_colors, legend_labels)]
ax.legend(handles=patches, loc="center left", bbox_to_anchor=(1.02, 0.5),
          fontsize=10, frameon=True, prop={"family": "monospace"})

ax.set_title(
    f"{workload} — four-class memory breakdown ({total_mb:.0f} MB total)\n"
    f"LtRAM-eligible (C1+C2+C3): {ltram_mb:.0f} MB "
    f"({100*ltram_mb/total_mb:.1f}%)   |   "
    f"DRAM-required (C4): {dram_mb:.0f} MB "
    f"({100*dram_mb/total_mb:.1f}%)",
    fontsize=12,
)
ax.set_aspect("equal")

plt.tight_layout()
plt.savefig(out_dir / f"dirty_tiers_{phase}.png", dpi=120, bbox_inches="tight")
plt.close()

# ---------------------------------------------------------------------------
# Plot C2: Four-class breakdown by final_stab_period
# ---------------------------------------------------------------------------
# Classify writable pages by the length of the epoch that was active at end
# of run (final_stab_period = time since last write at end of test). This
# answers "is the data currently sitting on this page in a long stable state?"
# rather than "did this page have a long stable phase at any point?".
if "final_stab_period" in df_all.columns:
    df_all["tier_final"] = "?"
    df_all.loc[~df_all["writable"], "tier_final"] = "1"
    df_all.loc[df_all["writable"] & (df_all["final_stab_period"] >= tier2_min_sweeps), "tier_final"] = "2"
    df_all.loc[df_all["writable"] & (df_all["final_stab_period"] <  tier2_min_sweeps) &
                                    (df_all["final_stab_period"] >= tier34_min_sweeps), "tier_final"] = "3"
    df_all.loc[df_all["writable"] & (df_all["final_stab_period"] <  tier34_min_sweeps), "tier_final"] = "4"

    def tier_stats_final(t):
        sub = df_all[df_all["tier_final"] == t]
        return len(sub), len(sub) * 4 / 1024

    f1_pages, f1_mb = tier_stats_final("1")
    f2_pages, f2_mb = tier_stats_final("2")
    f3_pages, f3_mb = tier_stats_final("3")
    f4_pages, f4_mb = tier_stats_final("4")
    f_total_mb = f1_mb + f2_mb + f3_mb + f4_mb
    f_ltram_mb = f1_mb + f2_mb + f3_mb
    f_dram_mb  = f4_mb

    f_specs = [
        ("Class 1\nOS-known",              f1_mb, "#009E73"),
        ("Class 2\nApp-known\nwrite-once", f2_mb, "#0072B2"),
        ("Class 3\nRead-heavy",            f3_mb, "#F0E442"),
        ("Class 4\nOthers",                f4_mb, "#D55E00"),
    ]
    f_nonzero = [(l, mb, c) for (l, mb, c) in f_specs if mb > 0]
    f_labels = [t[0] for t in f_nonzero]
    f_sizes  = [t[1] for t in f_nonzero]
    f_colors = [t[2] for t in f_nonzero]

    def autopct_fmt_f(pct):
        if pct < 1.5:
            return ""
        mb = pct * f_total_mb / 100
        return f"{pct:.1f}%\n({mb:.0f} MB)"

    f_legend_labels = [
        f"Class 1 (OS-known, static-RO):                            {f1_mb:>6.1f} MB  ({100*f1_mb/f_total_mb:>4.1f}%)",
        f"Class 2 (App-known write-once,  final-stab ≥ {tier2_min_secs:>5.1f}s):     {f2_mb:>6.1f} MB  ({100*f2_mb/f_total_mb:>4.1f}%)",
        f"Class 3 (read-heavy,            {tier34_min_secs:>5.1f}s ≤ final-stab < {tier2_min_secs:>5.1f}s): {f3_mb:>6.1f} MB  ({100*f3_mb/f_total_mb:>4.1f}%)",
        f"Class 4 (others,                final-stab <  {tier34_min_secs:>5.1f}s):    {f4_mb:>6.1f} MB  ({100*f4_mb/f_total_mb:>4.1f}%)",
    ]

    fig, ax = plt.subplots(figsize=(10, 7))
    wedges, _, autotexts = ax.pie(
        f_sizes, labels=None, colors=f_colors,
        autopct=autopct_fmt_f, startangle=90, pctdistance=0.72,
        wedgeprops=dict(edgecolor="black", linewidth=0.7),
    )
    for at in autotexts:
        at.set_fontsize(10)

    patches = [mpatches.Patch(color=c, label=l) for c, l in zip(legend_colors, f_legend_labels)]
    ax.legend(handles=patches, loc="center left", bbox_to_anchor=(1.02, 0.5),
              fontsize=10, frameon=True, prop={"family": "monospace"})
    ax.set_title(
        f"{workload} — four-class breakdown by FINAL EPOCH "
        f"(epoch active at end-of-run)\n"
        f"LtRAM-eligible (C1+C2+C3): {f_ltram_mb:.0f} MB "
        f"({100*f_ltram_mb/f_total_mb:.1f}%)   |   "
        f"DRAM-required (C4): {f_dram_mb:.0f} MB "
        f"({100*f_dram_mb/f_total_mb:.1f}%)",
        fontsize=12,
    )
    ax.set_aspect("equal")
    plt.tight_layout()
    plt.savefig(out_dir / f"dirty_tiers_final_{phase}.png", dpi=120, bbox_inches="tight")
    plt.close()

    print(f"Wrote dirty_hist_run.png, dirty_hist_full.png (if load CSV present), "
          f"dirty_cdf.png, dirty_tiers.png, dirty_tiers_final.png to {out_dir}")
else:
    print(f"Wrote dirty_hist_run.png, dirty_hist_full.png (if load CSV present), "
          f"dirty_cdf.png, dirty_tiers.png to {out_dir} "
          f"(no final_stab_period in CSV — re-run for tiers_final.png)")
