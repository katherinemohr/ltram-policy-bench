# LtRAM policy implementation ideas — architecture sketches

Seven directions extracted from LLFREE patterns, tailored to the LtRAM
(NOR-flash NUMA tier) migration policy problem. Each section gives a basic
architecture so you can decide which to prototype first.

Project context recap:
- LtRAM holds ~65,536 pages backed by NOR flash
- NOR cells endure ~100,000 erases each → chip endurance budget = 6.4×10⁹ writes
- Goal: pick migration policy that maximizes utility (page-seconds served from
  LtRAM) while staying inside the endurance budget over a 5-year deployment
- Today's policy: a single global stability threshold T (page must be clean for
  T seconds before migrating)
- Pages are already classified C1/C2/C3/C4 in `_phase_data.py::classify_pages`

---

## 1. Per-class policy (replace the global T)

**Frame:** instead of one threshold T applied to every page, treat T as a
4-tuple {T_C1, T_C2, T_C3, T_C4} — and recognize that the *mechanism*, not just
the value, should differ per class.

### Per-page state (4 bytes per page, ~260 KB at 65K pages)

```
struct PageState {
    uint8  class;          // 1..4 (or extended with substates)
    uint8  flags;          // burned, locked, hint_source
    uint16 T_p;            // current threshold (sweeps), per-page
}
```

### Class-specific actions

| Class | T | Action | Re-classification trigger |
|-------|---|--------|--------------------------|
| C1 (vma not writable) | 0 | Migrate immediately | mprotect → demote |
| C2 (writable, never touched) | small (~5s) | Migrate speculatively | first write → "burned" forever |
| C3 (writable, load-phase only) | wait for load-end | Migrate at load-end signal | post-init write → C4 |
| C4 (writable, run events) | ∞ or very long | Don't migrate; or only after long_idle | first hot-write → revert to DRAM |

### State machine for online transitions

- C2 → C4 on first write event during run phase
- C3 → C4 on write after the load-end signal
- Any class → C1 on mprotect-RO
- C1 → C4 on mprotect-RW (rare)
- "Burned" sticky bit: once a page transitions C2→C4 or has a migration roll
  back, it's permanently disqualified from speculative migration

### Why this beats global T

- C1 contributes ~100% utility per write (one-time placement; whole runtime of payback)
- C4 contributes near-zero utility per write
- A single global T forces a compromise. With per-class T, each class gets its
  Pareto-optimal point independently

### Online classification when there's no explicit load phase

Three options, increasing accuracy:
1. **Heuristic init window:** "first 30s of process lifetime is load phase."
   Cheap; somewhat works for most apps.
2. **Behavioral phase detector:** detect when write rate drops below a
   threshold and stays there → declare load-end. Requires a sliding-window
   write counter per process.
3. **Explicit signal:** new syscall `ltram_phase_transition()` or madvise hint
   `MADV_PHASE_RUN`. Apps that opt in get accurate C3 detection.

### Implementation in the simulator

- Add `class` and `T_p` fields to per-page table
- At every sweep boundary: re-classify if state changed, then apply per-class
  migration logic
- Keep current global-T policy as the C4 baseline
- Run head-to-head; report wear and utility per class

### Validation

Compare "best fixed global T" vs "per-class T tuned independently." If
per-class wins on every workload, this is a strict-dominance result.

---

## 2. Counter splitting for write tracking

**Why this matters at scale:** the current sweep-based design dodges contention
by reading soft-dirty bits (kernel maintains them atomically per-PTE). For any
*finer-grained* tracking (write-stride detection, per-event timestamps, ML
feature collection), per-event counters are needed — and those will contend on
multi-core.

### Option A: per-CPU exact counters (simplest)

```c
struct counter_shard {
    uint32 counts[N_PAGES];
} per_cpu __aligned(64);

struct global_counter {
    uint64 totals[N_PAGES];
    spinlock_t merge_lock;
};
```

Each CPU writes only to its own shard. Merge thread (or query-time aggregator)
sums shards into the global view.

Memory: 26 CPUs × 65K pages × 4 bytes ≈ 6 MB. Workable for LtRAM scale; doesn't
scale to GB-of-pages systems.

### Option B: per-CPU Count-Min Sketch (constant memory)

