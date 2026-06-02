// LtRAM placement (DRAM->LtRAM migration) test suite, with data integrity.
//
// Each page is filled with a UNIQUE, regenerable pattern (an LCG seeded per
// page), so we can verify byte-for-byte that the right data survived each
// copy -- and that two pages' contents were never swapped. Cases:
//
//   1 basic          single page DRAM->LtRAM: PFN crosses, migrated_in +1
//   2 integrity_mig  N pages migrated; content preserved across DRAM->LtRAM
//   3 integrity_rt   N pages migrated, then written -> repatriated; content
//                    preserved across BOTH copies, write applied, and the
//                    repatriations land in migrated_back_of_migrated (origin)
//   4 eligibility    read-only and shared pages are rejected by the trigger
//   5 token          a tight endurance budget throttles migration (-EBUSY)
//
// Migration is driven from userspace via /sys/kernel/debug/ltram/migrate_va.
// Run as root inside the guest. Build static.

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>
#include <sys/mman.h>

#define PGSZ 4096
#define NW   (PGSZ / 4)		/* 32-bit words per page */

static unsigned long g_lt;
static int g_pid, g_pass, g_fail;

/* ---- helpers ---- */
static unsigned long pagemap_pfn(void *va)
{
	uint64_t v, off = ((uintptr_t)va / PGSZ) * 8;
	int fd = open("/proc/self/pagemap", O_RDONLY);

	if (fd < 0)
		return 0;
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
/* migrate the page at @va; returns the write() result (>0 ok, <0 on errno). */
static int migrate_va(void *va)
{
	char cmd[64]; int fd = open("/sys/kernel/debug/ltram/migrate_va", O_WRONLY), r;

	if (fd < 0) return -1;
	r = (int)write(fd, cmd, snprintf(cmd, sizeof cmd, "%d %lx", g_pid, (unsigned long)va));
	close(fd);
	return r;
}
static void set_param(const char *name, const char *val)
{
	char path[128]; int fd;

	snprintf(path, sizeof path, "/sys/module/ltram/parameters/%s", name);
	fd = open(path, O_WRONLY);
	if (fd >= 0) { if (write(fd, val, strlen(val)) < 0) {} close(fd); }
}

/* unique regenerable pattern: page <- LCG(seed) */
static void fill_pat(void *p, uint32_t seed)
{
	uint32_t *w = p, s = seed | 1;
	int j;
	for (j = 0; j < NW; j++) { s = s * 1103515245u + 12345u; w[j] = s; }
}
/* check page == LCG(seed), optionally allowing word 0 to be a sentinel. */
static int check_pat(void *p, uint32_t seed, int allow_word0_sentinel, uint32_t sentinel)
{
	uint32_t *w = p, s = seed | 1;
	int j;
	for (j = 0; j < NW; j++) {
		s = s * 1103515245u + 12345u;
		if (j == 0 && allow_word0_sentinel) {
			if (w[0] != sentinel) return 0;
		} else if (w[j] != s) {
			return 0;
		}
	}
	return 1;
}

static void report(const char *n, int ok, const char *d)
{
	if (ok) { g_pass++; printf("  [PASS] %-14s %s\n", n, d); }
	else    { g_fail++; printf("  [FAIL] %-14s %s\n", n, d); }
}

/* ---- cases ---- */
static void case_basic(void)
{
	char *p = mmap(NULL, PGSZ, PROT_READ | PROT_WRITE,
		       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	unsigned long a, b; long m0, m1; char d[96];

	if (p == MAP_FAILED) { report("basic", 0, "mmap"); return; }
	p[0] = 1;
	a = pagemap_pfn(p);
	m0 = stat_get("placed_migrated_in");
	migrate_va(p);
	b = pagemap_pfn(p);
	m1 = stat_get("placed_migrated_in");
	snprintf(d, sizeof d, "PFN %lu(DRAM)->%lu(%s), migrated_in +%ld",
		 a, b, b >= g_lt ? "LtRAM" : "DRAM", m1 - m0);
	report("basic", a < g_lt && b >= g_lt && (m1 - m0) == 1, d);
	munmap(p, PGSZ);
}

static void case_integrity_mig(void)
{
	const int N = 32;
	char *p = mmap(NULL, (size_t)N * PGSZ, PROT_READ | PROT_WRITE,
		       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	int i, moved = 0, bad = 0; long m0, m1; char d[112];

	if (p == MAP_FAILED) { report("integrity_mig", 0, "mmap"); return; }
	for (i = 0; i < N; i++) fill_pat(p + i * PGSZ, 0xA5000000u + i);
	m0 = stat_get("placed_migrated_in");
	for (i = 0; i < N; i++) { migrate_va(p + i * PGSZ); if (in_ltram(p + i * PGSZ)) moved++; }
	/* read back from LtRAM and verify each page's unique content survived */
	for (i = 0; i < N; i++) if (!check_pat(p + i * PGSZ, 0xA5000000u + i, 0, 0)) bad++;
	m1 = stat_get("placed_migrated_in");
	snprintf(d, sizeof d, "%d/%d moved, %d content-mismatch, migrated_in +%ld",
		 moved, N, bad, m1 - m0);
	report("integrity_mig", moved == N && bad == 0 && (m1 - m0) == N, d);
	munmap(p, (size_t)N * PGSZ);
}

static void case_integrity_rt(void)
{
	const int N = 32;
	const uint32_t SENT = 0xDEADBEEFu;
	char *p = mmap(NULL, (size_t)N * PGSZ, PROT_READ | PROT_WRITE,
		       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	int i, back = 0, bad = 0; long mb0, mb1; char d[128];

	if (p == MAP_FAILED) { report("integrity_rt", 0, "mmap"); return; }
	for (i = 0; i < N; i++) fill_pat(p + i * PGSZ, 0x5C000000u + i);
	for (i = 0; i < N; i++) migrate_va(p + i * PGSZ);	/* -> LtRAM, WP'd */

	mb0 = stat_get("migrated_back_of_migrated");
	for (i = 0; i < N; i++) {
		uint32_t *w = (uint32_t *)(p + i * PGSZ);

		w[0] = SENT;				/* write -> repatriate */
		if (!in_ltram(p + i * PGSZ)) back++;	/* moved back to DRAM */
		/* word0 == sentinel, words 1.. == original pattern */
		if (!check_pat(p + i * PGSZ, 0x5C000000u + i, 1, SENT)) bad++;
	}
	mb1 = stat_get("migrated_back_of_migrated");
	snprintf(d, sizeof d, "%d/%d repatriated, %d content-mismatch, mb_of_migrated +%ld",
		 back, N, bad, mb1 - mb0);
	report("integrity_rt", back == N && bad == 0 && (mb1 - mb0) == N, d);
	munmap(p, (size_t)N * PGSZ);
}

static void case_eligibility(void)
{
	char *ro = mmap(NULL, PGSZ, PROT_READ, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	char *sh = mmap(NULL, PGSZ, PROT_READ | PROT_WRITE,
			MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	int r_ro, r_sh; char d[96];

	if (ro == MAP_FAILED || sh == MAP_FAILED) { report("eligibility", 0, "mmap"); return; }
	sh[0] = 1;					/* fault shared page */
	r_ro = migrate_va(ro);				/* read-only: reject */
	r_sh = migrate_va(sh);				/* shared: reject */
	snprintf(d, sizeof d, "read-only rejected=%d, shared rejected=%d",
		 r_ro < 0, r_sh < 0);
	report("eligibility", r_ro < 0 && r_sh < 0, d);
	munmap(ro, PGSZ); munmap(sh, PGSZ);
}

static void case_token(void)
{
	const int N = 8;
	char *p = mmap(NULL, (size_t)N * PGSZ, PROT_READ | PROT_WRITE,
		       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	int i, ebusy = 0; char d[96];

	if (p == MAP_FAILED) { report("token", 0, "mmap"); return; }
	for (i = 0; i < N; i++) p[i * PGSZ] = 1;
	set_param("token_cap", "1");			/* tiny burst */
	set_param("token_rate", "1");			/* 1/s -> starves fast */
	for (i = 0; i < N; i++)
		if (migrate_va(p + i * PGSZ) < 0) ebusy++;	/* over budget */
	set_param("token_rate", "42");			/* restore */
	set_param("token_cap", "512");
	snprintf(d, sizeof d, "%d/%d migrations throttled (-EBUSY)", ebusy, N);
	report("token", ebusy > 0, d);
	munmap(p, (size_t)N * PGSZ);
}

int main(void)
{
	g_lt = ltram_start_pfn();
	g_pid = getpid();
	printf("=== LtRAM migration suite ===\nLtRAM start_pfn = %lu\n", g_lt);
	if (!g_lt) { printf("ABORT: no LtRAM zone\n"); return 1; }

	case_basic();
	case_integrity_mig();
	case_integrity_rt();
	case_eligibility();
	case_token();

	printf("=== summary: %d passed, %d failed ===\n", g_pass, g_fail);
	return g_fail ? 1 : 0;
}
