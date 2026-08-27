# Gate ladder — Blast-Radius

Derived, not inherited. The LEAN-Bench gate ladder is **per-run-type and
cost-ordered**, re-derived for each kind of run rather than copied; that source
document contains two different ladders written in the same week for two
different run types. What transfers is the pattern: order the work by cost, put
the free hygiene first, and never generate new data on top of a known-defective
base.

Cost order: free → cheap → the thing that spends money.

| Gate | Subject | Discharged by | Cost |
|---|---|---|---|
| 0 | Back up the evidence tree | Project under version control with a real initial commit; brief frozen and hashed | minutes |
| 1 | Fix known-wrong things before generating anything | The brief's internal contradictions resolved; every known-defective artifact repaired | zero model calls |
| 2 | Is the instrument's premise of a fixed target true? | Scorer determinism and label reproducibility both demonstrated | hours, no model calls |
| 3 | Reference oracle as probe | A hand-built corpus whose at-risk regions are known **by construction**, and the scorer recovers them | ~days, no model calls |
| 4 | Number hygiene before anything ships | Every number and disclosure reconciled against the artifact that produced it, in the draft that ships | prose only |
| 5 | The runs that cost money | Sample size fixed with the PI **before** the first paid run | quota / $ |

---

## Gate 0 — back up the evidence tree

**Do not extend an unbacked-up evidence base.** Every later gate writes into this
tree.

Discharged 2026-08-23: repository initialised, initial commit taken, brief frozen
to `brief/BRIEF-v1-2026-08-23.md` with sha256 recorded in `brief/FREEZE.md`.

Remote discharged 2026-08-24: `origin` is `github.com/JoshuaPinckard/co-read`
under the personal account as directed; `public` is the curated branch, `main`
the full record. Residual defect, narrower than before: commits queue behind
the index lock while workers run, so the newest results can exist on one disk
for hours at a time. Push at every lock-free opportunity.

**Prompt freeze (standing rule, added 2026-08-24).** Every worker prompt is
frozen to `prompts/` with a SHA-256 before or at launch. The first wave's
prompts were lost with a session scratchpad — a real Gate 0 breach, disclosed
in `prompts/README.md` — which is why this is now a rule rather than a habit.

## Gate 1 — fix known-wrong things, zero model calls

Nothing is asked of any model while the problem statement contradicts itself.
Open defects in the brief as of v1:

1. **§4 versus §7A on blocking.** §4.2 and §4.3 state the objective in terms of a
   budget on the fraction of the repository blocked. §7A concludes the radius
   "must be advisory and must never block." Both are in the same document. §12.2
   raises the conflict as an open question rather than resolving it. A
   mathematician told that getting the objective right matters more than the
   model choice cannot be handed two incompatible objectives.
2. **§7 versus §7A on the estimator.** §7 feature f1 advocates PMI/lift — "not
   raw frequency". §7A measured plain and time-decayed *confidence*. The brief
   advocates one estimator and has evidence for a different one. Published work
   (Moonen et al., ASE 2016, 39 interestingness measures) reports confidence and
   support already in the top-performing class, which if it holds means the f1
   framing is not merely unsupported but pointed the wrong way.
3. **§4.3's derivation does not follow.** The constraint is on the size of a
   *union* of radii, which is a coverage function and therefore submodular. A
   Lagrangian on it does not yield a single per-claim scalar threshold, because
   the marginal cost of adding a region to one agent's radius depends on whether
   another agent's radius already covers it. Blocking is not thereby wrong; the
   stated derivation is.
4. **§7A's comparisons have no error bars and the natural ones would be wrong.**
   A commit touching *k* files yields a clique of *k(k−1)/2* mutually dependent
   pairs, not independent samples. Whether adjacent rows of the headline table
   are distinguishable at all is unestablished.

## Gate 2 — is the target fixed?

Two questions, both load-bearing, neither answered:

- **Scorer determinism.** Given identical repository state, does the scorer
  return identical output? Threats: file-traversal order, hash seeding, parallel
  workers, mtime-sensitive inputs, checkout nondeterminism.
- **Label reproducibility.** Whatever defines "regions this change put at risk"
  must produce the same mapping twice from the same inputs.