```c
struct cms_shard {
    uint32 table[D][W];
    hash_seed_t seeds[D];
} per_cpu __aligned(64);
```

Each write hashes the page-id into D row buckets, increments. Query: take min
across D rows. Per-CPU sketches merge by element-wise max.

Memory: D=4, W=4096 → 64 KB per CPU × 26 = 1.6 MB total. Constant in number of
pages.

Trade-off: counts are upper-bound estimates (slight over-count). Fine for "is
this page hot" decisions, bad for exact accounting.

### Option C: per-CPU ring buffer of recent events

```c
struct event_ring {
    struct { uint32 page_id; uint64 ts; } buf[RING_SIZE];
    uint32 head, tail;
} per_cpu __aligned(64);
```

Each CPU appends `(page_id, timestamp)` events to its local ring. Merger thread
drains all rings into a global event log every 100 ms.

Memory: RING_SIZE=4096 × 16 bytes × 26 = 1.6 MB. Lossy under high write rates,
but preserves event order — good for stride/burstiness features.

### Picking between them

| If you need… | Use |
|--------------|-----|
| Exact totals, fixed page set | A |
| Approximate "is this page hot" at GB-scale | B |
| Recent activity windows + temporal features | C |

### Validation

Synthetic workload: 26 cores all writing to the same page. Measure simulator
slowdown vs ideal. Apply Option A. Re-measure. Quantify the speedup.

The lesson from LLFREE isn't "use these specific structures" — it's "find every
shared write path and structurally remove the sharing."

---

## 3. Adaptive per-page T

**Frame:** every page has its own T_p, updated AIMD-style (TCP congestion-control
analog) based on its own success/failure history.

### Per-page state (4 bytes)

```c
struct PageState {
    uint16 T_p;            // current threshold, in sweeps
    uint8  miss_streak;    // recent misprediction count
    uint8  flags;
};
```

### Update rules (AIMD)

```python
on epoch_terminated_by_write(p, epoch_length):
    if epoch_length < T_p[p]:
        # we predicted stable, page got rewritten — bad
        T_p[p] = min(T_max, T_p[p] * 2)        # multiplicative back-off
        miss_streak[p] += 1
    else:
        # we waited longer than the actual epoch — slightly conservative but ok
        T_p[p] = max(T_min, T_p[p] - 1)        # linear decrease
        miss_streak[p] = 0

on successful_migration_completed(p):
    T_p[p] = max(T_min, T_p[p] * 0.95)         # gradual relaxation
```

### Behavior over time

- Churning page → T_p driven to T_max, effectively disqualified
- Consistently stable page → T_p settles at T_min, migrates at earliest opportunity
- The system finds per-page optima without manual tuning

### Alternatives to AIMD

1. **EMA of stable epoch length:**
   ```
   T_p[p] = α * observed_epoch_length + (1−α) * T_p[p]
   migrate_if: current_clean_streak > T_p[p] * β   (β=1.5 safety)
   ```

2. **Per-page bandit:** treat T as discrete arm choice (T=1s, 5s, 30s, 5m, ∞).
   Update arm rewards as `+page_seconds_gained − wear_cost`. Pick arm with
   highest reward. Page-by-page Q-learning.

### Implementation in the simulator

- Add T_p field to page table
- At each sweep, run AIMD update for any page whose epoch just terminated
- Migration trigger uses per-page T_p instead of global T
- Keep global T as fallback for unclassified pages

### Validation

Plot wear vs lifetime utility for:
- Best fixed global T
- AIMD-adaptive per-page T
- Oracle (knows future writes)

The gap between fixed-T and oracle is addressable improvement; gap between
AIMD and oracle is what's left on the table.

---

## 4. Constructive avoidance — categorize so the decision is trivial

**Frame:** stop tuning T; instead, eliminate as many pages as possible from
"needing T at all." Reduce the threshold-required population to the residual
ambiguous cases.

### Multi-stage classification pipeline

