# Yahoo! Cloud System Benchmark — XXL variant for overnight steady-state
# capture. 5× the operations of workloadxlong.spec so the run-phase
# stability distribution converges much further (long-tail epochs that get
# truncated even at 10M ops should now have room to play out).
#
#   Read/update ratio:    90 / 10
#   Record size:          1 KB (10 fields × 100 bytes + key)
#   Request distribution: uniform
#   500K records, 50M ops → ~4–5 hr run depending on Redis speed

# Connection details
redis.host=127.0.0.1
redis.port=6379
host=127.0.0.1
port=6379

# Workload size — same data, 5× the steady-state runtime
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

requestdistribution=uniform
