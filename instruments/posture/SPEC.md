# Blocking versus advisory — experimental specification

## The question

When a blast radius fires, should the system **hold the write** or **tell the
agent and let it proceed**? §4 of the brief assumes blocking and budgets the
blocked fraction. §7A concludes the radius "must be advisory and must never
block." Both sentences are in the same document. The owner believes it should
block. This is Gate 1 defect 1 and it is currently settled by opinion.

There is no single winner to find. Blocking buys correctness with throughput;
advisory buys throughput with correctness. **The quantity being measured is the
exchange rate: agent-minutes lost per semantically-wrong landing prevented.**

## Three arms, not two

| Arm | Mechanism |
|---|---|
| `advisory` | Radius computed and reported to the agent; nothing is withheld |
| `blocking` | A claim intersecting a live radius is held until the radius clears |
| `isolate` | Each agent works on its own branch; merge at the end |

The third arm is not optional. Isolate-and-merge is the architecture a reviewer
will demand a comparison against, CooperBench (arXiv:2601.13295) already occupies
it, and running it costs 50% more on an experiment that is being built anyway.

## Contention must be engineered, not hoped for

A randomly drawn task set produces almost no collisions. Tasks are therefore
constructed in **overlapping bundles**: for a chosen seed file, select tasks whose
ground-truth change sets share at least one file with another task in the same
bundle. Bundle overlap degree is a controlled factor, not an outcome.

Report the realised collision rate per arm. **If the collision rate is near zero,
the run measured nothing and that must be the first line of the report.**

## Tasks with ground truth

Reverted-commit tasks. Take a real commit `c` from the target repository's
history, revert it, and give an agent the commit's message and issue text as the
task. The true change set is known — it is `c` itself. This is the standard way
these benchmarks are built and it gives an outcome measure that does not depend
on anyone's judgement.

Requirements on the target repository: real history, a test suite that runs in
under about two minutes, and deterministic tests. Do **not** use
`toolsenabled-current` — another team is working in it. Use a clone under
`corpus/_clones/`, or a scraped repository chosen for fast tests.

## Outcome measures

Per task, per arm:

- **Landed and correct** — the edit applied and the full test suite passes.
- **Landed and wrong** — the edit applied, the suite fails, and the failing test
  was green before the run. This is the semantic-invalidation event the whole
  project exists to prevent, *observed* rather than proxied.
- **Blocked-then-completed** — held, released, finished. Record the wait.
- **Rework** — the agent re-read a changed region and revised. Record the cost.
- **Abandoned / unfinished.**

Per run: wall-clock to complete the bundle, total agent-minutes, fleet idle
fraction, and realised collision count.

**The headline is the exchange rate:** additional agent-minutes consumed by
`blocking` relative to `advisory`, divided by the reduction in landed-and-wrong
events. If `blocking` prevents no wrong landings, it has no defence at any price.

## The minimal mediating harness

Production Filekeeper does not exist — its claim layer is in-memory,
per-process, and its public export is a stub that throws. **Do not build
production Filekeeper.** Build only what the experiment needs:

- A shim mediating agent file writes, holding claims as half-open byte intervals,
  conflict by interval intersection.
- A radius computed from the existing co-change index (`instruments/replay/`),
  top-K above threshold.
- Two enforcement modes, plus a branch-per-agent mode for the `isolate` arm.
- A log of every claim, radius, read, write, block, release and test outcome.

The shim is experimental apparatus. It is not a prototype of the product and
should not be described as one.

## Repetitions and validity

Agents are stochastic; one draw per cell measures nothing. Pilot at **n=5** per
cell to size the effect, then **n≥30** for any cell a conclusion rests on.

**The fairness rule, inherited:** a draw where the model never finished is
excluded and retried; a finished-but-bad response is data. Everything is kept on
disk regardless. Retry is slot-based, not attempt-based.

Randomise which arm runs first per bundle. Repository state resets to the same
commit before every draw, verified by tree hash.

## What would make this experiment wrong

- **Collision rate near zero** — nothing was measured.
- **Test suite too slow or flaky** — the outcome measure is noise. Verify
  determinism by running the suite five times on an unmodified tree first; any
  variation and the repository is unusable.
- **Tasks too easy** — if every arm completes everything correctly, there is no
  contention cost to observe. Report the base completion rate before comparing.
- The harness's own radius is computed from co-change, which the unification test
  shows measures something substantially different from what agents read. So this
  experiment tests **blocking versus advisory given the current signal**, not the
  best possible signal. State that limitation; do not let the arms comparison be
  read as a verdict on the radius itself.

## What would change the verdict

An exchange rate that is very cheap (blocking costs little and prevents real
wrong landings) settles it for blocking. One that is very expensive settles it
for advisory. A rate that straddles the plausible range means the answer depends
on the value of a wrong landing, which is a business input, not a measurement —
and saying so is a legitimate outcome.