```
   page allocated
        │
        ▼
┌─────────────────────────────────────────┐
│ Stage 1: Static analysis (declarative)  │
│ - VMA permissions PROT_READ?    → C1    │
│ - File-backed RO mapping?       → C1    │
│ - mmap(MAP_FIXED) on RO file?   → C1    │
│ - madvise(MADV_TIER_LATE)?      → C1    │
│ - Slab page for RO inode cache? → C1    │
└─────────────────────────────────────────┘
        │ (didn't resolve)
        ▼
┌─────────────────────────────────────────┐
│ Stage 2: Allocation-context heuristic   │
│ - mmap'd by .rodata loader?  → C1       │
│ - JIT code page (post-emit)? → C1       │
│ - Anonymous + PROT_WRITE?    → C4 prior │
│ - tmpfs writable region?     → C4 prior │
└─────────────────────────────────────────┘
        │ (still ambiguous)
        ▼
┌─────────────────────────────────────────┐
│ Stage 3: Behavioral observation         │
│ Threshold-based stability sweep         │
│ This is the SLOW PATH — only here for   │
│ pages that resisted Stages 1-2          │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│ Stage 4: Online correction              │
│ If Stage 1/2 was wrong (a "C1" page got │
│ written), demote and add to "burned"    │
│ list — never re-promote that page       │
└─────────────────────────────────────────┘
```

### Per-page classification result (2 bits)

- `00` unclassified (apply behavioral default)
- `01` confirmed-C1 (Stage 1 declarative)
- `10` predicted-C1, awaiting confirmation (Stage 2 heuristic)
- `11` demoted (was C1, got written — never re-promote)

### Where the classification lives

- Stage 1: at `mmap()` / `mprotect()` time
- Stage 2: at first page-fault for the page
- Stage 3: existing sweep loop
- Stage 4: hook in page-fault handler that fires on writes to pages tagged
  `01` or `10`

### Real-world impact estimate

In gapbs xxlong, the graph data after `MakeGraph()` is read-only — likely 50%+
of process pages. Stage-1 PROT_READ check classifies all of them as C1
immediately, no T needed. Threshold-based classification reduces to working set
+ scratch buffers, maybe 10% of pages.

In redis: index tables, dispatch code, .rodata constants, JIT-emitted Lua
handlers are all C1. Probably 20–30% of resident pages.

### Why this is a real-world win, not just a sim trick

Soft-dirty sweep cost is proportional to the *observed* page set. If 50% of
pages are pre-classified, the sweep does half the work. Migration latency drops
because Stage-1 classified pages migrate immediately on first fault — no
waiting for a stability window. Time-to-LtRAM-utility startup collapses.

### Validation

- Annotate (manually, for now) the obvious C1 sources in the three workloads
- Measure: (a) % of pages now Stage-1 classifiable, (b) prediction accuracy of
  those classifications, (c) reduction in time-to-LtRAM-utility
- If (a) > 30% and (b) > 99%, you have a publishable result

---

## 5. Lazy migration

**Frame:** decouple "this page is *eligible* to migrate" from "migrate it now."
Eligibility is a prediction; migration is a cost. Defer the cost until
something forces the decision.

### Two-stage policy

```python
# Stage 1: Eligibility tracker
on every sweep:
    if clean_streak > T:
        if not eligible:
            eligible_since = current_sweep
            enqueue(eligible_pq, page, priority = stability_age)
    else:
        if eligible:
            dequeue(eligible_pq, page)
            eligible_since = NULL

# Stage 2: Migration trigger (any one fires)
trigger A: DRAM_pressure
    when free_dram_pages < watermark:
        drain top-K from eligible_pq, migrate them
trigger B: idle_scheduler
    when CPU idle and NOR bus idle:
        drain N pages from eligible_pq
trigger C: periodic batch
    every 10s, drain top-K oldest-eligible pages
trigger D: erase-block fill
    if N eligible pages in same NOR erase block:
        migrate them as a batch (NOR programming amortized)
trigger E: explicit
    app calls madvise(MADV_TIER_NOW) on a region
```

### Per-page state

```c
struct PageState {
    uint32 clean_streak;
    uint32 eligible_since;     // 0 if not eligible
    void  *pq_node;            // priority queue entry, NULL if not enqueued
};
```

### Eligibility queue

- Priority queue ordered by `eligible_since` (oldest first → migrate first when forced)
- Insertion when a page crosses T cleanly
- Removal when a page gets written
- Drain on trigger conditions

### Why lazy beats eager

Eager scenario: T=30s; page goes clean at t=0; eager migrates at t=30s; page
gets rewritten at t=35s. Cost = one migration + one write-back = wasted move.

