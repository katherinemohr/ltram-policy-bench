# Idea 6, Option A — implementation plan

Predictive migration policy via decision tree, deployed as a runtime
lookup-table.

**Design constraint (from user):** offline cost can be unbounded. Online cost
must be tiny (target: per-decision overhead under 100 ns, per-page state under
8 bytes). This shifts the architecture: train rich, deploy minimal.

The plan splits cleanly into an **offline pipeline** (Steps 1–10) that produces
a tiny artifact, and an **online policy** (Steps 11–13) that consumes only that
artifact. The online policy never runs scikit-learn, never traverses a tree,
and never recomputes features at sweep granularity.

---

## Architecture overview

```
                   ┌──────────────────────┐
                   │  Offline pipeline    │   (runs once per workload class)
                   │  - replay traces     │
                   │  - feature mining    │
                   │  - tree training     │
                   │  - feature pruning   │
                   │  - LUT quantization  │
                   └──────────┬───────────┘
                              │  emits a 200–500 byte LUT
                              ▼
                   ┌──────────────────────┐
                   │  Online policy       │
                   │  - per-page state:   │
                   │    4 bytes           │
                   │  - migration decide: │
                   │    1 LUT lookup      │
                   └──────────────────────┘
```

The runtime cost is dominated by:
- Per write event: O(1) update of one cache line of per-page state
- Per migration decision: 3 bin-lookups + 1 LUT read + 1 branch

That's it. No tree traversal, no float math, no feature recomputation.

---

## Data substrate

The CSVs already contain everything needed. From
`results/runs/<date>/<workload>/dirty_sweep.csv`:

```
# total_sweeps=54277 total_seconds=5971.683 interval_ms=100 pid=141
vma_start, vma_end, vma_perms, vma_path, vpage_idx,
present_count, dirty_count, max_stab_period, final_stab_period, write_events
```

`write_events` is a `;`-separated list of sweep indices. Sweep interval is
100 ms (from header). For each page, we have the full write timeline at
100 ms granularity. That's the source-of-truth for replay and label
generation.

---

## Step 0 — verify the data

```bash
head -5 results/runs/2026-05-08/redis_xxlong/dirty_sweep.csv
wc -l   results/runs/2026-05-08/redis_xxlong/dirty_sweep.csv
```

Confirm:
- Header line shows `total_sweeps`, `interval_ms`
- Body has one row per page, with `write_events` populated for written pages

If `write_events` is empty for most pages on a workload, that workload doesn't
have enough write activity to train against — pick a different one.

---

## Step 1 — replay infrastructure

Create `workloads/monitoring/replay_traces.py`:

```python
"""Generator yielding (sweep_t, page_id, event_type) tuples in sweep order."""
import pandas as pd
from pathlib import Path

def replay(out_dir: Path):
    df = pd.read_csv(out_dir / "dirty_sweep.csv", comment="#",
                     dtype={"write_events": str})
    df["write_events"] = df["write_events"].fillna("")
    # build per-sweep event index
    events = []  # (sweep_t, page_id)
    for page_id, row in enumerate(df.itertuples()):
        if not row.write_events:
            continue
        for sw in row.write_events.split(";"):
            events.append((int(sw), page_id))
    events.sort()
    for sweep_t, page_id in events:
        yield sweep_t, page_id, "write"
```

This is the source-of-truth for offline simulation. Every other offline step
consumes events in this order.

---

## Step 2 — feature pool (rich, offline only)

For each candidate sample point `(page_id, sweep_t)`, the offline pipeline
computes a wide feature vector. Many of these will be too expensive to
maintain online — that's fine; we'll prune in Step 6.

```python
def compute_features(page_id, sweep_t, history, vma_meta, sweeps_per_sec=10):
    """history: list of write sweep indices for this page, all <= sweep_t.
       vma_meta: vma_perms, vma_path, came_from_load_phase.
       Returns dict of features."""
    f = {}
    f["sla"] = (sweep_t - history[-1]) if history else 99999
    f["log_sla"] = np.log1p(f["sla"])
    for K in [10, 30, 100, 300, 1000]:
        f[f"writes_in_last_{K}"] = sum(1 for s in history if s > sweep_t - K)
    f["total_writes"] = len(history)
    f["max_clean_streak"] = max_gap(history, sweep_t)
    if len(history) >= 2:
        gaps = np.diff(history)
        f["mean_iwt"] = gaps.mean()
        f["std_iwt"]  = gaps.std()
        f["fano"]     = (gaps.var() / gaps.mean()) if gaps.mean() > 0 else 0
    else:
        f["mean_iwt"] = f["std_iwt"] = f["fano"] = 0
    f["vma_writable"]   = int(vma_meta["vma_perms"][1] == "w")
    f["from_load"]      = int(vma_meta["came_from_load_phase"])
    f["is_anon"]        = int(vma_meta["vma_path"] == "")
    f["is_heap"]        = int("[heap]" in vma_meta["vma_path"])
    return f
```

