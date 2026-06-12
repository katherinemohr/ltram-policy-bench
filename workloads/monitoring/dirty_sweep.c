/*
 * dirty_sweep.c — periodic soft-dirty sweep of a target process.
 *
 *   Every <interval_ms>:
 *     1. write "4" to /proc/<pid>/clear_refs   (clear soft-dirty bits)
 *     2. sleep <interval_ms>
 *     3. read /proc/<pid>/pagemap for each VMA, check bit 55 (soft-dirty)
 *        and bit 63 (present), increment per-page counters
 *
 * On the next iteration we re-clear and re-read, accumulating "in how many
 * intervals was this page written". Stops when the target pid exits.
 *
 * Build (on host, x86-64):  gcc -O2 -o dirty_sweep dirty_sweep.c
 * Run in VM:                dirty_sweep <pid> <output.csv> [<interval_ms>=100]
 *
 * Output CSV: a comment line with run metadata, then per-page rows.
 *   # total_sweeps=300 total_seconds=30.04 interval_ms=100
 *   vma_start,vma_end,vma_perms,vma_path,vpage_idx,present_count,dirty_count
 *   0x7f..., 0x7f..., rw-p, [heap], 0, 300, 287
 *   ...
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <time.h>

// Set by SIGINT/SIGTERM handler. Loop checks this and breaks out so the CSV
// gets written before exit, even when the user kills the process.
static volatile sig_atomic_t stop_requested = 0;
static void on_signal(int sig) { (void)sig; stop_requested = 1; }

#define PAGE_SIZE       4096
#define BIT_PRESENT     (1ULL << 63)
#define BIT_SOFT_DIRTY  (1ULL << 55)
#define MAX_VMAS        4096

// Per-page "incarnation" state, tracked IN PARALLEL with the legacy
// accumulators below. An incarnation is a maximal contiguous run of sweeps in
// which the page is present; a present->absent->present gap starts a new one
// (see C02 in the GRILL). This is what dirty_lifecycle_plot.py / the
// eligibility plot consume; the legacy fields keep dirty_sweep.csv unchanged.
struct page_inc {
    int  open;               // is an incarnation currently open for this page?
    int  count;              // how many incarnations opened so far (idx = count-1)
    int  first_seen;         // sweep this incarnation first became present
    int  last_present;       // most recent sweep present (becomes last_seen)
    int  present_count;      // sweeps present within this incarnation
    int  dirty_count;        // sweeps dirty within this incarnation
    int *write_events;       // sweep numbers written, within this incarnation
    int  write_event_count;
    int  write_event_cap;
};

struct vma {
    uint64_t start, end;
    char perms[8];
    char path[256];
    int n_pages;
    int *present_count;
    int *dirty_count;
    int *current_stab_period;   // per-page: consecutive clean sweeps so far (0 if just written)
    int *max_stab_period;       // per-page: longest stability period observed
    // Per-page write event log: array of sweep numbers at which the page
    // was observed dirty. Used by dirty_timeline_plot.py to draw per-page
    // timelines. Each list is dynamically grown.
    int **write_events;
    int  *write_event_count;
    int  *write_event_cap;
    // Per-page incarnation state (parallel to the above; feeds the lifecycle
    // sidecar CSV). One struct per page, (re)allocated alongside n_pages.
    struct page_inc *inc;
};

// Current sweep number, set just before each call to sweep_pagemap so the
// per-page event log can record when each write was observed.
static int g_current_sweep = 0;

static struct vma vmas[MAX_VMAS];
static int n_vmas = 0;

// Global histogram of stability period lengths.
// stability_hist[L]       = total (page, period) pairs of length L
// stability_hist_final[L] = subset of those that are FINAL (active at end of
//                           run, not terminated by a subsequent write)
// Dynamically resized as we encounter longer periods.
static uint64_t *stability_hist       = NULL;
static uint64_t *stability_hist_final = NULL;
static size_t    stability_hist_cap   = 0;

static void log_stability_period(int len, int is_final) {
    if (len <= 0) return;
    if ((size_t)len >= stability_hist_cap) {
        size_t new_cap = stability_hist_cap ? stability_hist_cap : 1024;
        while ((size_t)len >= new_cap) new_cap *= 2;
        stability_hist       = realloc(stability_hist,       new_cap * sizeof(uint64_t));
        stability_hist_final = realloc(stability_hist_final, new_cap * sizeof(uint64_t));
        if (!stability_hist || !stability_hist_final) {
            fprintf(stderr, "realloc(stability_hist[*], %zu) failed\n", new_cap);
            exit(1);
        }
        memset(&stability_hist      [stability_hist_cap], 0,
               (new_cap - stability_hist_cap) * sizeof(uint64_t));
        memset(&stability_hist_final[stability_hist_cap], 0,
               (new_cap - stability_hist_cap) * sizeof(uint64_t));
        stability_hist_cap = new_cap;
    }
    stability_hist[len]++;
    if (is_final) stability_hist_final[len]++;
}

// === Incarnation records (lifecycle sidecar) ============================
// Completed incarnations are buffered here and flushed to
// dirty_sweep_lifecycle.csv at end of run. We keep only an index back into
// `vmas` (resolved to start/end/perms/path at write time) so the row's
// vma_perms matches the final perms — same convention dirty_sweep.csv uses.
struct incarnation_rec {
    int vma_idx;
    int vpage_idx;
    int incarnation_idx;
    int first_seen;
    int last_seen;
    int present_count;
    int dirty_count;
    int *write_events;       // owned copy
    int write_event_count;
};

static struct incarnation_rec *inc_recs = NULL;
static size_t inc_recs_count = 0;
static size_t inc_recs_cap   = 0;

static void append_event(int **list, int *count, int *cap, int sweep) {
    if (*count >= *cap) {
        int nc = *cap ? *cap * 2 : 4;
        int *grown = realloc(*list, (size_t)nc * sizeof(int));
        if (!grown) { fprintf(stderr, "realloc write_events failed\n"); exit(1); }
        *list = grown;
        *cap = nc;
    }
    (*list)[*count] = sweep;
    (*count)++;
}

static void push_inc_rec(struct incarnation_rec r) {
    if (inc_recs_count >= inc_recs_cap) {
        size_t nc = inc_recs_cap ? inc_recs_cap * 2 : 1024;
        struct incarnation_rec *grown = realloc(inc_recs, nc * sizeof(*inc_recs));
        if (!grown) { fprintf(stderr, "realloc inc_recs failed\n"); exit(1); }
        inc_recs = grown;
        inc_recs_cap = nc;
    }
    inc_recs[inc_recs_count++] = r;
}

// Close the open incarnation for vmas[i].inc[p], emitting a completed record.
// last_seen = the most recent present sweep (the gap or end-of-run boundary).
static void close_incarnation(int i, int p) {
    struct page_inc *pi = &vmas[i].inc[p];
    if (!pi->open) return;
    struct incarnation_rec r;
    r.vma_idx         = i;
    r.vpage_idx       = p;
    r.incarnation_idx = pi->count - 1;
    r.first_seen      = pi->first_seen;
    r.last_seen       = pi->last_present;
    r.present_count   = pi->present_count;
    r.dirty_count     = pi->dirty_count;
    r.write_event_count = pi->write_event_count;
    if (pi->write_event_count > 0) {
        r.write_events = malloc((size_t)pi->write_event_count * sizeof(int));
        if (!r.write_events) { fprintf(stderr, "malloc rec write_events failed\n"); exit(1); }
        memcpy(r.write_events, pi->write_events,
               (size_t)pi->write_event_count * sizeof(int));
    } else {
        r.write_events = NULL;
    }
    push_inc_rec(r);
    pi->open = 0;
}

// Idempotent maps refresh. Called every sweep so we catch new/grown VMAs as
// the workload allocates more memory at runtime. Strategy:
//   * For each VMA in /proc/<pid>/maps:
//       - if its start address matches an existing entry, extend the per-page
//         arrays if the end grew (new pages start at count 0)
//       - otherwise append as a new VMA
//   * VMAs that disappear from /proc are kept in our list so their accumulated
//     counts are preserved for the final CSV.
static int merge_maps(int pid) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/maps", pid);
    FILE *f = fopen(path, "r");
    if (!f) return -1;

    char line[1024];
    while (fgets(line, sizeof(line), f)) {
        uint64_t start, end;
        char perms[8] = {0}, vpath[256] = {0};
        int parsed = sscanf(line, "%" SCNx64 "-%" SCNx64 " %7s %*s %*s %*s %255[^\n]",
                            &start, &end, perms, vpath);
        if (parsed < 3) continue;
        if (vpath[0] == '\0') strcpy(vpath, "[anon]");

        // Look up by start address
        struct vma *v = NULL;
        for (int i = 0; i < n_vmas; i++) {
            if (vmas[i].start == start) { v = &vmas[i]; break; }
        }

        int new_pages = (end - start) / PAGE_SIZE;

        if (v) {
            // Refresh perms and path — they can change at runtime via
            // mprotect() (e.g., matmul mprotects matrix A to PROT_READ
            // after filling it). Without this update, the page would
            // be misclassified as Class 2 even after becoming static-RO.
            strncpy(v->perms, perms, sizeof(v->perms) - 1);
            v->perms[sizeof(v->perms) - 1] = '\0';
            strncpy(v->path, vpath, sizeof(v->path) - 1);
            v->path[sizeof(v->path) - 1] = '\0';

            // Extend arrays if the VMA grew.
            if (new_pages > v->n_pages) {
                int *p = realloc(v->present_count, new_pages * sizeof(int));
                int *d = realloc(v->dirty_count,   new_pages * sizeof(int));
                int *s = realloc(v->current_stab_period, new_pages * sizeof(int));
                int *m = realloc(v->max_stab_period,     new_pages * sizeof(int));
                int **we   = realloc(v->write_events,     new_pages * sizeof(int*));
                int *wec   = realloc(v->write_event_count, new_pages * sizeof(int));
                int *wecp  = realloc(v->write_event_cap,   new_pages * sizeof(int));
                struct page_inc *pinc = realloc(v->inc, new_pages * sizeof(struct page_inc));
                if (!p || !d || !s || !m || !we || !wec || !wecp || !pinc) {
                    fprintf(stderr, "realloc failed for VMA %lx\n", v->start);
                    fclose(f);
                    return -1;
                }
                memset(&p[v->n_pages], 0, (new_pages - v->n_pages) * sizeof(int));
                memset(&d[v->n_pages], 0, (new_pages - v->n_pages) * sizeof(int));
                memset(&s[v->n_pages], 0, (new_pages - v->n_pages) * sizeof(int));
                memset(&m[v->n_pages], 0, (new_pages - v->n_pages) * sizeof(int));
                memset(&we [v->n_pages], 0, (new_pages - v->n_pages) * sizeof(int*));
                memset(&wec[v->n_pages], 0, (new_pages - v->n_pages) * sizeof(int));
                memset(&wecp[v->n_pages],0, (new_pages - v->n_pages) * sizeof(int));
                memset(&pinc[v->n_pages], 0,
                       (new_pages - v->n_pages) * sizeof(struct page_inc));
                v->present_count = p;
                v->dirty_count   = d;
                v->current_stab_period = s;
                v->max_stab_period     = m;
                v->write_events = we;
                v->write_event_count = wec;
                v->write_event_cap   = wecp;
                v->inc = pinc;
                v->n_pages = new_pages;
                v->end = end;
            }
        } else {
            // New VMA — append
            if (n_vmas >= MAX_VMAS) {
                static int warned = 0;
                if (!warned) {
                    fprintf(stderr, "[dirty_sweep] hit MAX_VMAS=%d, dropping new\n",
                            MAX_VMAS);
                    warned = 1;
                }
                continue;
            }
            v = &vmas[n_vmas];
            v->start = start;
            v->end = end;
            strncpy(v->perms, perms, sizeof(v->perms) - 1);
            v->perms[sizeof(v->perms) - 1] = '\0';
            strncpy(v->path, vpath, sizeof(v->path) - 1);
            v->path[sizeof(v->path) - 1] = '\0';
            v->n_pages = new_pages;
            v->present_count  = calloc(v->n_pages, sizeof(int));
            v->dirty_count    = calloc(v->n_pages, sizeof(int));
            v->current_stab_period = calloc(v->n_pages, sizeof(int));
            v->max_stab_period     = calloc(v->n_pages, sizeof(int));
            v->write_events     = calloc(v->n_pages, sizeof(int*));
            v->write_event_count = calloc(v->n_pages, sizeof(int));
            v->write_event_cap   = calloc(v->n_pages, sizeof(int));
            v->inc               = calloc(v->n_pages, sizeof(struct page_inc));
            if (!v->present_count || !v->dirty_count || !v->current_stab_period ||
                !v->max_stab_period || !v->write_events || !v->write_event_count ||
                !v->write_event_cap || !v->inc) {
                fprintf(stderr, "calloc failed for new VMA %lx (%d pages)\n",
                        v->start, v->n_pages);
                fclose(f);
                return -1;
            }
            n_vmas++;
        }
    }
    fclose(f);
    return 0;
}

static int clear_soft_dirty(int pid) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/clear_refs", pid);
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    int r = write(fd, "4\n", 2);
    close(fd);
    return (r == 2) ? 0 : -1;
}

static int target_alive(int pid) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/clear_refs", pid);
    return access(path, F_OK) == 0;
}

static void sweep_pagemap(int pagemap_fd) {
    for (int i = 0; i < n_vmas; i++) {
        struct vma *v = &vmas[i];
        if (v->perms[0] != 'r') continue;       // skip non-readable VMAs
        if (v->n_pages <= 0) continue;

        off_t off = (off_t)(v->start / PAGE_SIZE) * 8;
        if (lseek(pagemap_fd, off, SEEK_SET) < 0) continue;

        // Read in chunks to bound buffer size
        enum { CHUNK = 4096 };
        uint64_t buf[CHUNK];
        int remaining = v->n_pages;
        int idx = 0;
        while (remaining > 0) {
            int want = remaining < CHUNK ? remaining : CHUNK;
            ssize_t got = read(pagemap_fd, buf, (size_t)want * 8);
            if (got <= 0) break;
            int n = got / 8;
            for (int j = 0; j < n; j++) {
                int p = idx + j;
                if (buf[j] & BIT_PRESENT) {
                    v->present_count[p]++;
                    if (buf[j] & BIT_SOFT_DIRTY) {
                        v->dirty_count[p]++;
                        // End the current stability period (if any) and reset.
                        // is_final=0: this period was terminated by a write.
                        if (v->current_stab_period[p] > v->max_stab_period[p])
                            v->max_stab_period[p] = v->current_stab_period[p];
                        log_stability_period(v->current_stab_period[p], 0);
                        v->current_stab_period[p] = 0;
                        // Append this sweep number to the per-page write event list
                        int n = v->write_event_count[p];
                        if (n >= v->write_event_cap[p]) {
                            int new_cap = v->write_event_cap[p] ? v->write_event_cap[p] * 2 : 4;
                            v->write_events[p] = realloc(v->write_events[p], new_cap * sizeof(int));
                            v->write_event_cap[p] = new_cap;
                        }
                        v->write_events[p][n] = g_current_sweep;
                        v->write_event_count[p] = n + 1;
                    } else {
                        // Page was present and clean — extend stability period.
                        v->current_stab_period[p]++;
                    }

                    // --- Incarnation tracking (parallel; see C02) ---
                    // A present->absent->present gap (>1 sweep since last seen)
                    // closes the current incarnation and opens a new one.
                    struct page_inc *pi = &v->inc[p];
                    int s = g_current_sweep;
                    if (pi->open && (s - pi->last_present) > 1)
                        close_incarnation(i, p);     // sets pi->open = 0
                    if (!pi->open) {
                        pi->open = 1;
                        pi->count++;                 // idx of this incarnation = count-1
                        pi->first_seen = s;
                        pi->present_count = 0;
                        pi->dirty_count = 0;
                        pi->write_event_count = 0;
                    }
                    pi->present_count++;
                    if (buf[j] & BIT_SOFT_DIRTY) {
                        pi->dirty_count++;
                        append_event(&pi->write_events, &pi->write_event_count,
                                     &pi->write_event_cap, s);
                    }
                    pi->last_present = s;
                }
                // If !present this sweep: don't advance stability period, don't end it.
                // Page may come back later; treat the absence as a hold.
            }
            idx += n;
            remaining -= n;
        }
    }
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr,
            "Usage: %s <pid> <output.csv> [<interval_ms>=100]\n", argv[0]);
        return 1;
    }
    int pid = atoi(argv[1]);
    const char *output = argv[2];
    int interval_ms = (argc > 3) ? atoi(argv[3]) : 100;

    if (merge_maps(pid) < 0) return 1;
    fprintf(stderr, "[dirty_sweep] pid=%d, %d initial VMAs, interval=%d ms\n",
            pid, n_vmas, interval_ms);

    char pagemap_path[64];
    snprintf(pagemap_path, sizeof(pagemap_path), "/proc/%d/pagemap", pid);
    int pagemap_fd = open(pagemap_path, O_RDONLY);
    if (pagemap_fd < 0) { perror(pagemap_path); return 1; }

    // Install signal handlers so kill/Ctrl-C exits gracefully (writes CSV)
    struct sigaction sa = { .sa_handler = on_signal };
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;  // no SA_RESTART — nanosleep returns early on signal
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT,  &sa, NULL);

    // Initial clear so we begin tracking from a clean state
    if (clear_soft_dirty(pid) < 0)
        fprintf(stderr, "[dirty_sweep] initial clear failed: %s\n", strerror(errno));

    struct timespec t_start;
    clock_gettime(CLOCK_MONOTONIC, &t_start);

    int total_sweeps = 0;
    struct timespec ts = {
        .tv_sec  = interval_ms / 1000,
        .tv_nsec = (interval_ms % 1000) * 1000000L,
    };

    while (target_alive(pid) && !stop_requested) {
        nanosleep(&ts, NULL);
        if (stop_requested || !target_alive(pid)) break;

        // Refresh VMA list — catches new/grown allocations
        merge_maps(pid);
        g_current_sweep = total_sweeps;  // sweep number BEFORE increment
        sweep_pagemap(pagemap_fd);
        total_sweeps++;

        if (clear_soft_dirty(pid) < 0) {
            // Process likely exited mid-sweep; finish gracefully
            break;
        }
    }

    struct timespec t_end;
    clock_gettime(CLOCK_MONOTONIC, &t_end);
    double elapsed = (t_end.tv_sec - t_start.tv_sec) +
                     (t_end.tv_nsec - t_start.tv_nsec) / 1e9;

    close(pagemap_fd);

    // Finalize any in-progress stability periods (page was clean at the very end —
    // period didn't get terminated by a write, but should still be counted).
    for (int i = 0; i < n_vmas; i++) {
        struct vma *v = &vmas[i];
        for (int j = 0; j < v->n_pages; j++) {
            if (v->current_stab_period[j] > 0) {
                // is_final=1: this period is the one active at end-of-run,
                // not terminated by a write.
                if (v->current_stab_period[j] > v->max_stab_period[j])
                    v->max_stab_period[j] = v->current_stab_period[j];
                log_stability_period(v->current_stab_period[j], 1);
            }
        }
    }

    FILE *out = fopen(output, "w");
    if (!out) { perror(output); return 1; }
    fprintf(out, "# total_sweeps=%d total_seconds=%.3f interval_ms=%d pid=%d\n",
            total_sweeps, elapsed, interval_ms, pid);
    fprintf(out, "vma_start,vma_end,vma_perms,vma_path,vpage_idx,present_count,dirty_count,max_stab_period,final_stab_period,write_events\n");
    for (int i = 0; i < n_vmas; i++) {
        struct vma *v = &vmas[i];
        for (int j = 0; j < v->n_pages; j++) {
            if (v->present_count[j] == 0) continue;  // never present, skip
            // final_stab_period = current_stab_period at end of run = the
            // length of the epoch that was active when the run ended (i.e.,
            // time since the page's last write). 0 if the run ended right
            // after a write.
            fprintf(out, "0x%lx,0x%lx,%s,%s,%d,%d,%d,%d,%d,",
                    v->start, v->end, v->perms, v->path,
                    j, v->present_count[j], v->dirty_count[j],
                    v->max_stab_period[j],
                    v->current_stab_period[j]);
            // write_events as semicolon-separated list of sweep numbers
            int n = v->write_event_count[j];
            for (int k = 0; k < n; k++) {
                fprintf(out, "%s%d", k == 0 ? "" : ";", v->write_events[j][k]);
            }
            fprintf(out, "\n");
        }
    }
    fclose(out);

    // Write the stability-period histogram alongside the per-page CSV.
    // Filename: same as `output` but with "_stability" before the extension,
    // or just append. We'll derive: strip ".csv" if present, append "_stability.csv".
    char stability_path[512];
    {
        const char *base = output;
        size_t len = strlen(base);
        const char *suf = ".csv";
        size_t suflen = strlen(suf);
        if (len >= suflen && strcmp(base + len - suflen, suf) == 0) {
            snprintf(stability_path, sizeof(stability_path), "%.*s_stability.csv",
                     (int)(len - suflen), base);
        } else {
            snprintf(stability_path, sizeof(stability_path), "%s_stability.csv", base);
        }
    }
    FILE *sout = fopen(stability_path, "w");
    if (sout) {
        fprintf(sout, "# total_sweeps=%d total_seconds=%.3f interval_ms=%d pid=%d\n",
                total_sweeps, elapsed, interval_ms, pid);
        fprintf(sout, "stability_period_sweeps,count,final_count\n");
        for (size_t k = 1; k < stability_hist_cap; k++) {
            if (stability_hist[k] > 0 || stability_hist_final[k] > 0) {
                fprintf(sout, "%zu,%" PRIu64 ",%" PRIu64 "\n",
                        k, stability_hist[k], stability_hist_final[k]);
            }
        }
        fclose(sout);
        fprintf(stderr, "[dirty_sweep] stability-period histogram → %s\n", stability_path);
    } else {
        perror(stability_path);
    }

    // === Lifecycle sidecar: one row per page incarnation (C01) ===========
    // Close every still-open incarnation on this post-loop path (NOT in the
    // signal handler — that only sets stop_requested), then flush all records.
    for (int i = 0; i < n_vmas; i++) {
        struct vma *v = &vmas[i];
        for (int j = 0; j < v->n_pages; j++) {
            if (v->inc[j].open) close_incarnation(i, j);
        }
    }

    char lifecycle_path[512];
    {
        const char *base = output;
        size_t len = strlen(base);
        const char *suf = ".csv";
        size_t suflen = strlen(suf);
        if (len >= suflen && strcmp(base + len - suflen, suf) == 0) {
            snprintf(lifecycle_path, sizeof(lifecycle_path), "%.*s_lifecycle.csv",
                     (int)(len - suflen), base);
        } else {
            snprintf(lifecycle_path, sizeof(lifecycle_path), "%s_lifecycle.csv", base);
        }
    }
    FILE *lout = fopen(lifecycle_path, "w");
    if (lout) {
        fprintf(lout, "# total_sweeps=%d total_seconds=%.3f interval_ms=%d pid=%d\n",
                total_sweeps, elapsed, interval_ms, pid);
        fprintf(lout, "vma_start,vma_end,vma_perms,vma_path,vpage_idx,incarnation_idx,"
                      "first_seen,last_seen,present_count,dirty_count,write_events\n");
        for (size_t r = 0; r < inc_recs_count; r++) {
            struct incarnation_rec *rec = &inc_recs[r];
            if (rec->present_count == 0) continue;   // never present, skip (C01)
            struct vma *v = &vmas[rec->vma_idx];
            fprintf(lout, "0x%lx,0x%lx,%s,%s,%d,%d,%d,%d,%d,%d,",
                    v->start, v->end, v->perms, v->path,
                    rec->vpage_idx, rec->incarnation_idx,
                    rec->first_seen, rec->last_seen,
                    rec->present_count, rec->dirty_count);
            for (int k = 0; k < rec->write_event_count; k++) {
                fprintf(lout, "%s%d", k == 0 ? "" : ";", rec->write_events[k]);
            }
            fprintf(lout, "\n");
        }
        fclose(lout);
        fprintf(stderr, "[dirty_sweep] lifecycle (per-incarnation) → %s\n", lifecycle_path);
    } else {
        perror(lifecycle_path);
    }

    fprintf(stderr, "[dirty_sweep] %d sweeps in %.2fs, wrote %s\n",
            total_sweeps, elapsed, output);
    return 0;
}
