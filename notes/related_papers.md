# Related papers reading list — LtRAM policy work

Curated reading list, ordered by relevance to the LtRAM migration policy
work. Use as a directed syllabus rather than a citation dump.

For each paper: short description + why it matters for your work.

---

## Tier 1 — Closest cousins (read first)

These are direct analogs. Understanding them tells you what ground is
already covered and where your contribution sits.

### Software-Defined Far Memory in Warehouse-Scale Computers
*Lagar-Cavilla et al., ASPLOS '19 (Google)*
- Google's compression-based far-memory tier shipped in production since 2016
- **TMO's predecessor; the foundational WSC-tier-memory paper.** Read this
  before TMO to understand the Meta paper's heritage.
- Read for: promotion-rate SLO design, K-th-percentile threshold controller,
  fast simulator + GP-Bandit autotuner pattern, kstaled/kreclaimd
  architecture, online/offline split. The metric-design lessons here are
  directly transferable to LtRAM.
- **Caution on framing:** their "software-defined" approach uses no HW
  changes. The LtRAM work is *HW-SW co-designed*, not software-defined —
  don't conflate the framings.

### TMO: Transparent Memory Offloading in Datacenters
*Weiner et al., ASPLOS '22 (Meta + CMU)*
- Meta's later work on offloading cold pages to slower tiers via zswap
- Same Meta team as Contiguitas; spiritual sibling of Lagar-Cavilla '19
- **Your closest cousin (alongside Lagar-Cavilla).** Likely already
  establishes the production measurement framework + policy story you
  want to extend with endurance-awareness.
- Read for: how to measure cold pages in production, how to integrate
  with Linux PSI, how to frame a tiering-policy paper for a top venue.

### Reducing DRAM Footprint with NVM in Facebook
*Eisenman et al., EuroSys '18*
- Application-driven NVM use at Facebook (RocksDB)
- The *opposite* design choice from Lagar-Cavilla / TMO — application-aware
  rather than application-transparent
- Read for: trade-offs of application-driven vs application-agnostic
  tiering. The discussion section makes the case explicit.

### HeMem: Scalable Tiered Memory Management for Big Data Applications and Real NVM
*Raybuck et al., SOSP '21 (UT Austin + Microsoft)*
- First tiered-memory paper evaluated on **real Optane DC NVM** rather than
  emulation; finds prior systems (Nimble, X-Mem) don't scale on real
  hardware
- Replaces page-table scanning with **PEBS-based access tracking** (CPU
  hardware events) — O(access rate) instead of O(memory size)
- User-level library + minimal kernel patches; DMA-based migration;
  read/write counter separation; **10× less NVM wear** than memory mode
- **Read this for:** PEBS instrumentation pattern, async-everything
  architecture, mechanism/policy split, read/write asymmetry as policy
  signal, and Optane characterization (§2.2)
- **The substrate characterization in §2.2 is the canonical Optane
  reference** — cite it whenever you discuss real NVM behavior

### TPP: Transparent Page Placement for CXL-Enabled Tiered Memory
*Maruf et al., ASPLOS '23 (Meta)*
- Explicit tier-aware page placement for CXL memory
- Production deployment story
- Read for: tier abstraction at the OS level, hot/cold detection,
  closed-loop tier balancing

### Aqua: Transparent Page Placement for CXL-Enabled Tiered Memory
*Maruf et al., ASPLOS '23 (Meta + collaborators)*
- TPP's sibling — CXL-tiered transparent placement, with sharper
  focus on access tracking and migration mechanism for CXL-attached
  memory at Meta scale
- Cited by TMTS as the comparable Meta system
- Read for: how Meta's framing of the CXL substrate compares to
  Google's Optane-era framing (TMTS); the substrate evolution story
  for your dissertation chapter on related work

### Memtis: Efficient Memory Tiering with Dynamic Page Classification and Page Size Determination
*Lee et al., SOSP '23 (Sungkyunkwan + Virginia Tech + Igalia)*
- The **direct successor to HeMem**. Same problem (tiered DRAM+NVM,
  PEBS-based access tracking) but two new ideas:
  1. **Histogram-based dynamic threshold derivation** — replaces
     HeMem's fixed thresholds (8 reads / 4 writes) with an exponential-
     bin histogram that picks T_hot to exactly fill fast-tier capacity
  2. **Skewness-aware huge page splitting** — uses PEBS subpage-level
     addresses to detect huge pages with skewed subpage access patterns,
     splits them, and migrates only the hot subpages
- Beats HeMem by 33.6% on average with bounded CPU (<3%) and memory
  (<0.195%) overhead
