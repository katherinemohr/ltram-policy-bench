/*
 * lifecycle_target.c — synthetic target for the dirty_sweep incarnation test
 * (X1 in the page-lifecycle-view PLAN).
 *
 * Produces a deterministic present -> absent -> present residency pattern at a
 * REUSED virtual address so the sweeper must record two incarnations (C02):
 *
 *   Phase 1: mmap an anonymous region, write all pages repeatedly (~1.2s),
 *            then stay resident & untouched (~1.2s) so a clean stretch is seen.
 *   Phase 2: munmap -> the pages go absent (~1.5s gap).
 *   Phase 3: mmap again with MAP_FIXED at the SAME address, write again (~1.2s).
 *
 * Phase durations are comfortably larger than the 100ms sweep interval, so the
 * STRUCTURE (two incarnations with a gap) is robust to timing even though exact
 * sweep numbers are not. Build: gcc -O2 -o lifecycle_target lifecycle_target.c
 */
#include <sys/mman.h>
#include <stdio.h>
#include <unistd.h>
#include <stdint.h>

#define NPAGES 8
#define PAGE   4096

static void busy_write(volatile char *p, int rounds, useconds_t per_us) {
    for (int r = 0; r < rounds; r++) {
        for (int i = 0; i < NPAGES; i++)
            p[(size_t)i * PAGE] = (char)(r + i + 1);
        usleep(per_us);
    }
}

int main(void) {
    size_t len = (size_t)NPAGES * PAGE;

    /* Phase 1: map + write, then quiet-resident */
    char *a = mmap(NULL, len, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (a == MAP_FAILED) { perror("mmap phase1"); return 1; }
    fprintf(stderr, "lifecycle_target: phase1 addr=%p\n", (void *)a);
    busy_write(a, 12, 100000);   /* ~1.2s of writes */
    usleep(1200000);             /* ~1.2s clean, still resident */

    /* Phase 2: free -> residency gap */
    munmap(a, len);
    usleep(1500000);             /* ~1.5s absent */

    /* Phase 3: remap at the SAME address -> new incarnation */
    char *b = mmap(a, len, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
    if (b == MAP_FAILED) { perror("mmap phase3"); return 1; }
    fprintf(stderr, "lifecycle_target: phase3 addr=%p reused=%d\n",
            (void *)b, b == a);
    busy_write(b, 12, 100000);   /* ~1.2s of writes */
    usleep(400000);
    munmap(b, len);
    return 0;
}
