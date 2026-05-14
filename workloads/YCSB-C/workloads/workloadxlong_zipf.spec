# Yahoo! Cloud System Benchmark — Zipfian variant of workloadxlong.spec.
#
# Same record count, op count, and read/update ratio as workloadxlong.spec,
# BUT keys follow a Zipfian distribution with theta=0.99 (the canonical
# "highly skewed" parameter used in essentially every memory-hierarchy
# paper that includes a Zipfian YCSB run — TPP, Pond, Hyperloop, etc.).
#
# Why this matters: real OLTP workloads have hot keys (most-active 1% of
# records get half the traffic) and a long cold tail (bottom half of records
# get a tiny share of accesses). Uniform distribution (workloadxlong.spec)
# is the worst case for LtRAM because there's no cold tail to migrate; this
# Zipfian variant is the realistic case where most pages CAN be migrated to
# LtRAM and stay there.
#
#   Read/update ratio:    90 / 10
#   Record size:          1 KB (10 fields × 100 bytes + key)
#   Request distribution: zipfian (theta=0.99)
#   500K records, 10M ops → ~50–60 min run

# Connection details
redis.host=127.0.0.1
redis.port=6379
host=127.0.0.1
port=6379

# Workload size — same as workloadxlong.spec
recordcount=500000
operationcount=10000000
fieldcount=10
fieldlength=100

dbname=redis
readallfields=true

readproportion=0.9
updateproportion=0.1
scanproportion=0
insertproportion=0

# Zipfian skew. theta=0.99 matches YCSB Workload-default and is the value
# used in TPP / Pond / Hyperloop. theta=0 would degenerate to uniform;
# theta closer to 1 means more skew (top keys get more traffic).
requestdistribution=zipfian
zipfian_const=0.99
