"""
Plot meminfo_log.sh output. Produces three PNGs per workload:

  meminfo_plot_<workload>.png         linear-scale, system-wide only (legacy view)
  meminfo_log_<workload>.png          log-scale, system-wide only — small metrics readable
  meminfo_per_metric_<workload>.png   small multiples, sys/n0/n1 per metric

Visual choices for color-blind accessibility:
  - Wong's 8-color palette (designed for deuteranopia/protanopia/tritanopia).
  - Each metric also gets a unique linestyle and marker shape, so any single
    pair is distinguishable even in grayscale or with the most severe color
    blindness.
"""

import sys
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import pandas as pd

def fmt_kb(x, pos=None):
    """Format a kB value with human-readable suffix (kB / MB / GB / TB)."""
    if x is None:
        return ""
    if abs(x) >= 1024 * 1024 * 1024:
        return f"{x/(1024*1024*1024):g} TB"
    if abs(x) >= 1024 * 1024:
        return f"{x/(1024*1024):g} GB"
    if abs(x) >= 1024:
        return f"{x/1024:g} MB"
    return f"{x:g} kB"

def fmt_decimal(x, pos=None):
    """Format a number on a log axis as a plain decimal (not 10^N)."""
    return f"{x:g}"

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

workload = sys.argv[1]
run_name = sys.argv[2] if len(sys.argv) > 2 else None

if run_name:
    out_dir = RESULTS_DIR / "runs" / run_name
    filename = out_dir / "meminfo.csv"
else:
    # Legacy fallback for old top-level CSV layout
    out_dir = Path(".")
    filename = RESULTS_DIR / f"meminfo_{workload}.txt"

df = pd.read_csv(filename)

# Coerce numeric columns. Anything unparseable (literal "%N", garbled rows)
# becomes NaN; drop those rows so plotting doesn't choke.
for col in df.columns:
    df[col] = pd.to_numeric(df[col], errors="coerce")
df = df.dropna(subset=["ts_s"]).reset_index(drop=True)

# Normalize x-axis to "seconds since first sample" for readability
df["t"] = df["ts_s"] - df["ts_s"].iloc[0]

# Wong (2011) color-blind-safe palette. 8 hues, no red-green confusion.
WONG = ["#000000", "#E69F00", "#56B4E9", "#009E73",
        "#F0E442", "#0072B2", "#D55E00", "#CC79A7"]
LINESTYLES = ["-", "--", "-.", ":"]
MARKERS    = ["o", "s", "^", "D", "v", ">", "<", "p", "*", "X"]
MARK_EVERY = max(1, len(df) // 25)  # ~25 markers per line regardless of length

# (key, human label, sys col, n0 col, n1 col).
# n0/n1 = None where the metric is not exposed per-node.
#
# Layout for the 4x2 per-metric panels (axes.flat fills left-to-right,
# top-to-bottom):
#
#   [ AnonPages    ]  [ Cached/FilePages ]   <- anon column | file column
#   [ AnonHugePages]  [ Mapped           ]   <- THP variant of anon | subset of cached
#   [ Shmem        ]  [ PageTables       ]   <- shared/anon-like | kernel bookkeeping
#   [ KernelStack  ]  [ SUnreclaim       ]   <- kernel overhead pair
#
# Logical grouping: top half is user-process memory, bottom half is kernel
# overhead, with column 1 ~ "anon-side" and column 2 ~ "file/kernel-side".
METRICS = [
    ("anon",         "AnonPages",         "sys_anon_kb",          "n0_anon_kb",          "n1_anon_kb"),
    ("cache",        "Cached / FilePages","sys_cache_kb",         "n0_file_kb",          "n1_file_kb"),
    ("anon_huge",    "AnonHugePages",     "sys_anon_huge_kb",     "n0_anon_huge_kb",     "n1_anon_huge_kb"),
    ("mapped",       "Mapped",            "sys_mapped_kb",        "n0_mapped_kb",        "n1_mapped_kb"),
    ("shmem",        "Shmem",             "sys_shmem_kb",         "n0_shmem_kb",         "n1_shmem_kb"),
    ("pagetables",   "PageTables",        "sys_pagetables_kb",    "n0_pagetables_kb",    "n1_pagetables_kb"),
    ("kernel_stack", "KernelStack",       "sys_kernel_stack_kb",  "n0_kernel_stack_kb",  "n1_kernel_stack_kb"),
    ("sunreclaim",   "SUnreclaim",        "sys_sunreclaim_kb",    "n0_sunreclaim_kb",    "n1_sunreclaim_kb"),
    ("buffers",      "Buffers (sys-only)","sys_buffers_kb",       None,                  None),
    ("vmalloc",      "VmallocUsed (sys-only)", "sys_vmalloc_kb",  None,                  None),
]

# ---------------------------------------------------------------------------
# Plot 1: Linear-scale single panel, system-wide only (legacy view, refreshed)
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(11, 6))
for i, (key, label, sys_col, _, _) in enumerate(METRICS):
    ax.plot(
        df["t"], df[sys_col],
        color=WONG[i % len(WONG)],
        linestyle=LINESTYLES[i % len(LINESTYLES)],
        marker=MARKERS[i % len(MARKERS)],
        markevery=MARK_EVERY, markersize=6, linewidth=1.5,
        label=label,
    )