- **Read this for:** the histogram-based threshold pattern (directly
  applicable to your token-budget framework), the cooling-via-bin-shift
  trick, the skewness factor S_i = ΣH²/U² for huge-page splitting
  (analog of NOR erase-block skewness for your work), and the eHR/rHR
  benefit-gap signal for triggering policy actions

### Tiered Memory Management Beyond Hotness (Soar + Alto)
*Liu, Hadian, Xu, Li — OSDI '25 (Virginia Tech)*
- **Attacks the foundational metric of the entire tiering subfield:**
  hotness is not performance-criticality. MLP (memory-level parallelism)
  masks slow-tier latency, so a frequently-accessed high-MLP page is
  cheaper than a rarely-accessed pointer-chase page
- Introduces **AOL = Latency / MLP** computed from 4 PMU counters; uses
  it to rank objects (Soar, offline static allocation) and regulate
  page migrations (Alto, online plug-in for TPP/NBT/Nomad/Colloid)
- Headline: Figure 1 shows "place cold pages on DRAM" beats "place hot
  pages on DRAM" by 34% on their pointer-chase + sequential microbench
- Soar beats TPP by up to 1242%, Nomad by up to 547%
- Alto integrates with 4 tiering systems in ≤30 LOC each
- **Read this for:** (1) the question-the-metric meta-pattern; (2) the
  plug-in design (Alto) as a deployment story; (3) the NoTier baseline
  that prior work omitted; (4) calibration-from-microbenchmarks pattern
  for K = f(AOL); (5) the counter-intuitive demo (Figure 1) as a model
  for your own "write-burstiness > write-frequency" demo

### TMTS: Towards an Adaptable Systems Architecture for Memory Tiering at Warehouse-Scale
*Duraisamy et al., ASPLOS '23 (Google)* — **ANALYZED, meta-patterns 61-65**
- Google's WSC tiering paper, deployed for 2 years across thousands of
  services; **the operational benchmark for any tiering work**
- Targets stated as **percentile bands on proxy metrics**: median STAR
  < 0.5%, P95 STAR < 1.5%, STRR ≈ 25% when memutil > 75%. STAR =
  Secondary Tier Access Ratio (degradation proxy); STRR = Secondary
  Tier Residency Ratio (utilization proxy)
- Hybrid promotion: **30s A-bit scan + 1% PEBS LLC-miss sampling
  filtered to tier2 loads**. Belt-and-suspenders — PMU drops median
  promotion latency from ~25s → <1s
- **Asymmetric class-specific demotion** (8m HILS, 2m non-HILS); same
  mechanism, different policy per application class
- **NUMA jailing**: no remote-socket demotion (cross-socket QoS
  feedback too slow). Violating this in production caused
  orders-of-magnitude DRAM tail latency increase + kernel soft-lockups
- **No direct allocation into tier2** — all allocations go to tier1;
  only demotion populates tier2 (anti-thrashing + no-history-at-alloc)
- **TCMalloc hot/cold hints** segregate cold allocations into a
  separate virtual region without THP. Two annotation sites raised
  Spanner cold coverage 10% → 42%
- **Tier-friendly / tier-unfriendly scheduler hints** classify
  workloads offline; Borg routes unfriendly jobs (e.g., ML training)
  away from 2-tier machines. 30% STAR reduction
- **A/B testing methodology**: 2200 experimental + 1000 control
  servers, matched job replicas, 5% FPR confidence intervals, 4-week
  runs — the rigor IS the contribution
- **Read for:** the STAR/STRR proxy-metric pair (direct analog for
  endurance: EMR + LTRR + WAR), the percentile-band success criterion
  pattern, the negative-rule discipline (NUMA jailing, no direct
  alloc), the scheduler-as-fallback for unfriendly workloads, and the
  matched-replica A/B methodology adaptable to single-node phase-replay

### Pond: CXL-Based Memory Pooling Systems for Cloud Platforms
*Li et al., ASPLOS '23 (Microsoft)*
- Production CXL memory pooling at Azure
- Read for: how a hyperscaler frames the cost model of tiered memory,
  what they measure to justify the design

### Contiguitas: The Pursuit of Physical Memory Contiguity in Datacenters
*Zhao et al., ISCA '23 (CMU + Meta)*
- Already analyzed (see meta-patterns)
- Read for: HW/SW co-design template, async TLB invalidation,
  spatial confinement, production-grade evaluation

