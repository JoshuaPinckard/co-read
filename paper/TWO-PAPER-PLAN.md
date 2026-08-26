# The two-paper program

PI framing, 2026-08-26: the embedded workflow engine (graph engineering
applied FOR the user) is a research object of its own, and this study is
its precondition: how concurrent agents co-read and co-change correctly.
Paper 1 is the substrate. Paper 2 is the orchestration layer that can only
be studied once the substrate exists and is measured.

The program's through-line, LEAN-Bench included: measure the assumption the
field built on. LEAN-Bench did it for evaluation (determinacy), Paper 1 does
it for coordination (conflict), Paper 2 does it for orchestration (the
workflow graph).

---

## Paper 1 - the substrate (this repository, in flight)

**Thesis:** file-granularity fail-closed coordination is built on an
unmeasured assumption; measured, conflict is rare, monotone in divergence,
and sub-file; byte-granular mechanical claims plus a test-verified
integration gate recover the lost concurrency; the arms ladder tests the
coordination policies and the preregistered transfer prediction.

**Complete:** mining (25,073 merges), semantic replay, causal probe,
production collision corpus, transcript corpus, hazard model with the
preregistered prediction, null collision model, the wild counterexample,
25 validated sites in four strata, gate-verified shim and canary, the draft
in the mandated voice.

**Remaining, in order:**

1. PI approves Amendment 3 (the transfer prediction) - blocks first draw.
2. Redo the three cloud sweeps on pinned direct-CLI transport (hygiene;
   old runs retained as exploratory, transport-unknown).
3. Pinned artifact: engine extract (coordination modules only, from the
   frozen snapshot), credential scan, claim map, owner sign-off, publish.
   Private archive of raw transcript prefixes with recorded hash.
4. Pilot: 6 sites spanning strata x 6 arms x 2 draws, codex subjects,
   same-day canary certificate, interleaved arm order, stop rule armed.
5. Phase 2: 25 sites x 6 arms x 5 draws; claude subset for model
   generality; gemini in rationed cells (Vertex metering).
6. Score the preregistration: transfer interval [0.2%, 2.0%] on permissive
   sites, ordering claims on conflict-selected sites, P2-P5 metrics,
   wasted compute per arm.
7. Test-gate sensitivity instrument (preregistered) in parallel: inject
   clean-merging semantic breaks, measure suite catch rate.
8. Writing: arms results into Results, literature reconciliation pass
   (Ghiotto, Cassandra, Accioly, pseudo-tested-methods work - verify
   citations before naming numbers), pin the 2026 system citations,
   mentor review with the prereg file in hand.

**Rough shape:** pilot in days once approved; phase 2 is one to two weeks
of interleaved compute; writing overlaps. Venue shape: MSR/FSE-class
empirical SE with a preregistered agent experiment.

---

## Paper 2 - the workflow graph (new repository, to be created)

**Research object:** the layer that composes agent work into executed
graphs: dispatch, batching, wave scheduling, lane planning, isolation
duration, harvest ordering, retry policy. The engine's cloud-lane and
fleet system is the production instance. It was never Paper 1's studied
object; here it becomes the subject.

**Candidate thesis:** workflow graphs for agent fleets should be scheduled
against measured coordination hazards rather than static structure. Paper 1
hands over the objective function (the hazard curve, the collision
geometry, correct completions per agent-minute, wasted compute); Paper 2
studies the policy that picks each task's operating point: parallelism,
ordering, isolation duration, integration points.

**What already exists for it:**

- The arms ladder IS the seed experiment: six coordination policies
  compared under controlled contention. Paper 1 reports it as validation
  of the substrate; Paper 2 inherits it as the first row of a
  policy-comparison dataset.
- The production trace corpus: harvest reports (real wave adjudications
  are graph-execution traces with outcomes), the transcript operating
  distributions, and the transport-state snapshot of the routing itself.
- The buildout's event log and receipts (WP1-2) are the data instrument:
  once landed in the engine, every production workflow generates labeled
  graph-execution data. This is the "gathering data to optimize" the PI
  named, made deliberate.
- The hazard model and staleness budget: the scheduler's first cost terms,
  already fitted.

**Plan stages:**

A. Instrument (overlaps Paper 1 writing): land buildout WP0-WP2 in the
   engine so production workflows emit receipts and events. Every wave
   after that is data.
B. Formalize: the workflow graph model - nodes are contracted tasks, edges
   are dependencies plus shared-region hazard weights from the substrate;
   the optimization is throughput at fixed correctness with wasted compute
   priced. Preregister hypotheses before any policy comparison.
C. Datasets, three kinds: controlled (arms draws, extended with
   graph-shaped task sets rather than pairs), observational (production
   traces post-instrumentation), counterfactual (mined-history replays:
   what would policy X have cost on this real week of development).
D. Experiments: hazard-aware scheduling vs static batching vs
   optimal-offline bound; staleness-budget tuning against the measured
   curve; harvest-ordering policies; N-agent scaling beyond pairs (the
   concurrency measurements say provision for 24, the arms only test 2).
E. Same standards, inherited: gate ladder, freezes, prompt hashing,
   clean-room canary, preregistration before confirmatory runs, the shim
   extended from pairs to graphs.

**Hard dependency:** B and D wait on Paper 1's arms data and the WP1-2
event log. A can start now. The papers do not compete for the same draws;
Paper 2's controlled runs are new task-set shapes, not reruns.

**Home:** a new repository under the personal account, seeded with the
pinned artifact and the hazard model, so Paper 1 stays frozen and citable
while Paper 2 iterates.

---

## The handoff, in one line each

Paper 1 proves the substrate and measures the hazard. Paper 2 schedules
against it. The buildout is the bridge: it is simultaneously Paper 1's
recommended implementation and Paper 2's data-collection instrument.
