# Frozen pluggy causal-task selection rule

Frozen before any historical pluggy candidate was executed. Static Git
inspection of subjects, parents, paths, versions, modes, configuration history,
and patch bytes was permitted before this freeze; historical pytest outcomes
were not.

## Compatibility window and order

- Repository: `pytest-dev/pluggy` at gated HEAD
  `e382e72789f8d791991c489d4322aa04e660b952`.
- Candidate history is the first-parent range after the pluggy 0.12.0 tag
  `a878c473a66c2574615d943d78e3af67fe995169` through and including
  `5c16e15a963d5e66f37d05b1ccfb90adf71e8e0f`, newest to oldest.
- The lower bound is fixed because pytest 7 declares `pluggy>=0.12,<2`; the
  upper bound is the last first-parent commit before PR #523 raised the
  repository's pytest minimum to 8. This keeps every candidate in one
  statically justified pytest/pluggy compatibility era.
- A commit is an unambiguous PR landing only when it has one or two parents and
  its subject either ends in `(#N)` or starts with `Merge pull request #N`.
  Reverts are excluded. Two-parent commits are diffed against parent 1.
- A PR number may occur once; the first eligible landing in frozen order wins.

## Static eligibility

1. The first-parent diff contains 2 through 40 paths with rename detection
   fixed at 50%. A diff may contain additions, modifications, deletions, or
   renames, but must contain at least one A/M production Python file under
   `src/pluggy/` and at least one A/M Python test under `testing/`.
2. At most 12 changed paths are under `testing/`.
3. Every base-tree entry and every non-deleted changed entry is a regular Git
   blob with mode `100644` or `100755`; submodules and symlinks are excluded.
4. The full binary first-parent patch is at most 200 KiB.
5. The tests-only patch is the exact `testing/` subset of that same diff and is
   nonempty.

Configuration, documentation, and packaging paths are not excluded: they are
part of a real historical PR's non-test ground truth, just as they are in the
Click fixture. The command and interpreter remain identical across arms.

The complete ordered ledger and its SHA-256 digest must be written before the
first historical suite run. No eligibility rule may change after outcomes.

## Frozen execution and causal decision

- CPython 3.11.9 on Windows.
- Isolated historical environment: `pytest==7.4.4` and
  `pytest-benchmark==4.0.0` only, plus their transitive dependencies.
- Command: `python -m pytest --junitxml=<outside-arm-path>`.
- `PYTHONPATH=<arm>/src`, `PYTHONDONTWRITEBYTECODE=1`,
  `PYTHONNOUSERSITE=1`; inherited `PYTEST_ADDOPTS` is removed.
- Each arm times out at 120 seconds and must leave all files present at arm
  start byte-identical.

For each candidate, export raw Git blobs from parent 1 and verify independent
fresh copies:

1. base suite green;
2. exact `testing/` patch applied alone, suite red with at least one
   failure/error in a changed Python test module;
3. exact full patch applied to a separate base, reconstructed commit tree
   byte-for-byte, suite green.

The full patch includes test hunks and is never stacked on the tests-only arm.
Before pytest, reverse-applying the full patch to the landed commit must also
reconstruct parent 1 exactly. A singleton base is the one-commit form of
Click's cumulative-reversion construction: the anchor is the landed commit,
the selected commit is reversed to make the base, and replay reconstructs the
anchor.

Examine the entire frozen static ledger in order until 30 candidates pass or
the ledger is exhausted. Retain every rejection; never substitute by outcome.
