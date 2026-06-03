// numa_lat: raw memory latency + sequential bandwidth on the current NUMA binding.
//
// Latency  : random pointer-chase over a 256 MB buffer (>> LLC). Each load
//            depends on the previous one, so the HW prefetcher and MLP can't
//            hide it -> true load-to-use latency (lmbench lat_mem_rd style).
// Seq read : streaming sum (prefetchable) -> read BANDWIDTH (not latency).
// Seq write: streaming store              -> write BANDWIDTH.
//
// Run the SAME binary under two bindings to compare DRAM(node0) vs LtRAM(node1):
//   numactl --cpunodebind=0 --membind=0 ./numa_lat   # local  (config-A backing)
//   numactl --cpunodebind=0 --membind=1 ./numa_lat   # remote (config-B/LtRAM backing)
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

#define BUF  (256UL * 1024 * 1024)   /* 256 MB, matches the LtRAM tier size */
#define LINE 64UL

static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec * 1e-9;
}

int main(void)
{
	void **a = aligned_alloc(LINE, BUF);
	if (!a) { perror("alloc"); return 1; }
	memset(a, 0, BUF);                       /* fault in + first-touch on bound node */

	size_t m    = BUF / LINE;                /* number of cache lines */
	size_t step = LINE / sizeof(void *);     /* void* slots per cache line (=8) */
	size_t *perm = malloc(m * sizeof(size_t));
	for (size_t i = 0; i < m; i++) perm[i] = i;
	for (size_t i = m - 1; i > 0; i--) {     /* Fisher-Yates shuffle */
		size_t j = (size_t)rand() % (i + 1);
		size_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
	}
	/* link each cache line to the next line in the random permutation (one cycle) */
	for (size_t i = 0; i < m; i++)
		a[perm[i] * step] = (void *)&a[perm[(i + 1) % m] * step];

	/* --- latency: dependent random chase. XOR each pointer into a checksum
	 * so the optimizer cannot drop the loop (printed at the end). --- */
	void **p = (void **)&a[perm[0] * step];
	size_t iters = 30UL * 1000 * 1000;
	uintptr_t chk = 0;
	double t0 = now();
	for (size_t i = 0; i < iters; i++) { p = (void **)*p; chk ^= (uintptr_t)p; }
	double t1 = now();
	double lat_ns = (t1 - t0) / iters * 1e9;

	/* --- sequential read bandwidth (plain sum, printed so it stays live) --- */
	uint64_t *b = (uint64_t *)a;
	size_t nb = BUF / sizeof(uint64_t);
	uint64_t s = 0;
	int reps = 10;
	double r0 = now();
	for (int r = 0; r < reps; r++) for (size_t i = 0; i < nb; i++) s += b[i];
	double r1 = now();
	double rd_bw = (double)reps * BUF / (r1 - r0) / 1e9;

	/* --- sequential write bandwidth --- */
	double w0 = now();
	for (int r = 0; r < reps; r++) for (size_t i = 0; i < nb; i++) b[i] = i;
	double w1 = now();
	double wr_bw = (double)reps * BUF / (w1 - w0) / 1e9;

	printf("latency_random_ns %.2f\n", lat_ns);
	printf("seq_read_GBps     %.2f\n", rd_bw);
	printf("seq_write_GBps    %.2f\n", wr_bw);
	printf("# keepalive chk=%lx s=%lu\n", (unsigned long)chk, (unsigned long)s);
	return 0;
}
