# Linux Kernel Memory Management: A Guide to `mm/`

## 1. Boot-Time Memory Discovery and Freelist Population

### Step 1: The firmware hands the kernel a memory map

Before any kernel code runs, the firmware (BIOS/UEFI on x86, device tree on ARM) has already catalogued physical memory. On x86, this is the **e820 map** — a list of physical address ranges tagged as usable RAM, reserved, ACPI tables, etc. The kernel reads this very early in `arch/x86/kernel/e820.c` and converts it into `memblock`.

### Step 2: `memblock` — the bootstrap allocator

`memblock` is a dead-simple allocator that exists only for the period between \"kernel is running\" and \"buddy allocator is ready.\" It maintains two flat arrays of `(base, size, flags)` ranges:

```c
// mm/memblock.c
struct memblock {
    struct memblock_type memory;   // all physical RAM
    struct memblock_type reserved; // ranges that are unavailable
};
```

The architecture populates this via calls like:
```c
memblock_add(base, size);           // \"this RAM exists\"
memblock_reserve(base, size);       // \"don't give this out\"
```

For NUMA systems, ACPI's SRAT table tells the kernel which physical address ranges belong to which NUMA node, fed in via `memblock_set_node()`.

You can think of the free memory at any point during boot as `memory - reserved`.

### Step 3: Zone sizing — `free_area_init()`

Once memblock knows about all RAM, the architecture calls `free_area_init(max_zone_pfns)` (`mm/mm_init.c`), passing an array that says \"zone X's highest possible page frame number is Y\":

```c
// arch/x86/mm/init.c
void __init zone_sizes_init(void)
{
    unsigned long max_zone_pfns[MAX_NR_ZONES] = {0};

    max_zone_pfns[ZONE_DMA]    = min(MAX_DMA_PFN, max_low_pfn);
    max_zone_pfns[ZONE_DMA32]  = min(MAX_DMA32_PFN, max_low_pfn);
    max_zone_pfns[ZONE_NORMAL] = max_low_pfn;
    max_zone_pfns[ZONE_LTRAM]  = max_pfn;

    free_area_init(max_zone_pfns);
}
```

`free_area_init()` uses this array to compute the `arch_zone_lowest_possible_pfn` and `arch_zone_highest_possible_pfn` for each zone — the PFN bounds within which a zone *can* have pages. It then calls `free_area_init_node()` for each online NUMA node.

### Step 4: Per-node and per-zone setup

`free_area_init_node()` → `calculate_node_totalpages()` figures out how many pages each zone on this node actually has (accounting for holes in physical memory), setting `zone->zone_start_pfn`, `zone->spanned_pages`, and `zone->present_pages`.

Then `free_area_init_core()` sets up each zone's internal structures:

```c
// mm/mm_init.c
static void __init free_area_init_core(struct pglist_data *pgdat)
{
    for (j = 0; j < MAX_NR_ZONES; j++) {
        struct zone *zone = pgdat->node_zones + j;

        // ... accounting ...

        zone_init_internals(zone, j, nid, freesize);  // name, lock, PCP init
        setup_usemap(zone);                            // pageblock_flags bitmap
        init_currently_empty_zone(zone,                // initializes free_area[]
                                  zone->zone_start_pfn, size);
    }
}
```

`init_currently_empty_zone()` calls `zone_init_free_lists()` which does:
```c
for_each_migratetype_order(order, t) {
    INIT_LIST_HEAD(&zone->free_area[order].free_list[t]);
    zone->free_area[order].nr_free = 0;
}
```

At this point every free list is empty. The zone knows its bounds, but no pages are in it yet.

### Step 5: Struct page initialization — `memmap_init()`

Every physical page frame gets a `struct page`. These are allocated in a flat array (the **mem_map**), one `struct page` per PFN. `memmap_init()` walks every PFN range and calls `__init_single_page()`:

```c
// mm/mm_init.c
void __meminit __init_single_page(struct page *page, unsigned long pfn,
                                  unsigned long zone, int nid)
{
    mm_zero_struct_page(page);
    set_page_links(page, zone, nid, pfn);  // pack zone+node ID into page->flags
    init_page_count(page);                 // refcount = 1 (\"reserved\")
    INIT_LIST_HEAD(&page->lru);
}
```

