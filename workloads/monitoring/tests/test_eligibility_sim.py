"""
Unit tests for the C04 round-trip policy simulator (T05).
Run: python3 workloads/monitoring/tests/test_eligibility_sim.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import dirty_eligibility_plot as E

NA, FR, DR, LT, WE = (E.E_NOT_ALLOC, E.E_FREED, E.E_DRAM, E.E_LTRAM, E.E_WRITE_EVICT)


def check(args, exp_state, exp_migs, exp_on, msg):
    st, migs, on = E.simulate_state_row(*args)
    got = [int(x) for x in st]
    assert got == exp_state, f"{msg}\n  state got={got}\n        exp={exp_state}"
    assert migs == exp_migs, f"{msg}: migrations got={migs} exp={exp_migs}"
    assert on == exp_on, f"{msg}: on_ltram got={on} exp={exp_on}"


# Writable, T=2, written once at sweep 0 -> migrates at sweep 2, stays.
check((0, 9, [0], True, 2, 10),
      [WE, DR, LT, LT, LT, LT, LT, LT, LT, LT], 1, 8,
      "write-once: one migration, on LtRAM from sweep 2")

# Writable, T=2, written at 0 and 5 -> two migrations (re-earns after evict).
check((0, 9, [0, 5], True, 2, 10),
      [WE, DR, LT, LT, LT, WE, DR, LT, LT, LT], 2, 6,
      "round-trip: evict on write, re-migrate -> 2 LtRAM writes")

# Non-writable -> on LtRAM the whole life, one placement, alloc/free boundaries.
check((2, 7, [], False, 99, 10),
      [NA, NA, LT, LT, LT, LT, LT, LT, FR, FR], 1, 6,
      "RO page placed on LtRAM at fault, never evicted")

# Writable, never written, T=3 -> migrate at sweep 2, one write.
check((0, 9, [], True, 3, 10),
      [DR, DR, LT, LT, LT, LT, LT, LT, LT, LT], 1, 8,
      "clean writable migrates once after T")

# Writable, T longer than the quiet run -> never reaches LtRAM, zero cost.
check((0, 4, [0], True, 10, 6),
      [WE, DR, DR, DR, DR, FR], 0, 0,
      "T exceeds available clean run -> no migration, no endurance cost")

print("PASS test_eligibility_sim: C04 round-trip policy")
