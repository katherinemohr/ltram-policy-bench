# Ideas to implement — LtRAM policy work

Prioritized implementation backlog. Each entry: origin, why-it-matters,
architecture sketch, dependencies, status.

For broader conceptual catalogs of *design space* (not implementation
priorities), see:
- [ltram_idea_elaborations.md](ltram_idea_elaborations.md) — 7 broad
  directions with multiple options each
- [ltram_idea6_optionA_plan.md](ltram_idea6_optionA_plan.md) — step-by-step
  for the ML/decision-tree migration policy
- [llfree_meta_patterns.md](llfree_meta_patterns.md) — meta-patterns from
  paper reviews

## Status legend
- 🔵 not started
- 🟡 prototyping / partially built
- 🟢 implemented / validated
- 🔴 blocked or deprioritized

---

## 1. Deferred PTE update with COW transition window (user-original refinement) 🔵

**Origin:** User's own design refinement combining Contiguitas's
dual-mapping idea with COW semantics, addressing the LtRAM-specific
ping-pong risk.

**Why it matters:** the worst case in LtRAM migration is a page that gets
migrated to NOR, then immediately rewritten by the application — burning
NOR endurance for nothing. A COW-based transition window catches this case
*before* it commits to NOR. This is the highest-leverage idea on this list
because it improves wear with no HW changes.

**Architecture sketch:**

```
Migration decision pipeline (per page P):

  Phase 1 — "pending migration"
    policy says: P is eligible to migrate to LtRAM
    OS action:
      - mark PTE COW-bit
      - PTE still points to DRAM (NOT yet to NOR)
      - record "P pending → NOR" in migration metadata
      - start transition timer (T_commit, e.g., 30s)
    [no copy yet, no NOR write yet]

  Phase 2 — "transition window" (lasts T_commit)
    Case 2a: write to P arrives
      COW fault fires
      OS handler:
        - detect "pending migration" state
        - cancel migration
        - DRAM version is authoritative — just clear COW bit, allow write
        - add P to "burned" set: T_p ← T_p × 2 (longer next time) OR
          mark P as never-migrate (depending on burn-count policy)
        [no NOR write ever occurred]
    Case 2b: read to P arrives
      Normal read from DRAM. No state change.
    Case 2c: timer expires (no write seen)
      Proceed to Phase 3.

  Phase 3 — "commit migration"
    OS action:
      - copy DRAM → NOR (use idle scheduler or batch with other commits)
      - update PTE to point to NOR
      - clear COW bit
      - issue async TLB invalidations (see idea #2)
      - update migration metadata
    Page P now resident in LtRAM.

  Phase 4 — "post-commit window" (optional, lasts T_post)
    Track writes to P that happen shortly after commit.
    If write detected within T_post:
      - mark P as "burned" with high penalty
      - this signals our predictor mis-classified P
    After T_post elapses with no writes:
      Migration is permanent; P stays in LtRAM until next eviction signal.
```

**Key invariants:**
- During Phase 1–2: only DRAM version exists. A write is cheap to handle —
  no need to "move back from NOR" because nothing was moved to NOR.
- During Phase 3: instantaneous commit (after copy completes). The PTE
  update is the atomic boundary.
- During Phase 4: NOR is authoritative; we just monitor for
  mis-classification feedback.

**Trade-off parameters:**
- `T_commit` (transition window): shorter = more false commits; longer =
  LtRAM utility delayed
- `T_post` (post-commit observation): shorter = miss late-rewrite signal;
  longer = wasted policy bookkeeping
- Burn penalty curve: how aggressively to push `T_p` after a cancellation

**Implementation in simulator (no HW required):**
- Add a `pending_migration` state to per-page metadata
- Add a transition-window timer per pending page
- Modify the migration-trigger logic: instead of "migrate now," enter Phase 1
- Modify the write-event handler: check pending state, cancel if set
- Compare wear and utility against eager-migration baseline

**Connection to other ideas:**
- Pairs with #2 (async TLB invalidation) for cheap Phase-3 commits
- Pairs with #4 (spatial confinement) — pages in the "policy-evaluated"
  region go through this pipeline; pages in pre-classified regions don't
