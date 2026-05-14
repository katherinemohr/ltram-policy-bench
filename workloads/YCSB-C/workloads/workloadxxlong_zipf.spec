# Yahoo! Cloud System Benchmark — Zipfian XXL variant.
# Same Zipfian distribution as workloadxlong_zipf.spec (theta=0.99) but at
# 50M ops instead of 10M, for the X-vs-10X convergence test on the cold-tail
# C2/C3 set under skewed access. Zipfian converges MUCH slower than uniform
# because cold-tail records are touched infrequently — comparing xlong_zipf
# vs xxlong_zipf is the convergence witness for the Zipfian regime.
#
#   Read/update ratio:    90 / 10
#   Record size:          1 KB (10 fields × 100 bytes + key)
#   Request distribution: zipfian (theta=0.99)
#   500K records, 50M ops → ~4–5 hr run

# Connection details
redis.host=127.0.0.1
redis.port=6379
host=127.0.0.1
port=6379

# Workload size
recordcount=500000
operationcount=50000000
fieldcount=10
fieldlength=100

dbname=redis
readallfields=true

readproportion=0.9
updateproportion=0.1
scanproportion=0
insertproportion=0

requestdistribution=zipfian
zipfian_const=0.99
