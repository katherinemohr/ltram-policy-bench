#!/bin/bash
# Cache / MLP / prefetch isolation matrix, local (node0) vs remote (node1).
# Run on a QUIET host (no VM) — latency needs an idle machine.
#   bash scripts/numa_matrix_run.sh
# No root needed: random patterns (chase/gather) are prefetch-immune, and the
# sequential prefetch on-vs-off contribution already comes from the pagerank
# prefetch test (numa_lat.txt). To also toggle the prefetcher here, wrap with:
#   sudo wrmsr -a 0x1a4 0xf ; bash scripts/numa_matrix_run.sh ; sudo wrmsr -a 0x1a4 0x0
set -u
cd /scratch/hsshim/ltram-policy-bench
B=workloads/repat_test/numa_matrix
[ -x "$B" ] || gcc -O2 "$B.c" -o "$B"
OUT=results/perf/numa_matrix_$(date +%y%m%d__%H%M%S).txt
big=262144; small=256                       # 256 MB (>> LLC), 256 KB (fits L2)

val(){ numactl --cpunodebind=0 --membind=$1 "$B" $2 $3 | grep -o 'ns_per_line=[0-9.]*' | cut -d= -f2; }
row(){ # $1 label  $2 mode  $3 sizeKB
  local L R
  L=$(val 0 $2 $3); R=$(val 1 $2 $3)
  awk -v c="$1" -v ps="$2/${3}KB" -v l="$L" -v r="$R" \
    'BEGIN{printf "%-14s %-16s %9.2f %9.2f   %5.2fx\n",c,ps,l,r,(l>0?r/l:0)}' | tee -a "$OUT"
}
{
echo "# cache/MLP/prefetch isolation — effective ns per cache-line access"
echo "# (lower = more latency hidden; ratio = remote/local = surviving node penalty)"
printf "%-14s %-16s %9s %9s   %6s\n" cell pattern local remote ratio
} | tee "$OUT"
row "none(raw)"     chase  $big      # cache off, MLP off, prefetch off
row "cache-only"    chase  $small    # cache ON,  MLP off, prefetch off
row "MLP-only"      gather $big      # cache off, MLP ON,  prefetch off
row "cache+MLP"     gather $small    # cache ON,  MLP ON,  prefetch off
row "MLP+prefetch"  seq    $big      # cache off, MLP ON,  prefetch ON
row "all-three"     seq    $small    # cache ON,  MLP ON,  prefetch ON
echo ""
echo "prefetch contribution: compare MLP+prefetch (seq big) here vs the seq rows"
echo "in the pagerank prefetch test (numa_lat.txt, prefetch on vs off)."
echo "saved -> $OUT"
