# Locked model and estimand specification

This specification was written before the first model fit in this task. It is
the analysis contract for `compute_models.py`; changing it after seeing fitted
values requires an explicit amendment in `HAZARD.md`.

## 1. Historical conflict hazard

- Population: every evaluable row in `corpus/conflicts/_all_merges/*.jsonl`
  whose `divergence.combined_text_lines_changed` is non-null. The expected
  reconciliation is 23,428 rows.
- Outcome: `conflicted` as a Bernoulli indicator.
- Exposure: the two base-to-parent diffs' combined countable changed lines,
  exactly as stored in `divergence.combined_text_lines_changed`.
- Model, chosen before fitting:

  `logit P(conflict | x) = alpha + beta * log(1 + x)`, constrained to
  `beta >= 0`.

- Fit: row-level binomial maximum likelihood. Uncertainty is a repository-
  clustered CR1 sandwich covariance, with a deterministic 2,000-draw
  repository bootstrap (`seed=20260825`) as a small-cluster sensitivity.
- Sanity bins: MINING.md's fixed bins. The reported midpoint is the arithmetic
  midpoint of the minimum and maximum exposure actually observed in the bin;
  the table also reports the mean fitted probability over every row in the bin
  because a nonlinear curve at one representative exposure need not equal a
  pooled bin rate.
- Agent transfer: one task size `W` is `aggregate_claim_lines_per_write`.
  p50 and p90 predictions are `h(2*9)` and `h(2*56)`. The distributional
  prediction is `E[h(W1+W2)]` for two independent draws from the same empirical
  task-size distribution. `parameters.json` does not retain the 1,354 values,
  so the report must not label a reconstructed point as empirical. It will give
  (i) sharp monotonic bounds implied by count, p50, p90, p99, maximum, integer
  support at least one line, and the nearest-rank rule, and (ii) a deterministic
  piecewise-linear quantile reconstruction as a provisional point.
- Arms sites: identities are parsed from REANALYSIS.md's 19-row per-site join
  and checked against the Python manifest where possible, then joined exactly
  to one conflict row. Site exposure is the historical row's combined changed
  lines; no outcome-conditioned refit is performed. Implementation clarification
  made after the dry run exposed an input-null edge case, before fitted output
  was inspected: if the miner nulls `combined_text_lines_changed` solely because
  a side also contains a binary file, but both stored side-specific
  `text_lines_changed` values exist, the site table uses their sum and labels it
  `derived_text_component_binary_present`. Such a site was outside the fitting
  population and its hazard is a text-component extrapolation, not a like-for-
  like fitted-population point.

## 2. Collision null and claim granularity

- Paired span population: strict-classifiable conflict paths for which both
  parents have base-coordinate changes and a positive `base_blob_size`. The
  primary cohort additionally requires the path's mined operational class to
  be `handwritten`. This is conflict-selected and is not the 684-unit Click
  census.
- Null-width clarification made after the dry run, because the first
  implementation incorrectly counted unchanged gaps as changed bytes: `w_i` is
  the unioned byte mass of side i's nonempty refined base-coordinate intervals,
  plus one effective byte for each insertion anchor not already covered (the
  adjacent in-file byte at EOF). The null then places a *single contiguous span
  of that measured size* uniformly. This is the requested **contiguous-span
  null**, not a scattered-hunks null. The earlier smallest-bounding-hull width
  is retained as a named sensitivity so the correction is auditable; it is not
  selected or tuned by agreement with Click. The base blob size is the
  coordinate-correct `N`; result-blob size is retained as a disclosed
  sensitivity when available.
- Exact null: conditional on measured `(w1,w2,N)`, starts are independently and
  uniformly distributed over every integer start at which the half-open span
  fits in `[0,N)`. If `D=N-w1-w2 >= 0`,

  `P(disjoint) = (D+1)(D+2) / ((N-w1+1)(N-w2+1))`; otherwise it is zero.

  The birthday expression `exp(-w1*w2/N)` is reported only as the distinct
  scattered-byte reference; it is not tuned or substituted for the contiguous
  null.
- Granularity: for `g` in 64, 256, 1,024, and 4,096 bytes, a claim is the edit
  hull expanded outward to the fixed, file-origin-aligned `g`-byte blocks it
  touches, clipped at the file boundary. Over-blocking is the event that exact
  hulls are disjoint but their padded claims overlap. Exact-span over-block is
  zero; whole-file over-block equals exact-span disjointness. Null probabilities
  are counted exactly over the uniform integer-start support, not simulated.
- Empirical curve: the same block-padding rule is applied separately to every
  retained Click base-coordinate interval and effective insertion point; the
  miner's strict interval/anchor contact is the exact-overlap reference. It is
  reported on its own conflict-selected denominator. The full Click
  number remains 475/684 textually nonconflicting same-file units and is never
  relabeled as a base-coordinate-span curve.
- Clustering ratios: report both (a) the requested full Click textual overlap
  `209/684` divided by the pooled handwritten paired-span null expectation, with
  the denominator mismatch flagged, and (b) the apples-to-apples mined-strict
  overlap divided by the null expectation on the identical paired-span cohort.

## 3. Lease rule

- `FalseExpiry(L) = P(T > L)`, where `T` is first read result to absolute first
  write call (reported `n=405`).
- `ExpectedDanglingTime(L) = E[min(L,D)]`, where `D` is last write result to the
  observed actor/session end for a file claim (reported `n=3,094`). This assumes
  explicit close at the observed end and expiry `L` after last use; the source
  itself says end is censored and is not a close marker.
- Units: minutes. The objective is

  `P(T>L) * C_reacquire + E[min(L,D)] * C_block`.

- Default costs: `C_block=1 agent-minute/minute`. No launch-overhead measure is
  stored, so `C_reacquire` uses the measured p50 structured active span per
  core-active actor, 199.652 seconds = 3.3275 agent-minutes, explicitly as a
  workload-cost proxy rather than a startup benchmark.
- `parameters.json` retains only nearest-rank summaries. The reported provisional
  `L*` and curve therefore use a deterministic piecewise-linear quantile
  reconstruction through `(p,value)=(0,0),(0.50,p50),(0.90,p90),
  (0.99,p99),(1,max)`. Summary-consistent low/high order-statistic scenarios are
  reported, and exact empirical optimization is listed as unverified.
- Cost sensitivity evaluates the numeric ratio `C_reacquire:C_block` from
  `1:10` through `10:1`, including the measured default.
