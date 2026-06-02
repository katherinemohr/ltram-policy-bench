// LtRAM repatriation test suite -- exhaustive correctness cases.
//
// Each case lands read-only pages in ZONE_LTRAM (cache-miss fault on a
// PROT_READ mapping), then exercises one repatriation scenario and checks the
// physical frame (via /proc/self/pagemap) and the kernel counters (via
// /sys/kernel/debug/ltram/stats). Prints PASS/FAIL per case + a summary.
//
// Cases:
//   1 basic          single RO file page: write -> moves LtRAM->DRAM, mb +1
//   2 integrity      page contents preserved across the copy, write applied
//   3 multi          N pages written -> mb +N, all moved to DRAM
//   4 idempotent     two writes to one page -> mb +1 (2nd is already DRAM)
//   5 partial        write half a region -> only those move; rest stay LtRAM
//   6 segv_no_write  write a RO page WITHOUT mprotect -> SIGSEGV, no mb change
//   7 fork_cow       fork then child writes -> child gets DRAM copy, parent ok
//   8 shared_gap     MAP_SHARED page write -> currently NOT repatriated (gap)
//   9 concurrent     N threads write one page -> exactly one repatriation
//  10 mixed_random   random writes over a DRAM+LtRAM mix: DRAM PFN unchanged,
//                    LtRAM PFN -> DRAM; migrated_back == #LtRAM pages
//  11 mixed_random_mt same mix written by N threads (disjoint slices)
//
// Run as root inside the guest (needs pagemap PFNs + drop_caches). Build static.

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>
#include <signal.h>
#include <setjmp.h>
#include <pthread.h>
#include <time.h>
#include <sys/mman.h>
#include <sys/wait.h>

#define PGSZ 4096

static unsigned long g_lt_start;	/* LtRAM zone start_pfn */
static int g_pass, g_fail, g_gap;

/* ---- helpers ------------------------------------------------------------ */

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

static int in_ltram(void *va) { return pagemap_pfn(va) >= g_lt_start; }

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

static void drop_caches(void)
{
	int fd;

	sync();
	fd = open("/proc/sys/vm/drop_caches", O_WRONLY);
	if (fd >= 0) {
		if (write(fd, "3\n", 2) < 0) { }
		close(fd);
	}
}

/*
 * Create @path filled with a per-page pattern (page i -> all bytes = i+1),
 * evict caches, and map it. @shared selects MAP_SHARED vs MAP_PRIVATE.
 * On return *pp is the mapping; every page is read-faulted (so on a miss it
 * routes to LtRAM). Returns the fd (caller closes), or -1.
 */
static int setup_ro_ltram(const char *path, long npages, int shared, char **pp)
{
	size_t len = (size_t)npages * PGSZ;
	char *p, *buf;
	int fd, flags = shared ? MAP_SHARED : MAP_PRIVATE;
	long i;

	fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0600);
	if (fd < 0 || ftruncate(fd, len) < 0) { perror("file"); return -1; }
	buf = malloc(PGSZ);
	for (i = 0; i < npages; i++) {
		memset(buf, (int)(i + 1), PGSZ);
		if (pwrite(fd, buf, PGSZ, i * PGSZ) != PGSZ) { perror("pwrite"); }
	}
	free(buf);
	fsync(fd);
	close(fd);
	drop_caches();

	fd = open(path, shared ? O_RDWR : O_RDONLY);
	if (fd < 0) { perror("reopen"); return -1; }
	p = mmap(NULL, len, PROT_READ, flags, fd, 0);
	if (p == MAP_FAILED) { perror("mmap"); close(fd); return -1; }
	madvise(p, len, MADV_RANDOM);			/* no readahead */

	volatile char sink = 0;
	for (i = 0; i < npages; i++)
		sink += p[i * PGSZ];
	(void)sink;
	*pp = p;
	return fd;
}

