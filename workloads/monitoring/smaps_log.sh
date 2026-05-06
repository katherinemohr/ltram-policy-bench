#!/bin/sh
# Usage:
#   ./smaps_log.sh <pid> > smaps_log.txt      # start collecting
#   ./smaps_log.sh --end smaps_log.txt        # stop + print summary

PIDFILE=/tmp/smaps_log.pid
INTERVAL=0.1

collect_one() {
  pid=$1
  export TS="$EPOCHREALTIME"
  awk '
  /^[0-9a-f]+-[0-9a-f]+/ { perm=$2; next }
  /^Rss:/ {
      kb=$2
      if      (perm=="r--p") ro+=kb
      else if (perm=="r-xp") rx+=kb
      else if (perm=="rw-p") rw+=kb
      else if (perm=="r--s") ros+=kb
      else if (perm=="rw-s") rws+=kb
      else                   other+=kb
  }
  END { printf "%s,%d,%d,%d,%d,%d,%d\n",
        ENVIRON["TS"], ro+0, rx+0, rw+0, ros+0, rws+0, other+0 }
  ' /proc/"$pid"/smaps
}

collect() {
    pid=$1
    echo $$ > "$PIDFILE"
    echo "ts_s,r--p_kb,r-xp_kb,rw-p_kb,r--s_kb,rw-s_kb,other_kb"
    trap 'rm -f "$PIDFILE"; exit 0' TERM INT
    while true; do
      [ -f /proc/"$pid"/smaps ] || { echo "process $pid gone"; exit 0; }
      collect_one $pid
      sleep "$INTERVAL"
    done
}

summarize() {
    awk -F',' '
    NR==1 { for(i=2;i<=NF;i++) cols[i]=$i; ncols=NF; next }
    NF<2  { next }
    NR==2 { ts_first=$1 }
    {
        ts_last=$1; nrows++
        for(i=2;i<=ncols;i++) {
            v=$i+0
            if(nrows==1 || v<mins[i]) mins[i]=v
            if(v>maxs[i])             maxs[i]=v
            avgs[i]+=v
        }
    }
    END {
        printf "Duration : %ds   (%d samples)\n\n", ts_last-ts_first, nrows
        printf "%-12s %10s %10s %10s\n", "metric","min","avg","max"
        printf "%s\n", "----------------------------------------------"
        for(i=2;i<=ncols;i++)
            printf "%-12s %10d %10d %10d\n", cols[i], mins[i], avgs[i]/nrows, maxs[i]
    }' "$1"
}

case "$1" in
    --one) collect_one $$ ;;
    --end) kill "$(cat "$PIDFILE")" 2>/dev/null; sleep 0.2; summarize "$2" ;;
    *)     collect "$1" ;;
esac
