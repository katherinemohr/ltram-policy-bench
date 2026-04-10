#!/bin/sh

start_run() {
    mkdir -p /mnt/workloads
    mkdir -p /mnt/results

    # Mount the shared directories from the host
    mount -t 9p -o trans=virtio workloads /mnt/workloads
    mount -t 9p -o trans=virtio results /mnt/results

    # Verify NUMA topology was picked up correctly
    numactl --hardware > /mnt/results/numa-topology.txt

    # Enable NUMA balancing
    # echo 1 > /proc/sys/kernel/numa_balancing

    # Snapshot vmstat before workload
    cp /proc/vmstat /mnt/results/vmstat-before.txt
}

run_workload() {
    # Run workload (replace with your own)
    # /mnt/workloads/stream > /mnt/results/stream-output.txt 2>&1
    echo "doing some work..."
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
    poweroff -f
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
