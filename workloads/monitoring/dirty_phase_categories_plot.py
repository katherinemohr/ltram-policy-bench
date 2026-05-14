"""
Page-level C1/C2/C3/C4 breakdown — load-vs-run-event-aware classification.

Distinguishes pages by their event activity in the load and run phases:

  C1 — vma_perms[1] != 'w'                            (kernel can't write)
  C2 — writable, no events in load AND no in run      (truly cold)
  C3 — writable, events in load only, none in run     (init-once)
  C4 — writable, events in run                        (recurring)

C1+C2+C3 are the "stays-RO post-cold-start" set: each page in this set is
migrated at most once over a 5-year deployment. C4 pages re-migrate every
run cycle.

Output: dirty_phase_categories_{phase}.png  (single horizontal stacked bar
+ legend showing page count and MB per category)

Usage: dirty_phase_categories_plot.py <workload> <run_name> [phase]

phase is accepted for argv compatibility but ignored — this figure always
shows the full load+run picture (it's the categorization OF the deployment,
not a per-phase view). If the load CSV is missing, C2 and C3 collapse into
a combined "writable, no run events" bar.
"""

import sys
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

sys.path.insert(0, str(Path(__file__).parent))
from _phase_data import classify_pages, has_load, parse_args_phase

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

workload, run_name, phase = parse_args_phase(sys.argv)
out_dir = RESULTS_DIR / "runs" / run_name

pcat = classify_pages(out_dir)
total = pcat["total"]
if total == 0:
    print(f"phase_categories: no pages, skipping")
    sys.exit(0)

c1 = pcat["class1"]
c2 = pcat["class2"]
c3 = pcat["class3"]
c4 = pcat["class4"]
has_load_csv = pcat["has_load"]

mb = lambda n: n * 4 / 1024  # 4 KB pages → MB

# Color convention: C1+C2+C3 are "stays-RO" (cool/green palette);
# C4 is "recurring writable" (warm/red).
COLOR_C1 = "#009E73"   # bluish-green: OS-known static
COLOR_C2 = "#56B4E9"   # light blue: never touched
COLOR_C3 = "#0072B2"   # blue: init-once
COLOR_C4 = "#D55E00"   # vermilion: recurring

fig, ax = plt.subplots(figsize=(12, 3.5))

# Single horizontal stacked bar showing the four categories side-by-side.
sizes = [c1, c2, c3, c4]
colors = [COLOR_C1, COLOR_C2, COLOR_C3, COLOR_C4]
labels_short = ["C1: static-RO\n(VMA !W)",
                "C2: never-touched\n(W, 0 events)",
                "C3: init-once\n(W, load only)",
                "C4: recurring\n(W, run events)"]

left = 0
for s, c, lbl in zip(sizes, colors, labels_short):
    if s == 0:
        continue
    ax.barh([0], [s], left=left, color=c, edgecolor="black", linewidth=0.8)
    pct = 100 * s / total
    if pct > 3:    # only annotate slices with enough room
        ax.text(left + s / 2, 0, f"{pct:.1f}%",
                ha="center", va="center", fontsize=11, color="white",
                fontweight="bold")
    left += s

ax.set_xlim(0, total)
ax.set_ylim(-0.6, 0.6)
ax.set_yticks([])
ax.set_xlabel(f"page count (total = {total:,} pages, {mb(total):.0f} MB)",
              fontsize=11)
ax.set_title(
    f"{workload} — load/run-event-aware page categorization\n"
    f"C1+C2+C3 are 'stays-RO post-cold-start' (each migrated at most once "
    f"over deployment); C4 recurs every run cycle",
    fontsize=11,
)

# Detailed legend with counts and MB. If load CSV is missing, note that
# C2/C3 are combined.
norec = c1 + c2 + c3
if has_load_csv:
    legend_entries = [
        (COLOR_C1, f"C1 static-RO (VMA !writable):    {c1:>10,} pages  ({mb(c1):>7.1f} MB)"),
        (COLOR_C2, f"C2 never-touched (W, 0 events):  {c2:>10,} pages  ({mb(c2):>7.1f} MB)"),
        (COLOR_C3, f"C3 init-once (W, load only):     {c3:>10,} pages  ({mb(c3):>7.1f} MB)"),
        (COLOR_C4, f"C4 recurring (W, run events):    {c4:>10,} pages  ({mb(c4):>7.1f} MB)"),
        ("none",    f"  └── stays-RO total (C1+C2+C3): {norec:>10,} pages  ({mb(norec):>7.1f} MB)  "
                    f"= {100*norec/total:.1f}% of workload"),
    ]
else:
    legend_entries = [
        (COLOR_C1, f"C1 static-RO (VMA !writable):    {c1:>10,} pages  ({mb(c1):>7.1f} MB)"),
        (COLOR_C2, f"C2/C3 W, no run events:          {c2:>10,} pages  ({mb(c2):>7.1f} MB)  "
                   f"(load CSV absent — can't split C2 from C3)"),
        (COLOR_C4, f"C4 recurring (W, run events):    {c4:>10,} pages  ({mb(c4):>7.1f} MB)"),
        ("none",   f"  └── stays-RO total (C1+C2+C3): {norec:>10,} pages  ({mb(norec):>7.1f} MB)  "
                   f"= {100*norec/total:.1f}% of workload"),
    ]

patches = []
for color, label in legend_entries:
    if color == "none":
        patches.append(mpatches.Patch(color="white", edgecolor="white", label=label))
    else:
        patches.append(mpatches.Patch(color=color, edgecolor="black", label=label))
ax.legend(handles=patches, loc="upper center", bbox_to_anchor=(0.5, -0.35),
          fontsize=10, ncol=1, prop={"family": "monospace"}, framealpha=0.95)

plt.tight_layout()
plt.savefig(out_dir / f"dirty_phase_categories_{phase}.png",
            dpi=120, bbox_inches="tight")
plt.close()

print(f"{workload} ({run_name}) — page categorization (load+run aware)")
print(f"  total: {total:,} pages ({mb(total):.0f} MB)")
print(f"  C1 static-RO:                 {c1:>10,}  ({100*c1/total:>5.1f}%)")
if has_load_csv:
    print(f"  C2 never-touched:             {c2:>10,}  ({100*c2/total:>5.1f}%)")
    print(f"  C3 init-once (load only):     {c3:>10,}  ({100*c3/total:>5.1f}%)")
else:
    print(f"  C2 W, no run events (no load CSV):")
    print(f"     C2+C3 combined:            {c2:>10,}  ({100*c2/total:>5.1f}%)")
print(f"  C4 recurring (run events):    {c4:>10,}  ({100*c4/total:>5.1f}%)")
print(f"  stays-RO (C1+C2+C3):          {norec:>10,}  ({100*norec/total:>5.1f}%)")
print(f"Wrote dirty_phase_categories_{phase}.png to {out_dir}")
