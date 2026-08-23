# Posture task construction

No posture-arm outcome was run or inspected while these choices were made.

## Construction rules fixed in advance

- A task is the first-parent diff of a historical Click commit. The agent sees
  the commit message and the archived GitHub pull-request body, but not the
  commit identity, history, or ground-truth patch.
- The task base is formed by cumulatively reverting all selected commits from
  a historical anchor. Every ground-truth patch must apply alone to that base,
  and all patches applied in first-parent order must reconstruct the anchor
  tree exactly.
- Every task must modify regular files only, include historical test hunks,
  have a green source-absent suite before the hidden test overlay, fail when
  its hidden historical tests are overlaid without its source change, and pass
  with its full ground truth.
- Designed collisions use exact ground-truth path intersections. The overlap
  condition requires every task to have positive degree; the independent
  control requires every pair to be path-disjoint.
- Agent writes are conservatively coordinated as whole-file half-open claims
  `[0, 2**63-1)`. This is an interpretation choice forced by the textual
  `apply_patch` interface, which does not expose reliable byte opcodes before a
  write.

## Rejected bundles

### Pager overlap candidate

An earlier four-task candidate on pager behavior (`f36d58b`, `fc5e050`,
`1507be9`, `8b1e252`) was rejected. Its shared path looked like genuine
contention, and cumulative reversion was possible, but the commits were
temporally stacked: individual historical diffs did not all apply independently
to the all-reverted base. It therefore could not support task-level ground-truth
attribution.

### First independent-control candidate

The first control used `4529351`, `1458800`, `701b313`, and `737bfbd`. All six
pairs were path-disjoint, cumulative reversion was clean, each patch applied
alone, and the reverted base suite was green. The stricter exact-reconstruction
gate nevertheless rejected it: applying all four ground truths left the
12-line `StreamMixer.__del__` addition from `737bfbd` in
`src/click/testing.py`, while the anchor did not contain it. A later historical
change had removed that addition, so this was another hidden temporal
dependency. The failed preparation evidence is retained under
`exploratory/posture/preparation/` and
`exploratory/posture/task-artifacts/`; it is not pilot data.

Two later full preparation passes were deliberately aborted before producing
`TASKS.json` or any posture outcome. The first was stopped when the radius
cutoff policy was corrected from one global era to one pre-task cutoff per
bundle. The second was stopped when an audit found that held-out historical
tests are red on the reverted base and therefore cannot satisfy SPEC's
green-before requirement for a wrong landing. Their evidence remains under
`preparation-final{,-v2}/` and `task-artifacts-final{,-v2}/` as an integrity
record; neither pass is pilot data.

The fresh `-v3` pass was also rejected before `TASKS.json`: during its third
overlap-base determinism run, the managed corpus copy's Git object store
disappeared, invalidating every attached worktree. The target was re-cloned at
the exact accepted commit outside that managed path, passed `git fsck --full`
and a new five-run deterministic repository gate, and is the only clone used
afterward.

The fresh `-v4` pass was rejected before `TASKS.json` at the final radius gate.
Its control task PR #1061 changed `CHANGES.rst`, but that name did not yet exist
at the single cutoff immediately before the oldest control task (the file was
then named `CHANGES`). Allowing an absent claim path would have made radius
coverage silently era-dependent, so the cutoff and fail-closed coverage rule
were not relaxed. PR #1061 was removed from the design.

The first apparent replacement, PR #787 (`56314db`), was also rejected: its
path set was disjoint and live at the cutoff, but its cumulative reverse patch
conflicted in `tests/test_context.py` at the required common anchor. PR #994
(`1ea6a75`) was accepted by the pre-freeze screen: its two paths are live and
disjoint; all four reversions and all four individual ground truths apply;
chronological replay reconstructs the anchor exactly; every source-absent
leave-one-out suite is green; each historical hidden overlay is red without
its source change and green with the full ground truth; five base runs are
identical; and the reused replay radius covers all eight control paths.
Screening evidence is retained under
`exploratory/posture/control-radius-repair/`.

