# YCSB spec for the LtRAM scan_batch SENSITIVITY sweep.
#
# Goal: make the run long enough that EVERY N fully converges (places the whole
# read-cold working set), so the only thing that differs between N values is N's
# own cost -- not whether the scan had time to finish. And pure-read so there is
# zero write-back churn to confound the comparison (the write-RATIO sweep is a
# separate experiment that varies this).
#
#   recordcount     ~100 MB dataset (~25k 4KB pages) -- the thing to migrate
#   operationcount  long timed phase (~90s @ ~8 KTPS) >> the ~40-50s the scan
#                   needs to age+migrate the set even at small N
#   read-only       no updates -> no repatriation -> clean isolation of N
#   uniform         spread reads across the whole set (keep redis busy evenly)

recordcount=100000
operationcount=1000000
workload=com.yahoo.ycsb.workloads.CoreWorkload

readallfields=true

readproportion=1.0
updateproportion=0
scanproportion=0
insertproportion=0

requestdistribution=uniform