/* Index of the first page in the mapping that is physically in LtRAM, or -1. */
static long first_ltram_page(char *p, long npages)
{
	long i;

	for (i = 0; i < npages; i++)
		if (in_ltram(p + i * PGSZ))
			return i;
	return -1;
}

static void report(const char *name, int ok, const char *detail)
{
	if (ok) { g_pass++; printf("  [PASS] %-13s %s\n", name, detail); }
	else    { g_fail++; printf("  [FAIL] %-13s %s\n", name, detail); }
}

/* One writable test page with its pre-write frame and class. */
struct ent { char *a; unsigned long pfn; int lt; };
#define MIXED_MAX 64

/*
 * Build a shuffled set of writable pages spanning BOTH tiers: anonymous pages
 * (always DRAM) plus read-only-routed file pages (LtRAM), all made writable so
 * a store will either no-op (DRAM, PFN unchanged) or repatriate (LtRAM -> DRAM,
 * PFN changes). Returns the count, sets *exp_lt, and the two regions + fd for
 * cleanup via free_mixed().
 */
static int build_mixed(struct ent *e, int *exp_lt, char **da, char **fp,
		       int *fd, const char *path)
{
	const long ND = 24, NL = 24;
	long i;
	int n = 0;

	*exp_lt = 0;
	*da = mmap(NULL, ND * PGSZ, PROT_READ | PROT_WRITE,
		   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (*da == MAP_FAILED) { *da = NULL; return -1; }
	for (i = 0; i < ND; i++)
		(*da)[i * PGSZ] = 1;			/* fault anon -> DRAM */

	*fd = setup_ro_ltram(path, NL, 0, fp);		/* RO file -> LtRAM */
	if (*fd < 0)
		return -1;
	mprotect(*fp, NL * PGSZ, PROT_READ | PROT_WRITE); /* writable, still LtRAM */

	for (i = 0; i < ND && n < MIXED_MAX; i++) {
		unsigned long f = pagemap_pfn(*da + i * PGSZ);

		if (f && f < g_lt_start) {		/* confirmed DRAM */
			e[n].a = *da + i * PGSZ; e[n].pfn = f; e[n].lt = 0; n++;
		}
	}
	for (i = 0; i < NL && n < MIXED_MAX; i++) {
		unsigned long f = pagemap_pfn(*fp + i * PGSZ);

		if (f >= g_lt_start) {			/* confirmed LtRAM */
			e[n].a = *fp + i * PGSZ; e[n].pfn = f; e[n].lt = 1;
			n++; (*exp_lt)++;
		}
	}
	srand((unsigned)getpid() ^ (unsigned)time(NULL));
	for (i = n - 1; i > 0; i--) {			/* Fisher-Yates shuffle */
		long j = rand() % (i + 1);
		struct ent t = e[i]; e[i] = e[j]; e[j] = t;
	}
	return n;
}

static void free_mixed(char *da, char *fp, int fd, const char *path)
{
	if (da) munmap(da, 24 * PGSZ);
	if (fp) munmap(fp, 24 * PGSZ);
	if (fd >= 0) close(fd);
	unlink(path);
}

/* Write one entry and verify: DRAM stays put, LtRAM moves to DRAM. */
static int check_write(struct ent *e)
{
	unsigned long before = e->pfn, after;

	*(e->a) = 0x5a;
	after = pagemap_pfn(e->a);
	if (e->lt)
		return after != before && after < g_lt_start;	/* moved */
	return after == before;					/* unchanged */
}

/* ---- cases -------------------------------------------------------------- */

static void case_basic(void)
{
	char *p; char d[128];
	int fd = setup_ro_ltram("/root/.lt_basic", 8, 0, &p);
	long t, mb0, mb1; unsigned long a, b;

	if (fd < 0) { report("basic", 0, "setup failed"); return; }
	t = first_ltram_page(p, 8);
	if (t < 0) { report("basic", 0, "no page in LtRAM"); goto out; }
	a = pagemap_pfn(p + t * PGSZ);
	mb0 = stat_get("migrated_back_of_alloc");
	mprotect(p + t * PGSZ, PGSZ, PROT_READ | PROT_WRITE);
	p[t * PGSZ] = 0x55;
	b = pagemap_pfn(p + t * PGSZ);
	mb1 = stat_get("migrated_back_of_alloc");
	snprintf(d, sizeof d, "PFN %lu(LtRAM)->%lu(%s), mb +%ld",
		 a, b, b >= g_lt_start ? "LtRAM" : "DRAM", mb1 - mb0);
	report("basic", a >= g_lt_start && b < g_lt_start && mb1 - mb0 == 1, d);
out:
	munmap(p, 8 * PGSZ); close(fd); unlink("/root/.lt_basic");
}

static void case_integrity(void)
{
	char *p; char d[128];
	int fd = setup_ro_ltram("/root/.lt_integ", 8, 0, &p);
	long t; int ok;

	if (fd < 0) { report("integrity", 0, "setup failed"); return; }
	t = first_ltram_page(p, 8);
	if (t < 0) { report("integrity", 0, "no page in LtRAM"); goto out; }
	/* page content is all bytes == t+1; write byte 0, check rest preserved */
	mprotect(p + t * PGSZ, PGSZ, PROT_READ | PROT_WRITE);
	p[t * PGSZ] = (char)0xAB;
	ok = (unsigned char)p[t * PGSZ] == 0xAB &&
	     (unsigned char)p[t * PGSZ + 1] == (unsigned char)(t + 1) &&
	     (unsigned char)p[t * PGSZ + PGSZ - 1] == (unsigned char)(t + 1);
	snprintf(d, sizeof d, "written byte=0x%02x, preserved byte=0x%02x (want 0x%02x)",
		 (unsigned char)p[t * PGSZ], (unsigned char)p[t * PGSZ + 1],
		 (unsigned char)(t + 1));
	report("integrity", ok && !in_ltram(p + t * PGSZ), d);
out:
	munmap(p, 8 * PGSZ); close(fd); unlink("/root/.lt_integ");
}

static void case_multi(void)
{
	const long N = 32;
	char *p; char d[128];
	int fd = setup_ro_ltram("/root/.lt_multi", N, 0, &p);
	long i, mb0, mb1, moved = 0, lt = 0;

	if (fd < 0) { report("multi", 0, "setup failed"); return; }
	for (i = 0; i < N; i++) if (in_ltram(p + i * PGSZ)) lt++;
	mb0 = stat_get("migrated_back_of_alloc");
	mprotect(p, N * PGSZ, PROT_READ | PROT_WRITE);
	for (i = 0; i < N; i++) p[i * PGSZ] = 0x11;
	for (i = 0; i < N; i++) if (!in_ltram(p + i * PGSZ)) moved++;
	mb1 = stat_get("migrated_back_of_alloc");
	snprintf(d, sizeof d, "%ld pages in LtRAM, %ld moved to DRAM, mb +%ld",
		 lt, moved, mb1 - mb0);
	report("multi", lt > 0 && moved == N && (mb1 - mb0) == lt, d);
	munmap(p, N * PGSZ); close(fd); unlink("/root/.lt_multi");
}

static void case_idempotent(void)
{
	char *p; char d[128];
	int fd = setup_ro_ltram("/root/.lt_idem", 8, 0, &p);
	long t, mb0, mb1; unsigned long b1, b2;

	if (fd < 0) { report("idempotent", 0, "setup failed"); return; }
	t = first_ltram_page(p, 8);
	if (t < 0) { report("idempotent", 0, "no page in LtRAM"); goto out; }
	mb0 = stat_get("migrated_back_of_alloc");
	mprotect(p + t * PGSZ, PGSZ, PROT_READ | PROT_WRITE);
	p[t * PGSZ] = 1; b1 = pagemap_pfn(p + t * PGSZ);
	p[t * PGSZ] = 2; b2 = pagemap_pfn(p + t * PGSZ);	/* 2nd write: already DRAM */
	mb1 = stat_get("migrated_back_of_alloc");
	snprintf(d, sizeof d, "after 2 writes PFN %lu==%lu (DRAM), mb +%ld (want 1)",
		 b1, b2, mb1 - mb0);
	report("idempotent", b1 < g_lt_start && b1 == b2 && (mb1 - mb0) == 1, d);
out:
	munmap(p, 8 * PGSZ); close(fd); unlink("/root/.lt_idem");
}

static void case_partial(void)
{
	const long N = 16;
	char *p; char d[128];
	int fd = setup_ro_ltram("/root/.lt_part", N, 0, &p);
	long i, lt_first_half = 0, still_lt = 0, moved = 0;

	if (fd < 0) { report("partial", 0, "setup failed"); return; }
	for (i = 0; i < N / 2; i++) if (in_ltram(p + i * PGSZ)) lt_first_half++;
	/* write only the first half */
	mprotect(p, (N / 2) * PGSZ, PROT_READ | PROT_WRITE);
	for (i = 0; i < N / 2; i++) p[i * PGSZ] = 0x22;
	for (i = 0; i < N / 2; i++) if (!in_ltram(p + i * PGSZ)) moved++;
	for (i = N / 2; i < N; i++) if (in_ltram(p + i * PGSZ)) still_lt++;
	snprintf(d, sizeof d, "wrote %ld: moved %ld; untouched still in LtRAM %ld",
		 N / 2, moved, still_lt);
	report("partial", moved == lt_first_half && still_lt > 0, d);
	munmap(p, N * PGSZ); close(fd); unlink("/root/.lt_part");
}

static sigjmp_buf g_jb;
static void segv_handler(int s) { (void)s; siglongjmp(g_jb, 1); }

static void case_segv_no_write(void)
{
	char *p; char d[128];
	int fd = setup_ro_ltram("/root/.lt_segv", 8, 0, &p);
	long t, mb0, mb1; int caught = 0;
	struct sigaction sa = { 0 }, old;

	if (fd < 0) { report("segv_no_write", 0, "setup failed"); return; }
	t = first_ltram_page(p, 8);
	if (t < 0) { report("segv_no_write", 0, "no page in LtRAM"); goto out; }
	mb0 = stat_get("migrated_back_of_alloc");
	sa.sa_handler = segv_handler;
	sigaction(SIGSEGV, &sa, &old);
	if (sigsetjmp(g_jb, 1) == 0)
		p[t * PGSZ] = 0x33;		/* write to PROT_READ -> SIGSEGV */
	else
		caught = 1;
	sigaction(SIGSEGV, &old, NULL);
	mb1 = stat_get("migrated_back_of_alloc");
	snprintf(d, sizeof d, "SIGSEGV caught=%d, still LtRAM=%d, mb +%ld (want 0)",
		 caught, in_ltram(p + t * PGSZ), mb1 - mb0);
	report("segv_no_write", caught && in_ltram(p + t * PGSZ) && (mb1 - mb0) == 0, d);
out:
	munmap(p, 8 * PGSZ); close(fd); unlink("/root/.lt_segv");
}

static void case_fork_cow(void)
{
	char *p; char d[128];
	int fd = setup_ro_ltram("/root/.lt_fork", 8, 0, &p);
	long t; pid_t pid; int status, child_ok;

	if (fd < 0) { report("fork_cow", 0, "setup failed"); return; }
	t = first_ltram_page(p, 8);
	if (t < 0) { report("fork_cow", 0, "no page in LtRAM"); goto out; }

	pid = fork();
	if (pid == 0) {				/* child: write -> COW to DRAM */
		mprotect(p + t * PGSZ, PGSZ, PROT_READ | PROT_WRITE);
		p[t * PGSZ] = 0x44;
		_exit(in_ltram(p + t * PGSZ) ? 1 : 0);	/* 0 if moved to DRAM */
	}
	waitpid(pid, &status, 0);
	child_ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;
	/* parent's mapping must still read the original content (t+1) */
	snprintf(d, sizeof d, "child moved to DRAM=%d, parent reads 0x%02x (want 0x%02x)",
		 child_ok, (unsigned char)p[t * PGSZ], (unsigned char)(t + 1));
	report("fork_cow", child_ok && (unsigned char)p[t * PGSZ] == (unsigned char)(t + 1), d);
out:
	munmap(p, 8 * PGSZ); close(fd); unlink("/root/.lt_fork");
}

static void case_shared_gap(void)
{
	char *p; char d[128];
	int fd = setup_ro_ltram("/root/.lt_shared", 8, 1 /*shared*/, &p);
	long t, mb0, mb1; unsigned long a, b;

	if (fd < 0) { report("shared_gap", 0, "setup failed"); return; }
	t = first_ltram_page(p, 8);
	if (t < 0) {
		printf("  [SKIP] shared_gap    shared page did not land in LtRAM\n");
		goto out;
	}
	a = pagemap_pfn(p + t * PGSZ);
	mb0 = stat_get("migrated_back_of_alloc");
	mprotect(p + t * PGSZ, PGSZ, PROT_READ | PROT_WRITE);
	p[t * PGSZ] = 0x66;			/* shared write -> in place (gap) */
	b = pagemap_pfn(p + t * PGSZ);
	mb1 = stat_get("migrated_back_of_alloc");
	/* EXPECTED current behaviour: NOT repatriated (writes LtRAM in place). */
	if (b >= g_lt_start && (mb1 - mb0) == 0) {
		g_gap++;
		printf("  [GAP ] shared_gap    PFN stayed LtRAM (%lu), mb +0 "
		       "-- shared mappings not yet repatriated (needs migrate path)\n", b);
	} else {
		snprintf(d, sizeof d, "PFN %lu->%lu, mb +%ld (shared got handled!)",
			 a, b, mb1 - mb0);
		report("shared_gap", 1, d);	/* better than expected */
	}
out:
	munmap(p, 8 * PGSZ); close(fd); unlink("/root/.lt_shared");
}

struct thr_arg { char *addr; pthread_barrier_t *bar; };
static void *thr_write(void *a)
{
	struct thr_arg *t = a;
	pthread_barrier_wait(t->bar);
	*t->addr = 0x77;
	return NULL;
}

static void case_concurrent(void)
{
	const int T = 4;
	char *p; char d[128];
	int fd = setup_ro_ltram("/root/.lt_conc", 8, 0, &p);
	long t, mb0, mb1; pthread_t th[4]; struct thr_arg arg[4];
	pthread_barrier_t bar; int i;

	if (fd < 0) { report("concurrent", 0, "setup failed"); return; }
	t = first_ltram_page(p, 8);
	if (t < 0) { report("concurrent", 0, "no page in LtRAM"); goto out; }
	mb0 = stat_get("migrated_back_of_alloc");
	mprotect(p + t * PGSZ, PGSZ, PROT_READ | PROT_WRITE);
	pthread_barrier_init(&bar, NULL, T);
	for (i = 0; i < T; i++) {
		arg[i].addr = p + t * PGSZ; arg[i].bar = &bar;
		pthread_create(&th[i], NULL, thr_write, &arg[i]);
	}
	for (i = 0; i < T; i++) pthread_join(th[i], NULL);
	pthread_barrier_destroy(&bar);
	mb1 = stat_get("migrated_back_of_alloc");
	snprintf(d, sizeof d, "%d threads wrote one page, mb +%ld (want 1), now %s",
		 T, mb1 - mb0, in_ltram(p + t * PGSZ) ? "LtRAM" : "DRAM");
	report("concurrent", (mb1 - mb0) == 1 && !in_ltram(p + t * PGSZ), d);
out:
	munmap(p, 8 * PGSZ); close(fd); unlink("/root/.lt_conc");
}

static void case_mixed_random(void)
{
	struct ent e[MIXED_MAX]; char *da = NULL, *fp = NULL; char d[160];
	int fd = -1, n, exp_lt, i, bad = 0; long mb0, mb1;

	n = build_mixed(e, &exp_lt, &da, &fp, &fd, "/root/.lt_mix");
	if (n < 0) { report("mixed_random", 0, "setup failed"); goto out; }
	mb0 = stat_get("migrated_back_of_alloc");
	for (i = 0; i < n; i++)			/* random order (shuffled) */
		if (!check_write(&e[i]))
			bad++;
	mb1 = stat_get("migrated_back_of_alloc");
	snprintf(d, sizeof d, "%d pages (%d LtRAM, %d DRAM) random-written; "
		 "bad=%d; mb +%ld (want %d)",
		 n, exp_lt, n - exp_lt, bad, mb1 - mb0, exp_lt);
	report("mixed_random", exp_lt > 0 && bad == 0 && (mb1 - mb0) == exp_lt, d);
out:
	free_mixed(da, fp, fd, "/root/.lt_mix");
}

struct mt { struct ent *e; int start, count, fail; };
static void *mt_writer(void *a)
{
	struct mt *m = a;
	int i;

	for (i = m->start; i < m->start + m->count; i++)
		if (!check_write(&m->e[i]))
			m->fail++;
	return NULL;
}

static void case_mixed_random_mt(void)
{
	const int T = 4;
	struct ent e[MIXED_MAX]; char *da = NULL, *fp = NULL; char d[160];
	int fd = -1, n, exp_lt, i, off, bad = 0; long mb0, mb1;
	pthread_t th[4]; struct mt arg[4];

	n = build_mixed(e, &exp_lt, &da, &fp, &fd, "/root/.lt_mixmt");
	if (n < 0) { report("mixed_random_mt", 0, "setup failed"); goto out; }
	mb0 = stat_get("migrated_back_of_alloc");
	/* split the shuffled list into T disjoint slices -> no two threads
	 * touch the same page, so each LtRAM page repatriates exactly once. */
	off = 0;
	for (i = 0; i < T; i++) {
		int cnt = n / T + (i < n % T ? 1 : 0);
		arg[i].e = e; arg[i].start = off; arg[i].count = cnt; arg[i].fail = 0;
		off += cnt;
		pthread_create(&th[i], NULL, mt_writer, &arg[i]);
	}
	for (i = 0; i < T; i++) { pthread_join(th[i], NULL); bad += arg[i].fail; }
	mb1 = stat_get("migrated_back_of_alloc");
	snprintf(d, sizeof d, "%d pages (%d LtRAM) across %d threads; "
		 "bad=%d; mb +%ld (want %d)",
		 n, exp_lt, T, bad, mb1 - mb0, exp_lt);
	report("mixed_random_mt", exp_lt > 0 && bad == 0 && (mb1 - mb0) == exp_lt, d);
out:
	free_mixed(da, fp, fd, "/root/.lt_mixmt");
}

int main(void)
{
	g_lt_start = ltram_start_pfn();
	printf("=== LtRAM repatriation suite ===\n");
	printf("LtRAM start_pfn = %lu\n", g_lt_start);
	if (!g_lt_start) {
		printf("ABORT: no LtRAM zone (wrong kernel?)\n");
		return 1;
	}

	case_basic();
	case_integrity();
	case_multi();
	case_idempotent();
	case_partial();
	case_segv_no_write();
	case_fork_cow();
	case_shared_gap();
	case_concurrent();
	case_mixed_random();
	case_mixed_random_mt();

	printf("=== summary: %d passed, %d failed, %d known-gap ===\n",
	       g_pass, g_fail, g_gap);
	return g_fail ? 1 : 0;
}
