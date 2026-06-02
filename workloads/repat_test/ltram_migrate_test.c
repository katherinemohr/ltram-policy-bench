// LtRAM placement (DRAM -> LtRAM migration) mechanism test.
//
// Mirror of the repatriation test, in reverse:
//   1. Allocate a WRITABLE anonymous page and touch it -> it lands in DRAM.
//   2. Read its PFN via /proc/self/pagemap; confirm it is below the LtRAM line.
//   3. Trigger migration by writing "<pid> <va>" to
//      /sys/kernel/debug/ltram/migrate_va (token-gated in the kernel).
//   4. Re-read the PFN: it must now be >= LtRAM start (moved into LtRAM), and
//      placed_migrated_in must have incremented by 1.
//
// IMPORTANT: the migrated page is NOT write-protected yet, so we do NOT write
// it after migration (a write would land in LtRAM in place). This test only
// proves the move + the counter. Run as root in the guest.

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>
#include <sys/mman.h>

#define PGSZ 4096

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
	if (!(val & (1ULL << 63)))
		return 0;
	return (unsigned long)(val & ((1ULL << 55) - 1));
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

int main(void)
{
	unsigned long lt = ltram_start_pfn();
	unsigned long before, after;
	long mig0, mig1;
	char cmd[64];
	int fd, n;
	char *p;

	printf("LtRAM start_pfn = %lu\n", lt);
	if (!lt) {
		printf("ABORT: no LtRAM zone (wrong kernel?)\n");
		return 1;
	}

	/* 1. writable anon page -> DRAM */
	p = mmap(NULL, PGSZ, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) { perror("mmap"); return 1; }
	p[0] = 0x5a;					/* fault into DRAM */
	before = pagemap_pfn(p);
	mig0 = stat_get("placed_migrated_in");
	printf("before: VA=%p  PFN=%lu  (%s)\n", (void *)p, before,
	       before >= lt ? "LtRAM" : "DRAM");

	/* 3. trigger DRAM->LtRAM migration */
	fd = open("/sys/kernel/debug/ltram/migrate_va", O_WRONLY);
	if (fd < 0) { perror("open migrate_va"); return 1; }
	n = snprintf(cmd, sizeof(cmd), "%d %lx", getpid(), (unsigned long)p);
	if (write(fd, cmd, n) < 0)
		perror("write migrate_va");		/* report, then read result */
	close(fd);

	/* 4. verify */
	after = pagemap_pfn(p);
	mig1 = stat_get("placed_migrated_in");
	printf("after : VA=%p  PFN=%lu  (%s)\n", (void *)p, after,
	       after >= lt ? "LtRAM" : "DRAM");
	printf("PFN moved? %s  (%lu -> %lu)\n",
	       before != after ? "YES" : "NO", before, after);
	printf("placed_migrated_in delta: +%ld\n", mig1 - mig0);

	if (before < lt && after >= lt && (mig1 - mig0) >= 1)
		printf("RESULT: PASS -- DRAM page migrated to LtRAM, counter +1.\n");
	else
		printf("RESULT: FAIL -- see numbers above.\n");

	/* NOTE: do not write p here -- it is in LtRAM and not write-protected. */
	munmap(p, PGSZ);
	return 0;
}