The critical call is `set_page_links()`: it stuffs the zone index and node ID into the page's `flags` field as bitfields. This is how the allocator later knows which zone a page belongs to — it reads `page_zone(page)` which decodes those bitfields. Every page starts with refcount 1, meaning it is \"reserved\" and not free.

### Step 6: The watershed — `memblock_free_all()`

This is the moment the buddy allocator comes alive. Called from the architecture's `mem_init()`, it walks every free range in memblock and calls `__free_pages_core()` on each:

```c
// mm/memblock.c
static unsigned long __init free_low_memory_core_early(void)
{
    phys_addr_t start, end;
    u64 i;

    for_each_free_mem_range(i, NUMA_NO_NODE, MEMBLOCK_NONE, &start, &end, NULL)
        count += __free_memory_core(start, end);

    return count;
}
```

`__free_pages_core()` clears `PageReserved`, sets refcount to 0, and calls `__free_pages_ok()` which inserts the pages into the appropriate `free_area` list:

```c
// mm/page_alloc.c
void __free_pages_core(struct page *page, unsigned int order)
{
    // clear reserved flag, set refcount to 0
    for (loop = 0; loop < nr_pages; loop++, p++) {
        __ClearPageReserved(p);
        set_page_count(p, 0);
    }

    atomic_long_add(nr_pages, &page_zone(page)->managed_pages);
    __free_pages_ok(page, order, FPI_TO_TAIL);  // → buddy allocator
}
```

After `memblock_free_all()`, `memblock` is effectively retired (though the data structure remains for reference) and the buddy allocator owns all free memory.

---

## 2. How the Kernel Uses Memory Before MM Is Ready

There are roughly three eras:

### Era 1: Before `start_kernel()` — no allocator at all

The kernel runs entirely from statically linked `.text`/`.data`/`.bss`. All data structures are global variables or placed in preallocated arrays. Nothing is dynamically allocated.

### Era 2: After memblock, before buddy — `memblock_alloc()`

Once memblock knows about RAM, it can allocate:

```c
// Simple: give me N bytes, aligned to A, below limit
void *ptr = memblock_alloc(size, align);
```

Internally this just marks a range in `memblock.reserved`. It's a bump allocator — it cannot free individual allocations back to itself in arbitrary order. Almost everything set up during boot uses this: page tables, the `struct page` arrays themselves, per-cpu data, early device driver data.

The key insight is that `memblock_alloc()` allocations that survive into normal operation are never \"freed\" to memblock — they just stay reserved and the buddy allocator never sees those pages. Allocations for truly temporary boot data get explicitly freed via `memblock_free()`, and those pages eventually reach the buddy via `memblock_free_all()`.

### Era 3: After buddy, before slab — `alloc_pages()`

After `memblock_free_all()`, the buddy allocator works. But `kmalloc()` (the general-purpose byte-granularity allocator) isn't ready yet — it needs the slab/slub allocator, which itself needs the buddy to bootstrap.

For this window, kernel code uses raw page allocations:
```c
struct page *p = alloc_pages(GFP_KERNEL, order);
void *addr = page_address(p);
```

The slab allocator is initialized in `mm_init()` via `kmem_cache_init()`, which uses `alloc_pages()` to get its initial memory and builds its internal structures in place. After that, `kmalloc()` becomes available and the kernel is fully functional.

---

## 3. Data Structures: Nodes, Zones, Freelists, and the Zone Table

### The hierarchy

```
Physical memory
└── NUMA nodes  (pg_data_t, one per node)
    └── Zones   (struct zone, several per node)
        └── Buddy freelists  (free_area[], one per order)
            └── Migrate-type lists  (free_list[], one per migratetype)
                └── struct page chains (linked via page->lru)
```

### `pg_data_t` — one per NUMA node

