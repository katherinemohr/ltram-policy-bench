# Yahoo! Cloud System Benchmark
# Fake small workload
#   Read/update ratio: 90/10
#   Default data size: 0.1 KB records (10 fields, 10 bytes each, plus key)
#   Request distribution: uniform

# Connection details
redis.host=127.0.0.1
redis.port=6379
host=127.0.0.1
port=6379

# Mini workload size
recordcount=3200
operationcount=1000
fieldcount=10
fieldlength=10

# Required for some C++ YCSB versions
dbname=redis

readallfields=true

readproportion=0.9
updateproportion=0.1
scanproportion=0
insertproportion=0

requestdistribution=uniform

# Required Table Name
table=usertable

# Additional fields often required by C++ Map lookups
dotransactions=true
