#!/bin/bash
# Wrapper: runs run-vm.sh with histogram capture (dirty_sweep) enabled.
# Equivalent to: LTRAM_HIST=1 bash scripts/run-vm.sh "$@"
#
# Output goes to results/runs/<date>_<workload>_<config>_hist/ — note the
# `_hist` suffix in the run name, so histogram runs don't overwrite plain runs.
#
# Usage: bash scripts/run-vm-hist.sh [workload|all|interactive]
#   bash scripts/run-vm-hist.sh redis
#   bash scripts/run-vm-hist.sh all

LTRAM_HIST=1 exec "$(dirname "$0")/run-vm.sh" "$@"