```c
// include/linux/mmzone.h
typedef struct pglist_data {
    struct zone     node_zones[MAX_NR_ZONES];    // array of zones on this node
    struct zonelist node_zonelists[MAX_ZONELISTS]; // fallback ordering
    int             nr_zones;
    unsigned long   node_start_pfn;              // first PFN on this node
    unsigned long   node_present_pages;          // usable pages
    unsigned long   node_spanned_pages;          // including holes
    int             node_id;
    // kswapd thread, reclaim state, ...
} pg_data_t;
```

On UMA (single-node) systems there is exactly one `pg_data_t`, pointed to by `NODE_DATA(0)`. On NUMA, `NODE_DATA(nid)` returns the node's `pg_data_t`.

### `struct zone` — a contiguous range within one node

Zones exist because not all devices can DMA to all of RAM. Old ISA devices needed memory below 16 MiB (`ZONE_DMA`); 32-bit PCI devices needed below 4 GiB (`ZONE_DMA32`); everything else goes in `ZONE_NORMAL`. `ZONE_MOVABLE` is a soft zone used to reduce fragmentation. `ZONE_DEVICE` is for persistent memory and device-mapped pages.

```c
struct zone {
    /* Watermarks. kswapd wakes when free pages < WMARK_LOW */
    unsigned long _watermark[NR_WMARK];   // MIN, LOW, HIGH, PROMO
    unsigned long watermark_boost;

    unsigned long zone_start_pfn;         // first PFN in this zone
    unsigned long spanned_pages;          // total PFNs (including holes)
    unsigned long present_pages;          // actual usable pages
    atomic_long_t managed_pages;          // pages the buddy manages right now

    /* The buddy freelist — one entry per order */
    struct free_area free_area[MAX_ORDER + 1];

    spinlock_t      lock;
    const char     *name;
    // per-cpu page cache, statistics, ...
};
```

### `struct free_area` — the buddy freelist for one order

```c
struct free_area {
    struct list_head free_list[MIGRATE_TYPES];  // one list per migratetype
    unsigned long    nr_free;                   // total free blocks at this order
};
```

The buddy allocator groups pages into blocks of size `2^order`. `free_area[0]` holds individual pages, `free_area[1]` holds pairs, and so on up to `free_area[MAX_ORDER]` which holds the largest contiguous blocks the buddy tracks (typically 4 MiB blocks on a 4 KiB page system).

### Migrate types — the anti-fragmentation layer

Within each `free_area`, pages are further split by **migrate type**:

```c
enum migratetype {
    MIGRATE_UNMOVABLE,   // kernel data, can't be moved
    MIGRATE_MOVABLE,     // anonymous/file pages, can be migrated
    MIGRATE_RECLAIMABLE, // can be reclaimed under pressure (page cache)
    MIGRATE_PCPTYPES,    // per-cpu cache types end here
    MIGRATE_HIGHATOMIC,  // emergency reserve
    MIGRATE_CMA,         // contiguous memory allocator
    MIGRATE_ISOLATE,     // being moved, don't touch
    MIGRATE_TYPES
};
```

Keeping unmovable and movable pages on separate lists prevents a single long-lived kernel allocation from stranding a movable page between two free blocks, making large contiguous free regions impossible. When a migrate-type list is empty, the allocator **steals** an entire block from another list and splits it up.

### `struct page` — one per physical page frame

This is the most fundamental structure. There is one for every page of physical RAM, stored in the flat `mem_map[]` array. It is heavily overloaded — the same `struct page` is used for anonymous pages, page cache pages, slab objects, and buddy-free pages, with different fields active depending on the page's current use.

The most important invariant: `page->flags` packs the zone index, NUMA node ID, and various state flags into a single `unsigned long`. This is how `page_zone(page)` works — it shifts and masks the flags field, no pointer indirection needed.

```c
struct page {
    unsigned long flags;        // zone, node, PG_locked, PG_dirty, ...

    union {
        struct list_head lru;   // when in LRU or buddy free list
        // ... many other uses
    };

    union {
        atomic_t _mapcount;     // how many PTEs point here (-1 = not mapped)
        // ...
    };

    atomic_t _refcount;         // reference count; 0 = free to buddy
    // ...
};
```

When a page is on a buddy freelist, `page->lru` is the list node linking it into `free_area[order].free_list[migratetype]`. When it is in use, `page->lru` is reused for the LRU list, slab linkage, etc.

