# Yahoo! Cloud System Benchmark — short Zipfian variant for quick tests.
# Same record count and 90/10 R/W as the longer Zipfian variants, but
# only 1M ops so it finishes in ~5-8 min. Pair this with workloadlong_zipf
# for a 10× convergence test that completes in ~1 hour total.
#
#   Read/update ratio:    90 / 10
#   Record size:          1 KB (10 fields × 100 bytes + key)
#   Request distribution: zipfian (theta=0.99)
#   500K records, 1M ops → ~5-8 min run

# Connection details
redis.host=127.0.0.1
redis.port=6379
host=127.0.0.1
port=6379

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

requestdistribution=zipfian
zipfian_const=0.99