### LLFree: Scalable and Optionally-Persistent Page-Frame Allocation
*Wrenger et al., USENIX ATC '23*
- Already analyzed (see meta-patterns)
- Read for: lock-free design, cache-line atomic transactions,
  persistence + scalability co-design

### Illuminator: Effective Use of Huge Pages with Movable Memory
*Panwar et al., ASPLOS '18*
- Separates movable/unmovable allocations within 2 MB blocks
- Direct predecessor to Contiguitas (cited as such)
- Read for: the limitations that Contiguitas addresses; intuition for
  why fragmentation matters at multiple granularities

---

## Tier 2 — Tier-aware memory management

Direct policy cousins. They all answer "where should this page live in
a hierarchy" — your contribution adds the endurance-budget constraint.

### AutoTiering: Automatic Data Placement Manager in Multi-Tier All-Flash Datacenter
*Hsu et al., or similar — multiple AutoTiering papers exist*
- Auto-tuning placement across tiers
- Read for: closed-loop policy adaptation

### HeMem: Scalable Tiered Memory Management for Big Data Applications
*Raybuck et al., SOSP '21*
- Promoted to Tier 1 above — see full entry there

### Nimble Page Management for Tiered Memory Systems
*Yan et al., ASPLOS '19*
- Background migration mechanism + policy for tiered memory
- One of HeMem's main baselines; emulated NVM
- Read for: migration mechanism design (close to your work)

### HeteroOS: OS Design for Heterogeneous Memory Management in Datacenter
*Kannan et al., ISCA '17*
- OS/VMM-level heterogeneous memory manager coordinating with guest OSes
- Major baseline for HeMem
- Read for: the kernel-VMM coordination design pattern, and to understand
  what HeMem improves on

### KLOCs: Kernel-Level Object Contexts for Heterogeneous Memory Systems
*Kannan et al., ASPLOS '21*
- Extends tiering to kernel objects (page cache, slab allocations)
- The opposite design choice from HeMem (which deliberately excludes
  kernel objects from tiering)
- Read for: when kernel objects matter for tiering and when they don't.
  Most LtRAM workloads will be user-space-dominated, but kernel-heavy
  workloads (filesystem, network) may need this

### X-Mem: Data Tiering in Heterogeneous Memory Systems
*Dulloor et al., EuroSys '16*
- Language-runtime-based tiered memory; application annotates allocations
  with hints
- Major baseline for HeMem; emulated NVM only
- Read for: the application-aware tiering camp's argument and a
  useful counterpoint to transparent tiering

### Unimem: Runtime Data Management on NVM-Based Heterogeneous Main Memory
*Wu et al., SC '17*
- HPC-focused runtime data placement for NVM
- Read for: how the HPC community approaches the same problem

### Hawkeye: Efficient Fine-grained OS Support for Huge Pages
*Panwar et al., ASPLOS '19*
- Fine-grained huge page support
- Read for: detection mechanisms for which pages to promote

### Ingens: Coordinated and Efficient Huge Page Management
*Kwon et al., OSDI '16*
- Coordinated huge page management across multiple subsystems
- Read for: how a memory-management proposal handles cross-subsystem
  effects

### Thermostat: Application-Transparent Page Management for Two-Tiered Main Memory
*Agarwal & Wenisch, ASPLOS '17*
- Cold page classification for huge-page mappings, predates Lagar-Cavilla
- Uses page-fault-based sampling on randomly-selected cold pages to
  estimate slowdown
- Read for: an alternative cold-detection mechanism (sampling-based) and
  how to bound the slowdown estimation overhead. Contrast with the
  accessed-bit-scanning approach used in Lagar-Cavilla and your simulator.

### OSim: Preparing System Software for a World with Terabyte-Scale Memories
*Mansi & Swift, ASPLOS '20*
- Systematic study of how memory-management algorithms break at TB scale
- The "page-table scans don't scale" finding HeMem leverages
- Read for: the canonical reference for "why does TB-scale memory break
  things." Useful for motivating any scalable-by-design memory mechanism

### AIFM: High-Performance, Application-Integrated Far Memory
*Ruan et al., OSDI '20*
- Application-integrated far memory at object granularity (not page)
- Complementary design point to HeMem's page-granularity approach
- Read for: object-granularity tiering arguments; useful for the discussion
  section

