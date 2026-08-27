# attrs causal-fixture selection rule

Frozen on 2026-08-24 after attrs passed its five-run repository gate and
before any historical attrs green/red/green outcome was inspected.

## Fixed source, environment, and order

- Repository: `python-attrs/attrs`.
- Accepted gate commit: `30fd617c45854a5555550c9a0bf921bc3ee28786`.
- Walk only that commit's first-parent history, newest to oldest, with a lower
  committer-date bound of 2020-01-01T00:00:00Z.
- Stop at the first 30 accepted tasks, or after 200 structurally eligible
  candidates have been dynamically examined.
- Historical arms use CPython 3.11.9, pytest 8.4.2, Hypothesis 6.161.6,
  cloudpickle 3.1.2, Pympler 1.1, pytest-xdist 3.8.0, psutil 7.2.2, and
  pywin32 312 in one frozen experiment-local environment. Pyright is absent:
  the broken global npm shim directory is removed from `PATH`, activating the
  repository's explicit Pyright skip marker. This is fixed before task
  outcomes and is equal in every arm.

The separate pytest-8.4 historical environment follows the Click fixture's
practice of using an era-compatible historical test environment rather than
requiring the accepted HEAD gate's newer pytest version on every old base.

## Static eligibility

A commit enters the dynamically examined denominator only if all of the
following are true without running tests:

1. It has one or two parents, its subject ends in a unique decimal GitHub PR
   marker `(#N)`, and its subject does not begin `Revert `.
2. Its exact first-parent diff changes 2--20 paths, all with `A` or `M` status
   and resulting regular-file modes `100644` or `100755`.
3. At least one changed Python path is under `src/`, and at least one changed
   Python path is under `tests/`.
4. Every changed path is under `src/`, `tests/`, `typing_tests/`, `docs/`, or
   `changelog.d/`, or is `CHANGELOG.md` or `README.md`. There are at most six
   changed paths under `tests/`.
5. The full binary first-parent diff is at most 150 KiB. The complete parent
   tree contains regular files only.

Candidates are written in their complete fixed order to a JSON ledger and the
ledger is SHA-256 hashed before historical pytest starts.

## Patch and base construction

Generate the full patch with `git diff --binary --full-index
--find-renames=50% -l0 <parent> <commit>`. Generate the test overlay with the
same command restricted to literal `tests/`.

To match the Click fixture exactly, `patches/pr-N.patch` is the full
ground-truth first-parent diff, including its test hunks, while
`patches/pr-N.tests.patch` is the test-only overlay.

Each task has `base-pr-N/`, the exact first-parent tree. This is the singleton
case of cumulative reversion: reversing the full patch from the historical
commit must reproduce the base, and applying it to the base must reproduce the
historical commit tree byte-for-byte and mode-for-mode. Bases contain no Git
history.

## Dynamic acceptance

Run the repository-configured full suite as `python -m pytest`, with JUnit
written outside the disposable arm, `PYTHONPATH=<arm>/src`,
`PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, ambient `PYTEST_ADDOPTS`
removed, the frozen clean `PATH`, and a 120-second wall timeout.

For each structurally eligible candidate, in ledger order:

1. **Base green:** an unpatched fresh parent tree exits 0 with no JUnit
   failures or errors.
2. **Test-only red:** the test overlay applies to a separate fresh base; pytest
   exits 1, JUnit contains at least one failure or setup error attributable to
   a changed test module, and the run is neither a timeout nor a
   collection/usage failure.
3. **Full-patch green:** the full patch applies to a third fresh base,
   reconstructs the exact historical commit tree, and pytest exits 0 with no
   JUnit failures or errors.

Every run must leave all files present before the run byte-identical. A patch
failure, timeout, tree mismatch, non-green base/full arm, or non-qualifying red
rejects the candidate. Accept the first 30 that pass; never reorder or replace
one based on topic, patch size, failure count, overlap, or any perturbation
result. Do not run the perturbation sweep.

Yield is `accepted / dynamically examined`; the static funnel is reported
separately.
