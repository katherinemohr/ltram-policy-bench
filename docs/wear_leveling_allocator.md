# Wear-leveling free-frame selection for ZONE_LTRAM (PLANNED — not implemented)

## Problem (measured)

The repatriation stress harness (`ltram_repat_stress 1000000 512`) did 1,000,448
place→write→repatriate→free cycles and produced:

```
frames_ever_programmed   2,045   (of 65,536)
erase_count: min 1, mean 489, median 491, mode 1, max 1426
skew_max_over_mean       2.91x
```

1M operations concentrated on ~2,045 physical frames, hottest erased 1,426
times. On NOR flash (~10^4–10^5 erase endurance) this burns out a hot set while
most of the device sits idle.

## Root cause

The buddy allocator's per-CPU page lists (pcp) are **LIFO** — the most
recently freed page is handed back out first (good for cache locality, bad for
flash wear). So a churning workload recycles the same few frames repeatedly.

We do **not** need a new allocator. We need a different **free-frame selection
policy for ZONE_LTRAM only**: replace LIFO with **FIFO** so allocations
round-robin across all free frames → uniform erase.

## Design: FIFO free queue for ZONE_LTRAM

LtRAM only allocates **order-0** pages (read-only pages placed individually), so
no buddy coalescing is needed — a flat FIFO of free frames suffices.

- **Free path:** when an LtRAM page is freed, enqueue it at the **tail** of a
  per-zone FIFO (`page->lru` as the list node), instead of (or in addition to)
  returning it to the buddy pcp/free_area.
- **Alloc path:** when satisfying a `__GFP_LTRAM` order-0 request, dequeue from
  the **head** of the FIFO. Fall back to the buddy free_area only if the FIFO is
  empty (cold start / fragmentation).
- Result: a frame cannot be reused until every other free frame has been
  allocated once → each frame accrues ~1 erase per full cycle → uniform wear.

### Where to hook (sketch)
- Bypass the per-CPU page cache (pcp) for ZONE_LTRAM order-0 allocs — the pcp
  LIFO is the skew source. (`rmqueue()` / `rmqueue_pcplist()` already branch on
  zone/order; add an LtRAM FIFO path there.)
- Keep the FIFO + its lock in `mm/ltram.c` (a `list_head` + spinlock, or a small
  ring of PFNs). LtRAM alloc rate is far below DRAM, so a single zone-global
  lock is likely fine; revisit with per-CPU FIFOs if it contends.
- Initialize the FIFO at `ltram_init()` with all managed LtRAM frames.

## Alternatives (for reference)
- **(A) Minimal:** don't add a FIFO list; just make ZONE_LTRAM frees
  `list_add_tail()` to the buddy free_area and skip the pcp. Approximates FIFO
  with the smallest change, but the buddy's ordering guarantees are weaker than
  an explicit queue.
- **(C) Wear-aware (most rigorous):** we already maintain a per-frame
  `ltram_erase_count[]`. Instead of FIFO, allocate the **least-worn** free frame
  (min-heap / approximate bucketed selection by erase count). This is robust
  even when free timing is non-uniform (some frames held long-term), at the cost
  of heap maintenance. FIFO is the simple first cut; this is the upgrade if the
  skew isn't flat enough.

## Validation plan
Re-run `ltram_repat_stress 1000000 512` after the change and compare
`/sys/kernel/debug/ltram/stats`:
- expect `skew_max_over_mean_x1000` → ~1000 (1.0x),
- `erase_count_max` → ~`erase_count_mean`,
- `frames_ever_programmed` → close to the full managed frame count (spread out).
The before/after histogram (`erase_histogram`) is the contrast slide:
LIFO (hot spike) vs FIFO (flat).

## Status
Planned. Do **not** implement yet. Sits after: repatriation (done) → DRAM→LtRAM
placement scan + `ltram_migrate_from` shared-gap fix → this wear-leveling
allocator → ML placement model.