The fresh `-v5` pass was rejected before `TASKS.json` because the exact
generated ref `refs/posture/bases/control` still named the rejected `-v4`
synthetic base commit `6501647` (tree `db731c3`). The constructor refused to
retarget it silently. After recording and compare-and-deleting only that exact
apparatus ref, the authoritative construction pass uses fresh `-v6` roots. No
model or posture-arm outcome ran in any rejected preparation or screening pass.

## Frozen bundles

No posture-arm outcome had been generated when the following design was
frozen. `TASKS.json` is the machine-readable preregistration and records every
task path, pairwise intersection, overlap degree, base tree, patch hash,
hidden-test hash, expected focal testcase identity, and radius provenance.

| Bundle | Anchor | Tasks | Designed pair collisions | Degree |
|---|---|---|---:|---|
| overlap | `8c95c73` | `pr-3239`, `pr-3299`, `pr-3137`, `pr-2972` | 6 / 6 | 3, 3, 3, 3 |
| independent control | `b0c7523` | `pr-994`, `pr-1014`, `pr-999`, `pr-973` | 0 / 6 | 0, 0, 0, 0 |

Every overlap pair shares `src/click/core.py`. The independent-control paths
are respectively `{click/decorators.py, tests/test_options.py}`,
`{click/core.py, tests/test_arguments.py}`,
`{click/testing.py, tests/test_testing.py}`, and
`{click/types.py, tests/test_basic.py}`. Thus all six control intersections
are empty before any run.

The authoritative `-v6` pass reproduced control synthetic base tree
`9b963fa43ef6cdfdd6dabfccc35bc2fc3ecf8bf2`; applying its ground truths in
chronological order exactly reconstructed anchor tree
`6a9a3dafb46cbd8e26ffdc0cd5e410bfa197d583`. Five compatibility-enabled base
runs were identical (166 passed, 19 skipped, 3 xfailed; 1.091--1.563 seconds
including process overhead). It wrote `TASKS.json` only after the complete
overlap and control constructions, focal oracles, radius coverage checks, and
start/end apparatus integrity checks passed.

Commit `f27a8df` / PR #1000 was considered for the final control but rejected:
its 36 hidden test-only cases all passed without the source change. PR #973
replaced it and its hidden oracle failed one exact case without the source
change and passed with the full ground truth.

## Historical runtime and radius choices

The accepted repository gate remains the unmodified current Click HEAD. The
2018 control is evaluated on the same frozen Python 3.11.9 interpreter as the
modern overlap bundle, with one equal-across-arms, hashed `sitecustomize.py`
that restores only `collections.Iterable`. Loadable compatibility bytecode and
worktree `sitecustomize.py` shadows are forbidden; preparation hashes and
activation-probes the source before construction and again afterward.

Each bundle's co-change index is frozen immediately before that bundle's
oldest selected task. This excludes every selected task from its own signal
while preserving the era-appropriate pre-rename paths. Preparation fails if a
ground-truth claim path is absent from the frozen radius universe.

## Outcome and clean-null choices frozen before the pilot

The headline wrong-landing unit is per task, as required by SPEC. A task must
first have a fail-closed landing record. In shared arms that is explicitly a
collective landing, not byte authorship: a completed Codex file-change item
must map by tool ID to an allowed, changed apply-patch PostToolUse record, and
the final integrated tree must pass the apparatus audit. In isolate it must
also have a directly verified merge and presence record. If the live smoke
cannot demonstrate that mapping, shared task landings and the exchange rate
remain unavailable.

A landed task is wrong only when the actual integrated **visible** suite fails
and every cited failing testcase identity passed in the preregistered
synthetic-base runs. A passed reference test that disappears or becomes
skipped makes the outcome unverified. Held-out historical tests are restored
only in a disposable evaluator and measure task completion separately; their
source-absent failures are never headline wrong events. The headline exchange
uses overlap cells only. The independent control diagnoses overhead and false
positive collisions without diluting the contention estimand.

"Near zero" is operationalized as a pooled realised-designed-pair rate below
0.10 across accepted overlap draws. The denominator contains all six designed
pair opportunities per draw, including tasks that never claim; claim uptake is
reported separately. An individual arm below 0.10 is not compared even if the
pooled gate passes.
