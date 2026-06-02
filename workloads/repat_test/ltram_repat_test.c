// LtRAM repatriation correctness test.
//
// Proves the write-fault repatriation path end to end:
//   1. Build a backing file, evict caches, then fault a PRIVATE PROT_READ
//      mapping. Because the VMA is read-only, the cache-miss allocation in
//      filemap_fault routes the page into ZONE_LTRAM (node 1).
//   2. Read the page's physical frame (PFN) via /proc/self/pagemap and confirm
//      it is in the LtRAM PFN range.
//   3. mprotect() the page writable and write one byte -> do_wp_page forces a
//      copy to a fresh DRAM page (repatriation).
//   4. Re-read the PFN: it must change and now be in DRAM (PFN < LtRAM start).
//   5. Read /sys/kernel/debug/ltram/stats before/after: migrated_back must
//      increase by 1 (and write_faulted by 1).
//
// Run as root inside the guest (needs pagemap PFNs + drop_caches). Build static.

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>
#include <sys/mman.h>

#define PGSZ 4096

/* Physical frame number backing virtual address @va, or 0 if not present. */
static unsigned long pagemap_pfn(void *va)
{
	uint64_t val, off = ((uintptr_t)va / PGSZ) * sizeof(uint64_t);
	int fd = open("/proc/self/pagemap", O_RDONLY);

	if (fd < 0)
		return 0;
	if (pread(fd, &val, sizeof(val), off) != sizeof(val)) {
		close(fd);
		return 0;
	}
	close(fd);
	if (!(val & (1ULL << 63)))		/* page not present */
		return 0;
	return (unsigned long)(val & ((1ULL << 55) - 1));
}

/* start_pfn of ZONE_LTRAM from /proc/zoneinfo; PFN >= this means LtRAM/node1. */
static unsigned long ltram_start_pfn(void)
{
	FILE *f = fopen("/proc/zoneinfo", "r");
	char line[256];
	int in_ltram = 0;
	unsigned long start = 0;

	if (!f)
		return 0;
	while (fgets(line, sizeof(line), f)) {
		if (strstr(line, ", zone"))
			in_ltram = (strstr(line, "LtRAM") != NULL);
		if (in_ltram && strstr(line, "start_pfn:"))
			sscanf(strstr(line, "start_pfn:") + 10, "%lu", &start);
	}
	fclose(f);
	return start;
}

/* A named counter from /sys/kernel/debug/ltram/stats, or -1. */
static long stat_get(const char *key)
{
	FILE *f = fopen("/sys/kernel/debug/ltram/stats", "r");
	char line[256];
	long v = -1;

	if (!f)
		return -1;
	while (fgets(line, sizeof(line), f)) {
		if (strncmp(line, key, strlen(key)) == 0) {
			sscanf(line + strlen(key), "%ld", &v);
			break;
		}
	}
	fclose(f);
	return v;
}

static void drop_caches(void)
{
	int fd;

	sync();
	fd = open("/proc/sys/vm/drop_caches", O_WRONLY);
	if (fd >= 0) {
		if (write(fd, "3\n", 2) < 0) { /* best effort */ }
		close(fd);
	}
}

int main(int argc, char **argv)
{
	long npages = (argc > 1) ? atol(argv[1]) : 64;
	const char *path = "/root/.ltram_repat.dat";
	size_t len = (size_t)npages * PGSZ;
	unsigned long lt_start = ltram_start_pfn();
	char *p;
	long i, target = -1;
	unsigned long pfn_before = 0, pfn_after;
	int fd;

	printf("LtRAM start_pfn = %lu  (a PFN >= this is in LtRAM / node 1)\n", lt_start);
	if (!lt_start) {
		printf("Could not read LtRAM start_pfn from /proc/zoneinfo -- is this the LtRAM kernel?\n");
		return 1;
	}

	/* 1. backing file with content, then evict it from the page cache */
	fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0600);
	if (fd < 0 || ftruncate(fd, len) < 0) { perror("file"); return 1; }
	for (i = 0; i < npages; i++) {
		char b = (char)(i + 1);
		pwrite(fd, &b, 1, i * PGSZ);
	}
	fsync(fd);
	close(fd);
	drop_caches();				/* so the read below is a cache MISS */

	/* 2. private read-only mapping; MADV_RANDOM disables readahead so every
	 *    page is faulted individually through the LtRAM-routing path. */
	fd = open(path, O_RDONLY);
	if (fd < 0) { perror("reopen"); return 1; }
	p = mmap(NULL, len, PROT_READ, MAP_PRIVATE, fd, 0);
	if (p == MAP_FAILED) { perror("mmap"); return 1; }
	madvise(p, len, MADV_RANDOM);

	volatile char sink = 0;
	for (i = 0; i < npages; i++)
		sink += p[i * PGSZ];		/* read fault -> LtRAM (on miss) */
	(void)sink;

	/* 3. find a page that actually landed in LtRAM */
	for (i = 0; i < npages; i++) {
		unsigned long pfn = pagemap_pfn(p + i * PGSZ);

		if (pfn >= lt_start) {
			target = i;
			pfn_before = pfn;
			break;
		}
	}
	if (target < 0) {
		printf("FAIL: no page landed in LtRAM (routing did not place these).\n");
		printf("      page0 PFN = %lu (DRAM). Check stats placed_at_alloc.\n",
		       pagemap_pfn(p));
		return 1;
	}
	printf("target page %ld: VA=%p  PFN_before=%lu  -> LtRAM\n",
	       target, (void *)(p + target * PGSZ), pfn_before);

	long wf0 = stat_get("write_faulted_of_alloc");
	long mb0 = stat_get("migrated_back_of_alloc");

	/* 4. make it writable and write -> repatriation to DRAM */
	if (mprotect(p + target * PGSZ, PGSZ, PROT_READ | PROT_WRITE) < 0) {
		perror("mprotect"); return 1;
	}
	p[target * PGSZ] = 0x55;		/* the write that triggers it */

	pfn_after = pagemap_pfn(p + target * PGSZ);
	long wf1 = stat_get("write_faulted_of_alloc");
	long mb1 = stat_get("migrated_back_of_alloc");

	printf("after write : VA=%p  PFN_after =%lu  -> %s\n",
	       (void *)(p + target * PGSZ), pfn_after,
	       pfn_after >= lt_start ? "LtRAM" : "DRAM");
	printf("PTE moved?  %s   (PFN %lu -> %lu)\n",
	       pfn_before != pfn_after ? "YES" : "NO", pfn_before, pfn_after);
	printf("stats delta: write_faulted_of_alloc +%ld, migrated_back_of_alloc +%ld\n",
	       wf1 - wf0, mb1 - mb0);

	if (pfn_before != pfn_after && pfn_after < lt_start && (mb1 - mb0) >= 1)
		printf("RESULT: PASS -- LtRAM page repatriated to DRAM on write, counter +1.\n");
	else
		printf("RESULT: FAIL -- see numbers above.\n");

	munmap(p, len);
	close(fd);
	unlink(path);
	return 0;
}
