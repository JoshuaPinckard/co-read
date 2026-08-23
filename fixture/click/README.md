# Click fixture — causal dependency probe

Vendored because Codex Cloud sandboxes have **no network egress** (probed: both
`git clone` and `curl` fail with `CONNECT tunnel failed, response 403`), so an
experiment can only compute over what is committed to this repository.

## What this is

`pallets/click` at `2c8cd3ac958a7eb316d67f2d316c27086c4c0369`, with base trees
and thirteen historical pull requests reduced to paired patches.

`base-overlap/` and `base-control/` are trees formed by cumulatively reverting
the selected commits from a historical anchor. `patches/pr-N.patch` is a PR's
source change; `patches/pr-N.tests.patch` is its test change, kept separate on
purpose.

Thirteen PRs: 787, 973, 994, 999, 1014, 1061, 2972, 2991, 3013, 3137, 3239,
3299, 3330.

## Why it is a usable instrument

Click's suite was gated before any of this: 2,016 selected tests, 13–15 seconds
per run, and **five consecutive runs producing an identical normalised result
hash** (`7de20e03b60b4de313284e0c0e779dff8d4ff28de86e6040c3e68f4206d1381b`),
with HEAD, tree, index and tracked-diff hashes unchanged before and after every
run. A flaky suite would make the outcome measure noise; this one is verified
deterministic. See `GATE.md`.

Every task was then verified to satisfy three properties, recorded in
`TASKS.md`: green on the base with tests absent, **red when the test patch is
applied without the source patch**, and green with both. That green-red-green
property is what makes the fixture a probe rather than a corpus — it means each
PR's tests genuinely discriminate the presence of its source change.

## What it is for

Measuring dependency **causally** rather than by proxy. Apply a PR's source and
test patches so its tests pass, then perturb some other region of the tree and
re-run those tests. If they break, that PR's change genuinely depended on the
perturbed region. If they don't, it didn't.

Every published change-impact result the authors are aware of validates a proxy
against another proxy — usually co-change against co-change. This fixture
produces a ground truth by intervention, which is the thing the field has never
had.

## Provenance and limits

Click is BSD-3-Clause; the vendored tree carries its own licence. Base trees and
patches were produced by the task-construction pass recorded in `TASKS.md`,
which fixed its rules before any outcome was inspected.

Thirteen tasks in one repository is enough to establish that the method works
and far too few to support a general claim. Treat any rate computed here as a
demonstration of the instrument, not as a measurement of software in general.