### Zonelists — the fallback order

Each node has a `node_zonelists[]` array. A **zonelist** is a flat array of `struct zoneref` (zone pointer + index) sorted from most-preferred to least-preferred. When an allocation on node 0 fails (no memory), the allocator walks down the zonelist to try node 1's zones.

```c
// For node 0, the zonelist might look like:
// [ZONE_NORMAL/node0] → [ZONE_DMA32/node0] → [ZONE_DMA/node0]
//   → [ZONE_NORMAL/node1] → [ZONE_DMA32/node1] → NULL
```

`build_zonelists()` constructs these at boot. The order within a node goes from highest zone to lowest (NORMAL before DMA32 before DMA) because you want to preserve scarce low-memory zones for allocations that actually need them. The NUMA fallback ordering comes from ACPI node distances.

`ZONE_LTRAM` is explicitly excluded from these fallback lists because it must never be used as a fallback for normal allocations.

### GFP flags and `GFP_ZONE_TABLE`

`GFP_*` flags serve two purposes: they specify **which zone** to allocate from and **what the allocator is allowed to do** (sleep, reclaim, etc.).

The zone flags are `__GFP_DMA`, `__GFP_DMA32`, `__GFP_HIGHMEM`, `__GFP_MOVABLE`. The function `gfp_zone(flags)` maps combinations of these to a `zone_type` enum value. Rather than a chain of `if` statements, the kernel encodes this mapping in a single compile-time constant `GFP_ZONE_TABLE`:

```c
// include/linux/gfp.h
#define GFP_ZONE_TABLE ( \\
    (ZONE_NORMAL << 0 * GFP_ZONES_SHIFT)                              \\
    | (OPT_ZONE_DMA << ___GFP_DMA * GFP_ZONES_SHIFT)                 \\
    | (OPT_ZONE_DMA32 << ___GFP_DMA32 * GFP_ZONES_SHIFT)             \\
    | (ZONE_NORMAL << ___GFP_MOVABLE * GFP_ZONES_SHIFT)              \\
    | (ZONE_MOVABLE << (___GFP_MOVABLE | ___GFP_HIGHMEM) * GFP_ZONES_SHIFT) \\
    // ...
)

static inline enum zone_type gfp_zone(gfp_t flags)
{
    // Special cases first (e.g. __GFP_LTRAM → ZONE_LTRAM)
    if (unlikely(flags & __GFP_LTRAM))
        return ZONE_LTRAM;

    int bit = (__force int)(flags & GFP_ZONEMASK);
    return (GFP_ZONE_TABLE >> (bit * GFP_ZONES_SHIFT)) &
                              ((1 << GFP_ZONES_SHIFT) - 1);
}
```

`GFP_ZONEMASK` is the bitmask of all zone-selecting GFP bits (`DMA|DMA32|HIGHMEM|MOVABLE`). Each possible value of those bits (there are at most 16 combinations, so this fits in 64 bits with 4-bit entries) is precomputed at compile time. A lookup is just a shift and mask — one instruction.

`ZONE_LTRAM` is excluded from this table deliberately. It cannot be requested by setting a combination of the normal zone flags; the kernel added a separate `__GFP_LTRAM` bit and handles it with an early-exit check before the table lookup.

---

## 4. Page Allocation: Tracing `alloc_pages()` to the Buddy

### The call chain

```
alloc_pages(gfp_mask, order)
  └─ __alloc_pages(gfp_mask, order, preferred_nid, nodemask)
       ├─ prepare_alloc_pages()          // set up alloc_context
       ├─ get_page_from_freelist()       // fast path
       └─ __alloc_pages_slowpath()       // if fast path fails
```

### `prepare_alloc_pages()` — setting up the allocation context

```c
// mm/page_alloc.c
struct alloc_context {
    struct zonelist   *zonelist;        // which zonelist to use
    nodemask_t        *nodemask;        // NUMA node restriction
    struct zoneref    *preferred_zoneref; // starting point for iteration
    int                migratetype;     // UNMOVABLE, MOVABLE, etc.
    enum zone_type     highest_zoneidx; // don't go above this zone
    bool               spread_dirty_pages;
};
```

