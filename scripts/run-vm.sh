#!/bin/bash
set -e

# TODO(kmohr): make this more portable
KERNEL=~/workspace/ltram-policy-bench/linux/arch/x86/boot/bzImage
ROOTFS=~/workspace/ltram-policy-bench/buildroot/output/images/rootfs.ext2
RESULTS=~/workspace/ltram-policy-bench/results
WORKLOADS=~/workspace/ltram-policy-bench/workloads

mkdir -p $RESULTS

qemu-system-x86_64 \
  -enable-kvm \
  -cpu host \
  -smp 4 \
  -m 8G \
  \
  -object memory-backend-ram,id=m0,size=4G \
  -object memory-backend-ram,id=m1,size=4G \
  -numa node,nodeid=0,memdev=m0,cpus=0-3 \
  -numa node,nodeid=1,memdev=m1 \
  -kernel "$KERNEL" \
  -drive file="$ROOTFS",format=raw,if=virtio \
  -append "root=/dev/vda rw console=ttyS0 nokaslr numa=on" \
  \
  -virtfs local,path="$WORKLOADS",mount_tag=workloads,security_model=mapped \
  -virtfs local,path="$RESULTS",mount_tag=results,security_model=mapped \
  \
  -nographic \
  -serial mon:stdio \
  -no-reboot
