#!/usr/bin/env python3
"""Plot pagerank LtRAM residency over time (the offload-and-stay story).
Usage: scripts/plot_pagerank_timeseries.py results/<ts>/pagerank_timeseries.csv [out.png]"""
import sys
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

csv = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else csv.rsplit("/", 1)[0] + "/pagerank_timeseries.png"
df = pd.read_csv(csv)

UTIL = "tab:blue"          # Tableau palette; balanced so neither dominates
WB = "#d95f02"             # dark orange -> readable against the axis, not red

fig, ax1 = plt.subplots(figsize=(9, 5))

l1, = ax1.plot(df.t_s, df.util_pct, color=UTIL, linewidth=1.8,
               label="LtRAM utilization")
ax1.set_xlabel("Running time (s)", fontsize=11)
ax1.set_ylabel("LtRAM Utilization (%)\nPages in LtRAM / Total Memory Usage",
               color=UTIL, fontsize=11)
ax1.tick_params(axis="y", labelcolor=UTIL)
ax1.set_ylim(0, 100)                       # 0 at the bottom; full % range
ax1.set_xlim(left=0)

ax2 = ax1.twinx()
# markers (not just a line) so a flat ~0% series is visible off the x-axis
mark = max(1, len(df) // 15)
l2, = ax2.plot(df.t_s, df.repat_pct, color=WB, linewidth=1.6,
               marker="D", markersize=4, markevery=mark, label="Write-back")
ax2.set_ylabel("Write-back (%)\nPages moved out of LtRAM / moved in",
               color=WB, fontsize=11)
ax2.tick_params(axis="y", labelcolor=WB)
wb_top = max(5.0, df.repat_pct.max() * 1.2)
ax2.set_ylim(0, wb_top)                     # same 0 as the left axis, on the x-axis
ax2.annotate("write-back ≈ 0%  (stays in LtRAM)",
             xy=(df.t_s.iloc[-1] * 0.62, df.repat_pct.max()),
             xytext=(df.t_s.iloc[-1] * 0.40, wb_top * 0.30),
             color=WB, fontsize=10,
             arrowprops=dict(arrowstyle="->", color=WB, lw=1))

ax1.legend(handles=[l1, l2], loc="center right", framealpha=0.9)
ax1.set_title("PageRank: read-only graph offloads to LtRAM and stays", fontsize=12)
ax1.grid(True, alpha=0.3)
fig.tight_layout()
fig.savefig(out, dpi=130)
print("wrote", out)
