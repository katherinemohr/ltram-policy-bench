#!/usr/bin/env python3
"""Plot a scan_batch sweep produced by workloads/ltram_scan_sweep.sh.

The N=0 row is the scan-off baseline (overhead 0). For the scanning rows we plot,
against scan_batch (log x):
  - ycsbc throughput (KTPS) with the baseline as a dashed reference,
  - throughput overhead % (user-visible cost),
  - the ltram_scan kthread CPU seconds (background cost),
  - pages migrated into LtRAM (placement benefit).

Usage:  scripts/plot_scansweep.py results/scansweep/<ts>/sweep.csv [out.png]
"""
import sys
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

csv = sys.argv[1] if len(sys.argv) > 1 else "sweep.csv"
out = sys.argv[2] if len(sys.argv) > 2 else csv.rsplit("/", 1)[0] + "/scansweep.png"

df = pd.read_csv(csv, comment="#")
base = df[df.scan_batch == 0]
scan = df[df.scan_batch != 0].sort_values("scan_batch")
if not base.empty:                       # in-guest sweep: explicit N=0 baseline row
    base_ktps = float(base.ktps.iloc[0])
elif "base_ktps" in df.columns:          # per-boot sweep: per-row baseline column
    base_ktps = float(df.base_ktps.mean())
else:
    base_ktps = None

fig, ax = plt.subplots(2, 2, figsize=(11, 8))

ax[0, 0].plot(scan.scan_batch, scan.ktps, "o-", color="C0")
if base_ktps is not None:
    ax[0, 0].axhline(base_ktps, ls="--", color="gray",
                     label=f"baseline {base_ktps:.1f}")
    ax[0, 0].legend()
ax[0, 0].set(title="Throughput", xlabel="scan_batch (PTEs/wake)",
             ylabel="KTPS", xscale="linear")

ax[0, 1].plot(scan.scan_batch, scan.overhead_pct, "o-", color="C3")
ax[0, 1].set(title="User-visible overhead", xlabel="scan_batch (PTEs/wake)",
             ylabel="throughput drop vs baseline (%)", xscale="linear")
ax[0, 1].axhline(0, ls=":", color="gray")

ax[1, 0].plot(scan.scan_batch, scan.scan_cpu_s, "o-", color="C2")
ax[1, 0].set(title="Background cost (scan kthread)",
             xlabel="scan_batch (PTEs/wake)", ylabel="scan CPU (s)", xscale="linear")

ax[1, 1].plot(scan.scan_batch, scan.migrated_in, "o-", color="C4",
              label="migrated in")
ax[1, 1].plot(scan.scan_batch, scan.migrated_back, "s--", color="C1",
              label="repatriated")
ax[1, 1].set(title="Placement outcome", xlabel="scan_batch (PTEs/wake)",
             ylabel="pages", xscale="linear")
ax[1, 1].legend()

fig.suptitle("LtRAM scanning hand: scan_batch sensitivity (ycsbc/redis)")
fig.tight_layout()
fig.savefig(out, dpi=120)
print("wrote", out)