```c
static inline bool prepare_alloc_pages(gfp_t gfp_mask, unsigned int order,
        int preferred_nid, nodemask_t *nodemask,
        struct alloc_context *ac, ...)
{
    ac->highest_zoneidx = gfp_zone(gfp_mask); // e.g. ZONE_NORMAL for GFP_KERNEL
    ac->zonelist = node_zonelist(preferred_nid, gfp_mask); // node's fallback list
    ac->migratetype = gfp_migratetype(gfp_mask);

    // Find the preferred zone — starting point for the zone iterator
    ac->preferred_zoneref = first_zones_zonelist(ac->zonelist,
                                ac->highest_zoneidx, ac->nodemask);

    // LTRAM override: redirect to node 1's zonelist
    if (gfp_mask & __GFP_LTRAM) {
        ac->highest_zoneidx   = ZONE_LTRAM;
        ac->zonelist          = node_zonelist(1, gfp_mask);
        ac->preferred_zoneref = first_zones_zonelist(ac->zonelist,
                                    ac->highest_zoneidx, ac->nodemask);
    }
    return true;
}
```

`highest_zoneidx` acts as a ceiling: the zone iterator will only consider zones where `zone_idx(zone) <= highest_zoneidx`. For `GFP_KERNEL` this is `ZONE_NORMAL`, so the allocator will try ZONE_NORMAL and fall back to ZONE_DMA32 and ZONE_DMA, but will never touch ZONE_MOVABLE or ZONE_LTRAM.

### `get_page_from_freelist()` — the fast path

```c
// mm/page_alloc.c (simplified)
static struct page *get_page_from_freelist(gfp_t gfp_mask, unsigned int order,
        int alloc_flags, const struct alloc_context *ac)
{
    struct zoneref *z = ac->preferred_zoneref;
    struct zone *zone;

    for_next_zone_zonelist_nodemask(zone, z, ac->highest_zoneidx, ac->nodemask) {

        // 1. Watermark check — is this zone above its low watermark?
        if (!zone_watermark_fast(zone, order,
                wmark_pages(zone, alloc_flags & ALLOC_WMARK_MASK),
                ac->highest_zoneidx, alloc_flags, gfp_mask))
            goto check_alloc_wmark;  // try reclaim or skip zone

        // 2. NUMA policy check
        if (cpuset_zone_allowed(zone, gfp_mask) == 0)
            continue;

        // 3. Actually allocate from this zone
        page = rmqueue(ac->preferred_zoneref->zone, zone, order,
                       gfp_mask, alloc_flags, ac->migratetype);
        if (page)
            goto out;
    }
    return NULL;

out:
    // Update statistics
    zone_statistics(ac->preferred_zoneref->zone, zone, 1);
    return page;
}
```

### Watermarks

Three watermarks govern when the allocator is allowed to take pages and when it must back off:

- `WMARK_MIN`: absolute floor. Only `PF_MEMALLOC` (memory reclaim) code can allocate below this. Normal allocations fail.
- `WMARK_LOW`: kswapd wakes up and starts async reclaim when free pages cross below this.
- `WMARK_HIGH`: kswapd stops reclaiming once free pages reach this.

```c
// Simplified watermark check
static inline bool zone_watermark_fast(struct zone *z, unsigned int order,
        unsigned long mark, int highest_zoneidx, ...)
{
    long free_pages = zone_page_state(z, NR_FREE_PAGES);

    // Quick check for order-0
    if (free_pages > mark + z->lowmem_reserve[highest_zoneidx])
        return true;

    return __zone_watermark_ok(z, order, mark, ...);
}
```

### `rmqueue()` — dequeuing from the buddy

```c
static struct page *rmqueue(struct zone *preferred_zone, struct zone *zone,
        unsigned int order, gfp_t gfp_flags, ...)
{
    if (order == 0) {
        // Try per-CPU page cache first (avoids taking the zone lock)
        page = rmqueue_pcplist(preferred_zone, zone, gfp_flags, migratetype);
        if (page)
            return page;
    }

    // Fall back to the buddy allocator proper
    return rmqueue_buddy(preferred_zone, zone, order, alloc_flags, migratetype);
}
```

