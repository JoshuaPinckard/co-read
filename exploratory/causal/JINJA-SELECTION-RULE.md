# Frozen Jinja causal-task selection rule

Frozen before any historical Jinja candidate was executed. Static Git history
inspection (subjects, parents, paths, object modes, and patch bytes) was allowed
before this file was frozen; pytest outcomes were not.

## Repository and order

- Repository: `pallets/jinja`.
- Frozen HEAD: `5ef70112a1ff19c05324ff889dd30405b1002044`.
- Consider first-parent commits from HEAD, newest to oldest, committed on or
  after `2015-01-01T00:00:00Z`.
- A commit is an unambiguous historical PR landing only when it has one or two
  parents and its subject either ends in `(#N)` or starts with
  `Merge pull request #N`. Reverts are excluded. For a two-parent landing, the
  candidate diff is always first parent to merge commit.
- A PR number may occur at most once. The first eligible landing in the frozen
  order wins.

## Static eligibility

All of these conditions must hold before a candidate can be run:

1. The first-parent diff has 2 through 30 changed paths. Rename detection is
   fixed at 50%; every changed path must have Git name-status `A` or `M`.
2. At least one changed, added-or-modified production Python file is below
   either `src/jinja2/` or the historical `jinja2/` root.
3. At least one changed, added-or-modified Python test is below `tests/`.
4. At most eight changed paths are below `tests/`.
5. The diff does not touch `pyproject.toml`, `tox.ini`, `setup.cfg`,
   `setup.py`, `Pipfile`, `Pipfile.lock`, `.github/`, or `requirements/`.
   This prevents task patches from changing the execution environment.
6. Every changed entry and every entry in the first-parent base tree is a
   regular Git blob with mode `100644` or `100755`.
7. The full binary first-parent patch is no larger than 200 KiB.
8. The tests-only patch is the exact `tests/` path subset of that same diff and
   is nonempty.

The complete static ledger and its SHA-256 digest must be written before the
first candidate run. No static rule may be relaxed after observing an outcome.

## Frozen execution environment and dynamic decision

- Interpreter: CPython 3.11.9 on Windows.
- Historical-task environment: `pytest==8.4.2`,
  `pytest-timeout==2.4.0`, and `MarkupSafe==2.0.1` only (plus their transitive
  dependencies). This MarkupSafe release spans both legacy and `src/`-layout
  Jinja bases in the window.
- Command for each arm: `python -m pytest --junitxml=<outside-arm-path>`.
- `PYTHONPATH` is `<arm>/src;<arm>` so both historical source layouts resolve;
  `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, and any inherited
  `PYTEST_ADDOPTS` is removed.
- Each arm has a 120-second timeout and must leave all files that existed at
  arm start byte-identical. Test-created untracked files do not affect the
  tracked-state invariant.

For each candidate, construct the base from raw Git blobs at its first parent,
then verify independent fresh copies:

1. **Green base:** the full suite exits 0 with no JUnit failures or errors.
2. **Red tests-only:** the exact tests patch applies to a fresh base; the suite
   exits 1 with at least one failure or error whose JUnit classname belongs to
   a changed Python test module.
3. **Green full:** the exact full first-parent patch applies to another fresh
   base, reconstructs the landed commit tree byte-for-byte, and the full suite
   exits 0 with no JUnit failures or errors.

The full patch already contains its test hunks; it is never applied on top of
the tests-only arm. A reverse application to the landed tree must independently
reconstruct the first-parent tree before pytest is run.

Examine candidates in ledger order until 30 pass all three arms or the static
ledger is exhausted. Every examined rejection is retained. The dynamic budget
is the entire frozen static ledger; there is no outcome-based substitution or
manual cherry-picking.
