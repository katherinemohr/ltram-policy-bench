/*
 * YCSB-style DuckDB benchmark.
 * Usage: bench [OPTIONS]
 *   -d PATH       database file path (default: /tmp/bench.db)
 *   -r N          record count for load phase (default: 100000)
 *   -n N          operation count for run phase (default: 100000)
 *   -R RATIO      read ratio 0.0-1.0 (default: 0.5)
 *   -D DIST       key distribution: uniform or zipfian (default: uniform)
 *   -F BYTES      value field size in bytes (default: 100)
 *   -s N          random seed (default: 42)
 *   -S            skip load phase (table must already exist)
 */

#include "duckdb.h"
#include <unistd.h>
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

// ---- timing ---------------------------------------------------------------

static inline uint64_t now_ns() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

// ---- PRNG (xorshift64) ----------------------------------------------------

static uint64_t rng_state;

static inline uint64_t rng_next() {
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 7;
    rng_state ^= rng_state << 17;
    return rng_state;
}

static inline double rng_double() {
    return (double)(rng_next() >> 11) / (double)(1ULL << 53);
}

// ---- key distributions ----------------------------------------------------

static uint64_t uniform_next(uint64_t n) {
    return rng_next() % n;
}

struct ZipfianGen {
    uint64_t n;
    double theta, alpha, zeta_n, zeta_2, eta;
};

static ZipfianGen zipfian_init(uint64_t n, double theta) {
    ZipfianGen z;
    z.n = n;
    z.theta = theta;
    z.alpha = 1.0 / (1.0 - theta);

    z.zeta_2 = 0.0;
    for (uint64_t i = 1; i <= 2; i++)
        z.zeta_2 += 1.0 / pow((double)i, theta);

    z.zeta_n = z.zeta_2;
    for (uint64_t i = 3; i <= n; i++)
        z.zeta_n += 1.0 / pow((double)i, theta);

    z.eta = (1.0 - pow(2.0 / (double)n, 1.0 - theta)) /
            (1.0 - z.zeta_2 / z.zeta_n);
    return z;
}

static uint64_t zipfian_next(const ZipfianGen &z) {
    double u = rng_double();
    double uz = u * z.zeta_n;
    uint64_t raw;
    if (uz < 1.0)
        raw = 0;
    else if (uz < 1.0 + pow(0.5, z.theta))
        raw = 1;
    else
        raw = (uint64_t)((double)z.n * pow(z.eta * u - z.eta + 1.0, z.alpha));
    if (raw >= z.n) raw = z.n - 1;
    // scramble so hot keys aren't always at index 0
    return (raw * 2654435761ULL) % z.n;
}

// ---- stats ----------------------------------------------------------------

static double percentile(std::vector<uint64_t> &v, double p) {
    if (v.empty()) return 0.0;
    size_t idx = (size_t)(p * (v.size() - 1));
    return (double)v[idx];
}

static void print_stats(const char *label,
                        std::vector<uint64_t> &lats,
                        double elapsed_s) {
    if (lats.empty()) {
        printf("[%-5s] 0 ops\n", label);
        return;
    }
    std::sort(lats.begin(), lats.end());
    double total_s = elapsed_s > 0 ? elapsed_s : 1e-9;
    double avg_us = 0;
    for (auto x : lats) avg_us += x;
    avg_us /= (double)lats.size() * 1000.0;

    printf("[%-5s] ops=%-8zu  tput=%-10.1f ops/s  "
           "avg=%6.1fus  p50=%6.1fus  p95=%6.1fus  p99=%6.1fus\n",
           label, lats.size(),
           (double)lats.size() / total_s,
           avg_us,
           percentile(lats, 0.50) / 1000.0,
           percentile(lats, 0.95) / 1000.0,
           percentile(lats, 0.99) / 1000.0);
}

// ---- load phase -----------------------------------------------------------

static void phase_load(duckdb_connection con, int64_t record_count,
                       int field_size) {
    duckdb_result res;
    if (duckdb_query(con, "DROP TABLE IF EXISTS usertable", &res) == DuckDBError) {
        fprintf(stderr, "drop: %s\n", duckdb_result_error(&res));
        exit(1);
    }
    duckdb_destroy_result(&res);

    if (duckdb_query(con,
            "CREATE TABLE usertable ("
            "  ycsb_key INTEGER NOT NULL,"
            "  field0 VARCHAR, field1 VARCHAR, field2 VARCHAR,"
            "  field3 VARCHAR, field4 VARCHAR"
            ")", &res) == DuckDBError) {
        fprintf(stderr, "create: %s\n", duckdb_result_error(&res));
        exit(1);
    }
    duckdb_destroy_result(&res);

    std::string val(field_size, 'x');

    duckdb_appender app;
    if (duckdb_appender_create(con, NULL, "usertable", &app) == DuckDBError) {
        fprintf(stderr, "appender_create: %s\n", duckdb_appender_error(app));
        exit(1);
    }

    for (int64_t i = 0; i < record_count; i++) {
        duckdb_append_int32(app, (int32_t)i);
        for (int f = 0; f < 5; f++)
            duckdb_append_varchar(app, val.c_str());
        duckdb_appender_end_row(app);
    }

    if (duckdb_appender_close(app) == DuckDBError) {
        fprintf(stderr, "appender_close: %s\n", duckdb_appender_error(app));
        exit(1);
    }
    duckdb_appender_destroy(&app);

    printf("[load] inserted %ld records\n", (long)record_count);
}

// ---- run phase ------------------------------------------------------------

