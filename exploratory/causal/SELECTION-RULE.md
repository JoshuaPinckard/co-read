# Flask causal-fixture selection rule

Frozen on 2026-08-24 after the repository gate and before inspecting any
historical green/red/green outcome.

An initial pre-outcome draft recognized only single-parent squash subjects
ending in `(#N)`. Its exhaustive static screen yielded two candidates because
474 of the 600 first-parent commits in the time window are merge commits. That
draft was rejected without running historical pytest. This final rule changes
only the PR-landing recognizer to admit Flask's unambiguous two-parent forms
(both PR-title suffixes and explicit GitHub merge prefixes). Evidence for the rejected screen is retained as
`exploratory/causal/inventory/flask-static-eligibility.json`.

## Fixed source and order

- Repository: `pallets/flask`.
- Accepted gate commit: `d318b683471101618febed18996405ad26462110`.
- Walk only that commit's first-parent history, newest to oldest.
- Do not inspect commits older than 2020-01-01T00:00:00Z.
- Stop when 30 tasks pass every dynamic criterion below, or after 200
  structurally eligible candidates have been dynamically examined.

## Static eligibility

A commit enters the dynamically examined denominator only if all of these are
true, evaluated without running tests:

1. It has one or two parents and an unambiguous decimal PR marker in either of
   Flask's local-history forms: a subject ending in `(#N)`, or a two-parent
   subject beginning `Merge pull request #N `. The decimal PR number must be
   unique in the eligible sequence.
2. Its exact first-parent diff has 2--12 changed paths, all with `A` or `M`
   status and regular-file modes `100644` or `100755` on the resulting side.
3. At least one changed Python path is under `src/flask/`, and at least one
   changed Python path is under `tests/`.
4. Every changed path is under `src/flask/`, `tests/`, or `docs/`, or is one of
   `CHANGES.rst` and `README.md`. There are at most four changed test files.
5. The full binary first-parent diff is at most 100 KiB, and the PR number has
   not already appeared in an earlier structurally eligible commit.

The full patch is generated with `git diff --binary --full-index
--find-renames=50% -l0 <parent> <commit>`. The test overlay is the same diff
restricted to the literal `tests/` path. In the Click-compatible layout,
`pr-N.patch` is the full ground-truth diff, including its test hunks, and
`pr-N.tests.patch` is the test-only overlay.

## Base construction

Each candidate receives its own `base-pr-N/`, the exact first-parent tree. This
is the one-commit special case of cumulative reversion: reversing the full
patch from the historical commit must reproduce the base tree, and applying
the full patch to the base must reproduce the historical commit tree exactly.
The base must contain only regular files and directories and must not contain
Git history.

## Dynamic acceptance, in fixed order

Run the repository-configured full suite as `python -m pytest` under the one
frozen Flask environment used for the accepted gate, with `PYTHONPATH` set to
the arm's own `src`, `PYTHONDONTWRITEBYTECODE=1`, and JUnit output outside the
arm tree. Each invocation has a 120-second wall ceiling.

For each structurally eligible commit, examine these independent fresh trees:

1. **Base green:** the unpatched parent tree exits 0 with no JUnit failures or
   errors.
2. **Test-only red:** the test overlay applies cleanly to a fresh base; pytest
   exits 1, JUnit contains at least one failure, contains no collection/setup
   errors, and at least one failing case belongs to a changed test module.
3. **Full-patch green:** the full patch applies cleanly to another fresh base,
   reconstructs the historical commit tree exactly, and pytest exits 0 with no
   JUnit failures or errors.

Any timeout, patch failure, tree mismatch, non-test-caused red result, or other
exit code rejects the candidate. Accept the first 30 candidates satisfying all
criteria; do not replace an accepted task based on patch size, failure count,
topic, overlap, or any later perturbation result. The perturbation sweep is not
part of construction and must not be run.

The yield denominator is the number of structurally eligible candidates for
which dynamic examination starts. Static-filter counts are recorded
separately.
