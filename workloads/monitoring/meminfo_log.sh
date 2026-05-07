#!/bin/sh
# Captures /proc/meminfo (system-wide) and per-node meminfo at INTERVAL seconds.
# Output: a single wide CSV with system-wide columns prefixed sys_ and per-node
# columns prefixed n0_ / n1_.
#
# Usage:
#   ./meminfo_log.sh > meminfo_log.txt        # start collecting (blocks)
#   ./meminfo_log.sh --end meminfo_log.txt    # stop + print summary

INTERVAL=0.1
PIDFILE=/tmp/meminfo_log.pid

# /proc/meminfo lines are "Key:  value kB" — match $1 against "Key:".
PARSE_SYS_AWK='
$1=="AnonPages:"     { anon=$2 }
$1=="Buffers:"       { buf=$2 }
$1=="PageTables:"    { pt=$2 }
$1=="Cached:"        { cache=$2 }
$1=="Mapped:"        { mapped=$2 }
$1=="SUnreclaim:"    { sun=$2 }
$1=="KernelStack:"   { ks=$2 }
$1=="VmallocUsed:"   { vm=$2 }
$1=="Shmem:"         { shmem=$2 }
$1=="AnonHugePages:" { ahp=$2 }
END { printf "%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
      anon+0, buf+0, pt+0, cache+0, mapped+0,
      sun+0, ks+0, vm+0, shmem+0, ahp+0 }
'

# /sys/devices/system/node/nodeN/meminfo lines are "Node N Key: value kB" — match $3.
# Per-node has no Buffers / VmallocUsed; uses FilePages instead of Cached.
PARSE_NODE_AWK='
$3=="AnonPages:"     { anon=$4 }
$3=="PageTables:"    { pt=$4 }
$3=="FilePages:"     { fp=$4 }
$3=="Mapped:"        { mapped=$4 }
$3=="SUnreclaim:"    { sun=$4 }
$3=="KernelStack:"   { ks=$4 }
$3=="Shmem:"         { shmem=$4 }
$3=="AnonHugePages:" { ahp=$4 }
END { printf "%d,%d,%d,%d,%d,%d,%d,%d",
      anon+0, pt+0, fp+0, mapped+0,
      sun+0, ks+0, shmem+0, ahp+0 }
'

collect() {
    echo $$ > "$PIDFILE"

    # CSV header: 1 timestamp + 10 sys cols + 8 n0 cols + 8 n1 cols = 27 cols
    echo "ts_s,sys_anon_kb,sys_buffers_kb,sys_pagetables_kb,sys_cache_kb,sys_mapped_kb,sys_sunreclaim_kb,sys_kernel_stack_kb,sys_vmalloc_kb,sys_shmem_kb,sys_anon_huge_kb,n0_anon_kb,n0_pagetables_kb,n0_file_kb,n0_mapped_kb,n0_sunreclaim_kb,n0_kernel_stack_kb,n0_shmem_kb,n0_anon_huge_kb,n1_anon_kb,n1_pagetables_kb,n1_file_kb,n1_mapped_kb,n1_sunreclaim_kb,n1_kernel_stack_kb,n1_shmem_kb,n1_anon_huge_kb"

    trap 'rm -f "$PIDFILE"; exit 0' TERM INT

    while true; do
        # Sub-second timestamp via /proc/uptime (works regardless of busybox
        # date %N support). Value is seconds-since-boot with decimals.
        TS=$(awk '{print $1; exit}' /proc/uptime)
        SYS=$(awk "$PARSE_SYS_AWK" /proc/meminfo)
        N0=$(awk "$PARSE_NODE_AWK" /sys/devices/system/node/node0/meminfo 2>/dev/null \
             || echo "0,0,0,0,0,0,0,0")
        N1=$(awk "$PARSE_NODE_AWK" /sys/devices/system/node/node1/meminfo 2>/dev/null \
             || echo "0,0,0,0,0,0,0,0")
        echo "$TS,$SYS,$N0,$N1"
        sleep "$INTERVAL"
    done
}

summarize() {
    awk -F',' '
    NR==1 {
        for (i=2; i<=NF; i++) cols[i]=$i
        ncols=NF; next
    }
    NF < 2 { next }
    NR==2  { ts_first=$1 }
    {
        ts_last=$1; nrows++
        for (i=2; i<=ncols; i++) {
            v=$i+0
            if (nrows==1 || v < mins[i]) mins[i]=v
            if (v > maxs[i])             maxs[i]=v
            avgs[i]+=v
        }
    }
    END {
        dur=ts_last-ts_first
        printf "Duration : %.1fs   (%d samples)\n\n", dur, nrows
        printf "%-26s %12s %12s %12s\n", "metric","min","avg","max"
        printf "%s\n", "------------------------------------------------------------------"
        for (i=2; i<=ncols; i++)
            printf "%-26s %12d %12d %12d\n", cols[i], mins[i], avgs[i]/nrows, maxs[i]
    }' "$1"
}

case "$1" in
    --end)
        kill "$(cat "$PIDFILE")" 2>/dev/null
        sleep 0.2
        summarize "$2"
        ;;
    *)
        collect
        ;;
esac