- Generalizes the "lazy migration" idea (#5 in elaborations) with explicit
  state and feedback

**Estimated effort:** 1–2 weeks in the simulator. Highest-priority idea
because it's standalone, simulator-only, and tests the core hypothesis
(deferred commit reduces wasted migrations).

---

## 2. Async TLB invalidation for migration commits 🔵

**Origin:** Contiguitas (ISCA '23) async TLB invalidation pattern;
LATR (ASPLOS '18) lazy translation coherence.
Meta-pattern: "decouple coordination from correctness" (see meta-pattern #14).

**Why it matters:** synchronous TLB shootdowns cost ~thousand cycles per
core involved. For LtRAM migrations at hundreds per second across a
many-core system, this adds up to real overhead. Worse, the page is
*unavailable* during the shootdown — and NOR reads are slow enough that
you really don't want pages out of service.

**The principle:** correctness should not require all cores to agree
*simultaneously*. If the system is correct whether or not invalidation
has propagated, then invalidation can be lazy. Coordination becomes a
performance optimization, not a correctness requirement.

**Architecture sketch:**

```
On migration commit (Phase 3 in idea #1):

  1. Initiator core (the one running the OS migration thread):
     - Updates PTE to point to NOR
     - Invalidates its own local TLB
     - Marks migration metadata "post-commit, dual-mapping window open"
     - Continues — does NOT send IPIs, does NOT wait for acks

  2. Other cores:
     - Each core's local TLB still has the old PTE (pointing to DRAM)
     - When core C next runs the kernel (syscall, interrupt, context
       switch, timer), it checks a pending-invalidation queue and
       invalidates affected TLB entries locally
     - No coordination, no acknowledgements

  3. During the "dual-mapping window":
     - Reads using the OLD mapping (DRAM) are still correct because the
       DRAM page hasn't been deallocated yet (its content matches NOR)
     - Reads using the NEW mapping (NOR) are correct because NOR has
       the committed data
     - Writes are blocked during this window (see below) — they have
       to wait until the local TLB is updated

  4. Closing the dual-mapping window:
     - Initiator core polls a counter (atomic): how many TLBs still
       have the old mapping?
     - When counter reaches 0, the dual-mapping window closes
     - DRAM page can be freed/reused
```

**Handling writes during the dual-mapping window:**
- Simplest: don't allow writes during the window. Write fault → trap →
  OS forces local TLB invalidation → re-fault uses NOR mapping → write
  to NOR.
- Better (Contiguitas-style): allow writes, redirect via LLC migration
  metadata. Requires HW support (idea #3).

**Counting outstanding TLBs:**
- Either a per-page atomic counter incremented at commit and decremented
  on each local invalidation
- Or a global "epoch" counter — a TLB is "current" if its epoch matches;
  pages waiting on invalidation track the epoch they were committed at

**Without HW support (software-only fallback):**
- Use the existing IPI mechanism but mark each migration with a "lazy
  ok" flag
- The OS can batch invalidations across many migrations into a single IPI
  burst, amortizing the cost
- This is strictly worse than HW-supported lazy invalidation but better
  than per-migration synchronous shootdown

**Implementation in simulator:**
- Add per-TLB pending-invalidation queue
- Add migration metadata with "outstanding TLBs" counter
- Modify the simulated context-switch event to drain the queue
- Measure: distribution of "dual-mapping window duration" — how long
  until all TLBs converge?

**Dependencies:** idea #1 (deferred PTE update) — without it, you don't
have a window where dual-mapping is acceptable.

**Estimated effort:** 1 week in simulator. Real-HW prototype is a longer
story.

---

## 3. Dual-mapping migration with LLC redirection (Contiguitas-style) 🔵

**Origin:** Contiguitas (ISCA '23) hardware-assisted in-use page migration.

**Why it matters:** today's migration model requires either (a) blocking
access to the page during copy, or (b) being so fast no one notices.
Neither works well for NOR-backed LtRAM — reads are slow, programs are
slower, and pages should not go out of service. Dual-mapping lets the
page stay accessible throughout migration.

**For LtRAM, the value is even higher than in Contiguitas's original
context:**
- NOR read latency is much higher than DRAM
- Even brief unavailability is visible to performance
- The NOR side of the migration is itself slow (program latency)
- The dual-mapping window can therefore be longer (you have more time
  to converge invalidations) without hurting latency

**Architecture sketch:**

```
Hardware additions (analogous to Contiguitas-HW):

  Migration metadata table in LLC (16 entries per slice, ~0.0038 mm² each):
    Each entry: { Src_PPN, Dst_PPN, Ptr, Flags }
    Ptr tracks copy progress in cache-line units

  On LLC access for a page under migration:
    If access_PPN matches a Src in metadata:
      - Compute line_offset within the page
      - If line_offset < Ptr: redirect read/write to Dst_PPN
      - Else: serve from Src_PPN
    Else if access_PPN matches a Dst in metadata:
      - Serve from Dst_PPN normally
    Else:
      - Normal LLC operation

  Background copy engine in LLC:
    Iterates through cache lines of Src_PPN
    For each line:
      - Issue BusRdX to invalidate the line in private caches
      - Copy Src line to Dst line
      - Increment Ptr
    On Ptr == 64 (page complete):
      - Notify OS via interrupt or polled flag
      - OS does Phase 3 commit (idea #1)

  Software/HW interface:
    Migrate(Src_PPN, Dst_PPN, flag) — initiates the migration
    Clear(Src_PPN) — tells HW the migration metadata can be reclaimed
```

**For LtRAM specifically:**
- The Dst_PPN is in NOR physical address space
- Writes during migration: redirected to DRAM (or queued, see below)
- Reads during migration: redirected appropriately based on Ptr
- Combined with idea #1 (deferred PTE), this can be used to migrate
  speculatively: HW starts copying, but if a write arrives, the
  migration is cancelled mid-flight

**Write handling during HW migration:**
- Simplest: any write during HW migration cancels the migration. Set a
  cancel flag in metadata, drop the dst copy, OS reverts state.
- More aggressive: writes during migration go to DRAM (src side), then
  on commit, the modified lines are re-copied. This lets read-mostly
  workloads benefit even if there's occasional writes.

**Dependencies:**
- Idea #1 (deferred PTE) for the policy-side state machine
- Idea #2 (async invalidation) to handle the TLB transitions cheaply

**Estimated effort:** 3–4 weeks in simulator integration. Real hardware
prototype is a 6–12 month project (FPGA-level or full chip).

---

## 4. Spatial confinement: pre-partition virtual address space 🔵

**Origin:** Contiguitas (ISCA '23) movable/unmovable region separation,
applied to LtRAM tiering instead of fragmentation.

**Why it matters:** today the migration policy considers every page
individually. If 30–50% of pages are pre-classifiable (C1 via PROT_READ,
file-backed mappings; C4 via anonymous + heap), they shouldn't go through
the policy at all. The migration decision space collapses by a large
fraction, and the policy can focus its complexity budget on the
genuinely ambiguous middle.

**Architecture sketch:**

```
Virtual address space layout:

  ┌─────────────────────────────────────┐
  │ LtRAM-eligible region               │
  │   - PROT_READ-only VMAs             │
  │   - File-backed RO mappings         │
  │   - hint-tagged (MADV_TIER_LATE)    │
  │   - JIT code pages (post-emit)      │
  │ Pages here migrate to LtRAM at      │
  │ first fault, no T threshold         │
  └─────────────────────────────────────┘
  ┌─────────────────────────────────────┐
  │ Policy-evaluated region             │
  │   - Anonymous writable, unknown     │
  │   - VMAs with mixed access patterns │
  │ Pages here go through behavioral    │
  │ classification (the existing T      │
  │ threshold logic, possibly with      │
  │ idea #1's deferred-commit pipeline) │
  └─────────────────────────────────────┘
  ┌─────────────────────────────────────┐
  │ DRAM-only region                    │
  │   - Anonymous heap (high churn)     │
  │   - tmpfs writable                  │
  │   - Pages demoted from LtRAM        │
  │ Pages here never migrate to LtRAM   │
  └─────────────────────────────────────┘

Dynamic boundary:
  Boundaries between regions adjust based on demand and accuracy.
  - When LtRAM hit rate drops → consider promoting more pages from
    policy-evaluated to LtRAM-eligible
  - When mis-classifications mount → shrink LtRAM-eligible, expand
    policy-evaluated
```

**Per-page state:**
- 2-bit region tag in page metadata
- Demotion flag: "was LtRAM-eligible, got written, never re-promote"
  (the "burned" bit)
- Hint source: which classifier put this page in its current region
  (for accuracy tracking — see idea #5)

**Classification triggers:**
- At `mmap()` / `mprotect()`: static VMA-based classification
- At first page-fault: allocation-context heuristics
- At every sweep boundary (policy-evaluated region only): behavioral
  re-classification

**Connection to other ideas:**
- The transition window from idea #1 is the safety net: a mis-classified
  LtRAM-eligible page gets caught by the COW fault and demoted
- Idea #5 (hint accuracy) measures how often the static classification
  is right — drives the dynamic boundary policy

**Dependencies:** none — orthogonal to the migration mechanism work.

**Estimated effort:** 1 week in simulator. Real-system implementation
requires kernel-level VMA classification hooks (a few hundred LOC).

---

## 5. Hint-aware policy with measured accuracy 🟡 (user-original)

**Origin:** User's proposal — show how compiler / app hints reduce the
threshold and improve placement accuracy, while the policy stays
effective even without hints.

**Why it matters:** the strongest paper story is "we work great alone;
we work even better with help." This proves the design doesn't depend
on app cooperation, which is a recurring weakness in HW-hint papers.

**Architecture sketch:**

Three policy modes evaluated head-to-head on the same workload:

```
Mode A — no hints (baseline):
  - Pure behavioral classification (existing C1-C4 from observed events)
  - All pages go through threshold-T-based policy
  - This is the floor: what the system can do alone

Mode B — application hints:
  - New madvise codes:
      MADV_TIER_LATE     "this region is RO after init"
      MADV_TIER_SET_ONCE "this region is written once, never again"
      MADV_TIER_KEEP     "this region must stay in DRAM"
      MADV_TIER_AUTO     "use the default policy"
  - App-tagged pages bypass behavioral verification entirely
  - Verification framework tracks tagged-vs-actual accuracy

Mode C — compiler hints:
  - LLVM pass identifies set-once-after-init allocations
    (look for stores dominated by main_entry, no stores to ready point)
  - Emits hints in ELF metadata section
  - libc / loader reads section and calls madvise automatically
  - App doesn't change; binary recompile only
```

**Measurements per mode:**

| Metric | Mode A | Mode B | Mode C |
|--------|--------|--------|--------|
| Classification accuracy (%-correct per class) | baseline | should improve | should improve |
| Required threshold T | T_A | should drop | should drop |
| Time-to-LtRAM-utility (TTU) | TTU_A | should drop | should drop |
| Wear consumed at 5y horizon | W_A | should drop | should drop |
| LtRAM hit rate | H_A | should rise | should rise |

**Accuracy framework:**

```c
struct hint_stats {
    uint64 hinted_C1;             // pages hinted as RO-after-init
    uint64 actually_C1;           // never written after hint
    uint64 violations;            // got written despite hint
    uint64 false_negatives;       // could have been hinted but weren't
};

double accuracy_C1 = actually_C1 / hinted_C1;
double coverage_C1 = hinted_C1 / total_C1_pages;
```

Per-binary trust score: maintain `(binary_path, content_hash) → accuracy`
across runs. Bad-accuracy binaries get hints discounted; good-accuracy
binaries get hints trusted without behavioral verification.

**Pilot experiment (low effort, high information):**
- Pick 3–5 obvious set-once allocations in matmul / gapbs / redis
- Hand-add `madvise(addr, size, MADV_TIER_LATE)` calls
- Measure accuracy on existing workload runs
- If accuracy > 95%, you have an existence proof for Mode B without
  building any compiler infrastructure
- If accuracy is poor, you've identified the gap

**Paper story:**
"Mode A is competitive with state-of-the-art (no help needed).
Mode B / C shows the design *scales gracefully* with available information —
a property few prior systems have. With hints, we can lower threshold T by
X%, raise placement accuracy from Y% to Z%, and reduce wear by W%."

**Dependencies:** any subset of ideas #1–#4 to demonstrate. Hint
mechanisms are independent of the migration substrate.

**Estimated effort:** 2 weeks (1 week pilot, 1 week full hint
infrastructure if pilot validates).

---

## 6. Quantify the LtRAM controller cost 🔵 (user-original)

**Origin:** User's proposal — Contiguitas-style hardware cost
quantification, comparing against existing memory controllers.

**Why it matters:** any HW extension claim needs concrete numbers.
"Modest" doesn't convince anyone; mm² / nJ / mW does. If the proposed
LtRAM controller is meaningfully cheaper than existing DIMM or Optane
controllers, that's a publishable headline number on its own.

**What to quantify:**

| Component | Metric | Tool | Comparable |
|-----------|--------|------|------------|
| LtRAM controller area | mm² @ 22nm and 7nm | McPAT, vendor specs | DIMM controller, Optane controller |
| LtRAM controller dynamic energy/access | nJ | CACTI 7 | Optane controller (~5 nJ/access reported) |
| LtRAM controller leakage | mW | CACTI 7, McPAT | Standard memory controller |
| Migration metadata in LLC | bytes per LLC slice | CACTI 7 | Contiguitas: 0.014% per slice |
| Wear-counter table | bytes total | CACTI 7 | FTL metadata in commercial SSDs |
| Per-page state in struct page | bytes per page | static analysis | Existing struct page (64 bytes) |
| Total per-server overhead | bytes / dollar | summation | TPP, Memtis estimates |

**Tools to use:**
- **CACTI 7** for SRAM-based structures (migration metadata, wear counters)
- **McPAT** for full controller-level estimates
- Published datasheets for DIMM controllers (Intel, Samsung)
- Optane DCPMM specifications and reverse-engineered analyses (Yang
  et al. FAST '20)
- Open-source SSD FTL implementations (e.g., DFTL) for FTL-style metadata

**Comparison framework:**

```
For each component, report:
  - Absolute cost (mm², nJ, mW)
  - Cost as percentage of a comparable existing controller
  - Cost as percentage of a CPU core
  - Cost per GB of LtRAM managed

Headline numbers:
  - "LtRAM controller area is X% of an Optane controller"
  - "LtRAM management overhead is Y% of total chip area"
  - "Total memory-controller power dropped by Z W versus Optane"
```

**Why this builds on Contiguitas's playbook:**
Contiguitas's "0.014% of core area" is what makes its HW story
believable. Without that number, reviewers ask "how much does this
cost?" — and a vague answer ends the paper. With it, the conversation
moves to whether 0.014% is worth the gain.

For LtRAM, the equivalent number should be:
- "LtRAM controller is X% smaller than Optane controller because [we
  don't need lossy compression / we don't need PIN-based access control
  / we use simpler wear leveling]"

**Dependencies:** the HW design must be sufficiently locked-in to
analyze. Depends on which of ideas #1–#3 are part of the design (each
adds HW that needs to be costed).

**Estimated effort:** 1–2 weeks after the HW design is finalized.
Mostly running CACTI / McPAT and writing up the comparison.

---

## 7. Combined performance + wear-leveling SLO 🔵 (user-refined)

**Origin:** Lagar-Cavilla et al., ASPLOS '19 (promotion-rate SLO) +
user's refinement: "this could be better if it also included
wear-leveling policies."

**Why it matters:** a single promotion-rate SLO captures performance
impact but is blind to wear distribution. A policy could satisfy
promotion-rate at 0.2%/min while burning out one corner of the NOR chip.
LtRAM needs a **vector SLO** — multiple bounds enforced jointly.

**Architecture sketch:**

```
The LtRAM SLO is a 3-tuple bound, all checked per evaluation window:

  SLO_performance: promotion_rate ≤ P_perf
    where promotion_rate = (writes to migrated pages) / (working set per min)
    Bounds the cost paid for mis-classifications.
    Initial target: P_perf = 0.2%/min (Lagar-Cavilla's value).

  SLO_wear_rate: chip_write_rate ≤ P_wear
    where chip_write_rate = (total cell-writes per second) / (cells)
    Bounds how fast we consume the endurance budget.
    Initial target: P_wear = TOTAL_ERASES / (5y * SECS_PER_YEAR)
    i.e., the rate that exhausts the budget exactly at the deployment
    horizon. Any rate ≤ this is acceptable.

  SLO_wear_distribution: max(wear) / mean(wear) ≤ P_dist
    where wear is per-cell erase count
    Bounds how unevenly we consume the budget.
    Initial target: P_dist = 1.2 (max 20% above mean)
    A policy that burns out one corner fails this even if total writes
    are low.

Violation if ANY component exceeds its bound. The policy must
respect all three simultaneously.
```

**Per-policy decision logic:**

```python
def should_migrate(page_id, current_state):
    # ... existing decision based on classification and threshold ...
    if migrate_decision:
        # Check that this migration doesn't violate SLO_wear_distribution
        target_cells = pick_wear_leveled_target()  # see "wear-leveled target"
        if would_exceed_wear_dist(target_cells):
            # Defer migration; this target would overload one region
            return DEFER
    return migrate_decision
```

**Wear-leveled target selection:**

```python
def pick_wear_leveled_target():
    # Among free NOR pages, prefer those whose host cells
    # are at or below the wear distribution mean
    candidates = free_pages_with_wear_below(mean_wear * 1.0)
    if not candidates:
        # Fall back to least-worn available
        return min(free_pages, key=cell_wear)
    return random_pick(candidates)
```

**Implementation in simulator:**
- Add per-cell wear counter (4 bytes × cells; modest at LtRAM scale)
- Update wear counter on every NOR program (migration commit, in idea #1
  terms: Phase 3)
- Compute the three SLO components every evaluation window (1 minute)
- The policy controller (the K-th percentile threshold of idea #8 below)
  takes the most-violated SLO as its limiting constraint

**Why this is a contribution beyond Lagar-Cavilla:**
Their promotion-rate SLO works for a non-endurance-limited tier
(compression in DRAM doesn't wear). LtRAM is endurance-limited, so the
SLO must include wear. A single-axis SLO won't catch policies that
trade wear distribution for promotion-rate efficiency — and those
policies will quietly burn out the device.

**Dependencies:** orthogonal to migration mechanism (#1-#3). Pairs
naturally with the K-th percentile controller (idea #8 below — same
principle, applied to wear).

**Estimated effort:** 1–2 weeks. Mostly metric plumbing + a wear-aware
target picker.

---

## 8. Cold-start migration window 🔵

**Origin:** Lagar-Cavilla et al. — "disable zswap for first S seconds of
job execution to avoid making decisions based on insufficient information."

**Why it matters:** the first seconds of a process are dominated by
load-phase writes (init, library loads, jemalloc setup). Cold-start
classification is unreliable because there's no history. Migrating
during this window:
- wastes NOR endurance on pages that may be reclassified seconds later
- delays time-to-LtRAM-utility (true RO-after-init pages get held back)
- mis-classifies init-phase writes as recurring (C3 → C4 false negative)

**Architecture sketch:**

```c
struct cgroup_state {
    uint64 start_timestamp;
    uint32 cold_start_window_s;   // autotuned, default 30s
    ...
};

bool migration_eligible(page_id_t p) {
    cgroup_t *cg = page_owner(p);
    if (current_time() - cg->start_timestamp < cg->cold_start_window_s) {
        return false;  // still in cold-start window
    }
    return true;
}
```

**The cold-start window is per-cgroup, not per-page.** Once the cgroup
has been alive for S seconds, *all* its pages become eligible for
migration. This matches the empirical observation that load-end is a
process-wide phase boundary, not a per-page event.

**Autotuning S:**
- Too short → policy decisions made on insufficient data → high
  misclassification rate
- Too long → LtRAM utility delayed; RO-after-init pages sit in DRAM
  longer than necessary
- Initial value: S = 30s (matches Lagar-Cavilla's defaults)
- Autotune target: minimize misclassification rate during the first
  10 minutes of execution, subject to "LtRAM coverage ≥ X by minute Y"

**Interaction with idea #4 (spatial confinement):**
- C1 pages (PROT_READ from start): cold-start window can be shortened
  or skipped — these don't need behavioral data to classify
- C2-C4 pages: full cold-start window applies

**Implementation in simulator:**
- Track per-job start_timestamp (already trivially available)
- Add cold_start_window_s field per cgroup
- Gate migration on (now - start) > window
- Sweep window values offline to find the optimum per workload

**Dependencies:** none — orthogonal to all other ideas.

**Estimated effort:** half a day to implement, 1-2 weeks for sweeping
and validation across workloads.

---

## 9. A/B framework for policy evaluation 🔵

**Origin:** Lagar-Cavilla et al. — production-grade A/B testing as the
primary evaluation tool for any tiering policy change.

**Why it matters:** point comparisons ("policy X gets 20% LtRAM coverage")
are not defensible without paired statistics. A/B testing across the
full configuration grid gives confidence intervals and statistical
power — and forces honest reporting of variance, not just means.

**Architecture sketch:**

```python
class ABEvaluator:
    def __init__(self, workload_configurations):
        # 30 workloads × 5 lengths × 2 distributions (zipf/uniform)
        self.configs = workload_configurations

    def run(self, policy_A, policy_B, seed=0):
        # Randomly assign each config to either A or B
        rng = random.Random(seed)
        assignments = {c: rng.choice(["A", "B"]) for c in self.configs}

        results_A = []
        results_B = []
        for config, group in assignments.items():
            policy = policy_A if group == "A" else policy_B
            metrics = run_simulator(config, policy)
            (results_A if group == "A" else results_B).append(metrics)

        # Paired statistics: mean difference, CI, p-value
        return self.compare(results_A, results_B)

    def compare(self, results_A, results_B):
        for metric in ["wear_5y", "utility", "promotion_rate",
                       "ltram_coverage", "ttu"]:
            a_vals = [r[metric] for r in results_A]
            b_vals = [r[metric] for r in results_B]
            diff = mean(a_vals) - mean(b_vals)
            ci = bootstrap_ci(a_vals, b_vals, n=10000)
            print(f"{metric}: A − B = {diff:.3f} (95% CI: {ci})")
```

**Statistical methodology:**
- Use bootstrap confidence intervals (no distributional assumptions)
- Report CI in addition to means — a 2% improvement with CI [−5%, +9%]
  is not a real improvement
- Run with multiple seeds (10+) to characterize policy variance
- Pre-register hypotheses: "we expect SLO violation rate to drop by ≥X%"
  → tests have to clear that bar to count as a win

**Required baselines (always include):**
- **Pure-DRAM** (upper bound on performance, infinite wear) — what
  we'd do if endurance were free
- **NoLtRAM / no-migration** (lower bound on wear, baseline performance)
  — first-touch allocation only, no proactive migration. Soar/Alto
  showed that this baseline beats Nomad, Colloid, and NBT on several
  workloads — Memtis omitted this baseline and lost an honest signal
- **Threshold-T baseline** (your previous policy proposal) — what
  most prior tiering-for-endurance work would do
- **Memtis-equivalent** — the current state of the art for tiered
  memory placement
- Your policy under test

If your policy can't beat **NoLtRAM** for at least some workloads,
your policy doesn't pay for itself. This is the null hypothesis.

**Reporting template:**

| Metric | Policy A (baseline) | Policy B (new) | Δ | 95% CI | Win? |
|--------|--------------------|--------------------|------|---------|------|
| 5y wear | 87% | 64% | −23% | [−28%, −18%] | ✓ |
| LtRAM utility | 1.2e8 page-sec | 1.3e8 page-sec | +8% | [−2%, +18%] | ?  |
| SLO violations | 0.4%/min | 0.15%/min | −62% | [−71%, −51%] | ✓ |

The "?" row is honest: there's a positive point estimate but the CI
crosses zero, so we can't claim a win. This is the kind of honesty
that earns trust.

**Connection to other ideas:**
- Every policy change in this backlog should be evaluated through this
  framework, not by point comparison
- The fast simulator from the broader "LtRAM model" framing is the
  workhorse — A/B runs the simulator many times with different policies

**Dependencies:** the simulator itself (existing). The A/B framework is
a thin wrapper around it.

**Estimated effort:** 1 week. Mostly Python plumbing + statistics.

---

## 10. HW-SW contract redesign as the central paper framing 📐 (user-original framing)

**Origin:** User's refinement on the Lagar-Cavilla "reframe what is a
tier" pattern. User explicitly distinguished their work as **HW-SW
contract redesign**, NOT "software-defined like zswap."

**Why it matters:** the framing of the paper determines how reviewers
position the contribution. Wrong framing = wrong reviewers, wrong
comparisons, wrong takeaways. The LtRAM work needs to be framed
correctly to avoid two failure modes:

1. **"Software-defined" framing fails:** Lagar-Cavilla / TMO use
   commodity hardware. Their contribution is policy. If LtRAM is framed
   as software-defined, reviewers will ask "why do you need any HW
   changes?" and the work loses motivation.

2. **"Pure HW" framing fails:** if framed as a hardware paper, reviewers
   will ask for full silicon evaluation and ignore the OS/policy
   contribution. The substrate becomes the story; the policy gets lost.

**The correct framing: HW-SW co-designed memory tier.**

```
Hardware provides:
  - Dual-mapping LLC redirect (Contiguitas-style; idea #3)
  - Async TLB invalidation primitive (idea #2)
  - Wear-aware NOR controller with wear-counter exposure
  - Optional: compression engine (à la Lagar-Cavilla)

Software provides:
  - Classification (C1-C4 + hints)
  - Migration policy with COW transition window (idea #1)
  - Wear-leveling target selection (idea #7)
  - Adaptive controller (K-th percentile threshold; idea #8 of
    elaborations file, plus cold-start window from idea #8 here)

The contract:
  - SLO bounds enforced by software, with HW telemetry as input
  - Endurance budget exposed by HW, consumed by SW policy
  - Migration primitive provided by HW, scheduled by SW
```

**Paper positioning:**

Title candidate: "*HW-SW Co-design for Endurance-Aware Memory Tiering*"

Abstract should explicitly contrast with:
- Lagar-Cavilla / TMO: "These works show software-only tiering for
  non-endurance-limited substrates. We extend the framing to substrates
  where wear is a first-class constraint, which requires HW support."
- Contiguitas: "We adopt their migration substrate and co-design the
  endurance-aware policy that uses it."
- Pure NVM hardware papers: "We show that the policy is as important as
  the substrate; without our policy, the HW gains are not realized."

**Application across all other ideas:**
- Every implementation idea should be classified as HW-side or SW-side
  or interface
- The paper section structure should follow this split: §3 HW, §4 SW,
  §5 the contract between them
- Cost numbers should include both: SW overhead (idea #6) AND HW area
  (also idea #6) — the contract is justified by joint efficiency

**This isn't a code change.** It's a framing principle that shapes
every other decision. Treat it as load-bearing infrastructure for the
dissertation narrative.

**Dependencies:** none in the code sense; everything in the dissertation
sense.

**Effort:** ongoing — applied across every paper draft, talk, and
design decision.

---

## 11. Token-budget allocation: replace time-threshold policy with resource allocation 🔵 (PI-suggested)

**Origin:** PI feedback that threshold-based approaches don't work well in
prior literature; suggestion to look at "token-allocator-style" framings.
Plus user follow-up linking this to ML-probability + budget-derived
thresholds.

**Why it matters:** a single time threshold T is a 1D cut through a
high-dimensional problem. The page population is heterogeneous; one cut
can't be right for all of it. Worse, T has to be re-tuned per workload —
which the existing characterization plots already show. Many prior tiering
papers have tried threshold tuning; none have made it scale.

**The fundamental reframing:** migration is a **resource-allocation
problem, not a classification problem.** The resource is the endurance
budget (R writes/sec sustainable over a 5-year deployment). The task is
to spend that budget on the pages with the highest expected utility.

### Architecture sketch

Token bucket from networking QoS, applied to NOR endurance:

```
Token rate: R = (N_cells × erases_per_cell) / deployment_lifetime
            For 65K-page chip, 100K erases/cell, 5y:
            R = 6.4 × 10⁹ / (5 × 365 × 86400) ≈ 40 tokens/sec

Bucket capacity: B = K × R, e.g., 60 × R (allows 60s of bursting)

Per migration decision: consume 1 token
If bucket empty: defer migration until tokens accumulate
If bucket full: tokens stop accumulating (overflow)
```

Per evaluation epoch:
1. Refill bucket: `tokens = min(B, tokens + R · Δt)`
2. Build candidate set: pages eligible for migration (clean for ≥ T_min,
   not in cold-start window, etc.)
3. Rank candidates by score (see options below)
4. Migrate top-`tokens` pages; decrement bucket by that count

### Ranking function options (the substantive policy choice)

The ranking is where the policy intelligence lives. From cheapest to most
ambitious:

**(a) Write reuse distance (cheapest baseline)**
- Score = observed write reuse distance so far
- Pages with the largest reuse distance go first ("most cold")
- No training, no model — direct from the existing trace

**(b) ML probability of rewrite — see [ltram_idea6_optionA_plan.md](ltram_idea6_optionA_plan.md)**
- Score = 1 − P(rewrite within Δt | features)
- Decision tree → LUT for sub-100 ns inference at runtime
- Trained offline on existing workload traces

**(c) Cost-benefit ranking**
- Score = benefit(p) / cost(p)
- benefit(p) = expected page-seconds in LtRAM = P(stays_RO) × remaining_runtime
- cost(p) = 1 token (uniform) OR 1 + P(rewrite) × demotion_overhead (calibrated)

**(d) Queue-based (LRU / 2Q / LIRS / ARC)** — see also
[ltram_idea_elaborations.md#6](ltram_idea_elaborations.md) Option B
- Maintain two queues (recent / frequent); score = queue position
- ARC adapts queue lengths automatically based on observed access mix
- Cheap to implement; standard cache-replacement baseline

**(e) Per-page bandit / Q-learning (RL)** — see also
[ltram_idea_elaborations.md#6](ltram_idea_elaborations.md) Option C
- Score = max-Q-value over actions {migrate, wait}
- Heavyweight; only justifiable if simpler rankings underperform

### Oracle baseline: Belady's MIN

Not a deployable ranking — requires future knowledge — but essential as
an offline oracle. For any workload trace, replay with perfect future
knowledge: at each decision point, pick pages whose actual write reuse
distance is largest.

This gives the theoretical optimum under the budget constraint. Every
real policy reports "we achieve X% of Belady's optimal utility at the
target token rate." That's the headline number for any policy paper.

### Connection to existing ideas

- **Idea #1 (COW transition window):** the COW window is the
  token-refund mechanism. If a migrated page is rewritten within the
  window, the token is refunded and the NOR write never commits.
- **Idea #5 (hint-aware policy):** hints become priority boosts in the
  ranking — hinted pages get a score bonus on top of behavioral signal.
- **Idea #7 (perf + wear SLO):** the wear SLO IS the token bucket rate.
  Performance SLO acts as a sanity check on ranking quality (too many
  demotions → ranking is mis-calibrated).
- **Idea #8 (cold-start window):** during the window, tokens accumulate
  but aren't spent. The bucket "primes" for an informed burst of
  migrations when the window ends.

### Why this is the right dissertation framing

1. **Directly respects the endurance constraint.** Token rate IS the wear
   budget. No separate threshold to tune.
2. **Workload-invariant.** R is computed from hardware specs, not
   measured per workload.
3. **Huge prior-art base.** Token buckets, leaky buckets, CFS, BFQ, WFQ,
   Linux PSI-driven reclaim — your design can cite a deep tradition.
4. **Generalizes time-threshold.** T is recoverable as a special case
   (rank by `time_since_last_write`, set bucket size to infinity).
5. **Separates "what to migrate" from "how many."** Ranking is
   workload-dependent; budget is hardware-dependent. Clean separation
   that prior threshold-based work doesn't have.

### Implementation in simulator

- Per-context state: `tokens`, `tokens_per_sec`, `bucket_capacity`
- Per evaluation epoch: refill bucket → build candidate set → rank →
  migrate top-tokens-many → decrement bucket
- Ranking function as a pluggable interface so options (a)–(e) can be
  benchmarked head-to-head
- Belady oracle as a separate offline replay tool

### Position of time-threshold T after this change

**Time-threshold T is demoted to a baseline for comparison**, not the
primary policy. Existing plots can still sweep T to show "we beat the
threshold baseline by X% wear-adjusted utility." But the paper narrative
centers on the token-budget framework, with rankings as the substantive
contribution.

### Dependencies

- Idea #7 (perf + wear SLO) — wear SLO defines the token rate
- Idea #1 (COW transition window) — provides the token-refund mechanism
- The fast simulator from idea #6 Option A plan — for offline replay
  and ranking comparison

### Estimated effort

- Basic token bucket + reuse-distance ranking: 1 week in simulator
- Belady oracle baseline: 1 week (mostly replay infrastructure)
- ML-probability ranking (option b): references the Option A plan (~2 weeks total)
- Queue-based ranking (option d): 3–4 days
- Full head-to-head comparison of (a)–(e): 4 weeks
- **Phase-1 MVP** (token bucket + reuse-distance ranking + Belady oracle):
  2–3 weeks total

---

## 12. PEBS-based access tracking instead of soft-dirty scans 🔵 (HeMem)

**Origin:** HeMem (SOSP '21). Replaces page-table scanning (which scales
with memory size and incurs TLB shootdowns) with Processor Event-Based
Sampling (PEBS) — CPU hardware events that log memory access addresses
to a buffer as the application runs.

**Why it matters:** the user's current soft-dirty-bit scan walks all
PFNs every sweep — O(N_pages) per evaluation epoch. For a 65K-page
LtRAM this is tolerable; for production-scale systems (millions of
pages), it isn't. PEBS shifts cost to O(access rate): more accesses =
more samples, but inactive memory is invisible. The substrate scales
naturally.

For LtRAM specifically, PEBS lets you observe **write events as they
happen**, weighted naturally by frequency. This is a much richer signal
than periodic snapshots of soft-dirty bits, especially for
endurance-management decisions where what you care about is exactly
the write rate per page.

### Architecture sketch

```
PEBS configuration:
  Counter: MEM_INST_RETIRED.ALL_STORES (or similar AMD equivalent)
  Sample period: ~5000 stores (HeMem's empirical sweet spot)
  Buffer: kernel-allocated, shared with userspace via perf_event_open

PEBS thread (runs continuously):
  while alive:
    read PEBS buffer
    for each sample:
      page = page_from_va(sample.virtual_addr)
      update write_count[page]
      if write_count[page] > write_heavy_threshold:
        mark page as write-heavy
        move to "do not migrate to LtRAM" list

Policy thread (10ms tick):
  walk DRAM-cold list, LtRAM-hot list
  rank by write reuse distance (idea #11 ranking option a)
  migrate top-K under token budget (idea #11)
```

### What this replaces

- Replaces or supplements the soft-dirty bit + pagemap sweep
- The C1-C4 classification logic still applies, but is fed by PEBS
  events instead of periodic snapshots
- Write reuse distance is computed online from inter-event timing,
  not offline from `write_events` arrays

### What this enables

- Fine-grained per-event write rate measurement (per-page)
- Read vs. write distinction at the access-event level (use multiple
  PEBS counters)
- Sub-millisecond reaction to phase changes — your policy sees writes
  as they happen
- Hot/cold/burned classification updated in real time

### Practical caveats — hardware availability

- **PEBS is Intel-specific.** The ARM equivalent is SPE (Statistical
  Profiling Extension), introduced in ARMv8.2 (2017).
- **Enzian uses Cavium ThunderX2 (ARMv8.1) — no SPE.** Verify with
  `perf list | grep -i spe` (should return nothing) or `cat
  /proc/cpuinfo | grep architecture`. Plan accordingly.
- **QEMU/KVM emulation does not surface PEBS/SPE in any useful form.**
  Stay with soft-dirty bits in emulation.
- **Three paths forward:**
  1. **Soft-dirty / accessed-bit scanning** — works everywhere, lower
     fidelity. Acceptable for class project, DIMES, probably dissertation.
  2. **FPGA-based access tracking on Enzian** — implement an
     SPE-equivalent in the FPGA that streams (vaddr, R/W, ts) records
     to kernel-readable memory. This is a publishable HW-SW contract
     contribution on its own, and fits Enzian's design philosophy.
  3. **Switch to a SPE/PEBS-capable system** — Neoverse N1, Ampere
     Altra, or recent Intel. Only if you need head-to-head PEBS
     comparison numbers.
- **Linux integration when SPE/PEBS is present:** `perf_event_open(2)`
  is the entry point. HeMem's source code (publicly available) has a
  working Intel PEBS example; ARM SPE has similar perf-based APIs.

### Connection to other ideas

- **Idea #11 (token budget):** PEBS provides the access stream that
  the ranking function consumes. Reuse-distance ranking is computed
  from PEBS events; ML-probability ranking uses PEBS features.
- **Idea #7 (perf + wear SLO):** PEBS gives you the per-write
  telemetry needed to measure wear distribution in real time.
- **Idea #2 (async TLB invalidation):** no longer about avoiding
  scan-time invalidation — that problem disappears with PEBS.

### Dependencies

- Real hardware (or a future QEMU/KVM extension that emulates PEBS)
- `perf_event_open` access from the kernel side
- Per-PFN counters accessible via debugfs (already in proposal §4)

### Estimated effort

- Prototype on bare-metal Linux: 1–2 weeks
- Integration into your simulator: not applicable (sim uses soft-dirty)
- Comparison study (PEBS vs. soft-dirty fidelity, overhead): 2 weeks

---

## 13. Read/write counter separation in classification 🔵 (HeMem)

**Origin:** HeMem (SOSP '21). Maintains separate read and write access
counts per page; uses different thresholds (read = 8 samples, write = 4
samples) reflecting the cost asymmetry. Write-heavy pages get priority
for DRAM placement (front of hot list).

**Why it matters:** the user's current C1-C4 classification is implicitly
write-event-based (since soft-dirty captures writes). But within the
"writable" classes (C2, C3, C4), reads vs. writes aren't separated.
A page that gets many reads but few writes is a great LtRAM candidate
(reads are cheap, no wear). A page that gets few reads but many writes
is the *worst* LtRAM candidate (every write costs an erase). The
current policy can't distinguish these.

### Architecture sketch

```
Per-page state:
  uint16 read_count;
  uint16 write_count;
  uint8  flags;        // includes write_heavy bit

Classification update (per evaluation epoch):
  if write_count > WRITE_HEAVY_THRESHOLD:
      mark write_heavy
      → never migrate to LtRAM (no matter how cold)
  if read_count > READ_HOT_THRESHOLD:
      mark read_hot
      → prioritize for DRAM if currently in LtRAM
  // (the cold case is the residual: low both → migrate-candidate)

Cooling: halve both counters when total > COOLING_THRESHOLD
        (or use the clock-based lazy cooling from idea #14 in elaborations)
```

### Connection to existing classes

- C1 (vma not writable): read-only by definition → unchanged, ideal LtRAM
- C2 (writable, never touched): could be read-heavy or untouched
  entirely → use the new counters to distinguish
- C3 (writable, load-phase only): writes during init, then read-heavy →
  perfect LtRAM candidate
- C4 (writable, run events): the new split lets you find sub-class
  C4a (read-heavy, write-light) vs C4b (write-heavy) — C4a is
  migratable, C4b is not

### Implementation

- Add read counter to per-page metadata (small: 2 bytes per page)
- On every sweep (or every PEBS sample, if integrated with idea #12),
  update the appropriate counter
- Policy gates on (write_count vs. threshold) for migration decisions
- Wear-leveling target picker (from idea #7) also uses write_count to
  avoid hot-write target cells

### How to source read events without PEBS

Without PEBS, you don't have a read-event stream. Two options:
- **Coarse approximation:** infer reads from the *absence* of writes
  combined with the page-was-accessed signal (page-table accessed bit,
  read on every sweep). This is approximate but cheap.
- **Sampling-based:** Thermostat-style: select a small sample of pages,
  TLB-invalidate them, time the next access. Detects reads but is
  invasive.

For your class project, the absence-of-writes approximation is fine.
For DIMES and beyond, real read tracking matters and PEBS (idea #12)
is the cleanest path.

### Why this is a HW-SW co-design moment

The HW exposes access information at near-zero cost (counter increments,
access bits). The SW interprets the asymmetry. The contract between
them — "HW counts events; SW interprets cost" — is exactly the
HW-SW co-design framing from idea #10. Read/write separation is one of
the cheapest, highest-impact applications of that contract.

### Dependencies

- None for the absence-of-writes version
- Idea #12 (PEBS) for the full version

### Estimated effort

- Absence-of-writes version: 2–3 days (small metadata + policy gating)
- PEBS-integrated version: subsumed into idea #12

---

## 14. DMA-based migration via Linux I/OAT engine 🔵 (HeMem)

**Origin:** HeMem. Offloads page copy from the CPU to the Intel I/OAT
DMA engine. HeMem extends the Linux `ioatdma` driver to expose a
batched, multi-channel DMA copy API via `ioctl`.

**Why it matters:** for LtRAM specifically, NOR program latency is
high. If the CPU does the copy, it's stalled for the whole duration.
DMA offload lets the CPU continue running application code while
migration proceeds in the background. This is independent of (and
complementary to) the dual-mapping LLC redirect from idea #3 — DMA is
the software way; LLC redirect is the hardware way.

### Architecture sketch

```
Extend Linux ioatdma driver (HeMem's approach):
  - Add ioctl: COPY (src_va, dst_va, size, channels[])
  - Batched: up to 32 copy requests per ioctl
  - Per-process channel allocation/deallocation
  - DMA engine runs in parallel with CPU

Migration thread:
  while alive:
    batch = build_batch_from_pending_migrations(up_to=32)
    ioctl(ioatdma_fd, COPY_BATCH, batch)
    poll for completion (or use completion descriptor)
    for each completed migration:
      atomic PTE swap, async TLB invalidate (idea #2)
```

### For ARM / Enzian

I/OAT is Intel-specific. ARM equivalents:
- **CMN-600 / CMN-700 DMA engines** on some Neoverse SoCs
- **CCIX / CXL accelerators** for cross-tier copies
- **FPGA-based DMA on Enzian** — write a custom DMA engine in the FPGA
  that copies between DRAM and LtRAM-emulated regions

On Enzian specifically, this is again where the FPGA earns its keep:
implement DMA in the FPGA and expose it the same way ioatdma exposes
Intel I/OAT. The HW-SW contract: "OS submits descriptors; FPGA copies
in parallel; OS reaps completions."

### Dependencies

- For Intel: the ioatdma driver (already in mainline)
- For Enzian/ARM: FPGA-side DMA implementation (research project)

### Estimated effort

- Intel I/OAT integration: 1 week (kernel patch + libhemem-style ioctl)
- Enzian FPGA DMA: 4–8 weeks (significant FPGA work)

---

## 15. Multi-thread async architecture (PEBS / policy / fault / migration) 🔵 (HeMem)

**Origin:** HeMem's daemon structure: separate threads for memory
sampling (PEBS), policy decisions (10ms tick), page-fault handling
(userfaultfd), and migration (DMA-driven or worker threads). Plus
hot/cold/free FIFO queues per memory tier (six queues total).

**Why it matters:** the user's current kernel design has a single
kthread that scans the LRW queue, decides migrations, performs
migrations, and handles wear leveling. This serializes everything.
HeMem's experiment shows that **a single thread doing scan + migration
delays scanning enough to corrupt the hot-set estimate** (their PT-Sync
case at 18% of optimal vs. PT-Async at 43% — same algorithm, just
split into two threads).

For LtRAM, the same hazard applies. If the migration thread is busy
issuing NOR writes (slow!), it can't react to write-fault demotions or
update wear telemetry. Splitting it explicitly is a structural fix.

### Architecture sketch

```
Thread architecture:

  sampling_thread   — reads soft-dirty bits / PEBS / SPE; updates per-
                      page access counts; runs continuously, niced
  policy_thread     — 10ms tick; scans queues, decides migrations,
                      submits to migration_thread; gates by token
                      budget (idea #11)
  fault_thread      — handles write faults via userfaultfd / WP_DRAM
                      fault path; demotes pages from LtRAM to DRAM on
                      write
  migration_thread  — consumes pending migrations, issues DMA or
                      software copies; updates wear counters
                      (alternatively: DMA engine + ack thread)

Queues (per memory tier):
  hot_DRAM    — pages classified as hot, currently in DRAM
  cold_DRAM   — pages classified as cold, currently in DRAM (migration
                source for LtRAM)
  free_DRAM   — unallocated DRAM PFNs
  hot_LtRAM   — pages classified as hot, currently in LtRAM (migration
                source back to DRAM)
  cold_LtRAM  — pages currently in LtRAM, staying there
  free_LtRAM  — unallocated LtRAM PFNs (FIFO for wear leveling)
```

### Connection to other ideas

- **Idea #2 (async TLB invalidation):** the migration_thread issues
  invalidations asynchronously after each commit
- **Idea #11 (token budget):** the policy_thread gates migrations by
  available tokens
- **Idea #14 (DMA):** the migration_thread offloads copy to DMA

### Implementation in simulator first

Even before kernel work, structure your simulator with four
conceptually separate workers. This makes it easy to measure: "what
happens if I serialize sampling and policy?" and validate that
splitting matters.

### Estimated effort

- Simulator refactor to four threads: 1 week
- Kernel implementation (mm/ltram.c with four kthreads): 2–3 weeks

---

## 16. Clock-based lazy cooling for access counters 🔵 (HeMem)

**Origin:** HeMem. Instead of periodically iterating over all tracked
pages to decay their access counts, maintain a global clock counter.
When any page accumulates enough samples to trigger cooling, increment
the clock. On the next sample for a given page, check whether its
"last-cooled" tag matches the current clock; if not, cool the page
before incrementing.

**Why it matters:** sweeping all pages to decay counts is O(N_pages)
per cooling event. Lazy cooling is O(1) amortized — pages that are
never accessed are never cooled (and that's fine, they stay at their
old count). Avoids the sweep that ages your data structure.

### Architecture sketch

```c
struct page_state {
    uint32 access_count;
    uint8  last_cooled_clock;     // 8 bits, wraps naturally
    ...
};

uint8 global_cooling_clock = 0;
uint64 samples_since_last_cool = 0;

void on_sample(page_id_t p) {
    if (state[p].last_cooled_clock != global_cooling_clock) {
        state[p].access_count >>= 1;   // halve
        state[p].last_cooled_clock = global_cooling_clock;
    }
    state[p].access_count++;
    samples_since_last_cool++;
    if (samples_since_last_cool > COOLING_THRESHOLD) {
        global_cooling_clock++;
        samples_since_last_cool = 0;
    }
}
```

### Trade-offs

- **Pro:** O(1) per access; no sweep over inactive pages
- **Pro:** pages that go cold are *implicitly* cooled when they re-
  appear in samples — no special handling needed
- **Con:** pages that are never sampled retain stale counts
  indefinitely; this is mostly fine since they'd be cold anyway, but
  occasionally causes priority inversion at workload phase changes
- **Mitigation:** when a page is migrated, force a cooling pass on it

### Estimated effort

- Simulator integration: 2–3 days
- Kernel integration: 1 week (gated on the simulator working)

---

## 17. Selective management: skip small allocations 🔵 (HeMem)

**Origin:** HeMem only manages allocations ≥ 1 GB. Smaller allocations
fall through to the kernel and stay in DRAM. Region growth is tracked;
small allocations that grow past the threshold get promoted into
management later.

**Why it matters:** the policy machinery (sampling, classification,
ranking, migration) has fixed per-page overhead. For a 4 KB allocation
that lives for 100 ms, the overhead exceeds any conceivable benefit.
**Don't run policy where the answer is trivially "stay in DRAM."**

### Architecture sketch

For your kernel module:
- Intercept allocations at `mm/memory.c` (or `do_anonymous_page`, etc.)
- If size < THRESHOLD (1 GB default, configurable): skip the LRW queue
  entirely — page never enters management
- Track per-VMA cumulative size; if a VMA grows past THRESHOLD via
  many small allocations, promote it into management lazily
- The LRW queue, write-fault handler, etc. only see pages from large
  / promoted VMAs

### Connection to existing ideas

- **Idea #4 (spatial confinement):** small allocations effectively join
  the "DRAM-only region" by default. Selective management is the
  enforcement mechanism for the spatial split.
- **Idea #8 (cold-start window):** during cold start, even large
  allocations are skipped. After cold start ends, the size gate is
  the only filter.

### Estimated effort

- Add size gate to LRW queue insertion: 1 day
- Region-growth tracking for late promotion: 3 days
- Validation: 3 days

---

## 18. Sensitivity-study eval discipline 📐 (HeMem)

**Origin:** HeMem publishes a sensitivity sweep for every parameter
that survives to the paper: PEBS sampling period, hot read threshold,
hot write threshold, cooling threshold, DMA batch size, DMA channels.
Each is a single figure with the plateau visible.

**Why it matters:** "we chose X" is weak; "X is the middle of a
plateau and surrounding values give worse results" is strong. Sensitivity
studies are cheap to produce (run the sim many times) and
disproportionately defensive. Reviewers can't credibly say "you
cherry-picked X" when the figure shows otherwise.

### Parameters that need sensitivity studies in your work

| Parameter | Origin | Sweep range | Y-axis |
|---|---|---|---|
| `T` (or its replacement) | Threshold / token-budget | 0.1× to 10× expected optimum | wear, utility |
| `T_commit` | COW transition window | 1s to 5 min | mispredictions, utility delay |
| `T_post` | Post-commit observation | 0 to T_commit | late-rewrite detection rate |
| Token bucket capacity | Token-budget framework | 1× to 100× R | burstiness vs. smoothness |
| Cooling threshold | Lazy cooling | 1 to 100 events | phase adaptation speed |
| Write-heavy threshold | Read/write split | 1 to 50 writes | wear, false demotions |
| Burn penalty multiplier | COW cancellation | 1× to 10× | mis-classification rate |

### Format for each

A single figure (or panel) per parameter:
- X-axis: parameter value (log scale if range is large)
- Y-axis: primary metric (wear, utility, or SLO violation rate)
- Mark the chosen value with a vertical line
- Show min/max bars across multiple seeds

### Effort

- 1 day per parameter for the sweep + plot generation
- Plan to publish 4–6 of these in the dissertation, 2–3 in the
  workshop paper

---

## 19. Substrate characterization as paper §2 📐 (HeMem)

**Origin:** HeMem's §2.2 is a paper-within-the-paper: the Optane
performance profile (latency, bandwidth, scaling, asymmetry, media
access granularity). It's cited heavily by downstream work as the
canonical Optane reference.

**Why it matters:** your work depends on NOR LtRAM characteristics
(endurance, asymmetric R/W, erase-block granularity, wear distribution
under naive policies). If you don't characterize these explicitly,
reviewers will ask. If you do characterize them, you anchor every
design choice in measured behavior and contribute a canonical reference
that downstream work cites.

### What to characterize (your candidate §2)

- **NOR read/write/erase latency** — from datasheets or your emulation
- **R/W bandwidth scaling** — number of concurrent operations vs.
  effective throughput
- **Endurance distribution** — variance across cells, manufacturer-
  reported variance
- **Erase granularity** — block size (typically 64 KiB–256 KiB for NOR)
- **Read-while-erase support** — can other regions be read while one
  region is being erased?
- **Failure modes** — what happens to a worn-out cell? Bit-stuck? Block
  marked bad? Graceful?
- **Your workload characterization** — write reuse distance distributions
  across matmul / gapbs / redis (you already have this; promote it)
- **C1–C4 classification breakdowns** by workload (you have this too)
- **Naive-policy wear projection** — under threshold T baseline, how
  many years before first cell fails?

### Format

A §2 that builds the case for your design:
- §2.1 NOR LtRAM device profile (latency, endurance, erase)
- §2.2 Workload characterization (your existing CDFs, timelines, C1–C4)
- §2.3 Naive-policy projection (the "why current approaches fail"
  evidence — your costben_normalized 5y line)
- §2.4 Design implications (the bullet list that motivates the rest of
  the paper)

This is HeMem's §2 structure, adapted.

### Effort

- The data is mostly already in your `results/` directory
- Consolidating into paper-ready figures and tables: 1–2 weeks
- Writing the §2 prose: 1 week (during DIMES drafting)

---

## 20. Portable sampling abstraction with multi-backend implementation 🔵 (user-original, generalizes HeMem)

**Origin:** User's observation that HeMem hardcodes Intel PEBS, which
makes the work non-portable across substrates (ARM, FPGA-coupled
systems, commodity hardware). The right move is to define a portable
sampling interface that PEBS is just one backend for.

**Why it matters:** the policy contribution (token budget, ranking,
endurance-aware decisions) shouldn't be tied to a specific CPU vendor.
An abstraction layer makes the policy portable; the backend choice
becomes substrate-conditional. It also turns the FPGA implementation
on Enzian from a workaround into a **HW-SW contract research
contribution**.

### Architecture sketch

```
┌─────────────────────────────────────────────────┐
│  Policy layer (idea #11 token budget,           │
│  idea #13 R/W counters, idea #15 architecture)  │
│                                                 │
│      consumes: stream of (vaddr, op, ts)        │
└────────────────────┬────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────┐
│   ltram_sampler abstraction                     │
│                                                 │
│   produces: stream of (vaddr, op, ts)           │
│   API: register / unregister / read_batch       │
└─────────┬────────────────┬──────────────┬───────┘
          │                │              │
          ▼                ▼              ▼
   PEBS backend     SPE backend     FPGA backend     fallback
   (Intel)          (Neoverse N1+)  (Enzian)         (soft-dirty,
                                                      accessed-bit)
```

### Why this is publishable on its own

- HeMem's experiments are all on Intel because it hardcodes PEBS. **Not
  portable** across substrates.
- An abstraction with multiple backends is a research contribution:
  "the sampling primitive isn't the contribution; the policy that
  consumes it is. Same policy + different backends gets the same
  wear-reduction results."
- The FPGA backend on Enzian is a clean HW-SW contract paper section:
  "we implement an SPE-equivalent sampler in FPGA, exposing the same
  memory-mapped buffer interface; the kernel reads it identically to
  perf's PEBS buffer."
- Rebuts "you're doing HeMem on weaker hardware" — you're doing what
  HeMem couldn't: portable, multi-substrate, with a shipping HW-SW
  contract.

### The abstraction contract

```c
// One header that backends implement and policy consumes:

struct ltram_sample {
    uint64 vaddr;         // sampled virtual address (page-granular)
    uint8  op;            // LTRAM_OP_READ | LTRAM_OP_WRITE
    uint64 timestamp;     // CPU ticks or ns since epoch
    uint32 pid;
};

struct ltram_sampler_ops {
    int  (*init)(struct ltram_sampler_ctx *ctx, struct ltram_sampler_cfg *cfg);
    int  (*register_region)(struct ltram_sampler_ctx *ctx, void *start, size_t len);
    int  (*unregister_region)(struct ltram_sampler_ctx *ctx, void *start);
    int  (*read_batch)(struct ltram_sampler_ctx *ctx,
                       struct ltram_sample *out, size_t max);
    void (*destroy)(struct ltram_sampler_ctx *ctx);
};
```

Each backend implements this:
- **PEBS backend:** configures PEBS counters via `perf_event_open`,
  reads from the PEBS buffer, parses Intel's sample format into the
  common struct
- **SPE backend:** same idea with ARM SPE buffers (Neoverse N1+)
- **FPGA backend (Enzian):** reads a DMA'd buffer that the FPGA writes
  on each tracked access
- **Fallback:** periodic soft-dirty / accessed-bit scan converted into
  pseudo-events

### Implementation effort

| Backend | Effort | Notes |
|---|---|---|
| Fallback (soft-dirty) | 1 week | Mostly already exists in your code |
| PEBS backend | 2 weeks | HeMem's source is a working reference |
| SPE backend | 2 weeks | ARM `perf` integration; harder than PEBS but documented |
| FPGA backend (Enzian) | 6–10 weeks | Real research project; publishable |

### Dependencies

- Idea #15 (multi-thread daemon) — the sampler thread consumes from
  whatever backend
- Idea #11 (token-budget policy) — the consumer of the sample stream
- Idea #13 (R/W counters) — uses the op field of each sample

### Connection to dissertation narrative

This is the **HW-SW contract** (idea #10) made concrete. The OS provides
a uniform interface; HW backends (or software fallback) provide the
data. Reviewers see the contract and understand the contribution
without needing to know the substrate.

---

## 21. Cheap distinguisher signals — demotion history + VMA write rate 🔵 (user-original, complements PEBS)

**Origin:** User's observation that an additional cheap signal,
complementing PEBS-like sampling, can substantially improve the
cost/probability prediction for "this page will leave LtRAM." Two
candidates that together cover most of what PEBS misses.

**Why it matters:** PEBS tells you the current access rate to a page.
It does NOT tell you: (a) has this page been demoted before (i.e., did
we previously bet wrong on this page), or (b) is the broader VMA region
write-active. Both of these are **information HeMem cannot use** — and
both are nearly free for an endurance-bounded substrate to maintain.

### Signal A: per-page demotion history

A counter per page recording how many times this page has been
promoted to LtRAM and then demoted back to DRAM.

```c
struct page_meta {
    uint8 demotion_count;      // saturates at 255
    uint64 last_demoted_ts;    // optional: for time-windowed reset
    ...
};

// In the write-fault demotion path:
on_demotion(page p) {
    if (state[p].demotion_count < 255)
        state[p].demotion_count++;
    state[p].last_demoted_ts = now();
}

// In the migration decision:
bool can_migrate(page p) {
    if (state[p].demotion_count >= MAX_DEMOTIONS)
        return false;  // burned page, never migrate
    // ... rest of policy
}
```

**Properties:**
- **Cost:** literally free at runtime. One increment per demotion event
  (which already triggers many other writes).
- **Predictive power:** very high. The "twice shy" pattern — a page
  demoted N times will almost certainly be demoted again.
- **Captures information PEBS cannot:** PEBS sees writes happen, but
  doesn't remember the policy's mistakes. Demotion count is the
  policy's own self-correction signal.
- **Policy use:** exponential backoff or hard cutoff. After N
  demotions, set the migration threshold to infinity for that page.
  Alternative: probabilistic gate `P(migrate) = 0.5 ^ demotion_count`.

**Time-window variant:** to allow eventually-cold pages to recover from
historical demotion penalties, decay the count over time:

```c
// During periodic cooling (idea #16):
if (now() - state[p].last_demoted_ts > DEMOTION_RESET_WINDOW)
    state[p].demotion_count = max(0, state[p].demotion_count - 1);
```

### Signal B: VMA-level aggregate write rate

A single counter per VMA (not per page) tracking total writes to any
page within that VMA. ~hundreds of counters total per process.

```c
// VMA-level state, attached to vm_area_struct:
struct ltram_vma_state {
    atomic_t write_count_window;     // writes in last N seconds
    uint64 window_start_ts;
};

// In the write-fault path (already paid by kernel):
on_write_fault(addr) {
    struct vm_area_struct *vma = find_vma(current, addr);
    atomic_inc(&vma->ltram_state.write_count_window);
    // ... rest of fault handling
}

// In the migration decision:
bool can_migrate_in_vma(struct vm_area_struct *vma) {
    uint32 rate = atomic_read(&vma->ltram_state.write_count_window)
                / (now() - vma->ltram_state.window_start_ts);
    return rate < VMA_WRITE_RATE_THRESHOLD;
}
```

**Properties:**
- **Cost:** essentially free. The write fault handler already runs;
  one atomic increment per fault.
- **Predictive power:** strong. VMAs are usually internally consistent
  — a heap VMA is write-heavy throughout; an mmap'd `.rodata` VMA is
  never written; a tmpfs file VMA has its own pattern.
- **Drawback:** doesn't help within a heap VMA where some pages are
  hot and others aren't (PEBS still wins for fine-grained
  discrimination).
- **Policy use:** pages in low-write-rate VMAs are migration candidates
  regardless of per-page history; pages in high-write-rate VMAs are
  skipped entirely.

### Combined policy gate

The two signals are orthogonal. Use them together:

```python
def can_migrate(page p, vma v):
    if p.demotion_count >= MAX_DEMOTIONS:        # signal A
        return False  # this specific page is burned
    if v.write_rate > WRITE_RATE_THRESHOLD:      # signal B
        return False  # whole VMA is write-active
    # ... fall through to ranking-based decision (idea #11)
```

### Why this is a key distinguisher for the paper

- These two signals are **the signal HeMem doesn't have**. HeMem
  treats each evaluation cycle independently; doesn't remember
  mistakes or track region-level patterns.
- They directly address the **endurance objective**, not the
  bandwidth objective. They become irrelevant for Optane-style
  high-endurance substrates; they're load-bearing for NOR-style
  endurance-limited substrates.
- Both signals are computed with **zero PEBS dependency** — they work
  on every substrate, every backend of idea #20.
- They give you a **graceful degradation story**: even without PEBS
  (e.g., on Enzian without the FPGA), these two signals alone get you
  most of the wear-reduction benefit.

### Connection to other ideas

- **Idea #1 (COW transition window):** demotion-count increments on
  COW-cancellation events; the COW window is the natural place to
  detect mis-classification.
- **Idea #4 (spatial confinement):** VMA write rate is the dynamic
  refinement of static VMA classification. Static says "heap is C4";
  dynamic says "this specific heap VMA has cooled, reconsider."
- **Idea #11 (token-budget policy):** the gates filter the candidate
  set before ranking. Rankings only operate on pages that pass both
  gates.
- **Idea #20 (portable sampler):** independent of which sampling
  backend is active. These signals work without any sampling.

### Implementation effort

| Component | Effort |
|---|---|
| Per-page demotion counter + hook in demotion path | 2 days |
| VMA write-rate counter + hook in fault path | 3 days |
| Policy gates in migration decision | 2 days |
| Sweep `MAX_DEMOTIONS` and `WRITE_RATE_THRESHOLD` parameters | 1 week |
| Validation: does this beat the no-gates baseline? | 1 week |

**Total: ~2–3 weeks.** Very high return on time invested.

### What to call this in the paper

These are not novel mechanisms individually (page-history counters and
region-level aggregates exist throughout systems work). But the *use*
— as wear-aware policy gates that complement event-level sampling —
is novel for tiered memory.

Possible name: **"residual signals"** — the signals that remain useful
when you strip away the expensive sampling. The graceful-degradation
framing.

---

## 22. Histogram-based dynamic threshold derivation 🔵 (Memtis)

**Origin:** Memtis (SOSP '23). Replaces fixed magic-number thresholds
(HeMem's 8 reads / 4 writes, AutoNUMA's 1) with thresholds derived
from an exponential-bin histogram of per-page access counts. The
threshold is whichever bin cut makes the identified hot set fit
exactly into fast-tier capacity.

**Why it matters for LtRAM:** your token-budget framework (idea #11)
says *how many* pages to migrate. The histogram says *which* — the
top-K hottest, where K is determined by the budget. The two are
complementary, not redundant. Without the histogram, the ranking
function has to be re-tuned per workload; with it, the threshold
adapts automatically to whatever the access distribution looks like.

### Architecture sketch

```c
// 16 exponential bins, ~128 bytes total
struct write_reuse_histogram {
    uint64 bin_counts[16];        // # pages with reuse distance in
                                  // [2^n, 2^(n+1)) sweeps
};

// Update on each policy decision event:
void update_histogram(page_id_t p, uint32 reuse_distance) {
    uint8 bin = log2_clamped(reuse_distance);
    page_state[p].old_bin = page_state[p].current_bin;
    page_state[p].current_bin = bin;
    if (page_state[p].old_bin != bin) {
        bin_counts[page_state[p].old_bin]--;
        bin_counts[bin]++;
    }
}

// Derive threshold to fit token budget B (pages/sec) over an epoch:
uint8 derive_threshold(uint64 budget_pages_per_epoch) {
    uint64 cumulative = 0;
    // Walk from coldest bin (highest reuse distance = best migration
    // candidates) down toward hottest
    for (int b = 15; b >= 0; b--) {
        if (cumulative + bin_counts[b] > budget_pages_per_epoch)
            return b + 1;  // this bin would exceed budget
        cumulative += bin_counts[b];
    }
    return 0;  // budget allows migrating everything (unusual)
}
```

### Cooling via bin shift

Memtis's key trick: halve all access counts = shift all bin values
left by one. For your work, write reuse distance is the metric, but
the same idea applies if you maintain access counters in addition.
If you keep a cumulative write count per page (idea #21's
demotion-history-like counter), use bin-shift cooling:

```c
void cool_histogram() {
    for (int b = 0; b < 15; b++)
        bin_counts[b] = bin_counts[b + 1];
    bin_counts[15] = 0;
    // Now update per-page bin indices lazily on next access
}
```

O(16) work per cooling event, regardless of how many pages there are.

### Sensitivity-study discipline

The histogram has a few magic numbers (number of bins, cooling
interval, threshold adaptation interval). Memtis publishes sensitivity
studies for all of them. Plan to do the same — sweep number of bins
{8, 16, 32}, cooling interval, adaptation interval. See idea #18.

### Connection to existing ideas

- **Idea #11 (token budget):** the histogram provides the ranking;
  the budget provides the count. K = budget; ranking = top-K via
  histogram cut.
- **Idea #13 (R/W counter separation):** maintain two histograms,
  one for reads and one for writes. Different thresholds per axis.
- **Idea #21 (demotion-history gate):** the histogram tells you who
  is currently cold; the demotion history tells you who is
  *reliably* cold. Combine them: histogram identifies candidates,
  demotion-history gate filters out the burned ones.

### Dependencies

- The token-budget framework (#11) — the histogram needs a budget
  to know where to cut
- Per-page state to track current bin (1 byte per page)

### Estimated effort

- Histogram data structure + bin-index per page: 3 days
- Cooling-via-shift implementation: 1 day
- Integration with token-budget policy gate: 3 days
- Sensitivity sweep across bin counts, cooling intervals: 1 week
- **Total: ~2 weeks** for a defensible implementation + eval

---

## 23. Erase-block-skewness-aware migration 🔵 (Memtis-inspired, user-original for NOR)

**Origin:** Memtis splits skewed huge pages so that only hot subpages
go to fast tier. The analog for NOR: detect erase blocks with skewed
write activity (a few 4 KB pages write-heavy, most cold), and handle
them differently from uniformly cold or uniformly hot blocks.

**Why it matters for LtRAM:** NOR's erase granularity is 64 KB–256 KB.
If you migrate a 4 KB page to LtRAM and a write to a *different*
4 KB page in the same erase block triggers an erase, you've consumed
endurance for the whole block. The skewness of write activity within
an erase block is the same problem as subpage skewness within a huge
page.

**This is a genuinely new contribution direction** because:
- Memtis-style skewness-aware splitting doesn't exist for NOR
- The endurance constraint makes block-level skewness *more* important
  than huge-page skewness was for performance
- It directly motivates HW-level NOR controller design choices (idea
  #6's HW cost quantification has more to defend if the controller
  supports block-aware migration)

### Architecture sketch

For each NOR erase block (logical group of 16–64 4 KB pages):

```c
struct erase_block_meta {
    uint16 pages_in_block;       // typically 16-64
    uint32 write_count_total;    // sum of subpage writes
    uint32 write_count_max;      // max single subpage's writes
    uint8  erase_count;          // wear counter (existing)
    uint8  flags;
};

// Skewness factor (Memtis-style):
float skewness(struct erase_block_meta *b) {
    float sum_sq = 0;
    float U = 0;  // utilization: # of subpages with any writes
    for (each subpage in block) {
        if (subpage.write_count > 0) {
            U += 1;
            sum_sq += subpage.write_count * subpage.write_count;
        }
    }
    if (U == 0) return 0;
    return sum_sq / (U * U);
}

// High skewness = few hot pages, mostly cold. Treat specially.
```

### Three classes of erase blocks

- **Uniformly cold (low total writes, low skewness):** ideal LtRAM
  candidate. Migrate as a unit.
- **Uniformly hot (high total writes, low skewness):** ideal DRAM
  candidate. Keep in DRAM as a unit.
- **Skewed (medium total writes, high skewness):** the difficult case.
  Three options:
  1. Keep in DRAM (conservative — wastes capacity)
  2. Migrate to LtRAM and accept that the hot subpages will demote it
     soon (current approach)
  3. **Split the logical block:** migrate cold subpages to LtRAM,
     keep hot subpages in DRAM. Requires HW support for sub-block
     addressing (or software emulation via per-page mapping)

Option 3 is the Memtis pattern applied to NOR. It requires the LtRAM
controller (idea #6) to support sub-block reads — which most NOR
designs already do (NOR is byte-addressable for reads; the constraint
is only on erases).

### Connection to existing ideas

- **Idea #6 (HW cost quantification):** sub-block migration support
  is a HW feature with quantifiable cost. Compare against bulk-block
  migration.
- **Idea #21 (cheap gates):** VMA-level write rate from idea #21
  is the precursor signal — if the VMA has high write rate, its
  erase blocks are unlikely to be migration candidates anyway. The
  block-level skewness gate runs *after* VMA filtering.
- **Idea #22 (histogram threshold):** maintain a histogram of
  block-level skewness factors. Trigger sub-block migration only
  for blocks in the top-K skewness bins (analogous to Memtis's
  N_s computation).

### Why this distinguishes you from Memtis

Memtis's huge-page splitting addresses fast-tier *capacity* waste
(skewed huge pages waste DRAM by storing cold subpages). Your
erase-block-aware migration addresses **endurance** waste (skewed
blocks burn through erase budget on cold subpages co-located with
hot ones). Same mechanism, different objective — and the endurance
framing is the new contribution.

### Dependencies

- Idea #6 (HW cost quantification) — defines whether sub-block
  migration is feasible in your HW model
- Idea #22 (histogram threshold) — provides the ranking for which
  blocks are most worth treating specially
- Per-block metadata (~24 bytes per block, for 65K pages / 16 pages
  per block = 4K blocks × 24 bytes = 96 KB; small)

### Estimated effort

- Block-level metadata structure: 3 days
- Skewness factor computation + histogram: 3 days
- Sub-block migration policy (Option 3 above): 1-2 weeks
- HW-cost analysis for sub-block addressing: 1 week (idea #6 deliverable)
- **Total: ~4 weeks** for a research-quality implementation + cost study

---

## 24. Write burstiness as endurance signal (AOL analog for LtRAM) 🔵 (Soar/Alto-inspired)

**Origin:** Soar/Alto (OSDI '25, Virginia Tech). Their AOL = Latency / MLP
attacks the assumption that "hot" = "performance-critical." MLP masks
slow-tier latency, so frequently-accessed high-MLP pages are not the
performance-critical ones. For LtRAM, the analog is: **write-frequency
is not endurance-criticality.** Pages with bursty writes (many writes
clustered in time, then long cold tails) are cheaper to migrate than
pages with uniformly-spread writes, *even at the same total write count.*

**Why it matters for LtRAM:** your existing metrics (write reuse
distance, write count) capture the *mean* of write behavior but not the
*variance* or *clustering*. Two pages with identical mean reuse distance
can have vastly different endurance criticality:
- Page A: writes at t = {10, 20, 30, 40, 50} → mean gap 10, uniform
- Page B: writes at t = {1, 2, 3, 4, 5} then silence → mean gap 1.5, bursty

Page B has shorter mean gap but is *cheaper* to migrate: capture the
cold tail after the burst, amortize over the rest of the run. Page A is
the endurance killer: every epoch contains a write, migration just
delays the inevitable demotion.

**Soar/Alto's key insight applied here:** the per-access cost isn't
uniform. For performance, MLP determines per-access cost. For endurance,
**clustering** determines per-migration cost (because migration after a
burst amortizes its erase over the cold tail).

### Architecture sketch

Per-page state, add 4 bytes:

```c
struct page_endurance_state {
    uint32 total_writes_in_window;     // existing (raw count)
    uint32 burst_window_max;           // max writes seen in densest k-sweep
                                       // window over the page's lifetime
    uint64 last_write_sweep;
    uint32 inter_write_variance_q;     // quantized variance of gaps
};

// Update on each write event:
void on_write_event(page_id_t p, uint32 sweep_t) {
    state[p].total_writes_in_window++;

    uint32 gap = sweep_t - state[p].last_write_sweep;
    state[p].last_write_sweep = sweep_t;

    // EWMA of squared gaps approximates variance
    update_inter_write_variance(&state[p], gap);

    // Track max burst in any 100-sweep window (cheap approximation)
    update_burst_window_max(&state[p]);
}

// Compute endurance-criticality at decision time:
float endurance_criticality(page_id_t p) {
    float burstiness = state[p].burst_window_max
                     / max(1, state[p].total_writes_in_window);
    // burstiness ∈ (0, 1]: 1.0 = all writes in one window, very bursty
    //                      ~k/N for uniform spread
    float uniformity = 1 - burstiness;
    // High uniformity = high endurance cost per migration
    return state[p].total_writes_in_window * uniformity;
}
```

### How this enters the policy stack

This is **a new signal**, orthogonal to the existing ones:
- Idea #11 (token-budget) provides the budget cap
- Idea #22 (histogram thresholds) provides the cut over the distribution
- Idea #21 (demotion-history gates) provides the burned-page filter
- **Idea #24 (this) provides a better ranking input** within the
  remaining candidates: rank by endurance-criticality, not just by
  raw write count

The combined policy:
```python
def select_migration_candidates(pages, token_budget):
    # Apply gates first (cheap, eliminates most candidates)
    eligible = filter_by_gates(pages)   # idea #21
    eligible = filter_by_burned(eligible)
    eligible = filter_by_vma_writerate(eligible)

    # Rank surviving candidates by endurance-criticality (idea #24)
    ranked = sorted(eligible, key=endurance_criticality, reverse=False)
    # Lowest criticality first = best migration candidates

    # Apply token budget (idea #11) and histogram cut (idea #22)
    return ranked[:token_budget]
```

### The counter-intuitive demo

Construct a microbenchmark with two threads:
- **Thread A:** writes burstily — 1000 writes in 1 sec, then idle for 60s, repeats
- **Thread B:** writes uniformly — one write every 10 ms continuously

Conventional endurance policy: both look "write-heavy" by raw count.
Your policy with burstiness: migrate A after its burst (long cold tail
amortizes the erase); keep B in DRAM (every epoch costs an erase).

Plot: device lifetime under naive policy vs. your policy. If your
policy extends device life by 5–10× on this microbenchmark, you have
your Figure 1.

### Connection to Soar/Alto

- **Soar's K = f(AOL) curve** is workload-independent, calibrated from
  microbenchmarks at MLP extremes. **Your burstiness-vs-endurance-cost
  curve** can be similarly calibrated from microbenchmarks at clustering
  extremes (uniform vs. bursty writes).
- **Alto's stepwise throttling** based on AOL bands maps directly to your
  ranking: high burstiness = aggressive migration; low burstiness =
  throttle migration.

### Dependencies

- Existing per-page write tracking (already in place)
- A burstiness aggregator (the burst_window_max field) — small addition

### Estimated effort

- Adding burstiness tracking to your existing per-page state: 2 days
- Integration with the token-budget policy gate: 3 days
- The counter-intuitive demo microbenchmark: 2 days
- Sweep across workloads to characterize burstiness distribution: 1 week
- **Total: ~2 weeks** for a defensible new contribution dimension

---

## 25. Plug-in integration with Memtis / HeMem / TPP / Memtis 🔵 (Soar/Alto-inspired)

**Origin:** Soar/Alto's Alto component integrates as a plug-in regulator
into TPP, NBT, Nomad, and Colloid in ≤30 LOC each. This is a much
stronger deployment story than "we replace existing tiering with our
new system."

**Why it matters for LtRAM:** your dissertation gains immensely if you
can claim "our endurance-aware policy makes Memtis, HeMem, and TPP
better." Reviewers can't object that you chose the wrong baseline
because you enhance every baseline. The HW-SW contract framing
(idea #10) already positions your work as a substrate for others; the
plug-in design is the concrete deployment expression of that framing.

### Architecture sketch

Define a clean contract:

```c
// Existing tiering system calls into your endurance regulator
// before any migration decision is committed.

struct endurance_decision {
    bool      allow_migration;       // gate result
    int       priority_override;     // optional reorder
    uint32    suggested_target_pfn;  // optional wear-leveling target
};

// The endurance regulator hooks into the tiering system's migration
// decision point. Each system needs ~30-50 LOC of integration:

extern struct endurance_decision
ltram_endurance_check(page_id_t p, migration_intent_t intent);

// Inside Memtis (or HeMem or TPP):
void memtis_consider_migration(page_id_t p) {
    if (memtis_is_hot(p)) {           // existing Memtis logic
        struct endurance_decision d = ltram_endurance_check(p, PROMOTE);
        if (!d.allow_migration) {
            return;                   // your gate blocked it
        }
        memtis_migrate(p, d.suggested_target_pfn);
    }
}
```

### What "endurance_check" actually does

Combines all your gates and ranking:
```c
struct endurance_decision ltram_endurance_check(page_id_t p, intent_t i) {
    // Idea #21 gates
    if (demotion_count[p] >= MAX_DEMOTIONS)
        return REJECT;
    if (vma_write_rate(p->vma) > VMA_WRITE_THRESHOLD)
        return REJECT;

    // Idea #1 / #11 / #22 / #24 ranking
    float criticality = endurance_criticality(p);
    if (criticality > token_budget_threshold())
        return REJECT;

    // Idea #7 SLO check
    if (current_wear_rate() > SLO_WEAR_RATE)
        return REJECT;

    return ACCEPT_WITH_TARGET(wear_leveled_target_pfn());
}
```

### Integration points per existing system

| Tiering system | Integration point | LOC estimate |
|---|---|---|
| Memtis | Before `migrate_pages()` call in kmigrated | ~40 |
| HeMem | In `should_migrate()` in policy thread | ~30 |
| TPP | In NUMA-balancing-hint promotion path | ~50 |
| Lagar-Cavilla / TMO | In kreclaimd's promotion candidate filter | ~40 |
| Soar/Alto | In Alto's regulator (it's already a plug-in) | ~30 |

### Paper positioning

Recast your dissertation contribution as:
> "We propose an endurance-aware regulator that integrates as a plug-in
> with existing tiering systems including Memtis [SOSP '23], HeMem
> [SOSP '21], TPP [ASPLOS '23], and Soar/Alto [OSDI '25]. Our regulator
> adds the wear-aware constraints that all of these systems lack, while
> preserving their performance-oriented contributions. Across all five
> baseline systems, our regulator reduces NOR wear by X% with negligible
> performance impact."

This is much stronger than "we built a new tiering system." It also
makes reviewing easier: a reviewer comfortable with Memtis will
understand your work as "Memtis + endurance" without learning a new
system end-to-end.

### Dependencies

- All previous ideas need to be wrappable in the `endurance_check` API
- The API itself is small (a single function call with structured
  return)

### Estimated effort

- Define the API contract: 1 week
- Implement endurance_check using existing ideas: 1-2 weeks (mostly
  glue code)
- Integration with one existing system (start with Memtis): 1 week
- Integration with two more (HeMem + TPP): 2 weeks
- Cross-system evaluation: 2 weeks
- **Total: ~7-8 weeks** for the full plug-in story

---

## 26. Closed-form endurance equilibrium + watermark controller (Colloid-inspired) 🔵

> **📐 Full Lagrangian derivation lives at:**
> [`notes/closed_form_endurance_equilibrium.md`](closed_form_endurance_equilibrium.md)
> — sections 1–10 cover the optimization problem, KKT conditions,
> shadow-price interpretation, uniqueness proof, watermark controller
> pseudocode, 2D extension, and Colloid comparison table. **Treat that
> file as paper-section material; this entry summarizes.**

**Origin:** Colloid (SOSP '24, Cornell). Two big ideas:
(a) Replace tuned thresholds with a closed-form equilibrium derived
    from first principles (Colloid: L_D = L_A, balancing access
    latencies)
(b) Converge to that equilibrium via a watermark-driven binary
    search, with reset-on-drift for non-stationary workloads

**Core equations (verbatim from the derivation file):**

```
Maximize:    Σ utility(p) × x_p
Subject to:  Σ x_p ≤ B / T                  (endurance budget)
             x_p ∈ {0, 1}

Lagrangian:
  L(x, λ) = Σ utility(p) × x_p − λ × (Σ x_p − B/T)

KKT optimum:
  ∂L/∂x_p = utility(p) − λ = 0

  utility(p) > λ  ⇒  x_p = 1 (migrate)
  utility(p) < λ  ⇒  x_p = 0 (don't migrate)

Where:
  utility(p) = P(stays read-only for K epochs)
              × page_size × K × epoch_duration

  B = N_cells × erases_per_cell  (endurance budget)
  T = deployment_lifetime
  λ = shadow price of the endurance budget

Equivalently using EEU (endurance-effective utilization):
  EEU(p) = utility(p) / cost(p) = utility(p) / 1

  Migrate p   iff   EEU(p) > λ*

Uniqueness: f(λ) := |{p : utility(p) > λ}| is monotonically
non-increasing in λ. The unique equilibrium λ* satisfies
f(λ*) = B/T.
```

The watermark controller adapts λ over time (see the file for
pseudocode). Convergence is guaranteed under stationary workloads;
reset-on-drift handles non-stationary cases.

**Why it matters for LtRAM:** the token-budget framework (idea #11)
is a heuristic — "rank pages, migrate top-K." It works, but it has
tuned parameters (token rate, bucket size, ranking weights). Colloid
suggests a stronger framing: **what is the equilibrium state of the
system, and how do I converge to it?** A closed-form equilibrium is
cleaner, more defensible, and adapts automatically to workload shifts.

### The closed-form endurance equilibrium

For LtRAM, the equilibrium is **two-dimensional**:

```
Equilibrium condition (joint):
  (1) Total wear rate = endurance_budget / deployment_lifetime
      (use the budget exactly, no more, no less)
  (2) Max(cell_wear) / Mean(cell_wear) ≤ 1 + ε
      (wear distribution stays uniform)
```

Derivation:
- Constraint: total cell-writes over deployment_lifetime ≤
  endurance_budget = N_cells × erases_per_cell
- Objective: maximize utility = page-seconds-in-LtRAM
- At the optimum (Lagrangian): the marginal utility of one more
  migration = the marginal wear cost of one more migration
- This balance gives: wear rate equals budget rate (constraint
  binds), and wear distribution is uniform (no slack from variance)

This is the **Colloid analog**: instead of L_D = L_A, it's
(wear_rate, wear_distribution) = (budget_rate, uniform).

### Architecture sketch

```c
// Measured (via Little's Law analog or direct counters):
struct ltram_state {
    double observed_wear_rate;       // total NOR erases / sec
    double max_min_wear_ratio;       // max(erase_counts) / mean(...)
    uint64 active_migrations;        // queue depth
    double migration_rate;           // pages/sec being migrated
};

// Target:
struct ltram_target {
    double budget_wear_rate;         // hardware constant: budget /
                                     // deployment_lifetime
    double uniformity_threshold;     // e.g., 1.1 (max/mean ≤ 1.1)
};

// Watermarks (for migration intensity):
struct migration_watermarks {
    double m_lo, m_hi;               // bounds on equilibrium
                                     // migration rate
    double current_rate;
};

// Per-epoch controller:
void colloid_style_endurance_step(struct ltram_state *s,
                                  struct ltram_target *t,
                                  struct migration_watermarks *w) {
    // Measure deviation from each equilibrium dimension
    bool over_budget = s->observed_wear_rate > t->budget_wear_rate;
    bool wear_uniform = s->max_min_wear_ratio <= t->uniformity_threshold;

    // Update watermark based on observation
    if (over_budget) {
        w->m_hi = w->current_rate;     // current rate too high
    } else {
        w->m_lo = w->current_rate;     // current rate sustainable
    }

    // Reset on drift detection (watermarks converged but not at
    // equilibrium)
    if (w->m_hi - w->m_lo < EPSILON &&
        (abs(s->observed_wear_rate - t->budget_wear_rate) > TOL ||
         !wear_uniform)) {
        // Equilibrium has shifted (workload phase change)
        if (over_budget) w->m_hi = MAX_RATE;
        else             w->m_lo = 0;
    }

    // Move toward midpoint
    w->current_rate = (w->m_lo + w->m_hi) / 2;
}
```

### Little's Law for wear telemetry

Apply Little's Law to your erase pipeline:

```
NOR Erase Pipeline:
  Queue depth Q = number of pending erases at any moment
  Arrival rate λ = rate of new erase requests (= migration_rate)
  Departure rate μ = NOR's intrinsic erase processing rate

Little's Law: average time-in-queue = Q / λ
If Q / λ is growing: you're issuing erases faster than NOR can
  process them. Throttle migrations.
If Q / λ is steady: pipeline is balanced. Sustainable rate.
```

This is the LtRAM analog of Colloid's L_D = O_D / R_D. **Don't predict
NOR saturation from access patterns; measure it from the erase
queue.**

### Why this is stronger than the token-budget (idea #11) alone

| Aspect | Token Budget (idea #11) | Colloid-style equilibrium (idea #26) |
|---|---|---|
| Foundation | Heuristic (rate from spec) | Closed-form (derived from constraint) |
| Tuned params | Token rate, bucket size | One: budget_wear_rate (from hardware) |
| Workload adaptation | Static budget | Self-adapting via watermarks |
| Convergence proof | None (rate-limited) | Binary search → guaranteed |
| Drift handling | None | Reset-on-drift |
| Paper narrative | "We use a rate-limited migrator" | "We replace heuristics with a principle" |

Idea #11 is your **practical workhorse**; idea #26 is your
**theoretical foundation**. They're complementary: the equilibrium
gives you the target migration rate; the token bucket gives you the
mechanism to enforce it.

### Connection to other ideas

- **Idea #7 (perf + wear SLO):** the budget_wear_rate is exactly the
  wear-dimension of your SLO. Idea #26 gives you the controller; idea
  #7 gives you the target
- **Idea #11 (token budget):** idea #26 *derives* the right rate; idea
  #11 *enforces* it. Together, the rate is no longer a magic number
- **Idea #22 (histogram threshold):** the histogram gives you "which
  pages"; the equilibrium gives you "how many." Both layers are
  needed
- **Idea #21 (cheap gates):** the gates filter; the equilibrium-driven
  rate sets the throughput; the ranking picks within the rate
- **Idea #20 (portable sampler):** Colloid's CHA counters are
  hardware-specific. Your sampler abstraction can include a
  wear-pipeline measurement backend (per-PFN counters → erase rate)

### The paper-writing move

Colloid's introduction: "Existing systems use a heuristic; we replace
it with a principle derived from first principles." Adopt the same
narrative for your DIMES paper:

> Existing tiering systems use endurance heuristics — write-rate
> thresholds, migration rate limits, hand-tuned cooldowns. We derive
> the endurance equilibrium analytically from the deployment-lifetime
> budget constraint and design a controller that converges to it
> under non-stationary workloads. The framework subsumes existing
> heuristics as special cases.

### Dependencies

- Idea #7 (SLO) — defines the target metrics for the equilibrium
- Idea #11 (token budget) — provides the migration enforcement layer
- Per-PFN erase counters in debugfs (already in proposal §3.1)

### Estimated effort

- Derive the closed-form equilibrium carefully (paper-quality): 1-2
  weeks
- Implement the watermark controller: 1 week
- Validate convergence on multiple workloads: 2 weeks
- Integrate with idea #11 (replace static rate with controller-driven
  rate): 1 week
- **Total: ~5-6 weeks** for a defensible theoretical foundation
  + working controller

---

## 27. Post-commit shadow window for cheap migration rollback 🔵 (NOMAD-inspired)

**Origin:** NOMAD (OSDI '24). NOMAD keeps a slow-tier shadow copy
*after* successful promotion to make later demotion cheap (free PTE
remap). For LtRAM, flip the asymmetry: keep a *fast-tier* shadow copy
*after* successful migration-to-LtRAM, so that an early write triggers
a cheap rollback instead of an erase + DRAM copy.

**Why it matters:** idea #1 (COW transition window) protects against
writes *before* the migration commits — if a write arrives in the
window, abort and never touch NOR. Idea #27 protects against writes
*just after* the commit — if a write arrives in the post-commit
window, roll back to the DRAM copy without erasing more NOR. Together
they cover the full migration lifecycle.

### Architecture sketch

```
Phase 1 (pre-commit, idea #1):
  COW transition window. PTE points to DRAM. If write arrives, abort.

Phase 2 (commit):
  Copy DRAM → NOR. Atomic PTE swap to point at NOR.
  **DRAM copy is NOT freed yet** — kept as a "fast-tier shadow."
  Page is marked read-only (any write triggers shadow-page-fault).

Phase 3 (post-commit window, idea #27):
  Duration: T_post (e.g., 30 seconds).
  - If write arrives: shadow-page-fault fires.
    - Atomically remap PTE back to DRAM copy.
    - Free the NOR copy (mark cell as written but no useful data).
    - Mark page as "demoted, never re-promote" (burned bit, idea #21).
    - Cost: 1 wasted NOR erase + 1 PTE remap. NO additional DRAM
      copy needed.
  - If no write within T_post: free DRAM copy, page is permanently
    in LtRAM.
```

### Per-page state

```c
struct page_shadow_state {
    uint64 commit_timestamp;     // when phase 2 happened
    pfn_t  dram_shadow_pfn;      // PFN of the fast-tier shadow
    uint8  flags;                // POST_COMMIT, etc.
};
```

Stored in XArray indexed by NOR PFN (Linux-native, see NOMAD's
implementation).

### Comparison to idea #1

| Aspect | Idea #1 (pre-commit COW) | Idea #27 (post-commit shadow) |
|---|---|---|
| When | Before NOR write | After NOR write |
| Trigger | Write in transition window → abort | Write in post-commit window → rollback |
| NOR cost on rollback | Zero (never wrote) | One erase (wrote then abandoned) |
| DRAM cost | DRAM copy held during window | DRAM shadow held during window |
| Detects | Mis-predictions before commit | Mis-predictions just after commit |

Together they form a complete "migration lifecycle protection":
- Pre-commit window catches the cheap mistakes (no NOR write yet)
- Post-commit window catches the slightly-too-late mistakes (one NOR
  write happened, but at least no DRAM re-allocation needed)

### Memory pressure handling (NOMAD pattern)

Under DRAM pressure, kswapd should reclaim shadow DRAM copies before
ordinary pages. The shadow is recoverable (the data is in NOR);
ordinary pages aren't. Same priority ordering as NOMAD's kswapd shadow
reclamation.

```c
void kswapd_reclaim_priority() {
    // Free post-commit shadow DRAM first (recoverable from NOR)
    while (memory_pressure_high && shadow_dram_list_not_empty)
        free_oldest_shadow();
    // Only then evict ordinary pages
    if (memory_pressure_high)
        normal_kswapd_eviction();
}
```

### How this fits the policy stack

- **Idea #1 + #27 together = "OS-level OCC for endurance-aware
  migration"** (the framing meta-pattern #56 suggests)
- Both consume the same trigger mechanism (write detection via PTE
  permission)
- Both feed the same telemetry signal: rollback rate = mis-prediction
  rate for the ranking function

### Connection to NOMAD vs. user differentiation

NOMAD's shadows live in the slow tier (cheap demotion). Your shadows
live in the fast tier (cheap rollback). **Same mechanism, mirrored.**
The differentiation in your paper: "NOMAD optimizes demotion under
memory pressure; we optimize migration rollback under endurance
pressure. Same underlying OCC pattern, applied to a different
constraint."

### Dependencies

- Idea #1 (COW transition window) — pre-commit protection
- Idea #15 (multi-thread architecture) — the kswapd-style shadow
  reclamation runs as its own thread

### Estimated effort

- Add post-commit phase to migration state machine: 1 week
- XArray-indexed shadow tracking: 3 days
- kswapd integration for shadow reclamation: 3 days
- Validation (measure rollback rates, compare to no-shadow baseline):
  1-2 weeks
- **Total: ~3 weeks** for a NOMAD-inspired endurance-rollback
  mechanism

---

## 28. Proxy-metric trio for endurance SLO + percentile-band success criterion 🔵 (TMTS-inspired)

**Origin:** TMTS (ASPLOS '23). Google operates two-tier memory at WSC
scale not by directly measuring "performance" or "cost" but by
bracketing the goal with two cheap proxy metrics — STAR (degradation
proxy) and STRR (utilization proxy) — and stating success as
percentile bands ("median STAR < 0.5%, P95 STAR < 1.5%, STRR ≈ 25%
when memutil > 75%"). The proxies make the goal *measurable per
machine* and *comparable across experiments*. The percentile bands
absorb workload diversity that a single global threshold cannot.

**Why it matters for LtRAM:** idea #7 (perf + wear SLO) defines the
abstract objective; idea #11 (token budget) defines the control
mechanism. Neither defines what an *operator* or a *paper figure*
reads off the system to say "this is working". TMTS gives the
operational shape: a small set of proxies, each bracketing a
failure mode, with percentile-band targets baked into every
experiment's success criterion.

### Proposed proxy trio

| Proxy | What it measures | Failure mode it brackets |
|---|---|---|
| **LTRR** (LtRAM Tier Residency Ratio) | fraction of allocated memory resident in LtRAM | under-utilization (paying for LtRAM, getting no capacity) |
| **EMR** (Endurance-Migration Rate, normalized to B/T budget rate from idea #26) | migrations-to-LtRAM per second ÷ budget rate | endurance overspend (will not survive deployment lifetime) |
| **WAR** (Write-Access Ratio to LtRAM) | fraction of writes that hit LtRAM | misclassification leakage (writing to a write-cold tier means policy got the page wrong) |

### Operational targets (percentile bands)

State experiment success as bands, not thresholds:

```
LtRAM target band:
  - median EMR  < 1.0   (sustainable wear)
  - P95 EMR     < 2.0   (transient burst tolerable; smoothing absorbs it)
  - median WAR  < 0.5%  (classification working: write-cold pages stay write-cold)
  - P95 WAR     < 2.0%  (correctable via classifier retrain)
  - LTRR        within ±5% of deployment ratio, conditional on
                allocated-memory > 75%
```

The conditional ("when memutil > 75%") prevents trivial satisfaction
at low load — same trick as TMTS's "memutil > 75%" condition.

### Why each is a proxy, not a goal

- **LTRR** is a capacity proxy, not "we saved money." Savings comes
  from LtRAM being cheaper per GB; LTRR confirms that capacity is
  actually being used.
- **EMR** is an endurance proxy, not "we will survive 5 years."
  Survival is the goal; EMR < 1.0 over the deployment is the
  *necessary and sufficient* operational condition for it.
- **WAR** is a classifier-quality proxy, not "no writes ever."
  Some writes will leak. WAR bounds the leakage; sustained WAR > 1%
  signals the classifier (idea #24, idea #21) needs retraining.

### How to operationalize

1. Add a per-machine telemetry exporter that emits LTRR / EMR / WAR
   per (epoch, application-class) pair. The exporter runs at the same
   cadence as the watermark controller (idea #26).
2. Every experiment in idea #9 (A/B framework) reports the proxy
   trio with confidence intervals matched by phase, not by wall-clock
   time. The phase-matched comparison is the single-node analog of
   TMTS's matched-replica methodology.
3. Bake the percentile-band targets into the success criterion of
   the policy MVP. "Did this experiment work?" answered by: "did the
   trio stay within its bands?" — never by "did wear go down?"

### Dependencies

- Idea #7 (perf + wear SLO) — defines the *goal* the proxies bracket
- Idea #9 (A/B framework) — proxies are consumed here per experiment
- Idea #26 (closed-form equilibrium) — provides B/T, the EMR denominator
- Idea #24 (write burstiness) — feeds the classifier whose quality WAR measures

### Estimated effort

- Define proxy formulas + emit telemetry: 3 days
- Plumb proxy panels into A/B framework + per-phase confidence
  intervals: 1 week
- Document percentile-band success criterion + rewrite existing
  experiment scripts to use it: 3 days
- **Total: ~2 weeks**

### Paper framing

TMTS gets credit as the operational template. The contribution is
porting the *bracket-the-goal-with-proxies* discipline from
bandwidth-constrained tiering to endurance-constrained tiering, with
proxies derived from the closed-form Lagrangian (idea #26). The
percentile-band success criterion is what makes the policy claim
defensible at the figure level: every plot states success-or-not
against the same operational rule, not a per-experiment fudge.

---

## Dependency graph

```
   #10 (HW-SW contract framing — meta, applies to everything)
                          │
                          ▼
   #25 (PLUG-IN DEPLOYMENT — integrate with Memtis/HeMem/TPP/Colloid/Alto)
                          │
                          ▼
   #9 (A/B framework, with NoLtRAM baseline)
                          │
                          │ enables defensible claims
                          ▼
                          #7 (perf+wear SLO)
                                      │
                                      │ defines budget
                                      ▼
                          #26 (CLOSED-FORM EQUILIBRIUM)
                          says WHAT RATE the migration should target
                                      │
                                      ▼
                          #11 (TOKEN-BUDGET FRAMEWORK)
                          says HOW MANY to migrate (enforces #26's rate)
                                      │
                                      ▼
                          #22 HISTOGRAM-BASED THRESHOLD
                          says WHICH to migrate (top-K from distribution)
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        │                             │                             │
        ▼                             ▼                             ▼
  Gates (cheap)               Tracking signals           Ranking inputs
  #21 demotion history        #20 portable sampler       - write reuse distance
      + VMA write rate            ├── PEBS backend       - ML probability
                                  ├── SPE backend        - 2Q/LIRS/ARC
                                  ├── FPGA backend       - cost-benefit
                                  └── soft-dirty fallback - #24 BURSTINESS (NEW)
                              #12 PEBS-specific config
                              #13 R/W counter separation
                                      │
                                      ▼
                              Granularity-aware migration
                              #23 erase-block skewness handling
                                      │
                                      ▼
                              Mechanisms / constraints
                              #1 COW window
                              #4 spatial confinement
                              #5 hint-aware policy
                              #8 cold-start window
                              #17 selective management
                              #14 DMA migration
                              #15 multi-thread architecture
                              #16 lazy cooling
                                      │
                                      ▼
                              HW substrate
                              #2 async TLB
                              #3 dual-mapping LLC
                              #6 HW cost quantification
```

**Layers (top to bottom):**
- **#10** is the dissertation-wide framing; informs every decision below.
- **#9** is eval infrastructure; needed before any policy claim is
  defensible.
- **#7** is the metric layer; the wear SLO defines the token bucket rate
  that #11 consumes.
- **#11 is the policy framework.** Time-threshold T is demoted to a
  baseline. Ranking choice is the substantive policy decision.
- **#21 (gates)** filter the candidate set *before* ranking — cheap
  signals (demotion history, VMA write rate) that work without any
  sampling. Their presence is the difference between graceful and
  brittle behavior when PEBS isn't available.
- **#20 (portable sampler)** is the abstraction; #12 is the PEBS
  backend specifics; #13 is the R/W counter consumer. Soft-dirty
  fallback works on any substrate including Enzian.
- **#15** is the daemon architecture that hosts #11–#13–#20–#21 cleanly.
- **#1, #4, #5, #8, #17** are mechanisms / constraints / gates that the
  policy uses or respects.
- **#14, #16** are concrete kernel mechanisms that make the policy
  efficient (DMA migration, lazy cooling).
- **#2/#3** build the HW substrate that all the above runs on.
- **#6** is HW-cost paper-polish once HW is locked.
- **#18, #19** are paper / eval discipline (sensitivity studies +
  substrate characterization §2).

**Graceful-degradation principle (key dissertation framing):**

The architecture is structured so that **removing the expensive sampling
(#20 PEBS backend, #12) still leaves a working policy** that uses
#21's cheap gates + soft-dirty fallback. Each layer adds capability
without depending on it being present:

- Without PEBS: gates + soft-dirty + token budget = still works,
  slightly lower fidelity
- Without ranking ML model: gates + reuse-distance ranking = still works
- Without HW substrate (#2, #3): software-only migration with COW = still
  works, with some downtime

This is the **substrate-portable** story that distinguishes the
dissertation from HeMem.

---

## Recommended sequencing

**Phase 0 — Lock the framing (week 0, ongoing):**
- Idea #10 (HW-SW contract framing) — apply across all paper drafts,
  talks, and design choices

**Phase 1 — Eval infrastructure + policy framework + daemon architecture (5–6 weeks):**
- Idea #7 (perf + wear SLO) — defines budget for #26
- Idea #9 (A/B framework, **with NoLtRAM baseline**) — paired evaluation
  with the null-hypothesis baseline mandatory in every figure
- **Idea #28 (proxy-metric trio LTRR/EMR/WAR + percentile-band success
  criterion) — turns the abstract SLO into per-experiment pass/fail
  rules. TMTS pattern, ports cleanly to single-node phase-replay**
- **Idea #26 (closed-form equilibrium) — derive target wear rate
  from endurance budget; build watermark controller. This is the
  theoretical foundation Colloid pattern gives you**
- Idea #11 (token-budget framework, MVP) — enforces the rate from
  #26; ranking by reuse-distance + Belady oracle baseline
- **Idea #22 (histogram-based threshold) — provides the ranking cut
  for the token budget; replaces fixed-T baseline with adaptive cuts**
- **Idea #24 (write burstiness signal) — the new ranking dimension
  beyond raw write count; aligns with Soar/Alto's metric attack pattern**
- Idea #13 (read/write counter separation) — cheapest 10× wear win,
  works without PEBS
- **Idea #21 (cheap distinguisher gates) — demotion history + VMA write
  rate; the residual-signals story that works on every substrate**
- Idea #15 (multi-thread architecture in simulator) — structural prep
  for kernel implementation
- Result: theoretically-grounded policy framework + structural
  architecture; ready to drop in upgrades

**Phase 2 — Validate the core hypothesis (4–6 weeks):**
- Idea #1 (COW transition window) — token-refund mechanism for #11
- Idea #4 (spatial confinement) — orthogonal, decouples decision space
- Idea #8 (cold-start window) — orthogonal, low-effort win
- Idea #17 (selective management) — skip-small-allocations gate
- Idea #16 (clock-based lazy cooling) — eliminates sweep overhead
- Result: clear before/after on wear and utility under the token-budget
  framework, no HW changes needed

**Phase 3 — Ranking sophistication + mechanism upgrades (3–4 weeks):**
- Idea #11 option (b): ML-probability ranking (per Option A plan)
- Idea #11 option (d): queue-based ranking (LRU/2Q/ARC baselines)
- Idea #14 (DMA-based migration) — kernel-side mechanism upgrade
- **Idea #23 (erase-block skewness handling) — Memtis-style splitting
  applied to NOR's endurance constraint; genuinely new contribution
  direction**
- Head-to-head comparison of ranking options under fixed token budget

**Phase 4 — Build the HW substrate (8–12 weeks):**
- Idea #2 (async invalidation) — needs dual-mapping or fallback
- Idea #3 (dual-mapping migration) — HW co-design, longest pole
- **Idea #20 (portable sampling abstraction) — define the interface
  first, even if only the soft-dirty fallback backend exists**
- Idea #12 (PEBS / SPE / FPGA access tracking) — gated on hardware
  availability; on Enzian, this is the FPGA contribution and slots in
  as a backend of #20

**Phase 5 — Paper polish + plug-in deployment (5–6 weeks):**
- Idea #5 (hint accuracy) — Pareto-frontier story
- Idea #6 (HW cost quantification) — credibility story
- Idea #18 (sensitivity studies) — every parameter gets a sweep
- Idea #19 (substrate characterization §2) — consolidate existing data
  into paper-ready §2
- **Idea #25 (plug-in deployment) — integrate with Memtis, HeMem, TPP
  to demonstrate that your endurance regulator enhances every existing
  tiering system. The "we enhance everyone" story is much stronger than
  "we replace everyone"**

**Phase 6 — Generalization (ongoing):**
- Cross-workload validation
- Production-style fleet measurement framing
- Comparison against TMO, TPP, Memtis, Lagar-Cavilla, **HeMem**
- Threshold-T policies cited as historical baseline

---

## What's NOT on this list (and why)

Ideas from `ltram_idea_elaborations.md` that aren't yet promoted to
implementation:

- **Per-class policy with per-class T** — already partially done via
  C1-C4 classification; not a new build.
- **Counter splitting for write tracking** — premature; needed only
  when scaling beyond single-process. Revisit if/when porting to kernel.
- **Adaptive per-page T (AIMD-style)** — promising but lower priority
  than idea #1's deferred-commit, which captures most of the same
  benefit.
- **Lazy migration with eligibility queue** — superseded by idea #1,
  which is a more principled version of the same idea.
- **Predictive ML / decision tree (Option A)** — see `ltram_idea6_optionA_plan.md`
  for the full plan; not yet started because the simpler idea #1 may
  be sufficient. Revisit after Phase 1 results.
- **Compiler / runtime hint API** — covered as Mode B/C of idea #5.
