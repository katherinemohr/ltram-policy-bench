# Yahoo! Cloud System Benchmark — long-running variant for stability-period
# analysis. Same data set as workloadmini, but with 100x more operations so
# the steady-state run phase dominates over the load phase.
#
#   Read/update ratio:    90 / 10
#   Record size:          1 KB (10 fields × 100 bytes + key)
#   Request distribution: uniform
#   500K records, 1M ops → ~15-30 min run depending on Redis speed

# Connection details
redis.host=127.0.0.1
redis.port=6379
host=127.0.0.1
port=6379

# Workload size — same data, much more steady-state runtime
recordcount=500000
operationcount=1000000
fieldcount=10
fieldlength=100

dbname=redis
readallfields=true

readproportion=0.9
updateproportion=0.1
scanproportion=0
insertproportion=0

requestdistribution=uniform
