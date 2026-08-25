# Causal dependency sweep

## Verdict

**The pilot did not reproduce in full.** PR 973 reproduced at 9 / 42
(21.43%) and PR 999 reproduced at 9 / 42 (21.43%), but PR 2972 produced
9 / 31 (29.03%), not the pilot's 10 / 31 (32.26%). Under the study's stated
decision rule, that discrepancy outranks the other findings: the measurement
cannot currently be treated as deterministic across the pilot and this run.
No label was altered to force agreement.

Five of the thirteen requested historical patches could not be applied to
their predeclared copied base. They were therefore stopped before the
green-red-green oracle, exactly as the fail-closed protocol requires. The
remaining eight tasks passed green-red-green and exact byte and whole-tree
restoration checks, and yielded 292 `(task, file)` observations. `LABELS.json`
contains all task statuses, exact errors, oracle output, timings, provenance,
and those labels.

This instrument measures **dynamic execution dependency under one
perturbation operator and one focal test (or the failing parametrized cases)
per change**. A file can matter to correctness without being exercised by the
focal test, and an executed file can escape this particular function-entry
perturbation. Thirteen tasks in one repository demonstrate an instrument;
they do not measure software in general.

## Per-task results

The green/red/restored counts below are the actual pytest summaries. A dash
means that source application failed before pytest could run. Rates are not
imputed for unavailable tasks.

| Task | Base | Green | Tests-only red | Restored green | Files swept | Depended | Rate | Wall time |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| pr-787 | base-control | — | — | — | — | — | unavailable | 0.08 s |
| pr-973 | base-control | 20 passed | 1 failed, 19 passed | 20 passed | 42 | 9 | 21.43% | 24.18 s |
| pr-994 | base-control | 36 passed | 1 failed, 35 passed | 36 passed | 42 | 9 | 21.43% | 21.93 s |
| pr-999 | base-control | 16 passed | 1 failed, 15 passed | 16 passed | 42 | 9 | 21.43% | 20.38 s |
| pr-1014 | base-control | 18 passed | 1 failed, 17 passed | 18 passed | 42 | 4 | 9.52% | 20.84 s |
| pr-1061 | base-control | — | — | — | — | — | unavailable | 0.05 s |
| pr-2972 | base-overlap | 551 passed | 1 failed, 550 passed | 551 passed | 31 | 9 | 29.03% | 27.16 s |
| pr-2991 | base-overlap | — | — | — | — | — | unavailable | 0.05 s |
| pr-3013 | base-overlap | — | — | — | — | — | unavailable | 0.05 s |
| pr-3137 | base-overlap | 30 passed | 1 failed, 29 passed | 30 passed | 31 | 9 | 29.03% | 18.35 s |
| pr-3239 | base-overlap | 735 passed, 23 skipped | 4 failed, 731 passed, 23 skipped | 735 passed, 23 skipped | 31 | 8 | 25.81% | 32.47 s |
| pr-3299 | base-overlap | 552 passed | 1 failed, 551 passed | 552 passed | 31 | 5 | 16.13% | 27.68 s |
| pr-3330 | base-overlap | — | — | — | — | — | unavailable | 0.06 s |

Total instrument runtime was **193.29 seconds**. This includes setup,
green-red-green validation, all perturbation runs, bytecode removal, and hash
verification.

## Distribution, not a headline average

Across the eight measurable tasks, the ordered dependency rates were:

`9.52%, 16.13%, 21.43%, 21.43%, 21.43%, 25.81%, 29.03%, 29.03%`

The minimum was 9.52%, the median was 21.43%, and the maximum was 29.03%, for
a **19.51 percentage-point spread**. There is deliberately no mean or pooled
headline rate: the five unavailable tasks make an all-thirteen distribution
unavailable, and pooling would hide substantial task-level variation.

## Hubs and task-specific dependencies

For comparisons across the two Click layouts, `src/click/X.py` and
`click/X.py` are treated as the same logical path. Among the eight completed
tasks:

