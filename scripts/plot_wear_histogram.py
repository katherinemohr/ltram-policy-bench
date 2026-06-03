#!/usr/bin/env python3
"""Bar chart of LtRAM per-frame wear from a profile_<label>.histogram file.
x = program/erase count per frame (log2 buckets), y = number of frames (pages).
Usage: scripts/plot_wear_histogram.py results/<ts>/profile_<label>.histogram [out.png]"""
import sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

path = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else path.rsplit(".", 1)[0] + "_wear.png"

labels, counts, in_sec = [], [], False
for line in open(path):
    s = line.strip()
    if "erase-count range" in s:          # section header
        if in_sec:                        # a 2nd section -> stop (per-frame only)
            break
        in_sec = True
        continue
    if in_sec:
        p = s.split()
        if len(p) >= 3 and p[0].isdigit() and p[-1].isdigit():
            if p[1] == "0":               # skip never-programmed frames (not "wear")
                continue
            labels.append(p[1])
            counts.append(int(p[-1]))

fig, ax = plt.subplots(figsize=(8, 5))
ax.bar(range(len(labels)), counts, color="tab:blue", edgecolor="black", linewidth=0.4)
ax.set_xticks(range(len(labels)))
ax.set_xticklabels(labels, rotation=45, ha="right")
ax.set_xlabel("Program (write) count per frame")
ax.set_ylabel("Number of frames (4 KB pages)")
ax.set_title("LtRAM wear distribution (per-frame program counts)")
ax.grid(True, axis="y", alpha=0.3)
fig.tight_layout()
fig.savefig(out, dpi=130)
print("wrote", out, "  (%d programmed-frame buckets)" % len(labels))
