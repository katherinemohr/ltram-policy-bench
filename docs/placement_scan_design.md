# DRAM → LtRAM placement scan (baseline policy) — design

## Goal

Migrate **write-cold** DRAM pages into LtRAM to cut DRAM footprint, *including
read-hot pages* (NOR reads are fast; only writes hurt). This is the proactive
DRAM→LtRAM direction, the mirror of repatriation (LtRAM→DRAM on write, already
built via copy-on-write in `do_wp_page`).

## Why LRW, not LRU (the thesis)

The kernel LRU tracks **access** recency (reads + writes), for reclaim. We need
the opposite axis: **write** recency. A read-hot / write-cold page (e.g. the
PageRank CSR graph: built once, then read every iteration) is the *ideal* LtRAM
tenant, yet LRU keeps it on the **active** list — so any LRU/inactive-based
selection would *exclude exactly what we want*. LRU cannot express
"read-hot but write-cold"; the dirty axis can. Using LRU state would reduce this
to ordinary access-tiering, which is not the contribution.

## Candidate discovery: a scanning hand + soft-dirty (no per-page state)

No enqueue-on-allocation, no per-page queue. Instead:

- A **scanning hand** (cursor `{vma, addr}`) sweeps the **scoped** VMAs in
  address order, a **bounded chunk of PTEs per period** (caps CPU), wrapping at
  the end. (Precedent: NUMA balancing and MGLRU already do periodic PT scans.)
- Write-coldness is the **soft-dirty PTE bit** (x86-64 bit 58, software bit,
  already in the page table — **zero new per-page memory**, nothing added to
  `struct page`). Distinct from the hardware dirty bit; safe to clear (unlike the
  HW dirty bit, which drives file writeback).

### The lap is the clock (no per-node timestamps)

Aging is measured in **laps of the hand**, not stored timestamps:

- visit P → read soft-dirty: **set** (written since last visit) → clear, skip;
  **clear** (not written for a full lap) → write-cold candidate.

The soft-dirty bit holds exactly one lap of history, which is all a 1-lap
threshold needs. The wall-clock threshold is **implicit and self-scaling**:

```
write-cold threshold ≈ K × (pages in scope) / (PTEs scanned per period) × period
```

Bigger working set → longer to confirm cold (reasonable). Knobs: scan rate and
K. We use **K = 1** (aggressive); repatriation is the safety net for premature
migrations, and the pipeline below already yields an effective K ≥ 2.

## Scanner → migrator pipeline, and the soft-dirty re-arm handshake

Two threads with a tiny pending queue (the device double-buffer, ~2 deep):

- **Scanner** (coarse, ~100 ms): on a page clean for a lap, **clear soft-dirty
  again (re-arm)** and push to the pending queue.
- **Migrator** (fine / device-paced): pop, do the **final check**, commit.
  Final check = (1) *still the same live, mapped folio?* (PFN could have been
  freed/reused during the dwell) **and** (2) *soft-dirty still clear?* (no write
  during the dwell). Both pass → write to NOR (`ltram_migrate_to`).

### Adaptive K for free — the key property (for the report)

Because soft-dirty is re-armed at enqueue, the page is confirmed clean **twice**
— at scanner-enqueue and at migrator-commit — separated by the **dwell time D**
in the pending queue. So K = 1 in code behaves like **K ≥ 2** end-to-end, and D
is set by migration pressure:

- budget free / short queue → D ≈ 0 → behaves like K = 1 (migrate promptly).
- budget contended / backed-up queue (42/s cap) → D grows → only pages that stay
  cold *through the backlog* survive → effectively higher K — **more selective
  exactly when fewer writes are affordable.**

This is self-throttling selectivity with no extra mechanism, using only the
soft-dirty bit and the queue we already have. The "sparse watch set" is just the
pending queue; nodes need only a page reference (no timestamp).

### Final-check cost (why it stays in)

Runs at the migration rate (~42/s) → ~0.004% of a core. One PTE walk, and O(1)
because candidates are **private** (shared mappings are excluded from LtRAM). It
is ~1000× cheaper than the **migrate+repatriate round-trip (2 flash cycles)** it
prevents — the highest-ROI guard in the system. Never let the migrator trust
stale scanner state.

## Rate limiting: endurance tokens + device pacing

Two complementary throttles:

- **Token bucket — endurance.** NOR at 10^5 cycles, 5-year life, 65,536 frames:
  `65,536 × 10^5 / (5×365.25×86400 s) ≈ 42 programs/s`. Refill ~42 tokens/s
  (time-based), cap ~512 (burst). Each migration spends 1 token; empty bucket →
  skip. **Valid only with wear-leveling** (else the hottest frame's 10^5 limit
  binds first — see [wear_leveling_allocator.md](wear_leveling_allocator.md)).
- **Device pacing — throughput.** ~1 ms/write floor + the 2-deep write queue, so
  bursts (when tokens have accumulated) never overrun the hardware buffer. Token
  rate (42/s) binds long-term; pacing smooths the millisecond scale.

## Scoping (reduce the swept set)

- VMA kind: **skip stack** (`VM_GROWSDOWN`), skip read-only (already LtRAM),
  target **writable anon (heap) + writable file-backed**.
- One process at a time for now (no pid/cgroup filter needed yet).
- The dirty check is the backstop: anything write-hot re-queues and never
  migrates regardless of scoping, so scoping is an optimization, not correctness.

## Memory cost

- per-page: **0** (soft-dirty lives in the PTE).
- pending queue: O(a few entries).
- vs. an enqueue-on-allocation FIFO that tracks every DRAM page: ~64 B/page →
  ~1.6 % of all RAM if unscoped. The scanning hand avoids this entirely.

## Future (in plan, not baseline)

- **Hot-page cooldown / multi-generation (MGLRU-style):** pages found hot
  repeatedly get a cooldown so the hand skips them for several laps — pure CPU
  optimization, no memory change.
- K > 1 via global epoch (0 memory) or sparse counters, if K = 1 churns.
- pid/cgroup scope; multi-process.
- ML model replaces the "clean-for-a-lap" decision with a learned write-cold
  predictor; everything else (scan, pipeline, migrate, repatriate) is unchanged.

## Build order

1. Token bucket (endurance limiter) in `mm/ltram.c`.
2. Migrate mechanism: wire `ltram_migrate_to`; a debugfs `migrate_pid` trigger +
   a migrate-test (DRAM page → LtRAM, `placed_migrated_in` +1, PFN crosses the
   LtRAM line) — mirror of the repat test, in reverse.
3. Scanning hand + soft-dirty re-arm + scanner/migrator pipeline + final check,
   token + device throttled, write-protect migrated pages (so writes repatriate).
