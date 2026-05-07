# Yahoo! Cloud System Benchmark — extra-long variant for stability-period
# analysis. 10× more operations than workloadlong.spec so the natural max
# of the workload's stability distribution falls inside our observation
# window (instead of being truncated by run length).
#
#   Read/update ratio:    90 / 10
#   Record size:          1 KB (10 fields × 100 bytes + key)
#   Request distribution: uniform
#   500K records, 10M ops → ~50–60 min run depending on Redis speed

# Connection details
redis.host=127.0.0.1
redis.port=6379
host=127.0.0.1
port=6379

# Workload size — same data, much more steady-state runtime
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

requestdistribution=uniform