### NOMAD: Non-Exclusive Memory Tiering via Transactional Page Migration
*Xiang, Lin, Deng, Lu, Rao (UT Arlington) + Yuan, Wang (Intel Labs); OSDI '24*
- **Two underexploited design moves combined into one paper:**
  (1) **Transactional page migration (TPM):** OS-level optimistic
      concurrency control. Start the page copy without unmapping; check
      dirty bit at end; commit if clean, abort if dirty. Page stays
      accessible during copy — the unavailable window shrinks from
      ~12 µs to hundreds of ns.
  (2) **Non-exclusive memory tiering:** a third design point between
      inclusive caching and exclusive tiering. Pages have shadow
      copies on the slow tier only after promotion. Demotion becomes
      a free PTE remap when master is clean — half the migration cost
      disappears.
- Multi-platform evaluation (4 platforms: 2× Intel SPR + Agilex 7, Intel
  CLX + Optane, AMD Genoa + Micron CXL) — rare and credibility-building
- **Critical for the LtRAM work:** TPM is essentially the user's idea
  #1 (COW transition window) with a performance framing instead of an
  endurance framing. NOMAD provides peer-reviewed validation of the
  design pattern.
- Honest reporting: NOMAD loses to Memtis under severe thrashing; loses
  to NoTier on Redis with large RSS. Same NoTier-baseline-beats-everyone
  finding as Soar/Alto.