ax.set_xlabel("Time since workload start (s)")
ax.set_ylabel("memory")
ax.yaxis.set_major_formatter(mtick.FuncFormatter(fmt_kb))
ax.set_title(f"{workload} — /proc/meminfo (linear scale)")
ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), frameon=True)
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig(out_dir / "meminfo_plot.png", dpi=120, bbox_inches="tight")
plt.close()

# ---------------------------------------------------------------------------
# Plot 2: Log-scale single panel, system-wide only
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(11, 6))
for i, (key, label, sys_col, _, _) in enumerate(METRICS):
    # Replace 0 with NaN so log scale skips it instead of crashing
    series = df[sys_col].mask(df[sys_col] == 0)
    ax.plot(
        df["t"], series,
        color=WONG[i % len(WONG)],
        linestyle=LINESTYLES[i % len(LINESTYLES)],
        marker=MARKERS[i % len(MARKERS)],
        markevery=MARK_EVERY, markersize=6, linewidth=1.5,
        label=label,
    )
ax.set_yscale("log")
ax.set_xlabel("Time since workload start (s)")
ax.set_ylabel("memory (log)")
ax.yaxis.set_major_formatter(mtick.FuncFormatter(fmt_kb))
ax.set_title(f"{workload} — /proc/meminfo (log scale)")
ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), frameon=True)
ax.grid(True, which="both", alpha=0.3)
plt.tight_layout()
plt.savefig(out_dir / "meminfo_log.png", dpi=120, bbox_inches="tight")
plt.close()

# Per-node metrics only (8 of the 10 — Buffers and VmallocUsed are sys-only)
PER_NODE_METRICS = [m for m in METRICS if m[3] is not None]


def pick_unit(max_kb):
    """Choose (label, divisor) so y-ticks read as natural numbers."""
    if max_kb >= 1024 * 1024:
        return "GB", 1024 * 1024
    if max_kb >= 1024:
        return "MB", 1024
    return "kB", 1


def per_metric_panel(include_n1: bool, scale: str, fname: str, title_suffix: str):
    """Render a 4x2 small-multiples figure across PER_NODE_METRICS."""
    fig, axes = plt.subplots(4, 2, figsize=(13, 13), sharex=True)
    for ax, (key, label, sys_col, n0_col, n1_col) in zip(axes.flat, PER_NODE_METRICS):
        # Pick a per-panel unit based on the largest value visible in this panel
        cols_in_panel = [n0_col]
        if include_n1:
            cols_in_panel.append(n1_col)
        panel_max = max(df[c].max() for c in cols_in_panel)
        unit, divisor = pick_unit(panel_max)

        # Mask zeros so log scale skips them instead of erroring/blanking
        n0_series = df[n0_col].mask(df[n0_col] <= 0) if scale == "log" else df[n0_col]
        ax.plot(df["t"], n0_series / divisor,
                color="#0072B2", linestyle="-", linewidth=1.8,
                marker="s", markevery=MARK_EVERY, markersize=4,
                label="Node 0")
        if include_n1:
            n1_series = df[n1_col].mask(df[n1_col] <= 0) if scale == "log" else df[n1_col]
            ax.plot(df["t"], n1_series / divisor,
                    color="#D55E00", linestyle="--", linewidth=1.5,
                    marker="^", markevery=MARK_EVERY, markersize=4,
                    label="Node 1")
        ax.set_title(label)
        ax.set_ylabel(unit + (" (log)" if scale == "log" else ""))
        if scale == "log":
            ax.set_yscale("log")
            # Show plain decimals on the log axis instead of 10^N
            ax.yaxis.set_major_formatter(mtick.FuncFormatter(fmt_decimal))
            ax.yaxis.set_minor_formatter(mtick.FuncFormatter(fmt_decimal))
        ax.grid(True, which="both" if scale == "log" else "major", alpha=0.3)
        ax.legend(loc="best", fontsize=9)
    for ax in axes[-1, :]:
        ax.set_xlabel("Time since workload start (s)")
    plt.suptitle(f"{workload} — {title_suffix}", fontsize=14)
    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.savefig(fname, dpi=120, bbox_inches="tight")
    plt.close()


# Four per-metric variants
per_metric_panel(include_n1=False, scale="linear",
                 fname=out_dir / "meminfo_per_metric_n0.png",
                 title_suffix="per-metric, Node 0 only (linear)")

per_metric_panel(include_n1=False, scale="log",
                 fname=out_dir / "meminfo_per_metric_n0_log.png",
                 title_suffix="per-metric, Node 0 only (log)")

per_metric_panel(include_n1=True, scale="linear",
                 fname=out_dir / "meminfo_per_metric_n0n1.png",
                 title_suffix="per-metric, Node 0 vs Node 1 (linear)")

per_metric_panel(include_n1=True, scale="log",
                 fname=out_dir / "meminfo_per_metric_n0n1_log.png",
                 title_suffix="per-metric, Node 0 vs Node 1 (log)")

print(f"Wrote 6 PNGs into {out_dir}:")
print(f"  meminfo_plot.png              (sys, linear)")
print(f"  meminfo_log.png               (sys, log)")
print(f"  meminfo_per_metric_n0.png     (n0,    linear)")
print(f"  meminfo_per_metric_n0_log.png (n0,    log)")
print(f"  meminfo_per_metric_n0n1.png   (n0+n1, linear)")
print(f"  meminfo_per_metric_n0n1_log.png (n0+n1, log)")
