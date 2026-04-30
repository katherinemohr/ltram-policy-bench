#!/bin/bash
# Check if at least one argument is provided
if [ "$#" -lt 1 ]; then
    echo "Usage: ./run-vm.sh [workload_name|interactive]"
    echo "Available workloads: matmul, gapbs, redis, interactive"
    exit 1
fi

MODE=$1

if [ "$MODE" = "interactive" ]; then
    INTERACTIVE=1
    WORKLOAD=""
else
    INTERACTIVE=0
    WORKLOAD=$MODE
fi

set -e

# Dynamically find the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Go up one level to find the main ltram-policy-bench root folder
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# Set all paths relative to that root folder
KERNEL="${BASE_DIR}/linux/arch/x86/boot/bzImage"
ROOTFS="${BASE_DIR}/buildroot/output/images/rootfs.ext2"
RESULTS="${BASE_DIR}/results"
WORKLOADS="${BASE_DIR}/workloads"
INPUTS="${BASE_DIR}/inputs"

mkdir -p $RESULTS

qemu-system-x86_64 \
  -enable-kvm \
  -cpu host \
  -smp 4 \
  -m 8G \
  \
  -object memory-backend-ram,id=m0,size=7936M \
  -object memory-backend-ram,id=m1,size=256M \
  -numa node,nodeid=0,memdev=m0,cpus=0-3 \
  -numa node,nodeid=1,memdev=m1 \
  -numa dist,src=0,dst=1,val=20 \
  -numa dist,src=1,dst=0,val=20 \
  -kernel "$KERNEL" \
  -drive file="$ROOTFS",format=raw,if=virtio \
  -append "root=/dev/vda rw console=ttyS0 nokaslr numa=on ltram_workload=$WORKLOAD interactive=$INTERACTIVE" \
  \
  -virtfs local,path="$WORKLOADS",mount_tag=workloads,security_model=passthrough \
  -virtfs local,path="$RESULTS",mount_tag=results,security_model=none \
  -virtfs local,path="$INPUTS",mount_tag=inputs,security_model=passthrough \
  \
  -nographic \
  -serial mon:stdio \
  -no-reboot \
  # add these if you wish to debug with gdb
  # -s -S