**Per-CPU page cache (PCP):** For order-0 pages (by far the most common case), each CPU has its own `struct per_cpu_pages` — a short list of pre-allocated pages. Taking from this list requires no lock on the zone. This is a major performance win on multi-core systems.

**`rmqueue_buddy()`:** Takes the zone lock and calls `__rmqueue()`:

```c
static __always_inline struct page *__rmqueue(struct zone *zone,
        unsigned int order, int migratetype, ...)
{
    struct page *page;

retry:
    // Try to get a block of the right size from the requested migrate type
    page = __rmqueue_smallest(zone, order, migratetype);
    if (unlikely(!page)) {
        // That migrate type is empty; steal from another type
        page = __rmqueue_fallback(zone, order, migratetype, ...);
    }
    return page;
}
```

**`__rmqueue_smallest()`** is where the buddy math happens:

```c
static __always_inline struct page *__rmqueue_smallest(struct zone *zone,
        unsigned int order, int migratetype)
{
    unsigned int current_order;
    struct free_area *area;
    struct page *page;

    // Walk up from the requested order, looking for a free block
    for (current_order = order; current_order <= MAX_ORDER; current_order++) {
        area = &zone->free_area[current_order];
        page = get_page_from_free_area(area, migratetype);
        if (!page)
            continue;

        // Found one! Remove it from the free list
        del_page_from_free_list(page, zone, current_order);

        // If it's larger than needed, split it and put the remainder back
        expand(zone, page, order, current_order, migratetype);
        //  expand() adds 2^(current_order-1), 2^(current_order-2), ...
        //  back to the freelist until we have a block of exactly 2^order

        return page;
    }
    return NULL;  // zone is exhausted
}
```

The splitting in `expand()` is the dual of the merging in `__free_one_page()`. If you request 8 pages (`order=3`) and the smallest free block is 64 pages (`order=6`), you get the first 8 pages, and the remaining 56 pages are split into a 32-page block (order 5) + a 16-page block (order 4) + an 8-page block (order 3), each added to their respective freelists.

### The slow path

If `get_page_from_freelist()` returns NULL, `__alloc_pages_slowpath()` tries progressively more drastic measures:

1. Wake kswapd (async reclaim in the background)
2. Relax watermark constraints and try the freelist again
3. Direct reclaim (the calling process itself reclaims pages synchronously)
4. Memory compaction (migrate pages to defragment, create larger free blocks)
5. Try the freelist again with even looser constraints
6. OOM killer — pick a process to kill, free its memory, try once more

---

## 5. How Pages Are Freed: Tracing Back to the Buddy

### The call chain

```
free_pages(addr, order)
  └─ __free_pages(page, order)
       ├─ order == 0 → free_unref_page()    // returns to PCP cache
       └─ order > 0  → __free_pages_ok()
            └─ free_one_page()
                 └─ __free_one_page()       // buddy merging
```

### `free_unref_page()` — the PCP fast path

Order-0 frees go to the CPU's page cache rather than directly to the buddy, avoiding the zone lock entirely:

```c
// mm/page_alloc.c
static void free_unref_page(struct page *page, unsigned int order)
{
    struct per_cpu_pages *pcp = &this_cpu_ptr(zone->per_cpu_pageset)->pcp;

    // Add to the PCP free list for this migrate type
    list_add(&page->lru, &pcp->lists[migratetype]);
    pcp->count++;

    // If the cache is too full, drain some pages back to the buddy
    if (pcp->count >= pcp->high)
        free_pcppages_bulk(zone, pcp->batch, pcp);
}
```

Pages accumulate in the PCP cache and get drained to the buddy in batches, which amortizes the zone lock overhead.

### `__free_one_page()` — the buddy merge algorithm

This is the heart of the buddy allocator. Its job is to take a freshly freed block, find its **buddy** (the adjacent block of the same size that it would pair with), and merge them if the buddy is also free. It repeats this upward until no merge is possible.

