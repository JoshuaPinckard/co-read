# Pre-stated hypotheses — committed before any confirmatory run

Status: **APPROVED by the PI, 2026-08-25, as amended at pre-approval review
(commit `df41955`).** The approval covers the six propositions, the derived
instrument hypotheses with their timing disclosures, the arms-ladder design
with its realism note, and the four preconditions including the canary build
cost. Confirmatory runs are licensed only after H5's cell sizes, metrics,
and exclusion rule are fixed with the PI and committed as an amendment here;
that sizing amendment must precede the first draw. (Rule transferred from
LEAN-Bench `HYPOTHESES.md`, which enforces the same precondition.)

Everything to date lives under `exploratory/` and stays there. Exploratory
numbers are cited as sources of predictions, never as confirmations. A
hypothesis whose confirmatory result contradicts its exploratory source is
reported with both numbers side by side.

**Provenance of authorship.** The propositions in section A were stated by the
PI in the working conversation *before* the corresponding measurements landed
on disk; the conversation record is the timestamp. Their formalization into
testable predictions (this file) was written afterward, partly with results
already visible — each entry says which. Section B holds derived instrument
hypotheses; section C is the PI's experimental design.

---

## A. The PI's propositions

P1. **Byte-granularity claiming works.** Proposed against a named expert
    doubt (the PI's professor questioned whether byte-level claims are
    workable). Stated before the mining and semantic-merge results existed.
    Operationalized as three measurable parts:
    (a) file claims over-block — most same-file concurrent edits do not
    textually conflict (exploratory: 69.4% on Click's full census);
    (b) real conflicts genuinely intersect at byte level, so fine claims
    catch most of what matters (exploratory: 81.7% strict intersection,
    miss-population 11.1%);
    (c) the residue — byte-disjoint edits that are wrong together — is
    caught by a test-verified integration gate, not by any claim granularity
    (exploratory: 0/44 clean merges broke; thin exposure, gate retained).
    Confirmatory test: (a) per repository with whole-merge cluster bootstrap
    on unmined repositories or the unmeasured older windows; (c) on Pygments
    and the 354 older Click clean merges, exact binomial per repo.
    Standing conditions the PI accepts: ranges are content-anchored, and the
    granularity claim is published as conditional on verified integration.

P2. **Isolation plus deliberate integration ("harvesting") beats mid-flight
    coordination.** The PI rejected the abort-and-retry frame as archaic and
    identified the replacement as the harvest model his production system
    already operates: agents work isolated copies; results integrate
    deliberately, under tests, with complete information. Primary metric,
    fixed here so the comparison cannot deadlock: **correct completions**
    (tasks passing their oracle) per agent-minute and per wall-clock hour;
    correctness violations are reported beside the rate, never traded off
    inside a composite score. Prediction: the harvest arm exceeds
    shared-tree arms on the primary metric, and redone-or-discarded effort
    is lower under isolation. The measured divergence-conflict curve (12/12
    repositories monotone) supplies the staleness bound that makes lazy
    integration safe rather than sloppy.

P3. **A single-source contract layer with a coordinator and bounded retry
    prevents livelock.** The PI's design: agents do not own their tools — an
    API layer mediates; subagents receive contracts from a single source; a
    task failing after three attempts is failed and reassigned, not looped.
    Operational definition, since "unbounded" is unobservable in a finite
    run: a **livelock candidate** is any region whose cross-agent
    alternation count trips the escalation budget. Prediction: in the
    full-system arm, every livelock candidate terminates in a visible
    bounded escalation or failure — none reaches the run's wall-clock cap
    still cycling and unescalated. Sharpened by the wild contradictory case:
    mined sites whose two sides have mutually unsatisfiable tests must
    surface as escalations at contract issuance or bounded failures — never
    as silent oscillation. The professor's adversarial-agents question is
    the origin of this test.

P4. **Claims are mechanical, never agent-declared.** The PI's observation
    ("the agents aren't the ones claiming — it's mechanical") — the claim
    record derives from mediated calls, so it is a record, not a promise.
    Measurable form: under the full system, attribution of contested writes
    from the event log alone is **100%** — every contested write resolves to
    a unique actor with no recourse to reports — against the measured 69.2%
    (9/13; sensitivity 66.7–70.0%) when ownership rests on agent
    self-report. Any contested write the log cannot attribute falsifies P4
    outright; the design property ("structurally impossible") is claimed
    only as far as this observable reaches.

P5. **Stateless one-shot dispatch removes adaptive adversarial behavior.**
    The PI's proposition: each task goes to a brand-new agent with no
    memory, so nothing can learn to over-claim, game the mediator, or hold a
    grudge. By-construction claim with one measurable consequence and one
    accepted limit. Consequence — **a weak check, not a test**: agent
    behavior across successive draws in a cell is exchangeable, no
    position-in-sequence drift (order statistics across draw index). Small
    cells can only detect gross drift, and provider-side effects could break
    exchangeability benignly, so a pass is consistent with the claim rather
    than proof of it, and a fail triggers diagnosis before it counts against
    P5. Accepted limit, stated by the study:
    contradictory *contracts* reproduce conflict through fresh agents
    indefinitely — persistence lives in the contracts, so P3's issuance
    screen, not statelessness, carries that case.

P6. **Human repository history understates agent contention; the curve
    transfers, the operating point does not.** The PI's critique of the
    mining result (1.66% base rate): the repos are all human work, already
    socially coordinated before anything reached git. Prediction, two parts:
    (a) agent arms on re-enacted sites show contention above the human base
    rate at matched divergence; the PI's own production burst (11 adjudicated
    collisions in ~28 concurrent tasks, one week, one tree) is the
    exploratory anchor; (b) a bridge model — the human-measured
    divergence-conflict curve plus agent tempo and fleet size — predicts
    each arm's realized collision rate. Tolerance, fixed now rather than
    deferred: the prediction must fall inside the arm's 95%
    cluster-bootstrap interval, or within a factor of two of the point rate
    where the interval is degenerate (zero or near-zero counts). If (b)
    fails, the transfer question is answered negatively and that is the
    finding.

