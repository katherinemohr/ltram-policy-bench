// LtRAM migration stress: millions of DRAM->LtRAM migrations + repatriations,
// with per-page data integrity and wear. Mirrors repat_stress but exercises the
// MIGRATION path (placed_migrated_in) plus the migrate->write->repatriate loop.
//
// Each cycle: migrate a batch DRAM->LtRAM (via the batch trigger), then write
// word0 of each page -> repatriate to DRAM (so the next cycle can migrate
// again). Word 0 carries a sentinel; words 1.. are a fixed per-page pattern that
// must survive every migration AND repatriation copy -> a final byte check
// catches any corruption or page swap across the whole run.
//
// Usage:  ltram_migrate_stress [target_migrations] [batch_pages]   (default 1e6 512)
// Run as root in the guest. Build static.

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
#define NW   (PGSZ / 4)

static unsigned long g_lt;
static int g_pid;

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
static void migrate_range(void *start, long n)
{
	char cmd[96]; int fd = open("/sys/kernel/debug/ltram/migrate_range", O_WRONLY);

	if (fd < 0) return;
	if (write(fd, cmd, snprintf(cmd, sizeof cmd, "%d %lx %ld",
				    g_pid, (unsigned long)start, n)) < 0) {}
	close(fd);
}
static void fill_pat(void *p, uint32_t seed)
{
	uint32_t *w = p, s = seed | 1; int j;
	for (j = 0; j < NW; j++) { s = s * 1103515245u + 12345u; w[j] = s; }
}
/* words 1.. must equal the pattern (word 0 is the mutable sentinel). */
static int check_rest(void *p, uint32_t seed)
{
	uint32_t *w = p, s = seed | 1; int j;
	for (j = 0; j < NW; j++) {
		s = s * 1103515245u + 12345u;
		if (j > 0 && w[j] != s) return 0;
	}
	return 1;
}

int main(int argc, char **argv)
{
	unsigned long target = (argc > 1) ? strtoul(argv[1], NULL, 10) : 1000000UL;
	long batch = (argc > 2) ? atol(argv[2]) : 512;
	unsigned long done = 0, cycles = 0;
	long mi0, mi1, mb0, mb1;
	int i, integ_bad = 0;
	char *p;
	time_t t0;

	g_lt = ltram_start_pfn();
	g_pid = getpid();
	if (!g_lt) { printf("ABORT: no LtRAM zone\n"); return 1; }
	printf("migrate-stress: target=%lu migrations, batch=%ld pages\n", target, batch);

	p = mmap(NULL, (size_t)batch * PGSZ, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) { perror("mmap"); return 1; }
	for (i = 0; i < batch; i++)
		fill_pat(p + i * PGSZ, 0x1234u + i);	/* unique per page -> DRAM */

	set_param("token_rate", "0");			/* unlimited for the stress */
	mi0 = stat_get("placed_migrated_in");
	mb0 = stat_get("migrated_back_of_migrated");
	t0 = time(NULL);

	while (done < target) {
		migrate_range(p, batch);		/* DRAM -> LtRAM (whole batch) */
		for (i = 0; i < batch; i++)		/* write word0 -> repatriate */
			((uint32_t *)(p + i * PGSZ))[0] = 0xBEEF0000u + (cycles & 0xffff);
		done += batch;
		if (++cycles % 200 == 0)
			printf("  ... %lu migrations, %lu cycles\n", done, cycles);
	}

	for (i = 0; i < batch; i++)			/* final integrity */
		if (!check_rest(p + i * PGSZ, 0x1234u + i))
			integ_bad++;

	mi1 = stat_get("placed_migrated_in");
	mb1 = stat_get("migrated_back_of_migrated");
	set_param("token_rate", "42");			/* restore endurance budget */

	printf("\n=== migrate-stress done in %lds ===\n", (long)(time(NULL) - t0));
	printf("migrations issued                %lu\n", done);
	printf("placed_migrated_in delta         %ld  (want ~%lu)\n", mi1 - mi0, done);
	printf("migrated_back_of_migrated delta  %ld  (want ~%lu)\n", mb1 - mb0, done);
	printf("integrity mismatches             %d  (want 0)\n", integ_bad);
	printf("RESULT: %s\n", integ_bad == 0 ? "PASS" : "FAIL");

	munmap(p, (size_t)batch * PGSZ);
	return integ_bad ? 1 : 0;
}