```c
// mm/page_alloc.c (simplified)
static inline void __free_one_page(struct page *page, unsigned long pfn,
        struct zone *zone, unsigned int order, int migratetype, ...)
{
    unsigned long buddy_pfn;
    struct page *buddy;

    while (order < MAX_ORDER) {
        // The buddy of a block at PFN with order N is at PFN XOR 2^N.
        // This works because buddy pairs are always aligned to 2^(N+1).
        buddy_pfn = __find_buddy_pfn(pfn, order);
        buddy = page + (buddy_pfn - pfn);

        // Is the buddy actually free and at the same order?
        if (!page_is_buddy(page, buddy, order))
            goto done_merging;

        // Remove the buddy from its current free list
        del_page_from_free_list(buddy, zone, order);

        // Merge: the combined block starts at the lower PFN
        if (buddy_pfn < pfn) {
            page = buddy;
            pfn = buddy_pfn;
        }
        order++;  // go up one level
    }

done_merging:
    // Add the (possibly merged) block to the free list at its final order
    add_to_free_list(page, zone, order, migratetype);
    set_buddy_order(page, order);  // mark the page as the head of a free block
}
```

The buddy XOR trick: blocks are always aligned to their size. A block of `2^N` pages at PFN `P` has its buddy at PFN `P XOR 2^N`. XOR flips exactly one bit — the bit that distinguishes the two halves of a `2^(N+1)` block. This means you can find the buddy with a single XOR and compute the merged block's start with a bitwise AND.

```
Order 0: PFN  0  1  2  3  4  5  6  7
              |--|  |--|  |--|  |--|
Order 1: PFN  0     2     4     6
              |-----|     |-----|
Order 2: PFN  0           4
              |-----------|
Order 3: PFN  0
```

Example: page at PFN 2, order 0.
- Buddy = 2 XOR 1 = 3. If PFN 3 is free → merge to order 1 block at PFN 2.
- Buddy = 2 XOR 2 = 0. If PFN 0 is free → merge to order 2 block at PFN 0.
- And so on.

### `page_is_buddy()` — is the buddy actually free?

The allocator can't just check the PFN — it needs to confirm the buddy is:
1. In the same zone (can't merge across zone boundaries)
2. Currently free (not allocated)
3. At the same order (not a fragment of a larger free block)

```c
static inline bool page_is_buddy(struct page *page, struct page *buddy,
                                  unsigned int order)
{
    if (!page_is_guard(buddy) && !PageBuddy(buddy))
        return false;           // not free
    if (buddy_order(buddy) != order)
        return false;           // different size
    if (page_zone_id(page) != page_zone_id(buddy))
        return false;           // different zone
    return true;
}
```

`PageBuddy(page)` is a flag set in `page->flags` when the page is on a buddy freelist. `buddy_order(buddy)` reads the order stored in `page->private` when `PageBuddy` is set. Both are set/cleared by `add_to_free_list()` / `del_page_from_free_list()`.

### What happens to `page->flags` and `page->private`

| State | `PG_buddy` | `page->private` | `page->_refcount` |
|---|---|---|---|
| On buddy freelist | set | order of this block | 0 |
| Allocated, in use | clear | depends on use | ≥ 1 |
| On PCP list | clear | — | 0 |
| On LRU | clear | — | ≥ 1 |

The allocator uses these invariants to distinguish free from in-use pages without needing a separate bitmap.

---

## Putting It All Together

The full lifecycle of a typical page:

1. **Boot**: `__init_single_page()` creates its `struct page` with zone/node encoded in flags, refcount=1, `PageReserved` set.
2. **`memblock_free_all()`**: `__free_pages_core()` clears `PageReserved`, sets refcount=0, calls `__free_one_page()` which places it on the buddy freelist at order 0. `PageBuddy` is set.
3. **Allocation**: `__rmqueue_smallest()` removes it from the freelist, clears `PageBuddy`, calls `prep_new_page()` which sets refcount=1 and validates the page is clean.
4. **In use**: refcount ≥ 1, `page->lru` is used for the LRU list, `_mapcount` tracks how many PTEs point to it.
5. **Free**: last `put_page()` drops refcount to 0, calls `__free_pages()`, which (for order 0) puts it on the PCP list or directly calls `__free_one_page()` to merge back into the buddy.
