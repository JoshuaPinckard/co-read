# Frozen Pygments causal-task selection rule

Frozen after the current-tree repository gate and static Git/patch inspection,
but before any historical candidate or synthetic base was run with pytest.

## Repository, anchor, and cloud-compatible suite

- Repository: `pygments/pygments`, BSD-2-Clause.
- Gated HEAD and task anchor:
  `38f426a6b1cd4ffc6429f5808031b7c62ea57b1f`.
- Candidate order: first-parent commits from the anchor back through
  `2024-01-01T00:00:00Z`, newest to oldest.
- The current repository gate runs its complete declared pytest suite. Task
  construction instead fixes `python -m pytest --ignore=tests/contrast` for
  every full arm. `tests/contrast` is one current-only module whose collection
  requires `wcag-contrast-ratio`; excluding it makes emitted bases executable
  in the no-egress, pytest-only cloud. The exclusion is fixed before outcomes,
  identical across arms, and must be reported rather than hidden.
- Historical-task environment: CPython 3.11.9 and `pytest==8.4.2` only (plus
  pytest's transitive dependencies). Pygments has no runtime dependency.

## Historical PR and reduced-patch rule

A landing is accepted as a historical PR only if it has one or two parents,
is not a revert, and its subject either ends in `(#N)` or starts with
`Merge pull request #N`. A two-parent landing is diffed against parent 1. PR
numbers are unique; the first landing wins.

The historical PR is deliberately reduced to the paths that carry the causal
source/test pair, `pygments/` and `tests/`. Ancillary changelog, docs, and CI
hunks are provenance but not part of the ground-truth patch. For that reduced
first-parent diff:

1. 2 through 30 paths change, all by Git status A or M with rename detection
   fixed at 50%.
2. At least one A/M production Python path is below `pygments/`.
3. At least one A/M collectable test artifact is below `tests/` but outside
   `tests/contrast/`. A collectable artifact is a Python test, a non-`.output`
   example file, or a snippet input. Golden inputs and expected outputs are
   tests in Pygments' native collector style. An `.output` file remains part
   of the tests patch and test-path count, but it is not itself a changed
   collectable target, is never passed to pytest, and cannot make an
   output-only change qualify; a changed sibling input must qualify directly.
4. At most 10 paths are below `tests/`; the reduced binary patch is at most
   500 KiB; all affected objects and the anchor tree are regular blobs with
   mode `100644` or `100755`.
5. The full patch is the exact `pygments/` plus `tests/` subset, including test
   hunks. The tests patch is its exact `tests/` subset.
6. Using a temporary Git index loaded from the anchor, reverse application of
   the full reduced patch must pass `git apply --cached --reverse --check`.

After those filters, keep candidates greedily in frozen order only when their
reduced changed-path set is disjoint from every earlier kept candidate. This
guarantees that all retained patches can be cumulatively reversed from one
anchor and replayed in any order without textual overlap. The complete static
ledger, rejected-reason counts, and SHA-256 digest must exist before pytest.

## Outcome-blind selection and final causal verification

Candidate screening is ordered and cannot cherry-pick:

1. Reverse the candidate full patch from an exact raw-blob anchor snapshot.
2. Apply its tests-only patch to a fresh copy and run only its changed
   collectable test targets with `--ignore=tests/contrast`. It must exit 1 with
   at least one JUnit failure/error mapped to a changed test artifact.
3. Apply its full patch to another fresh singleton copy; it must reconstruct
   the anchor on all reduced paths and the same targeted command must be green.

Candidates passing the screen are provisional in ledger order. Take the first
30 provisional tasks, cumulatively reverse exactly those 30 full patches from
the anchor, and require replay of all 30 to reconstruct the anchor tree
byte-for-byte. On this final shared base, verify for every task using fresh
copies:

1. the complete fixed task suite is green on the shared base (one identical
   base run may be reused for all tasks);
2. the complete fixed task suite is red after that task's tests patch alone,
   with a failure/error mapped to its changed test artifact;
3. the complete fixed task suite is green after that task's full patch alone.

If a provisional task fails any final arm, reject it, take the next screened
candidate in ledger order, rebuild the exact 30-task base, and restart final
verification. Stop with the first ordered set of 30 that all pass, or exhaust
the ledger. No threshold, candidate order, environment, path rule, or test
command may change after an outcome.

`PYTHONPATH=<arm>`, `PYTHONDONTWRITEBYTECODE=1`, and
`PYTHONNOUSERSITE=1`; inherited `PYTEST_ADDOPTS` is removed. Every process has
a 120-second timeout and must leave all files present at arm start byte-identical.
