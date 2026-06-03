#!/usr/bin/env python3
"""Plot the LtRAM write-ratio sweep (headline result).

Usage:  scripts/plot_writeratio.py results/writeratio/<ts>/writeratio.csv [out.png]

Two curves vs write ratio:
  - LtRAM utilization %  ("how much of redis's footprint moved to LtRAM / saved DRAM")
  - Repatriation %       ("of what moved in, how much bounced back out")
"""
import sys
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

csv = sys.argv[1] if len(sys.argv) > 1 else "writeratio.csv"
out = sys.argv[2] if len(sys.argv) > 2 else csv.rsplit("/", 1)[0] + "/writeratio.png"

df = pd.read_csv(csv, comment="#").sort_values("write_ratio")

fig, ax1 = plt.subplots(figsize=(8, 5))

c1 = "C0"
ax1.plot(df.write_ratio, df.utilization_pct, "o-", color=c1, label="LtRAM utilization")
ax1.set_xlabel("write ratio (% of ops that are updates)")
ax1.set_ylabel("LtRAM utilization % (redis footprint in LtRAM)", color=c1)
ax1.tick_params(axis="y", labelcolor=c1)
ax1.set_ylim(0, max(5, df.utilization_pct.max() * 1.15))

c2 = "C3"
ax2 = ax1.twinx()
ax2.plot(df.write_ratio, df.repat_pct, "s--", color=c2, label="repatriation %")
ax2.set_ylabel("repatriation % (moved-in pages that bounced back)", color=c2)
ax2.tick_params(axis="y", labelcolor=c2)
ax2.set_ylim(0, max(5, df.repat_pct.max() * 1.15))

ax1.set_title("LtRAM offload vs write ratio (ycsbc/redis)")
ax1.grid(True, alpha=0.3)
fig.tight_layout()
fig.savefig(out, dpi=120)
print("wrote", out)
