"""
Cold-threshold bar chart: for each threshold T, how many pages are currently
cold, and of those, how many are high-churn writers?

  dirty_cold_threshold_{phase}.png

Two bars per threshold T:
  - "cold for T":       % of present pages with final_stab_period >= T seconds
                        (unwritten for at least T seconds at end of observation)
  - "cold + high churn": % of cold-for-T pages with write rate >= WRITE_RATE_THRESHOLD
                        writes per RATE_WINDOW_S seconds (pages in a quiet window but actively written
                        overall — risky LtRAM candidates)

Thresholds T are configurable via COLD_THRESHOLDS_S below (seconds).
T values exceeding the run length are skipped automatically.

Usage: dirty_cold_threshold_plot.py <workload> <run_name> [phase]
"""

import sys
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from _phase_data import load_pages, parse_args_phase

TOP_DIR = Path(__file__).parents[2]
RESULTS_DIR = TOP_DIR / "results"

# === Configuration ===
COLD_THRESHOLDS_S = [1, 2, 4, 8, 16, 32, 64, 128, 256]   # seconds
RATE_WINDOW_S = 8            # express write rate as writes per RATE_WINDOW_S seconds
WRITE_RATE_THRESHOLD = 1.0   # threshold in writes/RATE_WINDOW_S — bar 2 criterion

workload, run_name, phase = parse_args_phase(sys.argv)
out_dir = RESULTS_DIR / "runs" / run_name

df = load_pages(out_dir, phase)
total_sweeps  = df.attrs["total_sweeps"]
total_seconds = df.attrs["total_seconds"]
interval_ms   = df.attrs["interval_ms"]
phase_label   = df.attrs["label"]
sec_per_sweep = interval_ms / 1000.0

n_pages = len(df)
if n_pages == 0:
    print("WARNING: no pages, nothing to plot")
    sys.exit(0)

thresholds = [T for T in COLD_THRESHOLDS_S if T <= total_seconds]
if not thresholds:
    print(f"WARNING: all thresholds exceed run length ({total_seconds:.1f}s), nothing to plot")
    sys.exit(0)

final_stab_s   = df["final_stab_period"] * sec_per_sweep
write_rate     = df["dirty_count"] / total_seconds * RATE_WINDOW_S   # writes per RATE_WINDOW_S

cold_pct       = []
high_churn_pct = []

for T in thresholds:
    cold_mask       = final_stab_s >= T
    n_cold          = cold_mask.sum()
    cold_pct.append(100.0 * n_cold / n_pages)

    if n_cold > 0:
        high_churn_mask = cold_mask & (write_rate >= WRITE_RATE_THRESHOLD)
        high_churn_pct.append(100.0 * high_churn_mask.sum() / n_cold)
    else:
        high_churn_pct.append(0.0)

print(f"{workload} ({run_name}) — cold threshold summary ({phase_label})")
print(f"  run length: {total_seconds:.1f}s   pages: {n_pages}   "
      f"interval: {interval_ms}ms   high-churn threshold: {WRITE_RATE_THRESHOLD} per {RATE_WINDOW_S}s")
print(f"  {'T (s)':>8}  {'cold %':>8}  {'high-churn % of cold':>22}")
for T, cp, hcp in zip(thresholds, cold_pct, high_churn_pct):
    print(f"  {T:>8}  {cp:>8.1f}  {hcp:>22.1f}")

# === Plot ===
x = np.arange(len(thresholds))
width = 0.38

fig, ax = plt.subplots(figsize=(max(8, len(thresholds) * 1.2), 6))

bars1 = ax.bar(x - width / 2, cold_pct,       width, label="cold for ≥ T seconds",
               color="#4c72b0", alpha=0.85)
bars2 = ax.bar(x + width / 2, high_churn_pct, width,
               label=f"of cold: promotion rate ≥ {WRITE_RATE_THRESHOLD:.0f} per {RATE_WINDOW_S}s",
               color="#dd8452", alpha=0.85)

ax.set_xlabel("cold threshold T (seconds)", fontsize=11)
ax.set_ylabel("percent (%)", fontsize=11)
ax.set_title(
    f"{workload} — page cold-threshold analysis  [{phase_label}]\n"
    f"run length = {total_seconds:.0f}s  |  {n_pages:,} pages  |  "
    f"interval = {interval_ms}ms  |  high-churn ≥ {WRITE_RATE_THRESHOLD:.0f} per {RATE_WINDOW_S}s",
    fontsize=11,
)
ax.set_xticks(x)
ax.set_xticklabels([str(T) for T in thresholds])
ax.set_ylim(0, 105)
ax.legend(fontsize=10)
ax.grid(axis="y", alpha=0.3)

for bar in bars1:
    h = bar.get_height()
    if h > 2:
        ax.text(bar.get_x() + bar.get_width() / 2, h + 1, f"{h:.1f}",
                ha="center", va="bottom", fontsize=8)
for bar in bars2:
    h = bar.get_height()
    if h > 2:
        ax.text(bar.get_x() + bar.get_width() / 2, h + 1, f"{h:.1f}",
                ha="center", va="bottom", fontsize=8)

plt.tight_layout()
out_path = out_dir / f"dirty_cold_threshold_{phase}.png"
plt.savefig(out_path, dpi=120, bbox_inches="tight")
plt.close()
print(f"\nWrote {out_path}")