static void phase_run(duckdb_connection con,
                      int64_t operations, double read_ratio,
                      int64_t record_count, bool use_zipfian,
                      int field_size) {
    duckdb_prepared_statement read_stmt, write_stmt;

    if (duckdb_prepare(con,
            "SELECT field0, field1, field2, field3, field4 "
            "FROM usertable WHERE ycsb_key = $1",
            &read_stmt) == DuckDBError) {
        fprintf(stderr, "prepare read: %s\n",
                duckdb_prepare_error(read_stmt));
        exit(1);
    }
    if (duckdb_prepare(con,
            "UPDATE usertable SET field0 = $2 WHERE ycsb_key = $1",
            &write_stmt) == DuckDBError) {
        fprintf(stderr, "prepare write: %s\n",
                duckdb_prepare_error(write_stmt));
        exit(1);
    }

    std::string new_val(field_size, 'y');

    ZipfianGen zgen;
    if (use_zipfian)
        zgen = zipfian_init((uint64_t)record_count, 0.99);

    std::vector<uint64_t> read_lats, write_lats;
    read_lats.reserve((size_t)(operations * read_ratio * 1.2));
    write_lats.reserve((size_t)(operations * (1.0 - read_ratio) * 1.2));

    uint64_t run_start = now_ns();

    for (int64_t i = 0; i < operations; i++) {
        uint64_t key = use_zipfian
            ? zipfian_next(zgen)
            : uniform_next((uint64_t)record_count);

        duckdb_result res;
        uint64_t t0 = now_ns();

        if (rng_double() < read_ratio) {
            duckdb_bind_int32(read_stmt, 1, (int32_t)key);
            duckdb_execute_prepared(read_stmt, &res);
            read_lats.push_back(now_ns() - t0);
        } else {
            duckdb_bind_int32(write_stmt, 1, (int32_t)key);
            duckdb_bind_varchar(write_stmt, 2, new_val.c_str());
            duckdb_execute_prepared(write_stmt, &res);
            write_lats.push_back(now_ns() - t0);
        }
        duckdb_destroy_result(&res);
    }

    double elapsed = (double)(now_ns() - run_start) / 1e9;
    int64_t total = (int64_t)(read_lats.size() + write_lats.size());

    printf("[run]  ops=%-8ld  elapsed=%.2fs  tput=%.1f ops/s\n",
           (long)total, elapsed, (double)total / elapsed);
    print_stats("READ",  read_lats,  elapsed);
    print_stats("WRITE", write_lats, elapsed);

    duckdb_destroy_prepare(&read_stmt);
    duckdb_destroy_prepare(&write_stmt);
}

// ---- main -----------------------------------------------------------------

int main(int argc, char *argv[]) {
    const char *db_path  = "/tmp/bench.db";
    int64_t record_count = 100000;
    int64_t operations   = 100000;
    double  read_ratio   = 0.5;
    bool    use_zipfian  = false;
    bool    skip_load    = false;
    int     field_size   = 100;
    uint64_t seed        = 42;

    int opt;
    while ((opt = getopt(argc, argv, "d:r:n:R:D:F:s:S")) != -1) {
        switch (opt) {
        case 'd': db_path      = optarg;                         break;
        case 'r': record_count = atoll(optarg);                  break;
        case 'n': operations   = atoll(optarg);                  break;
        case 'R': read_ratio   = atof(optarg);                   break;
        case 'D': use_zipfian  = strcmp(optarg, "zipfian") == 0; break;
        case 'F': field_size   = atoi(optarg);                   break;
        case 's': seed         = (uint64_t)atoll(optarg);        break;
        case 'S': skip_load    = true;                           break;
        default:
            fprintf(stderr,
                "Usage: %s [-d PATH] [-r RECORDS] [-n OPS] [-R READ_RATIO]\n"
                "          [-D uniform|zipfian] [-F FIELD_BYTES] [-s SEED] [-S]\n",
                argv[0]);
            return 1;
        }
    }

    rng_state = seed ? seed : 1;

    printf("DuckDB YCSB-style benchmark\n");
    printf("  db:           %s\n", db_path);
    printf("  record-count: %ld\n", (long)record_count);
    printf("  operations:   %ld\n", (long)operations);
    printf("  read-ratio:   %.2f (read=%.0f%%  write=%.0f%%)\n",
           read_ratio, read_ratio * 100, (1.0 - read_ratio) * 100);
    printf("  distribution: %s\n", use_zipfian ? "zipfian" : "uniform");
    printf("  field-size:   %d bytes\n", field_size);
    printf("\n");

    duckdb_database db;
    duckdb_connection con;

    if (duckdb_open(db_path, &db) == DuckDBError) {
        fprintf(stderr, "duckdb_open failed\n");
        return 1;
    }
    if (duckdb_connect(db, &con) == DuckDBError) {
        fprintf(stderr, "duckdb_connect failed\n");
        return 1;
    }

    if (!skip_load) {
        uint64_t t0 = now_ns();
        phase_load(con, record_count, field_size);
        printf("[load] elapsed %.2fs\n\n", (double)(now_ns() - t0) / 1e9);

        const char *marker = getenv("LTRAM_DUCKDB_LOAD_DONE_MARKER");
        if (marker) {
            FILE *f = fopen(marker, "w");
            if (f) fclose(f);
        }
    }

    phase_run(con, operations, read_ratio, record_count, use_zipfian, field_size);

    duckdb_disconnect(&con);
    duckdb_close(&db);
    return 0;
}