Be generous: 15+ features. The point of offline is to *find* which ones matter,
not to commit to all of them.

---

## Step 3 — label generation

For each `(page_id, sweep_t)` sample:

```python
def label(history, sweep_t, K_horizon=30):
    """1 if a write occurs in (sweep_t, sweep_t + K_horizon]."""
    return int(any(sweep_t < s <= sweep_t + K_horizon for s in history))
```

`K_horizon` is the prediction horizon. Choose it to match the migration
decision cadence — if we make decisions every ~3s, K_horizon = 30 sweeps.
This is a tunable; in Step 7 we'll sweep it.

---

## Step 4 — sampling strategy

Don't enumerate `pages × sweeps` (e.g., 65K × 54K = 3.5B samples). Sample
strategically:

```python
def sample_decision_points(df, n_per_workload=20_000):
    """Yield (page_id, sweep_t) at points where a migration decision would
       plausibly be made — i.e., the page has been clean for > T_min sweeps
       and is therefore "eligible" for consideration."""
    T_min = 50  # 5 seconds at 100ms
    candidates = []
    for page_id, row in enumerate(df.itertuples()):
        events = parse_events(row.write_events)
        # decision points: T_min sweeps after each write event,
        # plus T_min sweeps after start-of-trace if no writes
        prev = 0
        for e in events:
            if e - prev >= T_min:
                # all sweeps in [prev + T_min, e) are candidates
                for t in range(prev + T_min, e):
                    candidates.append((page_id, t))
            prev = e
    return random.sample(candidates, min(n_per_workload, len(candidates)))
```

Why "decision points" instead of uniform sampling:
- Most sweeps for most pages are uninteresting (page is "obviously" hot or
  "obviously" cold)
- The hard cases — where the policy actually has to choose — are concentrated
  near stability-window boundaries
- Training on these gives more discriminative power per sample

Class-balance: stratify so positive/negative labels are ~50/50.

---

## Step 5 — train/test split (time-based, not random)

```python
# train: samples where sweep_t < total_sweeps * 0.5
# test:  samples where sweep_t >= total_sweeps * 0.5
```

A random split would leak future information into training. Time-based split
mimics deployment: model trained on early data, evaluated on later data.

---

## Step 6 — model fitting

Three models, all sklearn:

```python
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

# 1. Tiny tree — the deployment candidate
tree = DecisionTreeClassifier(max_depth=4, max_leaf_nodes=8,
                              class_weight="balanced")
tree.fit(X_train, y_train)

# 2. Boosted ensemble — upper-bound comparison
gb = GradientBoostingClassifier(n_estimators=100, max_depth=3)
gb.fit(X_train, y_train)

# 3. Logistic regression — sanity baseline
lr = LogisticRegression(class_weight="balanced", max_iter=1000)
lr.fit(X_train, y_train)
```

The tiny tree is the deployment target. The other two are baselines for the
"how much accuracy am I leaving on the floor" question. If the tree is within
3% AUC of the boosted ensemble, you're not losing much by going small.

Compute on the test set:
- AUC, precision, recall
- Confusion matrix (FP rate matters: false positive = wasted migration)
- Comparison to threshold-T baseline (best T from grid search)

---

## Step 7 — feature pruning

Find the smallest feature subset that achieves 95% of full-feature AUC:

```python
from sklearn.feature_selection import SequentialFeatureSelector

# greedily add features until AUC plateaus
sfs = SequentialFeatureSelector(
    DecisionTreeClassifier(max_depth=4, max_leaf_nodes=8),
    n_features_to_select="auto", tol=0.005,
)
sfs.fit(X_train, y_train)
selected = X_train.columns[sfs.get_support()]
print(f"Selected {len(selected)} features: {list(selected)}")
```

Hypothesis: 3 features will capture ≥95% of the signal. Likely candidates:
- `sla` (or `log_sla`)
- `writes_in_last_100`
- `vma_writable`

Print the resulting tree:

```python
from sklearn.tree import export_text
print(export_text(tree, feature_names=list(selected)))
```

You should see something readable like:
```
|--- sla <= 7.5
|    |--- class: 1   (migrate=NO; recently written, will write again)
|--- sla > 7.5
|    |--- writes_in_last_100 <= 3.5
|    |    |--- class: 0  (migrate=YES; cold and infrequent)
|    |--- writes_in_last_100 > 3.5
|    |    |--- vma_writable <= 0.5
|    |    |    |--- class: 0  (read-only, migrate)
|    |    |--- vma_writable > 0.5
|    |    |    |--- class: 1  (writable + active, hold)
```