- **Read this for:** the TPM protocol (validates idea #1); the page
  shadowing pattern (inspires idea #27); the non-exclusive tiering
  framing as a third design point; the multi-platform eval discipline.

### P2CACHE: Exploring Tiered Memory for In-Kernel File Systems Caching
*Lin, Xiang, Rao, Lu; USENIX ATC '23*
- Same first-author group as NOMAD; applies tiered memory ideas to
  in-kernel filesystem cache rather than anonymous memory
- Read for: how the group thinks about tiered memory across different
  application domains; the design philosophy behind NOMAD's later work

### Colloid: Tiered Memory Management — Access Latency is the Key!
*Vuppalapati & Agarwal, SOSP '24 (Cornell)*
- **The earlier, more mathematically clean version of the hotness-is-
  wrong attack.** Where Soar/Alto attacks hotness via MLP-aware
  per-access cost (AOL), Colloid attacks via per-tier loaded latency:
  under memory interconnect contention, the default tier's loaded
  latency can be 2.4× *higher* than the alternate tier's, inverting
  the conventional "hot pages on default tier" ordering
- **Closed-form equilibrium:** L_D = L_A. Derived from first principles:
  minimize p·L_D + (1-p)·L_A. Cleaner than empirical curves or
  histograms
- **Little's Law on CHA counters:** L = Queue Occupancy / Request Rate,
  per tier, microsecond timescale. No assumptions about arrival or
  service distributions
- **Watermark-driven binary search** for convergence to equilibrium
  under dynamic workloads. Reset-on-drift handles non-stationary cases
- Plug-in to HeMem (520 LOC), TPP (~315 LOC), MEMTIS (411 LOC). Same
  pattern Alto adopted a year later
- **Read this for:** (1) the closed-form equilibrium framing as a
  cleaner alternative to threshold tuning; (2) Little's Law as a
  deployment-ready measurement primitive; (3) the watermark binary-
  search convergence algorithm; (4) "measure, don't predict"
  philosophy. The lineage Colloid → Alto → (your work) is the
  cleanest plug-in cascade in the field.

### MTM: Rethinking Memory Profiling and Migration for Multi-Tiered Large Memory
*Ren et al., EuroSys '24*
- Profiling and migration for 3+ tier systems (DRAM + CXL + remote)
- Read for: how the policy story changes when you have more than two tiers.
  Eventually-relevant for LtRAM if you stack DRAM + LtRAM + something
  slower

### NeoMem: Hardware/Software Co-Design for CXL-Native Memory Tiering
*Zhou et al., MICRO '24*
- HW/SW co-designed CXL tiering with custom hardware support
- **Architecturally closest to your "HW-SW contract redesign" framing**
- Read for: how to position HW/SW co-design contributions; what minimal
  HW extensions look like in recent literature

### M5: Mastering Page Migration and Memory Management for CXL-based Tiered Memory Systems
*Sun et al., ASPLOS '25*
- Page migration mastery for CXL tiered memory
- Read for: state-of-the-art CXL migration mechanisms; useful for
  comparison framing

### FlexMem: Adaptive Page Profiling and Migration for Tiered Memory
*Xu et al., ATC '24*
- Adaptive profiling that adjusts to workload behavior
- Read for: adaptive-parameter approaches in tiering (analogous to your
  idea #11's token-budget framework)

### Chrono: Meticulous Hotness Measurement and Flexible Page Migration for Memory Tiering
*Qi et al., EuroSys '25*
- Fine-grained hotness measurement with flexible migration policies
- Read for: the recency-vs-frequency debate; useful as a baseline that
  Soar/Alto compares against

### Telescope: Telemetry for Gargantuan Memory Footprint Applications
*Nair et al., ATC '24*
- Telemetry for terabyte-scale applications
- Read for: scalability of access tracking at the upper memory-size limits

### Carrefour / Traffic Management: A Holistic Approach to Memory Placement on NUMA Systems
*Dashti et al., ASPLOS '13*
- **Colloid's spiritual predecessor.** Classical NUMA load balancing
  via memory page placement
- Balances request rates across NUMA nodes (vs. Colloid's balancing of
  latencies)
- Read for: the original "balance load across memory tiers/nodes"
  pattern Colloid extends with latency-awareness

### BATMAN: Techniques for Maximizing System Bandwidth of Memory Systems with Stacked-DRAM
*Chou et al., MEMSYS '17*
- Bandwidth-centric tiering: balance accesses by theoretical max
  bandwidth ratio
- The approach Colloid contrasts against — Colloid argues bandwidth-
  centric balancing fails under contention without saturation
- Read for: the bandwidth-as-metric perspective, useful for the
  related-work section's "alternative approaches" discussion

### Johnny Cache: The End of DRAM Cache Conflicts (in Tiered Main Memory Systems)
*Lepers & Zwaenepoel, OSDI '23*
- DRAM cache conflict misses in tiered memory (Intel memory mode)
- Relevant for understanding hardware-managed tiering failure modes
- Read for: a counter-case for hardware-managed tiering; useful for
  the "why software tiering" framing

### Understanding the Host Network
*Vuppalapati, Agarwal et al., SIGCOMM '24*
- Same first author as Colloid; earlier paper validating Little's
  Law-based latency measurements at the host network level
- Read for: the measurement methodology behind Colloid's latency
  inference; useful if you want to apply Little's Law to your own
  measurement infrastructure

### AutoTiering: Exploring the Design Space of Page Management for Multi-Tiered Memory Systems
*Kim et al., USENIX ATC '21*
- Multi-tier page management with promotion/demotion via page faults
- One of Memtis's six comparison baselines; emulated NVM
- Maintains access history as N-bit history vector; uses LFU-style demotion
- Read for: how page-fault-based access tracking actually performs against
  PEBS-based; the access-history-vector data structure

### HotBox: Reconsidering OS Memory Optimizations in the Presence of Disaggregated Memory
*Bergman et al., ISMM '22*
- Argues against huge pages in tiered memory due to access skewness
- Memtis directly refutes this by splitting skewed huge pages instead
- Read for: the case that huge pages are net-negative in tiered systems;
  useful counterpoint when discussing TLB-reach vs. fragmentation
  trade-offs for LtRAM

### DAMON: Data Access MONitor (Linux mainline)
*Park, kernel patches; promoted to upstream Linux 5.15+*
- Region-based access tracking with adjustable granularity and scan interval
- Memtis's Figure 1 shows DAMON's accuracy-vs-overhead trade-off (5ms +
  fine granularity = 72% CPU; 500ms + coarse = no accuracy)
- Read for: the region-vs-page granularity trade-off; the kernel-side
  mainline alternative to PEBS-based sampling

---

## Tier 3 — TLB coherence and async invalidation

Background for idea #2 (async TLB invalidation) in your implementation
plan.

### LATR: Lazy Translation Coherence
*Kumar et al., ASPLOS '18*
- **The original "lazy TLB invalidation" paper.** Direct precedent for
  Contiguitas's async invalidation.
- Read for: framing of "decouple coordination from correctness,"
  which mappings can afford to be lazy and which cannot

### Don't Shoot Down TLB Shootdowns!
*Amit et al., EuroSys '20*
- TLB shootdown reduction techniques
- Read for: dominant cost of TLB invalidation in real workloads,
  optimization patterns

### Optimizing the TLB Shootdown Algorithm with Page Access Tracking
*Amit, USENIX ATC '17*
- TLB shootdown optimization via access tracking
- Read for: how to avoid invalidations that aren't load-bearing

### UNITD: Unified Instruction/Translation/Data Coherence
*Romanescu et al., HPCA '10*
- Proposes including TLBs in standard cache coherence protocol
- Read for: alternative solutions to the same coordination problem

### DiDi: Mitigating the Performance Impact of TLB Shootdowns Using a Shared TLB Directory
*Villavieja et al., PACT '11*
- Shared TLB directory
- Read for: another approach to scalable TLB management

---

## Tier 4 — Translation overhead reduction

Background for "why does contiguity matter" arguments — Contiguitas
spent its first three sections on this.

### Direct Segments / Efficient Virtual Memory for Big Memory Servers
*Basu et al., ISCA '13*
- Segment-based translation for big-memory workloads
- Read for: how to reduce TLB pressure by adding contiguous segments

### Redundant Memory Mappings (RMM)
*Karakostas et al., ISCA '15*
- Range-based TLB designs
- Read for: alternative TLB structures that exploit contiguity

### Elastic Cuckoo Page Tables: Rethinking Virtual Memory Translation for Parallelism
*Skarlatos et al., ASPLOS '20*
- Hashed page table design
- Read for: how to avoid the cost of tree page-walks

### Hash, Don't Cache (the Page Table)
*Yaniv & Tsafrir, SIGMETRICS '16*
- Argues hashed PTs over multi-level cached PTs
- Read for: the case for hashing in translation

### BabelFish: Fusing Address Translations for Containers
*Skarlatos et al., ISCA '20*
- Reduces translation overhead for containers sharing memory
- Read for: another "decouple work from coordination" pattern
- Same first author as Contiguitas

### Memory-Efficient Hashed Page Tables
*Stojkovic et al., HPCA '23*
- Improvement on hashed page tables, reduces memory overhead
- Read for: how to revise a baseline design to lower its cost

---

## Tier 5 — Predictive / ML for memory

Direct background for idea #5 in your implementation plan (decision-tree
migration policy) and for the broader "learning-based memory management"
framing.

### Learning-based Memory Allocation for C++ Server Workloads
*Maas et al., ASPLOS '20 (Google)*
- ML-driven memory allocation
- Read for: how to deploy a tiny model in a hot path, feature
  engineering for memory workloads

### Adaptive Huge-Page Subrelease for Non-Moving Memory Allocators
*Maas et al., ISMM '21 (Google)*
- Adaptive subrelease policy
- Read for: how to adapt policy parameters to observed workload

### Beyond malloc efficiency to fleet efficiency: a hugepage-aware memory allocator
*Hunter et al., OSDI '21 (Google)*
- TCMalloc with huge-page awareness for fleet efficiency
- Read for: how userspace allocators can help OS-level decisions

### Pythia: A Customizable Hardware Prefetching Framework
*Bera et al., MICRO '21*
- ML for prefetching
- Read for: low-overhead model serving in hardware (sub-µs inference)

### CHiRP: Control-Flow History Reuse Prediction
*Mirbagher-Ajorpaz et al., MICRO '20*
- Control-flow prediction
- Read for: feature engineering for prediction tasks in HW

### Classic adaptive caching policies (LIRS / 2Q / ARC / LeCAR)
*Various 1994–2020*
- Replacement policy literature
- Read for: queue-based heuristics that your decision tree should
  compare against; the family of "two-list" adaptive policies

### Google Vizier: A Service for Black-Box Optimization
*Golovin et al., KDD '17*
- Production hyperparameter optimization infrastructure at Google
- The GP-Bandit machinery used by Lagar-Cavilla's autotuner
- Read for: how to deploy Bayesian optimization at scale. If you build an
  autotuner for your T / T_commit / T_post params, this is the template.

### Practical Bayesian Optimization of Machine Learning Algorithms
*Snoek, Larochelle, Adams, NeurIPS '12*
- Foundational Bayesian optimization paper
- Read for: theory behind GP Bandit; useful when explaining your autotuner

---

## Tier 6 — Non-volatile memory / persistence

Context for endurance-aware design.

### An Empirical Guide to the Behavior and Use of Scalable Persistent Memory
*Yang et al., FAST '20*
- Direct measurement of Optane DCPMM behavior
- **Crucial:** documents read/write asymmetry, latency cliffs, and the
  cost model you should mirror for LtRAM characterization
- Read for: how to characterize a real NV memory device — the template
  for any LtRAM characterization paper

### NV-Heaps: Making Persistent Objects Fast and Safe with Next-Generation, Non-Volatile Memories
*Coburn et al., ASPLOS '11*
- Persistent memory programming model
- Read for: persistence + safety co-design

### Mnemosyne: Lightweight Persistent Memory
*Volos et al., ASPLOS '11*
- Lightweight persistent memory
- Read for: programming-model framing

### Pangolin: A Fault-Tolerant Persistent Memory Programming Library
*Zhang & Swanson, USENIX ATC '19*
- Fault-tolerant persistent memory library
- Read for: how to surface persistence at the library level

### NOVA: A Log-Structured File System for Hybrid Volatile/Non-Volatile Main Memories
*Xu & Swanson, FAST '16*
- Log-structured filesystem for hybrid memory
- Read for: how to design for byte-addressable + persistent + wear-aware

### LightPC: Hardware and Software Co-design for Energy-Efficient Full System Persistence
*Lee et al., ISCA '22*
- HW/SW co-design for full-system persistence
- Read for: another HW/SW co-design template (compare to Contiguitas)

### Adaptive Memory Fusion: Towards Transparent, Agile Integration of Persistent Memory
*Xue et al., HPCA '18*
- Reactive persistent-memory scheme that augments capacity under pressure
- The "reactive" approach Lagar-Cavilla explicitly contrasts with their
  proactive design
- Read for: the trade-off between reactive (pressure-driven) and proactive
  (background) tier policies. Lagar-Cavilla argues proactive wins for WSC;
  understand the counter-argument.

---

## Tier 7 — Wear leveling and FTL

Direct background for endurance management.

### A Survey of Flash Translation Layer
*Various survey papers*
- How SSDs handle wear and address translation
- Read for: static/dynamic/hybrid wear leveling algorithms; map
  directly to LtRAM placement decisions

### DFTL: A Flash Translation Layer Employing Demand-based Selective Caching
*Gupta et al., ASPLOS '09*
- Selective caching for FTL metadata
- Read for: how to keep metadata small when managing wear at scale

### Survey on Wear Leveling for Phase Change Memory
*Various*
- Closer analog to NOR than SSD-flash literature
- Read for: cell-level wear-leveling algorithms

### Start-Gap Wear Leveling
*Qureshi et al., MICRO '09*
- Algebraic wear leveling, very cheap
- Read for: low-overhead wear-leveling primitive

---

## Tier 8 — Compiler / runtime hints to memory

Background for idea #5 (hint-aware policy) in your implementation plan.

### CHERI: A Hybrid Capability-System Architecture
*Woodruff et al., ISCA '14 (and follow-ups)*
- Pointers carry bounds and permissions at the HW level
- Read for: the extreme version of "expose semantics to hardware"

### Translation Ranger: Operating System Support for Contiguity-Aware TLBs
*Yan et al., ISCA '19*
- OS support for contiguity-aware TLBs
- Read for: how to surface allocation-time information to the
  translation layer

### Twizzler: A Data-Centric OS for Non-Volatile Memory
*Bittman et al., USENIX ATC '20*
- Non-volatile-memory-first OS design
- Read for: how to expose persistence semantics in the OS API

### Mesh: Compacting Memory Management for C/C++ Applications
*Powers et al., PLDI '19*
- Userspace memory compaction
- Read for: how to do compaction in places the OS can't

---

## Tier 9 — CXL and disaggregated memory

The future context for your work.

### CXL specification overview
*Compute Express Link Consortium, 2022/2023 white papers*
- Read the high-level overview, not the full spec
- Read for: how CXL.mem positions tiered memory at the hardware level

### Demystifying CXL Memory with Genuine CXL-Ready Systems and Devices
*Sun et al., MICRO '23*
- Real-hardware CXL memory characterization
- Read for: empirical latency/bandwidth numbers for actual CXL hardware

### Efficient Memory Disaggregation with Infiniswap
*Gu et al., NSDI '17*
- RDMA-based remote memory swap, predates CXL hype but same family
- Cited in Lagar-Cavilla as a remote-memory alternative
- Read for: the remote-memory branch of the design space — what it offers
  vs. local compression vs. NOR. Helps you defend "why local LtRAM, not
  remote memory" in your paper.

### Pond, Astraea, TMTS, etc.
*Multiple recent 2022–2024 papers*
- Production-grade CXL memory pooling
- Read for: how hyperscalers operationalize tiered memory

---

## Tier 10 — Foundational papers (read if you haven't)

These are the patterns the modern work descends from.

### The Multics Virtual Memory
*Bensoussan, Clingen, Daley, SOSP '69*
- The original paging paper
- Read for: vocabulary and framing that all subsequent VM papers inherit

### Surpassing the TLB Performance of Superpages with Less OS Support
*Talluri & Hill, ASPLOS '94*
- Classic huge-page design
- Read for: the multi-page-size trade-off framing

### A Fast Storage Allocator (the buddy system)
*Knowlton, CACM '65*
- The buddy allocator LLFREE eventually replaces
- Read for: historical context

### Memory Resource Management in VMware ESX Server
*Waldspurger, OSDI '02*
- Foundational paper on cold-page detection, ballooning, content-based
  page sharing in virtualized environments
- The accessed-bit-scanning intuition in Lagar-Cavilla descends from here
- Read for: how the early VM community established the cold-detection
  primitives we still use today

### The Datacenter as a Computer
*Barroso, Hölzle, Ranganathan, Morgan & Claypool (multiple editions)*
- The Google WSC book
- Read for: vocabulary and framings for WSC-scale work. If you're targeting
  a WSC audience, these are the words to use.

### Large-Scale Cluster Management at Google with Borg
*Verma et al., EuroSys '15*
- Google's cluster scheduler; defines the "fail-fast and restart elsewhere"
  philosophy Lagar-Cavilla relies on
- Read for: operational context for any WSC-scale tiering work

### Borg: the Next Generation
*Tirmazi et al., EuroSys '20 (Google)*
- The follow-up to Borg, providing the workload-diversity numbers TMTS
  relies on; characterizes HILS vs non-HILS workloads, the CPU/memory
  limit distributions, and the population-based analysis pattern
- Read for: vocabulary for population studies, the argument for why
  benchmark suites are insufficient for WSC claims, and the workload
  segmentation underlying TMTS's friendly/unfriendly scheduling hints

### CPI²: CPU Performance Isolation for Shared Compute Clusters
*Zhang et al., EuroSys '13*
- Defines an application-agnostic, low-level metric (CPI) that correlates
  with app performance — same philosophy as promotion rate
- Read for: how to design proxy metrics that generalize across heterogeneous
  workloads. The metric-design lessons here are directly applicable to
  your SLO design.

---

## Suggested reading order

If you read these in order, each one builds on the last:

1. **Lagar-Cavilla '19 (already done)** — predecessor to TMO, foundational
   metric design (promotion rate, K-th percentile control, online/offline
   split, fast simulator + GP Bandit autotuner)
2. **HeMem (already done)** — real-NVM tiering, PEBS-based tracking,
   async-everything architecture, read/write asymmetry. Read for the
   substrate characterization (§2.2) and the PEBS pattern
3. **Memtis (already done)** — HeMem's successor. Histogram-based
   dynamic thresholds, skewness-aware huge-page splitting
4. **Colloid (already done)** — Memtis's challenger #1. Closed-form
   equilibrium (L_D = L_A) via Little's Law on CHA counters. The
   measure-don't-predict pattern and watermark-binary-search controller
5. **Soar/Alto (already done)** — Memtis's challenger #2. Extends
   Colloid's per-tier latency to per-access cost (AOL). The plug-in
   design pattern (Alto) and NoTier baseline discipline
6. **TMTS** — Google WSC tiering, complement to Memtis
7. **TMO** — Meta's WSC tiering paper, builds on Lagar-Cavilla
8. **TPP** — tier-aware policy at production scale
9. **NeoMem** — closest architecturally to your HW-SW co-design framing
10. **Nomad** — non-exclusive page migration (transactional)
11. **Illuminator** → **Contiguitas (already done)** — spatial-confinement
    intuition
12. **LATR** — "decouple coordination from correctness" in detail
13. **Yang et al. (FAST '20 Optane)** — characterization template
14. **OSim** — why page-table scans don't scale at TB
15. **Waldspurger ESX '02** — foundational cold-detection primitives
16. **CPI²** — proxy-metric philosophy that generalizes across workloads
17. **Maas et al. (ML allocator)** — model-in-the-hot-path pattern
18. **Thermostat** — alternative cold-detection (sampling-based) approach
19. **Eisenman EuroSys '18** — app-driven counterpoint to Lagar-Cavilla
20. **HeteroOS, KLOCs, X-Mem, AIFM, AutoTiering, MTM, M5, FlexMem,
    Chrono, Telescope** — design-space alternatives for tiered memory
21. **HotBox** — the "don't use huge pages in tiered memory" argument
22. **DAMON** — Linux mainline region-based tracking
23. **Nimble** — migration mechanism baseline
24. **LLFree (already done)** — lock-free + persistence + cache-friendly
25. Everything else as needed when writing specific paper sections

---

## Reading discipline

For each paper, extract:
1. **The trick** — what's the novel idea, in one sentence?
2. **The meta-pattern** — what mental move did the authors make?
3. **The numbers** — what bar do their results clear? What's the cost?
4. **The gap** — what didn't they address that's relevant to your work?
5. **The framing** — how do they position the contribution? (steal good
   framings)

This is the structure used for the LLFREE and Contiguitas analyses; apply
it to every paper on this list.
