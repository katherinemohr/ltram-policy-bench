// numa_matrix: effective per-cache-line access time for a chosen access pattern
// and working-set size, to isolate the three latency-hiders (cache, MLP, prefetch).
//
//   chase  : dependent pointer-chase   -> NO MLP; prefetch useless (random)
//   gather : independent random index  -> MLP yes; prefetch useless (random)
//   seq    : sequential                -> MLP yes; prefetch yes
//
// Combine pattern x size to hit each realizable cell:
//   chase  <big>  = "none" (raw latency)        chase  <small> = "cache only"
//   gather <big>  = "MLP only"                  gather <small> = "cache+MLP"
//   seq    <big>  = "MLP+prefetch"              seq    <small> = "all three"
// (run the SAME binary under numactl --membind=0 vs =1 for local vs remote)
//
// usage: numa_matrix <chase|gather|seq> <size_KB>     (size must be a power of 2)
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

#define LINE 64UL
static double now(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec + t.tv_nsec*1e-9; }

int main(int argc, char **argv)
{
	const char *mode = argc > 1 ? argv[1] : "chase";
	size_t kb = argc > 2 ? strtoul(argv[2], 0, 10) : 262144;   /* 256 MB default */
	size_t BUF = kb * 1024UL;
	size_t m    = BUF / LINE;              /* number of cache lines */
	size_t step = LINE / sizeof(void *);   /* void* slots per line (=8) */
	size_t mask = m - 1;                   /* m is a power of 2 for pow2 KB sizes */

	void **a = aligned_alloc(LINE, BUF);
	if (!a) { perror("alloc"); return 1; }
	memset(a, 0, BUF);

	size_t *perm = malloc(m * sizeof(size_t));
	for (size_t i = 0; i < m; i++) perm[i] = i;
	for (size_t i = m - 1; i > 0; i--) { size_t j = (size_t)rand() % (i + 1); size_t t = perm[i]; perm[i] = perm[j]; perm[j] = t; }

	size_t iters = 40UL * 1000 * 1000;
	double t0, t1, ns = 0, gbps = 0;

	if (!strcmp(mode, "chase")) {
		for (size_t i = 0; i < m; i++) a[perm[i]*step] = (void *)&a[perm[(i+1)%m]*step];
		void **p = (void **)&a[perm[0]*step]; uintptr_t chk = 0;
		t0 = now(); for (size_t i = 0; i < iters; i++) { p = (void **)*p; chk ^= (uintptr_t)p; } t1 = now();
		ns = (t1 - t0) / iters * 1e9; printf("# chk=%lx\n", (unsigned long)chk);
	} else if (!strcmp(mode, "gather")) {
		uint64_t *b = (uint64_t *)a; uint64_t s = 0;            /* independent loads -> MLP overlaps them */
		t0 = now(); for (size_t i = 0; i < iters; i++) s += b[perm[i & mask]*step]; t1 = now();
		ns = (t1 - t0) / iters * 1e9; printf("# s=%lu\n", (unsigned long)s);
	} else { /* seq */
		uint64_t *b = (uint64_t *)a; size_t nb = BUF / sizeof(uint64_t), lines = BUF / LINE;
		int reps = (int)(iters / lines) + 1; uint64_t s = 0;
		t0 = now(); for (int r = 0; r < reps; r++) for (size_t i = 0; i < nb; i++) s += b[i]; t1 = now();
		double acc = (double)reps * lines; ns = (t1 - t0) / acc * 1e9;
		gbps = (double)reps * BUF / (t1 - t0) / 1e9; printf("# s=%lu\n", (unsigned long)s);
	}
	printf("mode=%s size_KB=%zu ns_per_line=%.2f", mode, kb, ns);
	if (gbps > 0) printf(" GBps=%.2f", gbps);
	printf("\n");
	return 0;
}
