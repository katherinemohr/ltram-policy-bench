#!/bin/sh

start_run() {
    mkdir -p /mnt/workloads
    mkdir -p /mnt/results
    mkdir -p /mnt/inputs

    # Mount the shared directories from the host
    mount -t 9p -o trans=virtio workloads /mnt/workloads
    mount -t 9p -o trans=virtio results /mnt/results
    mount -t 9p -o trans=virtio inputs /mnt/inputs

    # Verify NUMA topology was picked up correctly
    numactl --hardware > /mnt/results/numa-topology.txt

    # Enable NUMA balancing
    # echo 1 > /proc/sys/kernel/numa_balancing

    # Snapshot vmstat before workload
    cp /proc/vmstat /mnt/results/vmstat-before.txt
}

run_workload() {
    # Choose your workload
    matmul
    # gapbs_pagerank
    # redis
}

matmul() {
    echo "Starting Matrix Multiplication workload..."
    
    # Notice the added /matmul/ in the path
    /mnt/workloads/matmul/matrix_multiply > /mnt/results/matrix-output.txt
    
    echo "Matrix Multiplication complete."
}

gapbs_pagerank() {
    echo "Running GAPbs PageRank..."
    
    # -g 20 generates a Kronecker graph with 2^20 vertices
    # numactl --membind=0 forces all allocations to Node 0 (DRAM)
    numactl --cpunodebind=0 --membind=0 /mnt/workloads/pr -g 20 > /mnt/results/pr-output.txt 2>&1
    
    echo "PageRank complete."
}

redis() {
    echo "Starting Redis server on Node 0..."
    
    # Start Redis in the background, bound to Node 0 (DRAM)
    # --save "" disables disk snapshots so it stays purely in-memory
    numactl --cpunodebind=0 --membind=0 redis-server --save "" --daemonize yes
    
    # Give the daemon a moment to initialize
    sleep 2 
    
    echo "Phase 1: Loading data into Redis..."
    # Passing -p recordcount overrides the file
    #recordcount=3200000 is about 3.2GB of data.
    /mnt/workloads/YCSB-C/ycsbc -load -db redis -threads 1 -P /mnt/workloads/YCSB-C/workloads/workloadc -p recordcount=3200000 -p redis.host=127.0.0.1 -p redis.port=6379 > /mnt/results/ycsb-load.txt
    
    # Passing -p operationcount dictates how long the benchmark runs
    /mnt/workloads/YCSB-C/ycsbc -run -db redis -threads 4 -P /mnt/workloads/YCSB-C/workloads/workloadc -p operationcount=10000000 -p redis.host=127.0.0.1 -p redis.port=6379 > /mnt/results/ycsb-run.txt
    echo "YCSB workload complete."
}

end_run() {
    # Snapshot vmstat after
    cp /proc/vmstat /mnt/results/vmstat-after.txt

    # Per-node memory breakdown
    for node in 0 1; do
      cat /sys/devices/system/node/node${node}/meminfo \
        > /mnt/results/node${node}-meminfo.txt
    done

    # Your custom debugfs counters (once you've added them to the kernel)
    cat /sys/kernel/debug/ltram/stats > /mnt/results/ltram-stats.txt 2>/dev/null || true

    # Done — power off
    # poweroff -f
}


case "$1" in
	start)
        start_run
        run_workload
        end_run;;
	*)
		echo "Usage: $0 {start}"
		exit 1
esac
