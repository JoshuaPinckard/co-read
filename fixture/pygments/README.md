# Pygments fixture - causal dependency probe

This fixture vendors a plain source tree and 30 historical Pygments pull
requests as paired source/test patches. It is self-contained for a Codex Cloud
worker with Python and pytest but no network egress.

## What is here

- `base-shared/` is Pygments anchor
  `38f426a6b1cd4ffc6429f5808031b7c62ea57b1f` with the 30 accepted reduced
  PR patches cumulatively reversed. Its exact synthetic Git tree is
  `ad20930041fbd242b17a4ce3e84770b63743ef7e`.
- `patches/pr-N.patch` is the exact first-parent diff restricted to
  `pygments/` and `tests/`, including the PR's test hunks.
- `patches/pr-N.tests.patch` is the exact `tests/` subset of that reduced
  diff.
- `TASKS.json` is the machine-readable manifest with provenance, patch hashes,
  test targets, environment identity, and compact verification evidence.
- `TASKS.md` explains the frozen selection rule and records all 30 causal
  checks. `GATE.md` records repository selection and the five-run gate.
- `history/commit-stream.txt.gz` is a deterministic first-parent commit stream
  in the Click fixture's `C|sha|timestamp` plus name-status format. It is not a
  Git repository or object store.

There is no `.git` directory in the base and no vendored Git history.

## Why it is a causal fixture

The shared base passed 5,274 tests with 16 skips. For every accepted task, a
fresh base copy with only `pr-N.tests.patch` applied exited red with at least
one failure mapped to a changed test artifact. A different fresh copy with the
complete `pr-N.patch` applied exited green. The full task suite was used for
all three final arms. Those actual counts are in `TASKS.md` and `TASKS.json`.

The complete patch must be applied to a fresh base, not on top of the
tests-only arm: `pr-N.patch` already contains the test hunks.

## Selection rule

The rule was fixed before historical pytest outcomes. It scanned first-parent
landings since 2024-01-01 in newest-first order, accepted only unambiguous
non-revert PR subjects, reduced each diff to A/M paths under `pygments/` and
`tests/`, and required production Python plus a changed collectable test,
2-30 paths, at most 10 test paths, at most 500 KiB, regular blobs, and reverse
application from the anchor. Candidates were then kept greedily only when
their complete reduced path set was disjoint from all earlier kept paths.

All 47 kept candidates were frozen and hashed before pytest, cumulatively
reversed/replayed in a temporary index, then screened in order for targeted
red/green. The first 30 provisional candidates were placed on one common base
and had to pass complete-suite green-red-green. `TASKS.md` gives the complete
rule, accounting, and actual outcomes.

## Cloud-compatible test command

Run from a copied base tree with local Pygments first on the import path:

```text
cd <arm>
PYTHONPATH=<arm>
PYTHONDONTWRITEBYTECODE=1
PYTHONNOUSERSITE=1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python -m pytest --ignore=tests/contrast
```

Remove inherited `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, and `PYTHONHOME`. The
validated historical environment was CPython 3.11.9 with pytest 8.4.2 and only
pytest's bootstrap/transitive distributions. Pygments itself has no runtime
dependency.

`tests/contrast` is deliberately and consistently excluded from task arms
because its collection imports `wcag-contrast-ratio`, which is not available
in the stated pytest-only, no-egress cloud. The unmodified repository gate did
run that module using the repository's declared test dependencies. This
fixture therefore must not be described as running literally every upstream
test in its task arms.

When using `git apply`, copy `base-shared/` to a temporary directory outside
this parent Git checkout first. Otherwise Git may discover the parent
repository and interpret patch paths in the wrong worktree. The constructor
and all causal verification arms used outside-repository temporary trees.

## Provenance and licence

Source repository: `https://github.com/pygments/pygments.git`.

Accepted anchor:

- commit: `38f426a6b1cd4ffc6429f5808031b7c62ea57b1f`
- tree: `ef5ef11d79315fe64ed6663277d7466c4d065b16`
- local licence: BSD-2-Clause; the complete upstream licence is retained at
  `base-shared/LICENSE`
- selection-rule SHA-256:
  `c98608c8ab1f3168d693a615307c0edb0ff5d36587476b6bd81d225e4d7875bd`
- candidate-ledger SHA-256:
  `3746dde7222bcd854ed9d6af816ffaae9ed588345bdc13997928aadded4a51bf`
- verification-result SHA-256:
  `41625d3f47ddc3bd29296640d6f3c41dbf9076a0a1dc322bda0d5e051461f98c`
- emitted `TASKS.json` SHA-256:
  `ede0b9c52358fbb7d7239c0dea8ff2ba24266debf6ce85ee9e6f7dd6b2374dea`

PR identity was inferred from unambiguous first-parent commit subjects ending
in `(#N)` or beginning with `Merge pull request #N`; remote PR pages were not
queried during selection. See the limitations in
`exploratory/causal/SECOND-REPO.md`.

## Scope

This fixture removes the original measurement's one-repository dependency by
adding a repository with a very different architecture and testing style. It
does not by itself establish population-level generality, and the perturbation
sweep was intentionally not run while constructing it.
