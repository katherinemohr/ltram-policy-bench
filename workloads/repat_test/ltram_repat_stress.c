// LtRAM repatriation stress harness -- millions of repatriations.
//
// Recycles a small read-only file region through LtRAM over and over: each
// batch re-faults the region read-only (cache-miss -> LtRAM), then writes every
// page (-> repatriate to DRAM), verifying per page that an LtRAM frame moved to
// DRAM (PFN crosses the LtRAM line). Between batches it makes the region
// read-only again, drops the private copies (MADV_DONTNEED) and evicts the file
// cache (FADV_DONTNEED) so the next fault re-routes to LtRAM. No global
// drop_caches in the loop, so it is fast.
//
// Usage:  ltram_repat_stress [target_repatriations] [batch_pages]
//         defaults: 1000000  512
//
// Run as root inside the guest (needs pagemap PFNs). Build static.

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>
#include <time.h>
#include <sys/mman.h>

#define PGSZ 4096

static int g_pmfd = -1;
static unsigned long g_lt_start;

static unsigned long pfn(void *va)
{
	uint64_t v, off = ((uintptr_t)va / PGSZ) * sizeof(uint64_t);

	if (pread(g_pmfd, &v, sizeof(v), off) != sizeof(v))
		return 0;
	if (!(v & (1ULL << 63)))
		return 0;
	return (unsigned long)(v & ((1ULL << 55) - 1));
}

static unsigned long ltram_start_pfn(void)
{
	FILE *f = fopen("/proc/zoneinfo", "r");
	char line[256];
	int cur = 0;
	unsigned long start = 0;

	if (!f)
		return 0;
	while (fgets(line, sizeof(line), f)) {
		if (strstr(line, ", zone"))
			cur = (strstr(line, "LtRAM") != NULL);
		if (cur && strstr(line, "start_pfn:"))
			sscanf(strstr(line, "start_pfn:") + 10, "%lu", &start);
	}
	fclose(f);
	return start;
}

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

int main(int argc, char **argv)
{
	unsigned long target = (argc > 1) ? strtoul(argv[1], NULL, 10) : 1000000UL;
	long batch = (argc > 2) ? atol(argv[2]) : 512;
	size_t len = (size_t)batch * PGSZ;
	const char *path = "/root/.lt_stress.dat";
	unsigned long total_lt = 0, total_pages = 0, fails = 0, batches = 0;
	long mb0, mb1, wf0, wf1, i;
	char *p, *z;
	int fd;
	time_t t0;

	g_pmfd = open("/proc/self/pagemap", O_RDONLY);
	g_lt_start = ltram_start_pfn();
	if (g_pmfd < 0 || !g_lt_start) {
		printf("ABORT: pagemap or LtRAM zone unavailable (wrong kernel/not root?)\n");
		return 1;
	}
	printf("stress: target=%lu repatriations, batch=%ld pages, LtRAM start_pfn=%lu\n",
	       target, batch, g_lt_start);

	fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0600);
	if (fd < 0 || ftruncate(fd, len) < 0) { perror("file"); return 1; }
	z = calloc(1, PGSZ);
	for (i = 0; i < batch; i++)
		if (pwrite(fd, z, PGSZ, i * PGSZ) != PGSZ) { perror("pwrite"); }
	free(z);
	fsync(fd);

	p = mmap(NULL, len, PROT_READ, MAP_PRIVATE, fd, 0);
	if (p == MAP_FAILED) { perror("mmap"); return 1; }
	madvise(p, len, MADV_RANDOM);			/* no readahead */

	mb0 = stat_get("migrated_back_of_alloc");
	wf0 = stat_get("write_faulted_of_alloc");
	t0 = time(NULL);

	while (total_lt < target) {
		/* recycle the region back into LtRAM */
		mprotect(p, len, PROT_READ);
		madvise(p, len, MADV_DONTNEED);		/* drop private copies */
		posix_fadvise(fd, 0, len, POSIX_FADV_DONTNEED); /* evict -> free LtRAM */

		volatile char s = 0;
		for (i = 0; i < batch; i++)
			s += p[i * PGSZ];		/* read-fault RO -> LtRAM */
		(void)s;

		/* write every page; verify LtRAM frames move to DRAM */
		mprotect(p, len, PROT_READ | PROT_WRITE);
		for (i = 0; i < batch; i++) {
			unsigned long b = pfn(p + i * PGSZ);
			int was_lt = b >= g_lt_start;

			p[i * PGSZ] = 0x5a;		/* triggers repatriation */
			if (was_lt) {
				unsigned long a = pfn(p + i * PGSZ);

				total_lt++;
				if (!(a != b && a < g_lt_start))
					fails++;
			}
			total_pages++;
		}
		if (++batches % 200 == 0)
			printf("  ... %lu repatriated, %lu fails, %lu batches\n",
			       total_lt, fails, batches);
	}

	mb1 = stat_get("migrated_back_of_alloc");
	wf1 = stat_get("write_faulted_of_alloc");

	printf("\n=== stress done in %lds ===\n", (long)(time(NULL) - t0));
	printf("batches              %lu\n", batches);
	printf("pages written        %lu\n", total_pages);
	printf("LtRAM repatriations  %lu\n", total_lt);
	printf("per-page verify fails %lu\n", fails);
	printf("migrated_back_of_alloc delta %ld  (want ~%lu)\n", mb1 - mb0, total_lt);
	printf("write_faulted_of_alloc delta %ld\n", wf1 - wf0);
	printf("RESULT: %s\n", fails == 0 ? "PASS" : "FAIL");

	munmap(p, len);
	close(fd);
	unlink(path);
	return fails ? 1 : 0;
}
