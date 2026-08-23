# Plan

Adjudicated 2026-08-23 from two independent planning passes briefed to argue
opposite sides — empirical-first and theory-first. They converged. Where they
disagreed, the adjudication is recorded below.

## The cloud lane is closed, and not for a reason more compute fixes

- **No `ENV_ID` is configured** in `~/.codex/config.toml`. `codex cloud exec`
  cannot be invoked without one, and it must be read from the Codex Cloud TUI.
- **Blast-Radius has no remote.** The only environment available is bound to
  `JoshuaPinckard/toolsenabled`, so every cloud invocation would first require
  pushing this evidence tree into that repository.
- **The eval set is privacy-encumbered.** It derives from the owner's own
  transcripts and carries absolute paths, branch names and search patterns from
  real work, including **2,003 records from the legal lane**. Pushing it to
  GitHub to feed Codex Cloud uploads that corpus to a third party. That needs a
  same-moment, target-naming approval from the owner and will not be done on a
  general "unlimited compute" instruction.
- **The workload does not need cloud.** The entire ten-repository commit-stream
  corpus is **4.3 MB gzipped**, and a full five-arm re-sweep is on the order of
  **thirteen minutes** of local compute. Nothing here is compute-bound.

The one genuinely cloud-shaped workload is corpus breadth for the confirmatory
tier — cloning 50–200 more repositories, which is network-bound and
embarrassingly parallel. Whether Codex Cloud environments even have network
egress at execution time is **unverified**, and settling it is a ten-minute
probe, not a plan.

## What both sides agreed on

1. **A mass parameter sweep is the wrong instrument.** The empirical planner
   accepted Moonen et al. (ASE 2016) wholesale and declined to search over
   interestingness measures. Independently, the ten-repository run reproduces
   Moonen: time-decayed and plain confidence differ by ≤0.02 R@10 on 9 of 10
   repositories, and plain confidence is *better* on `commons-lang` and
   `ansible`.
2. **λ and K are derived, not fitted.** λ is the dual price of the byte budget;
   `K = ⌊B / mean region bytes⌋`. §7A's "hard top-K cap" stops being a separate
   design choice and falls out of the budget.
3. **The endpoint must change before any further measurement.** Recall has the
   unobservable `|T(C)|` in its denominator and is not comparable across
   repositories; a rank statistic is.
4. **The whole-commit bootstrap is the cheapest high-value step**, runs on data
   already on disk, and no arm-versus-arm claim in this project is
   distinguishable from noise until it exists.
5. **Read instrumentation (§12.10) is the top priority**, from both directions —
   it is the only route to identifying levels, and if co-change is the wrong
   observable no optimisation over it matters.

## The thing both passes caught that the orchestrator missed

On `hashicorp/terraform-provider-random`, the **random draw scores P@10 0.459
against time-decayed co-change's 0.456.** Chance beats the best model. A metric
on which that happens is not measuring retrieval on that repository — it is
measuring the ground-truth-set size distribution. That row was reported without
the observation being made.

Relatedly: the retrieval eval set is **one repository, not one organisation.**
11,607 of 11,773 records (98.6%) come from a single tree. Session-level held-out
splitting buys within-repository generalisation and nothing else.

And window choice alone moves the eval set by 10.6% (10,731 / 11,773 / 11,978
records at 60s / 300s / 900s), so **any arm difference smaller than that is not
interpretable.**

## The ordered plan

Free first, then cheap, then what costs something.

| # | Step | Cost | Establishes |
|---|---|---|---|
| 0 | Preregister the objective, every constant with its `@derived`/`@perrepo` tag, the fallback ladder with a hard cap of two advances, the bootstrap procedure, and a numerical equivalence margin | hours, no model calls | Without this the run is inadmissible under the fixture rule *regardless of outcome* |
| 1 | Whole-commit cluster bootstrap on the completed ten-repository run | minutes, existing data | Whether any two rows of RESULTS.md are distinguishable at all. **Must precede any new corpus spend** |
| 2 | Close Gate 1 defect 2 — §7 advocates PMI/lift, §7A measured confidence | prose | Removes a contradiction; two independent measurements say the variants are interchangeable |
| 3 | Re-score all arms under `coverage − λ·bytes` | hours, local | The first evaluation of the project's actual objective |
| 4 | Session-dedup arm: subtract regions already read earlier in the session | hours, local | The one direct test of the cannot-un-read claim. Invisible to P@K by construction |
| 5 | Zero-parameter derived scorer, leave-one-repository-out enforced in code | hours, local | Whether a derived design beats parameter-free fusion |
| 6 | Read instrumentation on a validation subsample | engineering | Point identification of the risk probability. Irreplaceable |
| 7 | Corpus breadth, then a confirmatory tier touched once | days | The only tier that licenses a generalisation claim |

## The falsifier that runs first

**If the arm ordering under `coverage − λ·bytes` is rank-identical to the ordering
under R@10 on all ten repositories at all three budgets**, then the new
functional is a monotone relabelling of the old one, the central claim that the
field measures the wrong functional is false, and Moonen's verdict applies to us
exactly as it applied to his thirty-nine measures. Step 3 tests this in an
afternoon, and it is the first thing that should run.

A specific prediction stated in advance: re-scored under the budgeted objective,
`terraform-provider-random`'s path-over-co-change P@1 advantage should
**disappear**, because it is carried by one oversized commit that a byte-budgeted
objective cannot exploit. If it survives, the metric-artifact reading is wrong.

## The disagreement, and how it was adjudicated

Theory-first argued that specification must precede measurement, citing that ten
repositories were already spent producing a headline the identification argument
says is not a comparable quantity.

Empirical-first argued that the theory piece is small and finished, that the
estimator is the entire remaining problem, and — pointedly — that this project's
derivation-led artifacts produced four internal contradictions in a frozen brief
and a prior-art sweep that flagged five real papers as fabricated, while its
measurement-led artifact killed a headline claim.

**Adjudication: the disagreement is about ordering, not substance, and it
dissolves because the cheapest steps are the ones both sides want.** Steps 0
through 2 are free and both sides asked for them. By the time they are done, the
identification argument has already changed what step 3 measures. No further
adjudication is needed.