Lazy scenario: same setup. Lazy enqueues at t=30s but doesn't migrate. Page
gets rewritten at t=35s, dequeued — never migrated. Saved one wasted migration.

The benefit scales with the fraction of pages that "almost made it" but don't.
The simulator probably already has telemetry to measure this fraction directly.

### Trade-off and tuning

- High DRAM pressure → drain queue aggressively (look more like eager)
- Low DRAM pressure → wait longer (let speculative cases self-resolve)
- Pure idle → background drain at low rate, fill LtRAM gradually

The effective T becomes a function of system pressure rather than fixed
wall-clock time. Closer to how Linux handles swap (LRU + pressure-driven
reclaim) and far better matches NOR's preference for batched programming.

### Implementation in the simulator

- Add eligibility queue (heap) keyed by `eligible_since`
- Replace "clean_streak > T → migrate" with "clean_streak > T → enqueue"
- Add simulated DRAM pressure model
- Add periodic batch drain
- Compare wear and utility against eager baseline

### Validation

For each workload, plot wear vs DRAM pressure curve. Eager is one point on
this curve (always migrate). Lazy gives a continuum — show lazy curve dominates
eager except at extremely high pressure.

---

## 6. Predictive ML / heuristic models

**Frame:** instead of a threshold rule, learn `P(rewrite within Δt | page features)`
and migrate only when P is low.

### Option A: hand-crafted features + small decision tree (start here)

**Per-page features tracked online:**
```c
struct PageFeatures {
    uint32 writes_in_last_10_sweeps;
    uint32 writes_in_last_100_sweeps;
    uint32 sweeps_since_last_write;
    uint32 max_clean_streak_seen;
    uint8  vma_writable;
    uint8  came_from_load_phase;
    uint32 avg_inter_write_time;
    uint32 stddev_inter_write_time;
};
```

Memory cost: ~32 bytes × 65K pages = 2 MB.

**Training:**
- Replay existing workload runs offline
- For each (page, sweep_t), compute features and label "did the page get
  rewritten in next K sweeps?"
- Fit small decision tree (≤10 leaves)

**Deployment:**
- Tree exports to a C nested-if statement
- Inference cost: log-depth comparisons, sub-µs

**Why this is a useful starting point:**
- Interpretable: read the tree to understand what it keys on
- Debuggable: when wrong, the failed feature is obvious
- Comparable: single migrate/no-migrate decision, drop-in for the threshold

### Option B: 2Q / LIRS-style queues (no training)

**Two queues:**
```
A1 (recently_touched): pages written in last N sweeps
Am (frequently_touched): pages with multiple recent writes
```

Page transitions:
- New write → A1 (front)
- A1 expiry (no write within window) → migration candidate
- Second write while in A1 → promote to Am
- Am pages are *never* migration candidates

Memory: 16 bytes × 65K = 1 MB.

The 2Q paper (Johnson & Shasha 1994) was for buffer cache eviction, but the
migration problem is the inverse: queue mechanics map directly.

### Option C: Online RL with bandits

Per-page bandit: discretize feature space into ~6^5 buckets. Each (state,
action) has Q-value. Updates via Q-learning.

```
Q(state, action) ← Q + α * (reward + γ * max Q(state', a') − Q)

reward = utility_gained_this_sweep
       − wear_cost_if_migrated
       − rollback_cost_if_wrong
```

Cold start: pages with no history use global policy as prior. Per-page state
takes over once data accumulates.

Memory: ~256 bytes per page (Q-table). Heavyweight.

### Option D: Pre-trained transformer (overkill but consider)

Treat per-page write history as a sequence. Train small transformer offline on
a corpus of workload traces. At runtime, infer next-write distribution.
Sub-µs inference per page on CPU at this size (~1M params).

Probably only worth it if simpler options fail to beat threshold.

### Recommendation

Start with Option A. Smallest delta from current approach, fully interpretable,
trivially benchmarkable. If it doesn't beat threshold by ≥10%, more complex
options likely won't either, and you've learned something important (workload
patterns are too noisy for ML to help).

### Validation experiment

- Take existing run data for a workload
- Hold out last 50% of sweeps as test
- Train Option A's decision tree on first 50%
- Measure: tree's prediction quality (precision/recall) and resulting wear vs
  current best fixed T
