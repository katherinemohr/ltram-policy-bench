#!/usr/bin/env python3
"""YCSB-style benchmark for DuckDB: configurable read/write ratio and operation count."""

import argparse
import math
import random
import statistics
import time

import duckdb


# --- key distribution generators ---

def uniform_generator(record_count):
    while True:
        yield random.randint(0, record_count - 1)


def zipfian_generator(record_count, theta=0.99):
    """Scrambled Zipfian matching YCSB-C's default."""
    zeta_n = sum(1.0 / (i ** theta) for i in range(1, record_count + 1))
    zeta_2 = 1.0 + 0.5 ** theta
    alpha = 1.0 / (1.0 - theta)
    eta = (1.0 - (2.0 / record_count) ** (1.0 - theta)) / (1.0 - zeta_2 / zeta_n)

    while True:
        u = random.random()
        uz = u * zeta_n
        if uz < 1.0:
            raw = 0
        elif uz < 1.0 + 0.5 ** theta:
            raw = 1
        else:
            raw = int(record_count * (eta * (u - 1.0) + 1.0) ** alpha)
            raw = min(raw, record_count - 1)
        # scramble so hot keys aren't all at the front
        yield (raw * 2654435761) % record_count


def make_generator(distribution, record_count):
    if distribution == "zipfian":
        return zipfian_generator(record_count)
    return uniform_generator(record_count)


# --- benchmark phases ---

VALUE_WIDTH = 100  # bytes per value field, matching YCSB default


def load(con, record_count):
    con.execute("DROP TABLE IF EXISTS usertable")
    con.execute("""
        CREATE TABLE usertable (
            ycsb_key  INTEGER PRIMARY KEY,
            field0    VARCHAR,
            field1    VARCHAR,
            field2    VARCHAR,
            field3    VARCHAR,
            field4    VARCHAR
        )
    """)
    rows = [
        (i,
         "x" * VALUE_WIDTH,
         "x" * VALUE_WIDTH,
         "x" * VALUE_WIDTH,
         "x" * VALUE_WIDTH,
         "x" * VALUE_WIDTH)
        for i in range(record_count)
    ]
    con.executemany(
        "INSERT INTO usertable VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    print(f"[load] inserted {record_count} records")


def run(con, operations, read_ratio, key_gen):
    read_latencies = []
    write_latencies = []

    for _ in range(operations):
        key = next(key_gen)
        if random.random() < read_ratio:
            t0 = time.perf_counter()
            con.execute("SELECT * FROM usertable WHERE ycsb_key = ?", [key])
            con.fetchone()
            read_latencies.append(time.perf_counter() - t0)
        else:
            new_val = "y" * VALUE_WIDTH
            t0 = time.perf_counter()
            con.execute(
                "UPDATE usertable SET field0 = ? WHERE ycsb_key = ?",
                [new_val, key],
            )
            write_latencies.append(time.perf_counter() - t0)

    return read_latencies, write_latencies


def percentile(data, p):
    if not data:
        return float("nan")
    data = sorted(data)
    idx = (len(data) - 1) * p / 100.0
    lo, hi = int(idx), min(int(idx) + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (idx - lo)


def print_stats(label, latencies, elapsed):
    if not latencies:
        print(f"[{label}] 0 ops")
        return
    ops = len(latencies)
    throughput = ops / elapsed
    avg_us = statistics.mean(latencies) * 1e6
    p50_us = percentile(latencies, 50) * 1e6
    p95_us = percentile(latencies, 95) * 1e6
    p99_us = percentile(latencies, 99) * 1e6
    print(
        f"[{label}] ops={ops}  throughput={throughput:.1f} ops/s"
        f"  avg={avg_us:.1f}us  p50={p50_us:.1f}us"
        f"  p95={p95_us:.1f}us  p99={p99_us:.1f}us"
    )


def main():
    parser = argparse.ArgumentParser(
        description="YCSB-style DuckDB benchmark"
    )
    parser.add_argument("--db", default=":memory:", help="DuckDB database path (default: in-memory)")
    parser.add_argument("--record-count", type=int, default=100_000, metavar="N",
                        help="number of records to load (default: 100000)")
    parser.add_argument("--operations", type=int, default=100_000, metavar="N",
                        help="number of operations to run (default: 100000)")
    parser.add_argument("--read-ratio", type=float, default=0.5, metavar="R",
                        help="fraction of ops that are reads, 0.0-1.0 (default: 0.5)")
    parser.add_argument("--distribution", choices=["uniform", "zipfian"], default="uniform",
                        help="key access distribution (default: uniform)")
    parser.add_argument("--skip-load", action="store_true",
                        help="skip the load phase (table must already exist)")
    parser.add_argument("--seed", type=int, default=None,
                        help="random seed for reproducibility")
    args = parser.parse_args()

    if not 0.0 <= args.read_ratio <= 1.0:
        parser.error("--read-ratio must be between 0.0 and 1.0")

    if args.seed is not None:
        random.seed(args.seed)

    print(f"DuckDB YCSB-style benchmark")
    print(f"  db:           {args.db}")
    print(f"  record-count: {args.record_count}")
    print(f"  operations:   {args.operations}")
    print(f"  read-ratio:   {args.read_ratio:.2f} "
          f"(read={args.read_ratio:.0%}, write={1-args.read_ratio:.0%})")
    print(f"  distribution: {args.distribution}")
    print()

    con = duckdb.connect(args.db)

    if not args.skip_load:
        t0 = time.perf_counter()
        load(con, args.record_count)
        print(f"[load] elapsed {time.perf_counter() - t0:.2f}s\n")

    key_gen = make_generator(args.distribution, args.record_count)

    t0 = time.perf_counter()
    reads, writes = run(con, args.operations, args.read_ratio, key_gen)
    elapsed = time.perf_counter() - t0

    total_ops = len(reads) + len(writes)
    print(f"[run]  total ops={total_ops}  elapsed={elapsed:.2f}s  "
          f"throughput={total_ops/elapsed:.1f} ops/s")
    print_stats("READ ", reads, elapsed)
    print_stats("WRITE", writes, elapsed)

    con.close()


if __name__ == "__main__":
    main()
