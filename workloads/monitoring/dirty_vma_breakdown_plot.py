"""
VMA-segment breakdown: pages by memory region type × write-behavior class.

  dirty_vma_breakdown_{phase}.png

Stacked bar chart:
  X-axis — memory region category ([heap], [stack], shared lib, binary, anonymous, etc.)
  Y-axis — size in KB
  Stacks — Class 1 (static-RO), Class 2 (write-once), Class 3 (read/write)

Classes:
  Class 1: vma not writable (r--p / r-xp)
  Class 2: writable, max_stab_period >= 50% of run  (write-once / effectively static)
  Class 3: writable, max_stab_period <  50% of run  (actively read/written)

Usage: dirty_vma_breakdown_plot.py <workload> <run_name> [phase]
"""

import sys
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from _phase_data import load_pages, parse_args_phase

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

TIER2_MIN_FRACTION = 0.50

CLASS_COLORS = {
    "1": "#009E73",
    "2": "#0072B2",
    "3": "#D55E00",
}
CLASS_LABELS = {
    "1": "Class 1: static-RO",
    "2": "Class 2: write-once",
    "3": "Class 3: read/write",
}


def categorize_vma(path):
    if path == "[heap]":
        return "[heap]"
    if path == "[stack]":
        return "[stack]"
    if path in ("[vvar]", "[vdso]", "[vsyscall]"):
        return "kernel\nspecial"
    if path.startswith("["):
        return "other\nspecial"
    if not path or (isinstance(path, float)):
        return "anonymous\n(mmap)"
    if ".so" in path:
        return "shared lib\n(.so)"
    if path.startswith("/"):
        return "binary /\nfile"
    return "other"


workload, run_name, phase = parse_args_phase(sys.argv)
out_dir = RESULTS_DIR / "runs" / run_name

df = load_pages(out_dir, phase)
total_sweeps  = df.attrs["total_sweeps"]
total_seconds = df.attrs["total_seconds"]
interval_ms   = df.attrs["interval_ms"]
phase_label   = df.attrs["label"]

n_pages = len(df)
if n_pages == 0:
    print("WARNING: no pages, nothing to plot")
    sys.exit(0)

tier2_min_sweeps = int(TIER2_MIN_FRACTION * total_sweeps)

df["writable"] = df["vma_perms"].str[1] == "w"
df["tier"] = "3"
df.loc[~df["writable"], "tier"] = "1"
if "max_stab_period" in df.columns:
    df.loc[df["writable"] & (df["max_stab_period"] >= tier2_min_sweeps), "tier"] = "2"
else:
    df.loc[df["writable"] & (df["dirty_count"] <= 1), "tier"] = "2"

df["category"] = df["vma_path"].apply(categorize_vma)
df["kb"] = 4.0   # 4 KB per page

# Pivot: category × tier → total KB
pivot = df.groupby(["category", "tier"])["kb"].sum().unstack(fill_value=0.0)
for c in ["1", "2", "3"]:
    if c not in pivot.columns:
        pivot[c] = 0.0
pivot = pivot[["1", "2", "3"]]

totals = pivot.sum(axis=1)
pivot = pivot.loc[totals.sort_values(ascending=False).index]
totals = pivot.sum(axis=1)

# Print summary table
total_kb = df["kb"].sum()
print(f"{workload} ({run_name}) — VMA segment breakdown ({phase_label})")
print(f"  {total_kb/1024:.1f} MB total  |  {n_pages:,} pages  |  interval={interval_ms}ms  |  run={total_seconds:.0f}s")
print(f"  {'segment':25}  {'total KB':>9}  {'C1 KB':>9}  {'C2 KB':>9}  {'C3 KB':>9}")
for cat in pivot.index:
    row = pivot.loc[cat]
    t = totals[cat]
    print(f"  {cat.replace(chr(10), ' '):25}  {t:>9.0f}  "
          f"{row['1']:>9.0f}  {row['2']:>9.0f}  {row['3']:>9.0f}")

# === Plot ===
x = np.arange(len(pivot))
width = 0.6

fig, ax = plt.subplots(figsize=(max(10, len(pivot) * 1.6), 7))

bottoms = np.zeros(len(pivot))
for cls in ["1", "2", "3"]:
    vals = pivot[cls].values
    ax.bar(x, vals, bottom=bottoms, width=width,
           label=CLASS_LABELS[cls],
           color=CLASS_COLORS[cls], alpha=0.9,
           edgecolor="black", linewidth=0.4)
    for i, (v, b) in enumerate(zip(vals, bottoms)):
        if v >= total_kb * 0.005:   # only label if ≥ 0.5% of total
            ax.text(x[i], b + v / 2, f"{v:.0f}",
                    ha="center", va="center", fontsize=8, color="black")
    bottoms += vals

for i, t in enumerate(totals.values):
    if t > 0:
        ax.text(x[i], t + total_kb * 0.005, f"{t:.0f} KB",
                ha="center", va="bottom", fontsize=9, fontweight="bold")

ax.set_xticks(x)
ax.set_xticklabels(pivot.index.tolist(), fontsize=10)
ax.set_ylabel("size (KB)", fontsize=11)
ax.set_xlabel("VMA segment type", fontsize=11)
ax.set_title(
    f"{workload} — memory by segment type × write-behavior class  [{phase_label}]\n"
    f"run length = {total_seconds:.0f}s  |  {n_pages:,} pages  |  interval = {interval_ms}ms",
    fontsize=11,
)
ax.legend(loc="upper right", fontsize=10)
ax.grid(axis="y", alpha=0.3)
ax.set_ylim(0, totals.max() * 1.12)

plt.tight_layout()
out_path = out_dir / f"dirty_vma_breakdown_{phase}.png"
plt.savefig(out_path, dpi=120, bbox_inches="tight")
plt.close()
print(f"\nWrote {out_path}")
