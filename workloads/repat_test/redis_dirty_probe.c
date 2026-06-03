// redis_dirty_probe: measure how many of a process's resident pages actually get
// WRITTEN during a window, and WHICH regions -- the hard ceiling on LtRAM offload
// plus a per-VMA breakdown so we can name what's doing the writing.
//
// Method (real Linux soft-dirty tracking, CONFIG_MEM_SOFT_DIRTY=y):
//   1. echo 4 > /proc/<pid>/clear_refs   (clear soft-dirty + write-protect all)
//   2. sleep <seconds>                    (a workload runs against it)
//   3. walk /proc/<pid>/pagemap          (bit 55 = soft-dirty -> got written)
//
// Usage:  redis_dirty_probe <pid> <seconds>
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>
#include <inttypes.h>

#define PGSZ 4096
#define MAXR 4096

struct region { uint64_t a, b; char tag[40]; long present, dirty; };
static struct region R[MAXR];
static int nR;
static long present, dirty, anon_present, anon_dirty;

static void scan_range(int pmfd, struct region *r, int is_anon_rw)
{
	for (uint64_t va = r->a; va < r->b; va += PGSZ) {
		uint64_t e, off = (va / PGSZ) * 8;
		if (pread(pmfd, &e, 8, off) != 8) continue;
		if (!(e & (1ULL << 63))) continue;          /* not present */
		int sd = (e >> 55) & 1;                     /* soft-dirty bit */
		present++; r->present++;
		if (sd) { dirty++; r->dirty++; }
		if (is_anon_rw) { anon_present++; if (sd) anon_dirty++; }
	}
}

static int by_dirty(const void *x, const void *y)
{
	long d = ((const struct region *)y)->dirty - ((const struct region *)x)->dirty;
	return d > 0 ? 1 : d < 0 ? -1 : 0;
}

int main(int argc, char **argv)
{
	if (argc < 3) { fprintf(stderr, "usage: %s <pid> <seconds>\n", argv[0]); return 1; }
	int pid = atoi(argv[1]), secs = atoi(argv[2]);
	char path[64], line[512];

	snprintf(path, sizeof path, "/proc/%d/clear_refs", pid);
	int fd = open(path, O_WRONLY);
	if (fd < 0) { perror("clear_refs"); return 1; }
	if (write(fd, "4\n", 2) < 0) perror("write clear_refs");
	close(fd);
	printf("[probe] cleared soft-dirty on pid %d; watching %d s of activity...\n", pid, secs);
	sleep(secs);

	snprintf(path, sizeof path, "/proc/%d/maps", pid);
	FILE *m = fopen(path, "r");
	snprintf(path, sizeof path, "/proc/%d/pagemap", pid);
	int pm = open(path, O_RDONLY);
	if (!m || pm < 0) { perror("maps/pagemap"); return 1; }

	while (fgets(line, sizeof line, m) && nR < MAXR) {
		uint64_t a, b; char perms[8] = {0}, rest[400] = {0};
		if (sscanf(line, "%" SCNx64 "-%" SCNx64 " %7s %*s %*s %*s %399[^\n]",
			   &a, &b, perms, rest) < 3) continue;
		int is_file = strchr(rest, '/') != NULL;
		int is_special = strstr(rest, "[stack]") || strstr(rest, "[vsyscall]") ||
				 strstr(rest, "[vvar]") || strstr(rest, "[vdso]");
		int anon_rw = (perms[1] == 'w' && perms[3] == 'p' && !is_file && !is_special);
		struct region *r = &R[nR++];
		r->a = a; r->b = b; r->present = r->dirty = 0;
		/* tag: file basename, [heap], or anon size */
		char *sl = strrchr(rest, '/');
		if (sl) snprintf(r->tag, sizeof r->tag, "%s", sl + 1);
		else if (rest[0]) snprintf(r->tag, sizeof r->tag, "%s", rest);
		else snprintf(r->tag, sizeof r->tag, "anon-%s", anon_rw ? "rw" : "ro");
		scan_range(pm, r, anon_rw);
	}
	fclose(m); close(pm);

	printf("=== dirty probe (pid %d, %d s) ===\n", pid, secs);
	printf("all present pages : %ld   written %ld (%.1f%%)\n",
	       present, dirty, present ? 100.0 * dirty / present : 0);
	printf("anon-rw (data)    : %ld   written %ld (%.1f%%)  <- ceiling on non-offloadable\n",
	       anon_present, anon_dirty, anon_present ? 100.0 * anon_dirty / anon_present : 0);
	printf("=> at best ~%.1f%% of the data is write-cold and could stay in LtRAM\n",
	       anon_present ? 100.0 * (anon_present - anon_dirty) / anon_present : 0);

	qsort(R, nR, sizeof R[0], by_dirty);
	printf("\ntop regions by pages written (where the writes land):\n");
	printf("  %-22s %10s %10s %7s\n", "region", "present", "written", "dirty%");
	for (int i = 0; i < nR && i < 12 && R[i].dirty > 0; i++)
		printf("  %-22.22s %10ld %10ld %6.1f%%   [%.1f MB region]\n",
		       R[i].tag, R[i].present, R[i].dirty,
		       R[i].present ? 100.0 * R[i].dirty / R[i].present : 0,
		       (R[i].b - R[i].a) / 1048576.0);
	return 0;
}
