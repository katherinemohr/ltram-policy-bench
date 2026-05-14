# Meta-patterns for systems research

A growing catalog of *mental moves* extracted from systems papers. These
generalize beyond their source papers — they apply to any systems-research
problem where you're trying to beat an entrenched, complex baseline.

Sources so far:
- LLFREE (USENIX ATC '23) — patterns 1–10
- Contiguitas (ISCA '23) — patterns 11–20
- Software-Defined Far Memory (ASPLOS '19, Google) — patterns 21–30
- HeMem (SOSP '21, UT Austin + Microsoft) — patterns 31–39
- Memtis (SOSP '23, Sungkyunkwan + Virginia Tech) — patterns 40–45
- Tiered Memory Beyond Hotness / Soar+Alto (OSDI '25, Virginia Tech) — patterns 46–50
- Colloid (SOSP '24, Cornell) — patterns 51–55
- NOMAD (OSDI '24, UT Arlington + Intel Labs) — patterns 56–60

---

## 1. Walk through every existing complexity and ask "why does this exist?"

LLFREE §2 is a forensic accounting of every Linux mechanism (per-CPU caches,
high-atomic page blocks, memory compaction, struct page) and shows each was a
*patch* for an earlier patch. The whole stack collapses if you remove the
original assumption (lock contention).

**Application:** when a system has 5 layers of "fixes," go to the bottom of the
stack and ask whether the original problem still needs solving the same way.

---

## 2. Find the orthogonal goal that's secretly aligned

Persistence and scalability *look* like they trade off (logging vs throughput).
Discovering they're aligned (both want lock-free + atomic primitives) is the
paper's core unlock.

**Application:** always ask — what other property would I get *for free* if I
solved this scalability/correctness/efficiency problem? Two-for-one designs are
how second-rate ideas become first-rate papers.

---

## 3. Pick the hardware unit as your design unit

Every size in the design (32 children, 512 bits, 64 B alignment, 16-bit CAS) is
dictated by hardware. They didn't pick "convenient" sizes — they picked sizes
where atomics were free.

**Application:** start with the hardware constraints (cache line, erase block,
page size, NUMA boundary) and work *up*. Don't design clean abstractions and
then deal with hardware as an afterthought.

---

## 4. Constructive avoidance > corrective machinery

Don't add compaction to fix fragmentation; arrange allocations so fragmentation
can't happen. Don't add WAL to fix crashes; structure operations so partial
states are impossible. Each layer of corrective machinery is a code smell.

**Application:** when reaching for a "fixer" (defrag, GC, WAL, retry, repair
pass), pause and ask whether the bad case can be designed out instead.

---

## 5. Quantify the trade-off, then accept the loss

LLFREE doesn't claim zero crash loss — they bound it (≤#CPUs frames) and
compare to the cost of avoiding it (logging on every op). Once numbers are on
the page, the decision is obvious.

**Application:** vague "we want correctness" loses to "we accept ≤N lost frames
per crash, here's why and here's the cost of the alternative." Always make the
loss numerical and bounded.

---

## 6. Invent the right axis for comparison

State-dispersion isn't from prior work. They needed a number that captured
"memory touched per operation" because raw metadata size doesn't capture cache
cost. When existing benchmarks don't show your story, define a new one — but
justify it.

**Application:** if your contribution is invisible on standard metrics, the
metric is wrong, not the contribution. Inventing the right metric is half the
paper.

---

## 7. Test the structurally hard cases, not just average

Bulk + Random + Repeat span the *adversarial* space (Repeat is best-case for
Linux; Random is worst-case for LLFREE). They pick benchmarks that *expose*
trade-offs, not hide them.

**Application:** a flat "we win everywhere" story is suspicious. A "we win
where it matters, lose where it's least common" story is honest and stronger.
Pick benchmarks that could embarrass you.

---

## 8. Use a forcing function for design discipline

"Single cache line per transaction" is a constraint that *forces* good
decisions. Without it, they would have added auxiliary state and lost the
property.

**Application:** pick one constraint and refuse to violate it. The constraint
prunes the design space and surfaces second-best solutions you would have
otherwise drifted into.

---

## 9. Co-design seemingly orthogonal goals from the start

Persistence + scalability isn't bolted on — both fall out of the single
cache-line atomic discipline. Adding either as an afterthought would have
destroyed the design.

**Application:** if you suspect two goals are aligned, design for both
simultaneously from day one. Sequential design will pick a path that satisfies
the first goal at the cost of the second.

---

## 10. Identify implicit assumptions to break

Linux assumes you need per-CPU caches for scalability. LLFREE breaks this
assumption and the design simplifies dramatically.

**Application:** for any "obviously needed" mechanism in the baseline, ask why
it's needed. If the answer is a chain of three "becauses," there's likely a
shorter path that skips them.

---

# From Contiguitas (ISCA '23)

## 11. Measure before you design

Section 2 of Contiguitas is the paper's foundation: sampled tens of thousands
of Meta servers, did full physical-memory scans, computed contiguity CDFs at
multiple sizes, traced unmovable allocations to source. The 23% / 73% / 31%
numbers are *load-bearing*. Without them, the design is "we built a cool
thing." With them, it's "we solved a measured, fleet-scale problem."

**Application:** spend 1–2 weeks measuring the actual problem before sketching
a design. Quantify how often it occurs, what fraction of cases it affects, and
what the dominant sources are. The measurement isn't preamble — it *is* part
of the contribution.

---

## 12. Constraint relaxation as a primitive

"Make X movable" was hard. Contiguitas reframed it as "make X not matter for
the rest of memory." Same goal, different angle. They didn't solve the original
problem; they reframed what counts as a solution.

**Application:** when stuck, ask whether the constraint can be loosened
instead of the solution tightened. Reframing what counts as a solution is
often easier than solving the original problem.

---

## 13. HW/SW co-design unlocks designs neither alone can achieve

Software alone can't migrate in-use unmovable pages (can't atomically update
PTE + copy data). Hardware alone can't decide which pages to migrate.
Together, they can.

**Application:** when you hit a software wall, ask what *minimal* HW
extension would unlock the design. Not a major ISA change — a small surgical
addition. Conversely, when you hit a HW limit, ask what minimal OS support
would close the gap.

---

## 14. Decouple coordination from correctness

Synchronous TLB shootdowns are slow because they require global coordination.
Contiguitas makes both mappings valid concurrently, so the system is *correct
without coordination*. Coordination becomes optimization, not correctness.

**Application:** in any system with coordination bottlenecks, ask whether
correctness *actually* requires the coordination. Often it doesn't — it just
makes the design simpler. The complex-but-coordination-free version is often
strictly better. This is how nearly every great scalability win works.

---

## 15. Invert hazards into mechanisms

Aliasing in cache design is normally a coherence hazard; in Contiguitas it's
the migration primitive. False sharing is normally bad; in LLFREE's counter
splitting it's deliberately avoided per-CPU.

**Application:** when something is universally treated as "bad," ask in which
contexts it's actually useful. Those contexts are often unexploited design
space.

---

## 16. Pick the smallest HW delta

Contiguitas piggybacks on existing BusRdX, PCD bit, ENQCMD, IOMMU. The HW
addition is 0.014% of a core's area. Minimal HW delta = realistic adoption
story = paper is taken seriously.

**Application:** constraints on what HW you can change become a *feature*:
they force designs that can ship. "We added a new ISA instruction" is a
decade-long path; "We reused an existing bit" is years.

---

## 17. Show the trade-off space, not just the optimum

Contiguitas presents two design points (cacheable vs noncacheable LLC
behavior) and multiple sizing options. Not "the optimal point."

**Application:** reviewers will argue with a single point; they won't argue
with a Pareto frontier. Showing the trade-off is more honest and more useful.
It also turns weak spots into design choices.

---

## 18. Quantify cost at the unit you'll be challenged on

HW vendors challenge HW papers on mm², nJ, mW. OS people challenge OS papers
on cycles or ns. Contiguitas reports area (0.0038 mm²), energy per access
(0.0017 nJ), leakage (0.64 mW). LLFREE reports cache lines accessed.

**Application:** match the unit to the audience you need to convince. "Modest
cost" doesn't convince anyone; numbers do. Pick the unit ahead of time and
design your evaluation to produce it.

---

## 19. Test in production, not just in simulators

Contiguitas built the OS component into Linux 5.12 and ran it under real Meta
production traffic with A/B testing. The HW component was simulated (you can't
run hypothetical chips).

**Application:** different validation tools for different scopes. Be explicit
about which results came from production, which from simulation, which from
microbenchmarks. The honesty of the framing is part of the trust the work
earns.

---

## 20. Frame your work as an enabler, not a competitor

Contiguitas's closing framing: "we enable a plethora of prior and future
works that aim to tackle address translation overhead." They position their
work as the substrate that lets a whole subfield work better.

**Application:** when positioning a contribution, ask whether it makes other
people's work *better* (enabler) or *obsolete* (competitor). Enablers get
cited and adopted; competitors get attacked. Choose the framing that earns
you allies.

---

# From Software-Defined Far Memory (ASPLOS '19, Google)

## 21. Use existing infrastructure as your "novel" tier

zswap was a 7-year-old Linux feature when Google shipped Software-Defined
Far Memory. Their contribution is using it *proactively* and *with a
controller*, not inventing it. The hardware delta is zero.

**Application:** novelty isn't always new mechanism; sometimes it's new
orchestration of existing ones. Before proposing new infrastructure, ask
what underutilized existing infrastructure could be repurposed.

---

## 22. Define a measurable, application-agnostic SLO via a proxy metric

They can't define "did we slow your application down?" for thousands of
heterogeneous workloads. So they define **promotion rate** (pages moving
far→near per minute, normalized to working set) as a proxy. It correlates
with slowdown but is measurable by the OS without per-app instrumentation.

**Application:** if you don't have a single load-bearing metric that
correlates with what you actually care about, you don't have a deployable
policy. The metric design *is* the policy design.

---

## 23. Separate online from offline by ~1000×

Online things (kernel daemons, node agent) run every minute. Offline
things (ML autotuner, fast simulator) run nightly or weekly. The
simulator processes a week of fleet behavior in an hour.

**Application:** identify what needs to be fresh (online) and what only
needs to be approximately current (offline). Don't put cluster-scale or
fleet-scale work in the per-event control loop.

---

## 24. Build a fast simulator of your own policy. Use it for everything.

The "fast far memory model" lets them tune parameters, do ablations,
project savings, and defend choices — all offline at fleet scale, risking
nothing in production.

**Application:** the simulator is a research artifact in itself, often
more valuable than the production deployment because it scales infinitely
and is risk-free. Build it deliberately and treat it as first-class
infrastructure, not a one-off script.

---

## 25. Use ML where the problem is genuinely a black-box

Their policy (compress cold pages older than T) is interpretable and
hand-tunable. Their *hyperparameter search* (find K and S given fleet-wide
objectives) is high-dimensional, non-convex, expensive to evaluate. That's
the right place for ML.

**Application:** don't ML the parts you can reason about; ML the parts
you can't. Policy stays interpretable; meta-optimization is opaque but
bounded.

---

## 26. Quantify worst-case behavior in the controller design

K-th percentile control gives explicit worst-case SLO violation rate of
(100−K)%. They don't claim "never violates SLO"; they claim "violates at
a known, bounded rate."

**Application:** bounded badness beats unbounded goodness in production
design. Make the worst case a number you can defend. "Almost always
works" is not a contract; "violates SLO at most 5% of the time" is.

---

## 27. Pick the cluster-economic optimum, not the machine-local optimum

Killing a failing job and restarting it elsewhere beats keeping it alive
with extra compression. Cluster-level math wins because cluster-level
schedulers exist.

**Application:** in any multi-tenant setting, "what's locally optimal"
and "what's globally optimal" diverge. Design for the latter and let the
machine accept inefficiency that the cluster cleans up.

---

## 28. Reframe "what is a tier?"

A tier is whatever the policy treats as separate cost/performance.
Compressed pages in the same DRAM are a tier. Remote memory is a tier.
NOR-backed memory is a tier. The tier abstraction is more flexible than
people assume.

**Application:** when designing for a new memory class, don't restrict
"tier" to "different DIMM type." A tier can be a different *use* of the
same hardware, or a different HW/SW *contract* on existing hardware.

---

## 29. Cluster-level stability hides machine-level variability

Per-machine cold-memory ratio varies 1–52%. Cluster-wide it's stable at
~32%. They provision at cluster level (stable) and react at machine level
(dynamic).

**Application:** the right level for provisioning vs. policy is rarely
the same. Find the level at which your metric is stable; provision there.
Find the level at which it varies; react there. The two-level split is
load-bearing.

---

## 30. Time-to-deployment is itself a design constraint

They chose zswap over NVM partly because zswap shipped today. NVM would
have taken years. They quantify and report this trade-off explicitly.

**Application:** the faster path to production is part of the value of
the design. If your work is 5× slower to deploy than an existing solution,
that's a 5× cost. Account for it explicitly when comparing approaches.

---

# From HeMem (SOSP '21, UT Austin + Microsoft)

## 31. Test against real hardware, not emulation

Prior tiered-memory work (Thermostat, X-Mem, HeteroOS, Nimble) evaluated
on emulated NVM and claimed software tiering was viable. HeMem evaluated
on real Optane DC and found those systems don't scale. *Emulation
flatters bad designs.*

**Application:** when real hardware becomes available for the substrate
you're targeting, re-evaluate everything. Emulation hides characteristics
(media access granularity, asymmetric bandwidth, conflict-miss behavior)
that drive real design choices. If you're stuck with emulation, validate
the assumptions empirically wherever possible.

---

## 32. Hardware counters are a free observation resource

PEBS has existed in every Intel CPU since Nehalem (2008). HeMem uses it
to track memory accesses as a stream of sampled addresses, eliminating
the need for page-table scans. Cost shifts from O(memory size) to
O(access rate) — naturally scalable.

**Application:** when your observation cost is high, check what the
hardware is already producing for free. Performance counters, branch
predictors, cache miss events, IBS/PEBS samples — all of these are
"telemetry exhaust" most OS designs ignore. Using them is often a
better trade than instrumenting in software.

---

## 33. Make every synchronous operation asynchronous

Sampling, policy, page-fault handling, memory copy — each runs in its
own context at its own pace, with no cross-blocking. PEBS thread,
policy thread (10 ms tick), userfaultfd thread, DMA engine for copies.
The pattern: identify every blocking operation and ask whether
correctness actually requires it to block.

**Application:** this is meta-pattern #14 (decouple coordination from
correctness) applied at higher density. For LtRAM: sampling, policy,
migration, write-fault handling, wear telemetry should each be its own
context. Synchronous coupling between them is a code smell.

---

## 34. Asymmetry is a feature to exploit, not an inconvenience to hide

Optane writes are 7× slower than reads. Rather than treating all
accesses uniformly, HeMem maintains separate read and write counters
per page, with different thresholds (read = 8 samples, write = 4
samples). Write-heavy pages get priority for DRAM. Result: 10× less
NVM wear.

**Application:** when your substrate is asymmetric (read vs. write
latency, read vs. write endurance, hot vs. cold cells), the policy
should expose and exploit the asymmetry. The biggest endurance wins
in LtRAM will come from this discipline.

---

## 35. Selective management beats universal management

Don't manage memory that doesn't need managing. Small / ephemeral data
stays in DRAM by default — HeMem only intercepts allocations ≥ 1 GB.
Overhead is paid only where benefit exists.

**Application:** the opposite of "treat all memory uniformly." This is
the same insight as constructive avoidance (meta-pattern #4) applied to
overhead: don't run the policy machinery where you already know the
answer. For LtRAM, this means skipping small allocations, init-phase
pages, and pre-classified C1/C4 pages from the policy pipeline.

---

## 36. Mechanism/policy split: kernel for mechanism, user-level for policy

HeMem puts policy in a 4,177-line user-space library and uses 1,337
lines of kernel patches to expose mechanisms (userfaultfd, DAX,
ioatdma). This lets per-app policies coexist without per-app kernel
patches.

**Application:** when policy is expected to vary (per app, per
workload, per phase) and mechanism is stable (page faults, DMA, page
mapping), put policy at user-level. The Exokernel argument, modernized.
For LtRAM: if your eventual policy needs to differ per workload, this
split is worth the user-level cost.

---

## 37. Defend every parameter with a sensitivity study

Every magic number in HeMem (sampling period 5,000; hot read threshold
8; hot write threshold 4; cooling threshold 18) has a sensitivity
sweep (Figures 10, 11, 12). The reader sees the plateau and understands
the choice.

**Application:** sensitivity studies are cheap to produce and
disproportionately defensive. Reviewers can't say "you cherry-picked
X" when the figure shows X is in the middle of a plateau. For LtRAM,
sweep T, T_commit, T_post, token bucket capacity, ranking thresholds —
every parameter that survives to the paper.

---

## 38. Substrate characterization is part of the contribution

§2.2 of HeMem (the Optane performance profile) is a paper-within-the-
paper. It's cited heavily by downstream work as the canonical Optane
characterization. The characterization isn't preamble; it's a
research artifact.

**Application:** when your work depends on a substrate that hasn't
been thoroughly characterized in the literature, measure it yourself
and publish the characterization. The characterization paper-within-the-
paper anchors your design choices and becomes its own contribution.
For LtRAM, this is your NOR-on-NUMA emulation characterization plus
any real-hardware measurements you can get.

---

## 39. Real-workload benchmarks across orthogonal categories

HeMem evaluates on transactional DB (Silo TPC-C), latency-sensitive
KVS (FlexKVS), and graph processing (GAP). Each stresses a different
dimension; microbenchmarks (GUPS) supplement, not replace.

**Application:** pick benchmarks that cover the orthogonal axes of
your problem space. For tiered memory: one throughput-bound, one
latency-bound, one bandwidth-bound. For LtRAM: also one
write-intensive (to stress endurance) and one read-mostly (to stress
utilization).

---

# From Memtis (SOSP '23, Sungkyunkwan + Virginia Tech)

## 40. Distribution-aware decisions beat threshold-based decisions

The Memtis core insight: when your data has a natural distribution
(Zipf, Pareto, normal, etc.), don't pick a fixed cutoff. Use a
histogram and *derive* the cutoff from the distribution itself. HeMem
says "hot = 8 accesses." Memtis says "hot = whatever bin cuts fast-tier
capacity exactly." No magic number to tune per workload.

**Application:** any time you find yourself writing "if X > THRESHOLD"
with a magic-number THRESHOLD, ask whether THRESHOLD should come from
a histogram of X across the population. The histogram is cheap to
maintain; the threshold becomes free and adaptive.

---

## 41. Match the data structure to the data distribution

Memtis uses exponential bins because access counts follow Zipf/Pareto
distributions. Linear bins would have terrible resolution at the low
end and saturate at the high end. The data structure shape mirrors
the data shape.

**Application:** log-scale histograms for log-scale data, linear for
linear. For LtRAM, write reuse distances are heavy-tailed — use
exponential bins. Wear distribution across cells should be roughly
normal under good wear leveling — linear bins suffice there.

---

## 42. Cooling via structural shift, not iteration

Memtis halves all access counts by shifting all bin values left by one
in an exponential-bin histogram. O(1) instead of O(N_pages). The
structural property of the data structure does the work that iteration
would otherwise do.

**Application:** any time you see "iterate over all N items and apply
operation X to each," ask whether the data structure can do X in
aggregate. Histograms support cooling, sketches support merging,
append-only logs support truncation — all O(1) at the data level even
when O(N) at the item level.

---

## 43. Ask "what's missing?" not "what's wrong?"

Memtis doesn't fix prior systems' bugs — it identifies what they
*lack* (distribution information, subpage granularity, dynamic
adaptation) and supplies it. Both missing pieces are absences, not
errors. Adding them gives the win without re-implementing anything in
the baseline.

**Application:** when you study a baseline, list what *information*
it doesn't use. Pages have access histories — used. Allocation contexts
— used? Neighbor relationships — used? The unused information is the
next contribution. This is structurally different from "fix bug X" or
"improve metric Y."

---

## 44. Two complementary metrics across orthogonal axes

Memtis tracks hotness (access frequency) AND skewness (subpage
uniformity). They're orthogonal: a page can be (hot, uniform),
(hot, skewed), (cold, uniform), or (cold, skewed). Only the
(hot, skewed) case triggers splitting. Prior work used hotness alone
and missed the second axis.

**Application:** for LtRAM you already have hotness (write count) and
recency. What's the third orthogonal axis? Spatial concentration?
Temporal burstiness? Allocator lineage? Picking the right second
(or third) axis is the contribution that nobody else has.

---

## 45. Top-K hottest, not just "hotter than T"

Threshold-based classification ("hot if access count > 8") doesn't
order within the hot set. If 1000 pages exceed the threshold but only
500 fast-tier slots are available, you pick 500 arbitrarily.
Histogram-based classification gives you exactly the top-N for your
fast-tier size, in order.

**Application:** for any budgeted resource allocation (your token
budget, ranking under wear constraint, etc.), the histogram gives
top-K for free. The budget says *how many*; the histogram says
*which*. Don't conflate the two.

---

# From Tiered Memory Management Beyond Hotness / Soar+Alto (OSDI '25, Virginia Tech)

## 46. Question the metric, not the algorithm

Memtis improved how hotness is thresholded. HeMem improved how
hotness is sampled. TPP improved how hotness-driven migration is
implemented. Soar/Alto attacks the metric itself: **hotness is
wrong.** When a whole subfield uses one metric, the highest-leverage
move is to question the metric. Algorithmic improvements stack
linearly; metric improvements multiply across the field.

**Application:** for any "obviously needed" metric in your subfield,
list the cases where it's misleading. Hotness is misleading when MLP
masks latency. Write-frequency is misleading when writes cluster
temporally. Each "misleading" case is a contribution candidate.

---

## 47. Hardware-independent / workload-independent calibration

Soar/Alto's K = f(AOL) curve uses constants that are platform-specific
but workload-independent. Calibrated once per machine via two
microbenchmarks at the MLP extremes (sequential + pointer-chase).
Model generalizes across 56 workloads.

**Application:** a model calibratable from microbenchmarks is far
more deployable than one needing per-workload tuning. Same insight as
Lagar-Cavilla's K-th-percentile control (hardware-derived rate) and
LLFREE's atomic-primitive sizes (cache-line-derived). Always ask:
can these constants be derived from substrate properties rather than
tuned per workload?

---

## 48. Run the no-baseline baseline

The NoTier comparison is what makes Soar/Alto credible. Showing your
system beats Nomad/TPP/etc. is fine. Showing your system also beats
"no tiering at all" is essential. Memtis omitted this baseline and
their paper is harder to interpret as a result.

**Application:** for any new mechanism, always include the "do
nothing" baseline. If your mechanism can't beat doing nothing for at
least some workloads, you don't have a contribution. This is the
null hypothesis your work has to clear.

---

## 49. Plug-in design as the integration story

Soar replaces allocation; Alto enhances migration. Alto integrates
with four different tiering systems (TPP, NBT, Nomad, Colloid) in
≤30 LOC each. A plug-in is a much easier sell than a replacement.

**Application:** don't pick sides in the existing-systems war; design
your contribution as a regulator/enhancer that plugs into multiple
existing systems. Reviewers can't object that you chose the wrong
baseline because you enhance every baseline. Same dissertation move
as positioning yourself as an enabler (meta-pattern #20).

---

## 50. Counter-intuitive demos force engagement

Figure 1 of Soar/Alto: "hot-on-DRAM is WORSE than cold-on-DRAM by
34%." Single microbenchmark, complete refutation of conventional
wisdom. Memorable, reproducible, forces the reader to engage with
the argument.

**Application:** find one counter-intuitive headline demo for your
work. For LtRAM: "the page with 100× more writes goes to LtRAM and
the page with fewer writes stays in DRAM, and we're right because
the high-write page's writes cluster" is your candidate. One demo
like this is worth ten incremental improvements for paper acceptance.

---

# From Colloid (SOSP '24, Cornell)

## 51. Use closed-form optimality conditions when available

Colloid's equilibrium (L_D = L_A) is derived from first principles:
minimize average latency subject to placement constraints. The math
is one line. Compare to Memtis's heuristic histogram thresholds, or
Soar/Alto's empirical K(AOL) curve.

**Application:** when designing a policy, ask "does this optimization
have a closed-form equilibrium?" before falling back to heuristics or
learned models. No magic constants, no curve fitting, no per-platform
calibration. For LtRAM: "uniform wear distribution + total wear rate
= endurance budget rate" is your candidate closed-form equilibrium —
derivable from the constraint, not a tuned threshold.

---

## 52. Measure, don't predict

Colloid measures latency directly from CHA counters via Little's Law.
It doesn't predict bandwidth saturation, doesn't model interconnect
behavior, doesn't fit curves. **The measurement IS the signal.**
Compare to Soar/Alto's AOL predictor (more accurate but harder to
deploy) or bandwidth-centric policies (BATMAN) that predict latency
from bandwidth ratios.

**Application:** when a signal can be measured directly via available
hardware/software counters, measure it. Don't build a predictive
model unless measurement is impossible or prohibitively expensive.
Direct measurement doesn't drift, doesn't need recalibration when
substrate changes, and is faster than running a predictor.

---

## 53. Little's Law as a deployment-ready measurement primitive

L = O / R works for any stable queueing system, no assumptions about
arrival or service distributions. CHA gives you O and R for free.
Little's Law turns any queueing system into a measurable system.

**Application:** any time you have queue-like behavior in your system
(request queue, migration queue, write buffer, erase queue), check
whether Little's Law gives you a useful latency or throughput signal.
The counters often exist already; the formula is a one-liner.

---

## 54. Binary search via watermarks for convergence under noise

Two watermarks bound the unknown equilibrium. Each quantum: tighten
one watermark based on measurement, move toward midpoint. Reset both
when watermarks converge but equilibrium hasn't been reached
(equilibrium has moved). Clean, deterministic convergence algorithm
without hyperparameters like learning rate.

**Application:** any time you're converging to an unknown but
measurable equilibrium under noisy measurements, watermark-binary-
search is a strong default. Cleaner than gradient descent (no
learning rate), more responsive than EWMA-only smoothing. Reset-on-
drift handles non-stationary workloads without explicit phase
detection.

---

## 55. A unified framework that subsumes special cases

"Balance access latencies" handles unloaded-latency-driven placement
(L_D < L_A always → pack hot in D), bandwidth-driven placement
(saturation inflates L_D), and contention-driven placement (queueing
inflates L_D without bandwidth saturation). One principle, many
regimes.

**Application:** when you have a metric that captures everything you
care about, your policy collapses to a single principle. For LtRAM:
if you can find one metric that captures wear, performance, and
migration cost simultaneously, your policy framework collapses
dramatically. This is also a paper-writing move — "we replace N
heuristics with one principle" is a much cleaner narrative than
"we improve heuristic K."

---

# From NOMAD (OSDI '24, UT Arlington + Intel Labs)

## 56. Optimistic concurrency control for the OS

NOMAD's transactional page migration (TPM) is a database-style OCC
pattern (start without locking; commit only if no conflict) applied to
OS memory management. The OS community typically uses pessimistic
locking ("unmap before copy"). Bringing OCC across domains gives a
clean win: the page stays accessible during the operation, and the
only cost of a failed commit is one wasted copy.

**Application:** any time you have a long-running OS operation that
locks resources for its full duration, ask whether OCC applies. For
LtRAM: the COW transition window (idea #1) is your equivalent — defer
the commit, check for conflicts at the end, abort if any. NOMAD
validates this pattern works in real Linux kernels with measurable
performance gains.

---

## 57. The third design point between two extremes

Inclusive vs. exclusive caching has been a binary debate for decades.
NOMAD finds a *third* point: non-exclusive (shadows only after
promotion). The third point inherits useful properties from both
extremes without their full costs.

**Application:** when a subfield is stuck in a binary choice (write-
back vs write-through, eager vs lazy, push vs pull, exclusive vs
inclusive), look for the asymmetric middle. For LtRAM: "migrate but
rollback-able" is the third point between "migrate" and "don't
migrate." Your COW transition window (idea #1) already lives there;
this pattern gives it explicit framing.

---

## 58. Asymmetric optimization (pick the direction that matters)

NOMAD makes *demotion* cheap (free PTE remap via shadows) but leaves
*promotion* cost unchanged (still a full page copy). The asymmetry
pays off because thrashing workloads have many demotions per migration
cycle; making the more-common operation cheap wins.

**Application:** before optimizing both directions of a bidirectional
operation, ask which direction dominates in the workloads you care
about. For LtRAM: write-back from LtRAM to DRAM is the rare-but-
expensive direction (one wasted erase). Optimize this direction
first; bulk promotion is easier to amortize.

---

## 59. Page-visible-during-copy (deferred unmapping)

Conventional wisdom: unmap before modifying. NOMAD: copy first, unmap
only on commit. The page is visible throughout the copy because the
copy goes to a *new* physical page — the original is untouched. The
dirty bit check catches any writes during the copy and triggers abort.

**Application:** when an operation modifies data but produces output
in a separate location, the original can stay accessible. The
challenge is the commit point — making the swap atomic. Pair with
async TLB invalidation (Contiguitas's pattern) to make the commit
itself non-blocking.

---

## 60. Report when the baseline beats you

NOMAD's Section 4.2 explicitly shows that for Redis with large RSS,
both NOMAD AND Memtis underperform "no migration." Most tiering papers
omit the no-migration baseline; NOMAD includes it and reports the
loss. Soar/Alto did the same with NoTier.

**Application:** include the do-nothing baseline. If it beats your
policy on some workloads, say so explicitly. Pretending universal
superiority makes reviewers skeptical of every claim. Honest losses
build credibility for honest wins.

---

# From TMTS (ASPLOS '23, Google)

## 61. Bracket the goal with proxy metrics — one per failure mode

TMTS's STAR (Secondary Tier Access Ratio) and STRR (Secondary Tier
Residency Ratio) don't measure the goal directly (cost-perf product
across the fleet). They bracket it: STRR proxies utilization; STAR
proxies degradation. Together they sharpen an otherwise unmeasurable
fleet-wide objective into something every machine can compute and
every operator can read on a dashboard.

**Application:** for LtRAM, define one proxy per failure mode.
Candidates: LTRR (residency in LtRAM, mirrors STRR), EMR
(migration-rate-to-LtRAM normalized to budget B/T, the endurance
overspend proxy), WAR (write-access ratio to LtRAM, the leakage
proxy — should be ~0 for a write-cold tier). State the system
objective as a constraint over the proxies, never the goal.

---

## 62. Operational targets stated as percentile bands, not thresholds

TMTS's success criterion is "median STAR < 0.5%, P95 STAR < 1.5%,
STRR ≈ 25% when memutil > 75%". The bands accommodate workload
diversity — a single global threshold is either too strict or too
loose. The conditional ("when memutil > 75%") prevents trivial
satisfaction at low load.

**Application:** state LtRAM targets as percentile bands with a
load condition: "median EMR < 1.0, P95 EMR < 2.0; median WAR < 0.5%;
LTRR tracking the deployment ratio *when allocated memory exceeds
75%*". The band is the success criterion for every experiment,
not the per-machine state.

---

## 63. Multi-tenancy as a degree of freedom — schedule away from the problem you can't fix

TMTS does NOT try to make the ML training pipeline tier-friendly.
It tags the workload as "tier-unfriendly" in an offline pipeline
and asks the cluster scheduler to keep it off 2-tier machines. The
scheduler operates on minute-to-hour scales, but it doesn't need
to react fast — it just needs to *route correctly* in the first
place.

**Application:** for LtRAM, classify workload phases as
endurance-friendly / endurance-hostile / neutral from historical
WAR + EMR. When the regulator detects an endurance-hostile phase
underway, it can opt out (refuse to migrate) — the analog of
"don't put this job on a 2-tier machine." Don't fight a workload
whose access pattern fundamentally breaks the tiering assumption.

---

## 64. Belt-and-suspenders: pair the timely detector with the eventual detector

TMTS uses BOTH a 30-second A-bit scan AND 1% PEBS LLC-miss sampling
filtered to tier2 loads. Either alone has known failure modes
(scan is page-granular and slow; PEBS is statistical and store-blind).
The union of their candidate sets drives promotion. Combined, median
promotion latency drops from ~25s to <1s.

**Application:** for LtRAM, pair the slow-but-exhaustive A-bit scan
with the fast-but-lossy FPGA tracker (your ThunderX2 analog of PEBS).
Promote any page either source flags. Demotion can rely on the slow
detector alone — cold pages don't care about latency.

---

## 65. Forbid the configuration that hardware feedback can't service in time

TMTS's "NUMA jailing" rule: no demotions to remote-socket tier2.
Reason: the QoS controller's feedback signal can't make the
cross-socket round-trip in time to throttle. When the rule was
violated experimentally, DRAM tail latency rose orders of magnitude
and kernel soft-lockups followed within days. The rule is *negative*
— a refusal — and stronger than any positive optimization.

**Application:** for LtRAM, write the negative rules first. No
cross-NOR-controller migrations (back-pressure too slow). No
direct allocation into LtRAM (no access history at allocation
time). No promotion during an endurance-hostile phase (would just
churn). The negative rules are cheaper to enforce than the
positive policies and prevent the failures that matter most.
