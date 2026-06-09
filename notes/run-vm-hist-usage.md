# run-vm-hist.sh usage guide

`scripts/run-vm-hist.sh` boots a QEMU guest VM, runs a benchmark workload, and
captures per-page write events via `dirty_sweep` throughout the run. After the
VM exits, `analyze.sh` runs automatically and produces all plots.

It is a thin wrapper around `run-vm.sh` that sets `LTRAM_HIST=1`. All
configuration is via environment variables.

## Workloads

| Name | Description |
|------|-------------|
| `matmul` | Matrix multiply. Load: alloc + fill_random. Run: multiply loop. |
| `gapbs` | GAPbs PageRank on a synthetic Kronecker graph. Load: graph build. Run: PR trials. |
| `redis` | Redis + YCSB-C. Load: bulk insert. Run: workload ops. |
| `duckdb` | DuckDB in-memory. YCSB (default) or TPC-H (`LTRAM_DUCKDB_TPCH=1`). |
| `all` | Runs matmul, gapbs, redis, duckdb sequentially as four separate VMs. |
| `interactive` | Boots the VM but does not run any workload. Leaves you at a shell. |

## Environment variables

### Memory sizing

| Variable | Default | Description |
|----------|---------|-------------|
| `LTRAM_MEM_DRAM_MB` | `7936` | Guest node 0 (DRAM tier), MB. |
| `LTRAM_MEM_LTRAM_MB` | `256` | Guest node 1 (LtRAM tier), MB. 256 MB = 65,536 pages. |

### Run identification

| Variable | Default | Description |
|----------|---------|-------------|
| `LTRAM_RUN` | `<date>/<workload>_<config-tag>` | Override the full run directory path under `results/runs/`. |
| `LTRAM_CONFIG` | _(auto-generated)_ | Override just the config tag. Auto-tags encode workload params, e.g. `d7936m-l256m-tpch-sf1-qall-n10`. |

### Histogram / sweep settings

| Variable | Default | Description |
|----------|---------|-------------|
| `LTRAM_SWEEP_INTERVAL_MS` | `100` | Dirty-sweep polling interval in ms. Finer = more data, more overhead. |
| `LTRAM_NO_ANALYZE` | `0` | Set to `1` to skip the post-run `analyze.sh` call. |

### DuckDB — YCSB mode (default)

| Variable | Default | Description |
|----------|---------|-------------|
| `LTRAM_DUCKDB_RECORDS` | `1000000` | Number of records to load. |
| `LTRAM_DUCKDB_OPS` | _(bench default: 100,000)_ | Operation count. Blank = use bench default. |
| `LTRAM_DUCKDB_READ_RATIO` | `1.0` | Fraction of ops that are reads. `1.0` = read-only. |
| `LTRAM_DUCKDB_DIST` | `uniform` | Key distribution: `uniform` or `zipfian`. |

### DuckDB — TPC-H mode

| Variable | Default | Description |
|----------|---------|-------------|
| `LTRAM_DUCKDB_TPCH` | `0` | Set to `1` to enable TPC-H mode. |
| `LTRAM_DUCKDB_TPCH_SF` | `1` | Scale factor. SF=1 ≈ 1 GB of tables in memory. |
| `LTRAM_DUCKDB_TPCH_QUERY` | _(all 22)_ | Run only query N (1–22). Blank = all queries. |
| `LTRAM_DUCKDB_OPS` | _(bench default: 10)_ | Rounds through the query suite. Blank = use bench default. |

### GAPbs

| Variable | Default | Description |
|----------|---------|-------------|
| `LTRAM_GAPBS_GRAPH` | `20` | Kronecker graph scale: 2^N vertices. `20` ≈ 1M vertices. |
| `LTRAM_GAPBS_TRIALS` | _(binary default)_ | PageRank trial count. |
| `LTRAM_GAPBS_ITERS` | _(binary default)_ | Iterations per trial. |

### Matmul

| Variable | Default | Description |
|----------|---------|-------------|
| `LTRAM_MATMUL_ITERS` | _(binary default: 10)_ | Number of multiply iterations. |

### Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `LTRAM_REDIS_SPEC` | `workloadmini.spec` | YCSB spec file. Alternative: `workloadlong.spec`. |

## Examples

```bash
# TPC-H, all 22 queries, 10 rounds, scale factor 1
LTRAM_DUCKDB_TPCH=1 scripts/run-vm-hist.sh duckdb

# TPC-H, scale factor 4, only Q6, 20 rounds
LTRAM_DUCKDB_TPCH=1 LTRAM_DUCKDB_TPCH_SF=4 LTRAM_DUCKDB_TPCH_QUERY=6 \
  LTRAM_DUCKDB_OPS=20 scripts/run-vm-hist.sh duckdb

# YCSB 50% read / 50% write, zipfian key distribution
LTRAM_DUCKDB_READ_RATIO=0.5 LTRAM_DUCKDB_DIST=zipfian scripts/run-vm-hist.sh duckdb

# Large GAPbs graph (2^22 vertices)
LTRAM_GAPBS_GRAPH=22 scripts/run-vm-hist.sh gapbs

# Redis with longer workload spec
LTRAM_REDIS_SPEC=workloadlong.spec scripts/run-vm-hist.sh redis

# Custom run name (avoids overwriting a previous result)
LTRAM_DUCKDB_TPCH=1 LTRAM_RUN=experiments/tpch-baseline scripts/run-vm-hist.sh duckdb

# All workloads back to back
scripts/run-vm-hist.sh all
```

## Output

Results land in `results/runs/<date>/<workload>_<config-tag>/`:

| File | Description |
|------|-------------|
| `workload-stdout.txt` | stdout/stderr from the workload binary. |
| `dirty_sweep.csv` | Per-page write events, run phase. |
| `dirty_sweep_load.csv` | Per-page write events, load phase (when split is available). |
| `dirty_sweep_stability.csv` | Stability-period histogram, run phase. |
| `dirty_sweep_load_stability.csv` | Stability-period histogram, load phase. |
| `meminfo.csv` | `/proc/meminfo` time series. |
| `cmdline.txt` | Kernel command line used for this run. |
| `numa-topology.txt` | Guest `numactl --hardware` output. |
| `*.png` | All plots produced by `analyze.sh`. |

To re-run analysis on an existing run directory without re-running the VM:

```bash
scripts/analyze.sh duckdb 2026-06-09/duckdb_d7936m-l256m-tpch-sf1-qall-n10
```
