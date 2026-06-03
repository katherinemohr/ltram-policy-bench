#!/usr/bin/env python3
# Two-bar performance-degradation figure: vanilla 6.8 baseline vs LtRAM (one
# config). y = mean PR trial time (s, lower=better), error bars = min/max,
# degradation % annotated. y pinned at 0 so the (tiny) gap is shown honestly.
#
# usage: plot_perf_degradation.py <base_s> <ltram_s> <title> <out.png>
#                                 [base_min base_max ltram_min ltram_max] [util%]
import sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

base = float(sys.argv[1]); ltram = float(sys.argv[2])
title = sys.argv[3]; out = sys.argv[4]
err = None
if len(sys.argv) >= 9:
    bmn, bmx, lmn, lmx = map(float, sys.argv[5:9])
    err = [[base - bmn, ltram - lmn], [bmx - base, lmx - ltram]]
util = sys.argv[9] if len(sys.argv) >= 10 else None

deg = 100.0 * (ltram - base) / base
fig, ax = plt.subplots(figsize=(4.2, 4.6))
labels = ["vanilla 6.8\n(baseline)", "LtRAM"]
vals = [base, ltram]
colors = ["#7f7f7f", "#1f77b4"]
bars = ax.bar(labels, vals, color=colors, width=0.6,
              yerr=err, capsize=6, ecolor="#333333")
ax.set_ylabel("PageRank trial time (s)  —  lower is better")
ax.set_ylim(0, max(vals) * 1.25)
ax.set_title(title)
for b, v in zip(bars, vals):
    ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.3f}s",
            ha="center", va="bottom", fontsize=10)
# degradation callout
ax.annotate(f"{deg:+.1f}%\ndegradation",
            xy=(1, ltram), xytext=(1, max(vals) * 1.13),
            ha="center", fontsize=12, fontweight="bold",
            color=("#d62728" if deg > 1 else "#2ca02c"))
if util:
    ax.text(0.98, 0.02, f"LtRAM utilization: {util}%", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=8, color="#555555")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(out, dpi=140)
print(f"wrote {out}  (base={base:.4f} ltram={ltram:.4f} deg={deg:+.2f}%)")