- Plot ROC curve of "migrate-or-not" decisions

---

## 7. Compiler / runtime hints

**Frame:** apps and compilers know more than the kernel can observe. Let them
communicate intent. Don't trust blindly — measure accuracy and gate on it.

### Layer 1: Library-level wrappers (lowest effort)

**API:**
```c
void *malloc_ro_after_init(size_t size);    // app promises: write only during init
void *malloc_set_once(size_t size);         // app promises: write once, never again
void *malloc_writable(size_t size);         // app explicitly opts out of LtRAM
```

**Implementation:**
- Wrappers call regular `malloc`, then `madvise(MADV_TIER_LATE, addr, size)`
  for the appropriate hint
- Hint stored per-VMA in the kernel
- Migration policy reads the hint at first page-fault

**For C++:** type attributes that propagate through templates:
```cpp
class GraphData [[ltram::tier("late_ro")]] {
    // allocator specialization extracts the hint
};
```

**Hooks needed in kernel:**
- New madvise codes: `MADV_TIER_LATE`, `MADV_TIER_DRAM`, `MADV_TIER_AUTO`
- Per-VMA hint flag
- Page-fault handler reads hint, applies appropriate initial classification

### Layer 2: Compile-time analysis pass

**LLVM pass identifies "set-once" allocations:**
```
For each AllocaInst / call to malloc:
    forward-trace returned pointer through the function
    collect set of stores to *ptr
    if every store is dominated by main_entry
       AND no store reaches any "ready point":
       mark allocation as RO-after-init
```

**Definition of "ready point":**
- End of `__attribute__((constructor))` functions
- A marker function the developer calls (e.g., `tier_init_complete()`)
- End of C++ object construction
- Linker-emitted "init done" symbol

**Output:** allocations get attributes that the runtime consumes:
```c
void *p = __ltram_marked_malloc(size, /* tier_hint = */ LATE_RO);
```

The hint propagates through linker into a special ELF section. Loader / libc
reads section, arms madvise hints when allocations happen.

### Layer 3: JIT runtime hints (highest leverage)

JITs have explicit information about page lifecycle:
- Generated machine code → C1 immediately after emission
- Constant pools, vtables → C1
- Inline caches (mutating) → C4
- GC tenured / old generation → C2-C3 (rarely written after promotion)

**JIT integration:**
- After emitting code page: `madvise(addr, size, MADV_TIER_LATE)` directly
- After GC promotes objects to old gen: madvise the old-gen region
- Per-allocation-site policy: GC tracks "this site produced N young, M
  long-lived objects" and tags new allocations accordingly

### Verification framework (essential)

Don't blindly trust hints:
```c
struct hint_stats {
    uint64 hinted_as_ro;          // pages tagged RO-after-init
    uint64 actually_ro;           // never written after hint
    uint64 violations;            // got written despite hint
    double accuracy;
};
```

**Per-binary trust scores:**
- Maintain profile keyed by `(binary path, content_hash)`
- Track hint accuracy across runs of the same binary
- If accuracy < 95%, downweight hints from this binary
- If accuracy consistently > 99%, trust hints aggressively (skip behavioral
  verification entirely)

This is the LLFREE pattern of "quantify the loss, accept it within bounds."
Hints are a probabilistic optimization, not a guarantee.

### Pilot experiment (low effort, high information)

Don't build the LLVM pass yet. Instead:
1. Pick 3-5 obvious C1 allocations in matmul / gapbs / redis (data structures
   that should be RO after init)
2. Hand-add `madvise(addr, size, /* hypothetical */ MADV_TIER_LATE)` calls in
   the source
3. Run workload, measure: do those pages stay RO?
4. If yes, you have an existence proof — apps would gladly opt in
5. If no, you've found the gap (some "RO" pages are actually written rarely;
   need to refine hint semantics)

A day of work; the empirical accuracy number is the value. Any compiler-pass
version has to clear that bar.

### Why this is publishable

Compiler-emitted memory-tier hints are not new. Contribution opportunity is:
- Clean accuracy/coverage measurement framework
- Empirical demonstration that real workloads have high-accuracy hints available
- Policy that *trades off* hint-based and behavior-based classification under
  uncertainty
- Verification + trust-score machinery (LLFREE-style "we accept ≤N% errors,
  here's what happens when we're wrong")
