# Yahoo! Cloud System Benchmark — "long" Zipfian variant.
# 10× the ops of workloadshort_zipf.spec. Pair the two for a 10× convergence
# test on the Zipfian C2/C3 (cold-tail) set: if short and long agree, the
# cold-tail categorization has converged for this workload size.
#
#   Read/update ratio:    90 / 10
#   Record size:          1 KB (10 fields × 100 bytes + key)
#   Request distribution: zipfian (theta=0.99)
#   500K records, 10M ops → ~50-60 min run

# Connection details
redis.host=127.0.0.1
redis.port=6379
host=127.0.0.1
port=6379

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

requestdistribution=zipfian
zipfian_const=0.99
