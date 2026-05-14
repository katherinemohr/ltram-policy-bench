# Closed-form endurance equilibrium for LtRAM tiering

Paper-ready derivation of the closed-form equilibrium that grounds the
LtRAM policy framework. Pattern adapted from Colloid (SOSP '24).

This file is intended to seed the §3 (Approach) of the DIMES paper and
the corresponding dissertation chapter.

---

## 1. The optimization problem

**Variables and notation:**
- `P` = set of memory pages eligible for tiering
- `x_p ∈ {0, 1}` = decision variable; 1 if page p is migrated to LtRAM
- `cost(p) = 1` erase per migration (uniform across pages — wear
  leveling absorbs variance at the substrate level)
- `utility(p)` = expected page-seconds-in-LtRAM if migrated now
  - = `P(stays read-only for next K epochs) × page_size × K × epoch_duration`

**Goal:** maximize total utility subject to the endurance budget
constraint.

```
maximize:    Σ utility(p) × x_p
             p ∈ P

subject to:  Σ x_p  ≤  B / T              (endurance budget)
             p ∈ P

             x_p ∈ {0, 1}                  (binary migration decision)
```

where:
- `B = N_cells × erases_per_cell` is the total endurance budget
- `T = deployment_lifetime` (e.g., 5 years × 365 × 86400 seconds)

The constraint says: over the deployment lifetime, the total number of
migrations cannot exceed the cell-erase budget. Equivalently, the
average migration rate cannot exceed B/T.

---

## 2. Lagrangian relaxation

Relax the binary constraint to `x_p ∈ [0, 1]` and form the Lagrangian:

```
L(x, λ) = Σ utility(p) × x_p  -  λ × ( Σ x_p  -  B/T )
          p                              p
```

Differentiate with respect to each `x_p`:

```
∂L/∂x_p = utility(p) - λ
```

At the KKT optimum:
- If `utility(p) > λ`:  x_p = 1 (migrate)
- If `utility(p) < λ`:  x_p = 0 (don't migrate)
- If `utility(p) = λ`:  x_p ∈ [0, 1] (boundary; tie-break)

**This is the closed-form equilibrium.**

The threshold λ is the *shadow price* of the endurance budget.
Economically: λ is the minimum utility a page must promise to justify
spending one cell-erase on its migration.

---

## 3. Connection to EEU (endurance-effective utilization)

Define:
```
EEU(p) = utility(p) / cost(p) = utility(p) / 1 = utility(p)
```

Then the equilibrium becomes:

```
migrate p   iff   EEU(p) > λ*
```

where λ* is the shadow price at equilibrium.

**The equilibrium IS the efficiency threshold.** The policy and the
metric collapse to a single principle: spend budget only on pages
whose EEU exceeds the equilibrium shadow price.

---

## 4. Uniqueness of the equilibrium

Define `f(λ)` = number of pages with utility(p) > λ.

- `f(λ)` is monotonically non-increasing in λ
- `f(0) = |P|` (migrate everything if free)
- `f(∞) = 0` (migrate nothing if infinitely expensive)

The equilibrium λ* satisfies:
```
f(λ*) = B/T
```

This equation has a unique solution by monotonicity (modulo ties at the
boundary, which can be tie-broken arbitrarily).

**At λ*, the migration rate exactly equals the budget rate, and the
policy is migrating the most-utility pages possible subject to the
budget constraint.**

---

## 5. The watermark controller

You don't know λ* in advance. It depends on workload, hardware, and
deployment parameters. Adapt λ over time via watermark binary search
(Colloid pattern):

```
State: λ_lo, λ_hi (lower and upper bounds on λ*)
       λ_current = (λ_lo + λ_hi) / 2

Each evaluation epoch:
  1. Measure realized migration rate r over the past epoch
  2. Compare to budget rate B/T:

     If r > B/T:    current λ is too low (too aggressive)
                    → λ_lo ← λ_current
                    (migration rate exceeds budget; raise the bar)

     If r < B/T:    current λ is too high (too conservative)
                    → λ_hi ← λ_current
                    (budget is underspent; lower the bar)

  3. Update: λ_current ← (λ_lo + λ_hi) / 2

  4. Drift detection:
     If λ_hi - λ_lo < ε and |r - B/T| > δ:
        Workload or budget has shifted.
        Reset bounds: λ_lo ← 0; λ_hi ← λ_max
```

This converges to λ* under stationary workloads. The drift detection
handles non-stationary cases (workload phase changes, hardware aging,
SLO changes).

---

## 6. Two-dimensional extension: wear distribution

The single-constraint formulation above ensures the total wear rate
equals the budget rate. But it doesn't ensure uniform wear across
cells.

Add a second constraint:

```
max(cell_wear[c]) / mean(cell_wear)  ≤  1 + δ      (uniformity)
                                              over all cells c
```

This becomes a second Lagrange multiplier μ. The wear-leveling target
picker uses μ to bias migration target selection toward less-worn
cells:

```
target_pfn = argmin (cell_wear[pfn] + μ × congestion[pfn])
             over candidate target pfns
```

Where `congestion[pfn]` accounts for recently-migrated cells (to avoid
hot-spotting after a burst of migrations).

The equilibrium is now a 2D point `(λ*, μ*)` in shadow-price space.
Both are adapted via independent watermark controllers.

---

## 7. Read/write asymmetry refinement

The basic utility formulation captures R/W asymmetry implicitly — pages
with high write rates have low P(stays read-only), so low utility. But
you can make it explicit:

```
utility(p) = P(read-only for K epochs)  × page_size × K × epoch_duration
           - P(written within K epochs) × demotion_cost
```

where demotion_cost = (DRAM page allocation cost) + (DRAM data copy
cost) + (one wasted NOR erase cycle).

The second term naturally penalizes mis-predicted migrations directly
in the utility function. The equilibrium λ then accounts for both
benefit and risk.

---

## 8. Comparison to Colloid

| Aspect | Colloid (performance) | This work (endurance) |
|---|---|---|
| Quantity balanced | L_D = L_A | utility(p) = λ for all migrated p |
| Equilibrium variable | p (fraction of accesses in default tier) | λ (shadow price of endurance budget) |
| Optimization | min avg latency | max total utility s.t. budget |
| Measurement | L = O/R via Little's Law on CHA | per-page utility from observed write patterns |
| Controller | watermarks p_lo, p_hi | watermarks λ_lo, λ_hi |
| Drift handling | reset on watermark convergence without latency balance | reset on watermark convergence without rate match |

**Same structure, different domain.** Colloid balances per-tier latency
for performance; this work balances per-page utility for endurance.

---

## 9. What this gives the dissertation

Three benefits over the prior heuristic framing:

1. **Theoretical foundation.** The equilibrium is derived from
   constraints, not tuned. The paper §3 has a Lagrangian derivation
   instead of magic thresholds.

2. **Self-adapting policy.** The watermark controller adapts to
   workload drift, hardware aging, and SLO changes without manual
   retuning.

3. **Closed narrative.** "Existing systems use endurance heuristics;
   we derive the endurance equilibrium from first principles and
   design a controller that converges to it." Matches Colloid's
   narrative arc — a defensible upgrade over heuristics.

The math is one paragraph; the controller is ~50 LOC; the convergence
proof is straightforward (monotonic binary search). The contribution
is the *framing*, not the complexity.

---

## 10. What this does NOT replace

This equilibrium framework is the *target rate* and *threshold* for
migration. It does not replace:

- The **ranking function** that orders pages by utility (idea #11
  option a-e) — utility(p) needs to be computed, and the ranking
  function is how
- The **gates** (idea #21: demotion history, VMA write rate) — these
  filter the candidate set before utility computation
- The **classification** (C1-C4) — class membership informs the prior
  on P(stays read-only)
- The **mechanism** (idea #1 COW window, idea #2 async TLB, idea #3
  dual-mapping) — these are how migrations are executed
- The **HW substrate** (NOR controller, FPGA access tracker) — these
  are how telemetry is gathered

The equilibrium ties everything together: it says *how many*
migrations are sustainable and *which utility threshold* gates them.
The ranking, gates, classification, mechanism, and substrate are the
machinery beneath.
