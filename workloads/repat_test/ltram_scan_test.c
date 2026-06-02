// LtRAM scanning-hand (autonomous placement policy) end-to-end test.
//
// Proves the policy distinguishes write-COLD from write-HOT pages:
//   - COLD: a block of anon pages written once, then never again.
//   - HOT : a block of anon pages rewritten on every iteration.
// We point the kernel scanner at ourselves (write our pid to scan_pid) and let
// it run. After a few sweeps the scanning hand should migrate the COLD pages to
// LtRAM (they aged a full lap without a write) and leave the HOT pages in DRAM
// (re-dirtied every lap). Data integrity of the migrated cold pages is checked
// with a per-page pattern. Run as root in the guest.

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>
#include <sys/mman.h>

#define PGSZ  4096
#define NW    (PGSZ / 4)
#define NCOLD 256
#define NHOT  64

static unsigned long g_lt;

static unsigned long pagemap_pfn(void *va)
{
	uint64_t v, off = ((uintptr_t)va / PGSZ) * 8;
	int fd = open("/proc/self/pagemap", O_RDONLY);

	if (fd < 0) return 0;
	if (pread(fd, &v, 8, off) != 8) { close(fd); return 0; }
	close(fd);
	return (v & (1ULL << 63)) ? (unsigned long)(v & ((1ULL << 55) - 1)) : 0;
}
static int in_ltram(void *va) { return pagemap_pfn(va) >= g_lt; }

static unsigned long ltram_start_pfn(void)
{
	FILE *f = fopen("/proc/zoneinfo", "r");
	char line[256]; int cur = 0; unsigned long s = 0;

	if (!f) return 0;
	while (fgets(line, sizeof line, f)) {
		if (strstr(line, ", zone")) cur = (strstr(line, "LtRAM") != NULL);
		if (cur && strstr(line, "start_pfn:"))
			sscanf(strstr(line, "start_pfn:") + 10, "%lu", &s);
	}
	fclose(f);
	return s;
}
static long stat_get(const char *key)
{
	FILE *f = fopen("/sys/kernel/debug/ltram/stats", "r");
	char line[256]; long v = -1;

	if (!f) return -1;
	while (fgets(line, sizeof line, f))
		if (!strncmp(line, key, strlen(key))) { sscanf(line + strlen(key), "%ld", &v); break; }
	fclose(f);
	return v;
}
static void set_param(const char *name, const char *val)
{
	char path[128]; int fd;

	snprintf(path, sizeof path, "/sys/module/ltram/parameters/%s", name);
	fd = open(path, O_WRONLY);
	if (fd >= 0) { if (write(fd, val, strlen(val)) < 0) {} close(fd); }
}
static void scan_set(int pid)
{
	int fd = open("/sys/kernel/debug/ltram/scan_pid", O_WRONLY);
	char b[16];

	if (fd < 0) return;
	if (write(fd, b, snprintf(b, sizeof b, "%d", pid)) < 0) {}
	close(fd);
}
static void fill_pat(void *p, uint32_t seed)
{
	uint32_t *w = p, s = seed | 1; int j;
	for (j = 0; j < NW; j++) { s = s * 1103515245u + 12345u; w[j] = s; }
}
static int check_pat(void *p, uint32_t seed)
{
	uint32_t *w = p, s = seed | 1; int j;
	for (j = 0; j < NW; j++) { s = s * 1103515245u + 12345u; if (w[j] != s) return 0; }
	return 1;
}

int main(void)
{
	char *cold, *hot;
	int i, cold_lt, hot_lt, bad, t;
	long m0, m1;

	g_lt = ltram_start_pfn();
	if (!g_lt) { printf("ABORT: no LtRAM zone\n"); return 1; }
	printf("=== scanning-hand test ===\nLtRAM start_pfn = %lu\n", g_lt);

	cold = mmap(NULL, NCOLD * PGSZ, PROT_READ | PROT_WRITE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	hot  = mmap(NULL, NHOT * PGSZ, PROT_READ | PROT_WRITE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (cold == MAP_FAILED || hot == MAP_FAILED) { perror("mmap"); return 1; }

	for (i = 0; i < NCOLD; i++) fill_pat(cold + i * PGSZ, 0xC0DE0000u + i);
	for (i = 0; i < NHOT; i++)  hot[i * PGSZ] = 1;

	set_param("token_rate", "0");		/* unlimited so the test is quick */
	m0 = stat_get("scan_migrated");
	scan_set(getpid());			/* point the scanner at us */

	/* Run ~4 s of sweeps; keep the HOT block dirty every 100 ms. */
	for (t = 0; t < 40; t++) {
		for (i = 0; i < NHOT; i++)
			((volatile char *)hot)[i * PGSZ] += 1;
		usleep(100 * 1000);
	}

	scan_set(-1);				/* stop scanning */
	m1 = stat_get("scan_migrated");

	/* Measure placement + integrity. */
	cold_lt = hot_lt = bad = 0;
	for (i = 0; i < NCOLD; i++) {
		if (in_ltram(cold + i * PGSZ)) cold_lt++;
		if (!check_pat(cold + i * PGSZ, 0xC0DE0000u + i)) bad++;
	}
	for (i = 0; i < NHOT; i++)
		if (in_ltram(hot + i * PGSZ)) hot_lt++;

	set_param("token_rate", "42");		/* restore endurance budget */

	printf("scan_migrated delta : %ld\n", m1 - m0);
	printf("COLD in LtRAM       : %d / %d  (want most)\n", cold_lt, NCOLD);
	printf("HOT  in LtRAM       : %d / %d  (want ~0)\n", hot_lt, NHOT);
	printf("COLD integrity bad  : %d  (want 0)\n", bad);
	/* Policy is correct if it migrated the cold majority, kept the hot block
	 * in DRAM, and preserved data. Allow a few stragglers either way. */
	if (cold_lt >= NCOLD * 9 / 10 && hot_lt <= NHOT / 10 && bad == 0)
		printf("RESULT: PASS -- scanner migrated write-cold, kept write-hot.\n");
	else
		printf("RESULT: FAIL -- see numbers above.\n");

	munmap(cold, NCOLD * PGSZ);
	munmap(hot, NHOT * PGSZ);
	return 0;
}