Until both are answered, no exact-match, rank-equality, or top-K claim is
licensed. Any variation found means every equality claim needs a stated
tolerance, and that tolerance is published rather than patched.

Recording a deliberate decision **not** to run a gate check is a legitimate way
to close it — but then the paper must not assert the property, and the decision
is written down before the item is closed.

## Gate 3 — reference oracle as probe

The upgrade this buys: "our scorer agrees with a baseline" becomes "our scorer
recovers a known answer." Hand-build repositories and changes whose at-risk
regions are known by construction, and require recovery before shipping.

This is where the language hole gets closed. §7A states plainly that all three
measured repositories are Node-dominated and that portability to Rust,
Terraform, notebooks or vendored blobs is argued from mechanism and **not
measured**. That is the study's main validity threat, so by the Gate 5
prioritisation rule below it is where marginal evaluation effort goes first.

## Gate 4 — number hygiene

Every number and every required disclosure reconciled against the artifact that
produced it, in the draft that actually ships. The observed failure mode in the
source corpus was drafts diverging from each other, not arithmetic errors.

## Gate 5 — the runs that cost money

Sample size fixed with the PI before the first paid run. Standing constraint
from the owner: subscriptions only; nothing metered without his word. Codex is
unlimited and is therefore the workhorse; Claude and Gemini via Vertex are
rationed.

**Prioritisation rule:** spend the marginal evaluation on the axis that attacks
the main validity threat, not on more of the same. Here that means
cross-language generalisation before more JavaScript repositories.

**Confirmatory precondition (added 2026-08-24, transferred from LEAN-Bench).**
No confirmatory run until `HYPOTHESES.md` is PI-approved and its commit
precedes the first draw. Everything before that stays under `exploratory/` and
is cited as the source of predictions, never as their confirmation.

**Cloud transport policy (added 2026-08-26, after the PI's challenge).** The
engine's cloud routing was itself under concurrent modification during this
study: at the audit snapshot its freshness gate was an uncommitted
working-tree edit, 33 dirty paths sat on or near the launch seams, and the
MCP server serving launches is the live checkout. Consequences, standing:

1. Per-launch transport state for launches before this date is unknown and,
   for the dirty components, unrecoverable. Disclosed, not repaired.
2. Cloud results are consumed only through mechanical verification —
   declared tree hashes, pytest counts, label schemas — never through worker
   narrative alone, with the transport caveat attached at ingestion.
3. Future research launches either bypass the engine routing (direct
   provider CLI, pinned and recorded) or stamp engine HEAD plus a status
   digest at launch. Every cloud prompt embeds its own SHA-256 for the
   worker to echo into its report, and opens by inventorying and hashing
   every instruction file visible in its container.
4. The engine seam audit is snapshot evidence (HEAD `42ccc28` plus the
   recorded dirty set, 2026-08-25T14:11Z); every buildout seam is
   re-verified at build time.
5. Cloud is for mechanical label sweeps over hash-gated fixtures only.
   Subjects never run there: no event stream, no shared tree, no
   controllable instruction surface.

**Clean-room bar for agent subjects (added 2026-08-24).** When agents are the
thing being measured — the arms ladder — the LEAN-Bench clean-room rule
applies: environment manifest, same-day calibrated planted-marker canary,
certificates beside the data. The Codex CLI reads instruction files from
exactly its home and working directories (channel forensics, LEAN-Bench
cleanroom 2026-08-21); both locations are recorded per run. Analysis workers
are instruments, not subjects, and carry the lighter standing disclosure in
`prompts/README.md`.

---

## Document conventions inherited from the source corpus

Every report in this project carries three mandatory sections, all of which
appear in the source reports and none of which survived into the live tree as a
stated rule:

- **What I got wrong** — a table of the author's own prior claims with verdicts.
- **Claims that could NOT be verified** — a quarantine list. Listed so it can be
  checked later, *not* so it can be cited now.
- **What would change this verdict** — the falsifier.

Plus: confidence is stated **per claim with its reason**, never as a
document-level label. Coverage caveats go *before* the content they qualify.
Scope-naming rule: say "N repositories drawn under X sampling", never
"repositories".
