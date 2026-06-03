#!/usr/bin/env python3
"""Generic LtRAM offload timeseries plot (util + write-back vs time).
Usage: plot_timeseries.py <csv> [out.png] ["Title"]"""
import sys
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
csv = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else csv.rsplit("/", 1)[0] + "/timeseries.png"
title = sys.argv[3] if len(sys.argv) > 3 else "LtRAM offload over time"
df = pd.read_csv(csv)
UTIL, WB = "tab:blue", "#d95f02"
fig, ax1 = plt.subplots(figsize=(9, 5))
l1, = ax1.plot(df.t_s, df.util_pct, color=UTIL, linewidth=1.8, label="LtRAM utilization")
ax1.set_xlabel("Running time (s)", fontsize=11)
ax1.set_ylabel("LtRAM Utilization (%)\nPages in LtRAM / Total Memory Usage", color=UTIL, fontsize=11)
ax1.tick_params(axis="y", labelcolor=UTIL); ax1.set_ylim(0, 100); ax1.set_xlim(left=0)
ax2 = ax1.twinx()
mark = max(1, len(df) // 15)
l2, = ax2.plot(df.t_s, df.repat_pct, color=WB, linewidth=1.6, marker="D", markersize=4, markevery=mark, label="Write-back")
ax2.set_ylabel("Write-back (%)\nPages moved out of LtRAM / moved in", color=WB, fontsize=11)
ax2.tick_params(axis="y", labelcolor=WB); ax2.set_ylim(0, max(5.0, df.repat_pct.max() * 1.2))
ax1.legend(handles=[l1, l2], loc="center right", framealpha=0.9)
ax1.set_title(title, fontsize=12); ax1.grid(True, alpha=0.3)
fig.tight_layout(); fig.savefig(out, dpi=130); print("wrote", out)
