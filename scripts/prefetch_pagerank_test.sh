#!/bin/bash
# ===========================================================================
# Hypothesis test: is HW prefetching (+ out-of-order MLP) what lets PageRank
# tolerate LtRAM's ~1.7x higher access latency?
#
# It runs PageRank in:
#     config A = LtRAM backed by host node 0 (local)
#     config B = LtRAM backed by host node 1 (remote, real cross-socket latency)
# each with the CPU HW prefetcher ON, then OFF, and prints the remote-vs-local
# penalty for both. If the penalty jumps when the prefetcher is OFF, prefetch
# was hiding the latency. It also runs the raw numa_lat microbench in each state.
#
# HOW TO RUN  (as your NORMAL user, NOT `sudo bash`):
#     bash scripts/prefetch_pagerank_test.sh
# It sudo's only for the privileged knobs and will prompt for your password once.
#
# WHAT IT CHANGES (all reverted automatically on exit):
#     - MSR 0x1A4  (HW prefetcher enable)        -> restored to original
#     - cpufreq governor                          -> restored to original
#     - /proc/sys/kernel/numa_balancing           -> restored to original
#
# MANUAL REVERT (only needed if the script is killed -9 and can't run cleanup):
#     sudo wrmsr -a 0x1a4 0x0
#     sudo sh -c 'echo 1 > /proc/sys/kernel/numa_balancing'
#     (and set your governor back, e.g. ondemand)
# ===========================================================================
set -u
ROOT=/scratch/hsshim/ltram-policy-bench
cd "$ROOT"
LAT="$ROOT/workloads/repat_test/numa_lat"
OUT="$ROOT/results/perf/prefetch_test_$(date +%y%m%d__%H%M%S)"; mkdir -p "$OUT"
echo "results -> $OUT"

# ---- need a quiet host: bail if a VM is already running ----
if pgrep -af qemu-system | grep -q ltram_workload; then
    echo "ERROR: a testbench VM is already running. Wait for it to finish first."; exit 1
fi
[ -x "$LAT" ] || { echo "building numa_lat..."; gcc -O2 "$LAT.c" -o "$LAT"; }

# ---- prime sudo + keep its timestamp alive for the whole ~30 min run ----
echo "### sudo is required for MSR / governor / numa_balancing (one prompt)."
sudo -v || { echo "need sudo"; exit 1; }
( while true; do sudo -n true 2>/dev/null; sleep 50; done ) & KEEPALIVE=$!
sudo modprobe msr 2>/dev/null

# ---- save original state so we can restore exactly ----
ORIG_MSR=$(sudo rdmsr -p 0 0x1a4 2>/dev/null); ORIG_MSR=${ORIG_MSR:-0}
ORIG_GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)
ORIG_NB=$(cat /proc/sys/kernel/numa_balancing 2>/dev/null)
echo "saved original: msr0x1a4=0x$ORIG_MSR  governor=$ORIG_GOV  numa_balancing=$ORIG_NB"

revert() {
    echo ">>> reverting host state..."
    sudo wrmsr -a 0x1a4 0x${ORIG_MSR} 2>/dev/null
    if [ -n "${ORIG_GOV:-}" ]; then
        for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
            echo "$ORIG_GOV" | sudo tee "$g" >/dev/null 2>&1
        done
    fi
    [ -n "${ORIG_NB:-}" ] && echo "$ORIG_NB" | sudo tee /proc/sys/kernel/numa_balancing >/dev/null 2>&1
    kill "$KEEPALIVE" 2>/dev/null
    echo ">>> reverted: prefetcher(msr0x1a4)=0x$(sudo rdmsr -p 0 0x1a4 2>/dev/null)  governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)  numa_balancing=$(cat /proc/sys/kernel/numa_balancing 2>/dev/null)"
}
trap revert EXIT INT TERM

# ---- pin for clean measurement ----
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance | sudo tee "$g" >/dev/null; done
echo 0 | sudo tee /proc/sys/kernel/numa_balancing >/dev/null

set_prefetch() {   # $1 = on|off   (MSR 0x1A4 bits 0-3: 1=disable each of the 4 prefetchers)
    if [ "$1" = off ]; then sudo wrmsr -a 0x1a4 0xf; else sudo wrmsr -a 0x1a4 0x0; fi
    echo "[prefetch=$1]  msr0x1a4=0x$(sudo rdmsr -p 0 0x1a4)"
}

# fresh pagerank run -> steady-state (last 150 trials) avg trial time, echoed
pr_run() {   # $1=label  $2.. = extra env (e.g. CONFIG_B=1)
    local label="$1"; shift
    env TOKEN_RATE=0 "$@" bash scripts/run-vm.sh pagerankrun > "$OUT/$label.runlog" 2>&1
    local d; d=$(ls -td results/pagerankrun/*/ | head -1)
    cp "$d/.prout" "$OUT/$label.prout" 2>/dev/null
    awk '/Trial Time/{n++;t[n]=$3} END{for(i=n-149;i<=n;i++)if(i>0){s+=t[i];c++}; if(c)printf "%.4f",s/c; else printf "NA"}' "$d/.prout"
}

echo "config,prefetch,trial_s" > "$OUT/pagerank.csv"
for pf in on off; do
    echo ""; echo "############################## prefetch $pf ##############################"
    set_prefetch "$pf"

    # raw latency / bandwidth at this prefetch state (host is quiet here)
    echo "## numa_lat (prefetch=$pf) ##" | tee -a "$OUT/numa_lat.txt"
    { echo -n "local  node0: "; numactl --cpunodebind=0 --membind=0 "$LAT" | tr '\n' '  '; echo; \
      echo -n "remote node1: "; numactl --cpunodebind=0 --membind=1 "$LAT" | tr '\n' '  '; echo; } | tee -a "$OUT/numa_lat.txt"

    # PageRank: config A (local LtRAM) then config B (remote LtRAM)
    A=$(pr_run "prA_$pf");            echo "A_local,$pf,$A"  | tee -a "$OUT/pagerank.csv"
    B=$(pr_run "prB_$pf" CONFIG_B=1); echo "B_remote,$pf,$B" | tee -a "$OUT/pagerank.csv"
    awk -v a="$A" -v b="$B" -v p="$pf" 'BEGIN{printf ">>> prefetch=%s: A_local=%.4fs  B_remote=%.4fs  remote penalty=%+.1f%%\n",p,a,b,100*(b-a)/a}'
done

echo ""; echo "================== SUMMARY =================="
cat "$OUT/numa_lat.txt"
echo ""
awk -F, 'NR>1{v[$1"_"$2]=$3} END{
    printf "PageRank remote-LtRAM penalty (config B vs A):\n";
    printf "   prefetch ON  : %+.1f%%\n", 100*(v["B_remote_on"]-v["A_local_on"])/v["A_local_on"];
    printf "   prefetch OFF : %+.1f%%\n", 100*(v["B_remote_off"]-v["A_local_off"])/v["A_local_off"];
    printf "(if OFF >> ON, prefetch/MLP were hiding LtRAM latency -> hypothesis confirmed)\n";
}' "$OUT/pagerank.csv"
echo "files in $OUT"
# trap revert() runs now, restoring prefetcher / governor / numa_balancing