If the tree isn't readable, the model isn't deployable. Reduce `max_depth`.

---

## Step 8 — online feature encoding (the runtime contract)

For each surviving feature, design a cheap-to-maintain online representation.
**This is where runtime overhead is decided.**

Per-page state (4 bytes total, fits in 1/16th of a cache line):

```c
struct page_pred_state {
    uint16 last_write_sweep;     // wraps; treat as relative
    uint8  density_q;            // EWMA of recent writes, quantized 0-255
    uint8  class_bits;           // C1-C4 (2 bits) + flags (6 bits)
};
```

Maintenance hooks:

```c
// Called on every write event detected in the soft-dirty sweep
void on_write_event(page_id_t p, uint32 sweep_t) {
    state[p].last_write_sweep = sweep_t & 0xFFFF;

    // EWMA: density ← α*density + (1−α)*1.0, scaled to uint8
    // α = 7/8 → density_q = (density_q * 7 + 32) >> 3
    state[p].density_q = (state[p].density_q * 7 + 32) >> 3;
}

// Called on every sweep where we look for migration candidates
// (only invoked for pages already passing eligibility, not all pages)
bool should_migrate(page_id_t p, uint32 sweep_t) {
    /* see Step 11 */
}
```

Critical: `on_write_event` is the only per-event work. It updates one struct
(8 bytes touched, one cache line). No list traversal, no global counters.

Per-decision feature derivation:

```c
uint16 sla(page_id_t p, uint32 sweep_t) {
    uint16 then = state[p].last_write_sweep;
    uint16 now  = sweep_t & 0xFFFF;
    return (uint16)(now - then);   // wraps correctly
}
```

`writes_in_last_100` is approximated by `density_q` (EWMA proxy). The offline
pipeline must verify this approximation is faithful enough — see Step 9.

---

## Step 9 — verify the online encoding offline

The online encoding (uint16 sla, EWMA density_q) is *not* identical to the
exact features computed during training. Verify the gap is acceptable:

```python
# Replay: compute both exact features and online-encoded features for every
# sample in the test set
exact_X = compute_exact_features(...)
online_X = simulate_online_encoding(...)

# Train tree on exact, evaluate on both
tree.fit(exact_X_train, y_train)
auc_exact  = roc_auc_score(y_test, tree.predict_proba(exact_X_test)[:, 1])
auc_online = roc_auc_score(y_test, tree.predict_proba(online_X_test)[:, 1])

# If auc_online drops by >2% relative to auc_exact, the online encoding is
# losing signal — adjust α, density_q resolution, etc.
```

Iterate on encoding parameters until the gap is acceptable (target: <2%).

---

## Step 10 — quantize the model into a LUT

This is what makes the runtime overhead constant.

```python
def build_lut(tree, sla_bins, dens_bins):
    """Pre-evaluate tree at every quantized feature combination."""
    n_classes = 16   # 2 bits class + 2 bits flags
    LUT = np.zeros((len(sla_bins), len(dens_bins), n_classes), dtype=np.uint8)
    for i, sla in enumerate(sla_bins):
        for j, dens in enumerate(dens_bins):
            for k in range(n_classes):
                cls    = k & 0x3
                writ   = (k >> 2) & 0x1
                # ... reconstruct feature vector matching online encoding
                X = encode(sla, dens, cls, writ)
                LUT[i, j, k] = tree.predict([X])[0]
    return LUT
```

Choose bin edges geometrically (the relevant time scales span 1ms to hours):

```python
sla_bins  = [0, 5, 30, 300, 3000, 30000]    # 6 bins
dens_bins = [0, 16, 64, 128, 192, 255]      # 6 bins
# LUT shape: 6 × 6 × 16 = 576 entries × 1 byte = 576 bytes
```

The whole LUT fits in 9 cache lines. Will be in L1 within microseconds of
first access and stay there. Free at runtime.

Emit the LUT as a C array:

```c
static const uint8_t MIGRATION_LUT[6][6][16] = {
    /* generated by offline pipeline */
    {{0, 1, 1, 0, ...}, ...},
    ...
};
```

Drop into the kernel module / simulator at compile time.

---

## Step 11 — runtime decision function

```c
static inline uint8_t bin_sla(uint16 v) {
    if (v < 5)     return 0;
    if (v < 30)    return 1;
    if (v < 300)   return 2;
    if (v < 3000)  return 3;
    if (v < 30000) return 4;
    return 5;
}

static inline uint8_t bin_dens(uint8 v) {
    if (v < 16)  return 0;
    if (v < 64)  return 1;
    if (v < 128) return 2;
    if (v < 192) return 3;
    return 4;
}

bool should_migrate(page_id_t p, uint32 sweep_t) {
    struct page_pred_state s = state[p];
    uint16 sla = sweep_t - s.last_write_sweep;
    uint8 i = bin_sla(sla);
    uint8 j = bin_dens(s.density_q);
    return MIGRATION_LUT[i][j][s.class_bits] != 0;
}
```