---

## B. Derived instrument hypotheses (mined history; no agents)

H2. **Clean textual merges are usually semantically sound, but not always.**
    Prediction, interval-based so a zero count is scoreable: the exact 95%
    binomial **upper bound** on the silent-breakage rate is below 0.10 per
    gated repository. The "but not always" half is a program-level claim —
    at least one stable breakage observed somewhere across all evaluated
    repositories and windows; if none is ever observed, that half is
    reported unsupported at the achieved upper bounds, not quietly dropped.
    **Timing disclosure:** the Click result (0/44) landed on disk before
    this hypothesis was first drafted and was unread at drafting; by
    timestamps it is not pre-stated for Click. It remains pre-stated for
    Pygments, the 354 older Click clean merges, and any further repository.

H3. **Conflict probability rises with divergence from the merge base.**
    Pre-stated properly: drafted and committed (`34e8b64`) before
    `MINING.md` landed. Scored on the mined corpus: supported — 12/12
    informative repositories positive, within-repo AUC 0.815 [0.735, 0.885]
    on commit exposure, monotone in all three exposure measures. Remains
    open on any repository outside the mined sixteen.

H4. **Real conflicts genuinely intersect at byte level.** Pre-stated
    properly (same commit). Scored: supported — 81.7% strict intersection
    among decidable conflicted merges; the fine-claiming miss-population is
    11.1% same-file-disjoint. The quantified minority is the load the
    integration gate must carry, feeding P1(c).

---

## C. The arms ladder — the PI's experimental design

The design as the PI specified it: identify situations in real repositories
where conflicts occurred at high rates, deterministically; put agents into
those situations with the tasks that actually collided; measure what happens
with no coordination, then with a coordinator, then with contracts and the
full system.

Arms: sequential → unmediated concurrent → file locks → coordinator →
contracts + mechanical claims + harvest. Sequential is the correctness
ceiling and proves concurrency bought anything; file locks are the field's
current answer and must be run to be beaten. Population: the 107 mined
candidate sites with tests on both sides (Click 24, commons-lang 19,
terraform 12, ansible 10, others per `MINING.md`), narrowed to sites where
the fixture gate holds. **Realism note, recorded before sizing:** the
runner machinery (AST perturbation, pytest oracles, determinism gates) is
Python-only today, so the immediately runnable population is roughly
Click's 24 plus Pygments' 3 — not 107. Ansible's suite is unlikely to pass
a determinism gate at all. The choice at sizing time is explicit: run the
arms on the Python subset and keep the cross-language claim mined-only, or
build per-language runners (Go, Java) first as their own gated
instruments. Sites with mutually unsatisfiable tests are the
contradictory-task subset for P3.

**Sizing decisions fixed with the PI, 2026-08-25:**

- **Scope: build the Go and Java runners before the arms run.** The PI chose
  the largest population over the fastest start. Candidate sites before
  validation: Click 24, commons-lang 19 (Java), terraform 12 (Go), hugo 3
  (Go), Pygments 2 — ~60. Each runner is its own gated instrument: a
  perturbation operator for its language, focal-test oracles, and a
  five-run determinism gate at each site's base before that site is
  eligible. A site failing its gate is a recorded rejection.
- **Draws: 5 per cell** (site × arm), two agents per draw, all five arms.
- **Phases:** Phase 0 — site validation (each side's test patch must
  independently discriminate its source change from the shared base),
  runner builds, shim with event log and the PI's N=3 escalation budget,
  canary build and calibration. Phase 1 — pilot, 6 sites × 5 arms × 2
  draws, with a stop rule: if unmediated concurrency produces no collisions,
  the sites are not exercising the phenomenon and the main phase does not
  start until that is understood. Phase 2 — all validated sites × 5 arms ×
  5 draws.
- **Metrics as fixed in P2/P3/P4/P6:** correct completions per agent-minute
  and per wall-clock (primary); realized collision rate at byte and file
  granularity over identical events; redone effort; escalation count and
  disposition; log-only attribution rate; livelock candidates.
- The surviving-site count is a measured input, not a choice; the final
  cell table is committed here as a further amendment when validation and
  the runner gates report, before the first pilot draw.

Preconditions for any arms run, non-negotiable:
1. This file approved and committed first.
2. Agent-subject runs meet the LEAN-Bench clean-room bar: environment
   manifest, same-day calibrated planted-marker canary, certificates beside
   the data. Analysis workers are instruments and carry the lighter
   disclosure in `prompts/README.md`; subjects carry the full bar. Noted
   cost, accepted at approval: the canary instrument for the arms harness
   does not exist yet and must be built and calibrated before the first
   draw.
3. Prompts frozen in `prompts/` with hashes before launch.
4. The fairness rule: an agent that never finished is excluded and its slot
   redrawn; a finished-but-bad result is data. Everything stays on disk.