| Logical file | Tasks depending on it |
|---|---:|
| `click/_compat.py` | 8 / 8 |
| `click/parser.py`, `click/testing.py`, `click/types.py` | 7 / 8 each |
| `click/decorators.py`, `click/globals.py`, `click/utils.py` | 6 / 8 each |
| `click/exceptions.py` | 4 / 8 |
| `click/core.py`, `click/formatting.py`, `click/_unicodefun.py` | 3 / 8 each |
| `click/termui.py`, `click/_utils.py` | 1 / 8 each |

`_compat.py` is therefore a dynamic hub under this operator and these focal
tests. Conversely, `termui.py` and `_utils.py` are observed only for one task
each. “One” does not mean unimportant; it only means rarely exposed in these
eight focal executions. Files with zero positive labels are retained in
`LABELS.json`, rather than omitted.

## Unavailable tasks and exact failure stage

Both source-only `git apply --check` probes reported success for both fixture
layouts for every task, so layout did not select a base; the bundle declaration
in `TASKS.md` broke every tie. Applying in a standalone temporary copy then
failed for five tasks:

* **pr-787:** `click/decorators.py:61` did not apply.
* **pr-1061:** `CHANGES.rst:84` and `click/_bashcomplete.py:23` did not apply.
* **pr-2991:** `src/click/testing.py:99` did not apply.
* **pr-3013:** `src/click/shell_completion.py:405` did not apply.
* **pr-3330:** `src/click/_termui_impl.py:367` did not apply.

The exact multi-line `git apply` diagnostics are preserved in each task's
`error` field. These tasks have no fabricated `(task, file)` dependency labels:
their absence from `labels`, paired with explicit failed `task_results`, means
“not swept,” not `depended: false`.

## Claims that could NOT be verified

* A complete thirteen-task causal ground truth could not be verified: five
  patches did not reach the oracle and have no defensible dependency labels.
* Cross-run determinism could not be verified because PR 2972 differed from
  the pilot by one positive file (9 rather than 10).
* The cause of that one-file pilot discrepancy could not be identified from
  the absent pilot checkout or artifact.
* No claim about static necessity, semantic correctness, unexecuted branches,
  other perturbation operators, other tests, other Click revisions, or other
  repositories can be verified from this experiment.
* The positive hub frequencies cannot be extrapolated to the five unswept
  tasks.

## What would change this verdict

1. Supply or reconstruct bases against which the five failing source portions
   really apply, then require their own green-red-green and restoration gates
   before adding labels.
2. Recover the exact pilot instrument, interpreter, environment, and raw PR
   2972 labels. Re-running both instruments repeatedly from byte-identical
   inputs must produce the same 10 / 31 (or the same corrected result) before
   claiming determinism.
3. Repeat with additional focal tests and preregistered perturbation operators
   to broaden the verdict beyond function-entry dynamic execution.
4. Replicate on independently selected repositories and eras before making a
   general software-engineering claim.

## Claim confidence

| Claim | Confidence | Reason |
|---|---|---|
| The 292 emitted labels accurately record this run | High | Every completed task passed green-red-green; each file was byte-restored by SHA-256; bytecode was deleted before every run; and the whole copied tree was hash-verified. |
| PR 973 and PR 999 reproduced their pilot totals | High | Their observed numerators and denominators exactly match the supplied references. |
| PR 2972 did not reproduce its pilot total | High | The observed 9 / 31 differs directly from the supplied 10 / 31 reference; no rounding is involved. |
| The measurement is deterministic | Low / contradicted | The preregistered pilot comparison failed for PR 2972. |
| `_compat.py` is a hub for completed tasks under this operator | High | It has a positive label for all eight measurable tasks after normalizing only the documented `src/` layout prefix. |
| The five unavailable tasks would have similar rates | None | They never passed source application or the oracle; estimating their labels would be fabrication. |
| These rates describe software generally | None | The sample is thirteen requested historical changes from one repository, with only eight measurable and one focal failure set per change. |