Cost analysis:
- 1 struct load (one cache line)
- 2 small switches (≤6 branches each, predictable)
- 1 LUT lookup (resident in L1)
- 1 final branch
- **Total: ~10 ns on modern hardware, branch-prediction-friendly**

That's the runtime budget. Compare to:
- Threshold-T policy: 1 comparison ≈ 1 ns
- Decision tree traversal: 3-4 branches × predictability penalties ≈ 20 ns
- Random forest: 100+ branches ≈ 200+ ns

The LUT is within an order of magnitude of bare threshold-T while delivering
ML-quality decisions.

---

## Step 12 — validate end-to-end

Create `workloads/monitoring/eval_predictive_policy.py`:

```python
def evaluate_policy(out_dir, policy_fn):
    """Replay the workload's write events; for each decision point,
       call policy_fn(features) -> {migrate, hold}; tally wear and utility."""
    total_migs = 0
    total_page_seconds = 0
    for sweep_t, page_id, _ in replay(out_dir):
        # update per-page state on write
        update_state(page_id, sweep_t)
    # then iterate decision points and call policy_fn
    ...
    return total_migs, total_page_seconds

# Compare three policies:
results = {
    "threshold_best_T":   evaluate_policy(out_dir, threshold_policy(T=best_T)),
    "lut_predictive":     evaluate_policy(out_dir, lut_policy()),
    "oracle":             evaluate_policy(out_dir, oracle_policy()),
}
```

Plot for each workload:
- x-axis: total migrations (= NOR endurance consumed)
- y-axis: page-seconds served from LtRAM (= utility)
- One point per policy (or curve, sweeping policy parameters)

The contribution claim: **the LUT policy lies on a Pareto-optimal point that
fixed-T cannot reach** — same utility at fewer migrations, or more utility at
the same migrations.

---

## Step 13 — generalization tests

A model trained on one workload may not generalize. Test:

| Train | Test | Expected |
|-------|------|----------|
| redis_short_zipf | redis_xxlong_zipf | Should generalize (same app, longer run) |
| redis_xlong | redis_xxlong | Should generalize (uniform converged) |
| redis_xlong | gapbs_xxlong | Likely won't generalize (different access patterns) |
| Pooled (redis + gapbs + matmul) | held-out workload | Maybe |

If single-workload training generalizes within-app: ship per-app LUTs.
If pooled training works: ship one LUT for everything.
If neither works: per-app + online correction (drop in Option C territory).

---

## Step 14 — stretch: online retraining

If the deployment context drifts (workload shifts), a once-trained LUT becomes
stale. Two retraining strategies:

1. **Periodic offline retrain.** Every day/week, run the offline pipeline on
   recent telemetry. Hot-swap the LUT. No runtime overhead added; just
   logistics.
2. **Trust score on the LUT.** Per-decision: track whether prediction was
   correct (compare migrate/hold against actual subsequent behavior). If
   trust drops below threshold, fall back to threshold-T policy and trigger a
   retrain.

Both keep the runtime fast path identical — the LUT itself never changes
during inference, only between epochs.

---

## Effort estimate

| Step | Effort | Notes |
|------|--------|-------|
| 0–1 (data + replay) | 1 day | Mostly reading existing CSVs |
| 2–4 (features + sampling) | 1 day | Generous feature pool, mostly pandas |
| 5–7 (training + pruning) | 1 day | Standard sklearn |
| 8–9 (online encoding + verify) | 2 days | The fiddly part — α tuning, bin edges |
| 10–11 (LUT + runtime function) | 1 day | Mostly mechanical |
| 12 (end-to-end eval) | 2 days | Build the simulator if not already there |
| 13 (generalization) | 1 day | Permutations of train/test |

**Total: ~9 days for a research-quality result.** First measurable signal
(LUT vs threshold AUC) by end of day 4.

---

## What to look for in the results

- **Tree is readable.** A 4-leaf tree using 3 features is interpretable. If
  you can't write down what the tree learned in two sentences, simplify.
- **AUC > 0.85.** Below this, threshold-T is probably good enough.
- **Wear improvement of ≥15%** at equal utility, or **utility improvement of
  ≥10%** at equal wear. Smaller deltas aren't worth the complexity.
- **LUT vs full tree AUC gap < 2%.** Otherwise the quantization is too coarse.
- **Cross-workload generalization within app family.** If redis_short→redis_xxlong
  fails, the policy isn't learning something fundamental — it's overfitting.
