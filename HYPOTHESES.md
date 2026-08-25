# Pre-stated hypotheses — committed before any confirmatory run

Status: **DRAFT for PI review. No confirmatory run happens until the PI
approves this file and its commit precedes the first draw.** (Rule transferred
from LEAN-Bench `HYPOTHESES.md`, which enforces the same precondition.)

Everything to date lives under `exploratory/` and stays there. Exploratory
numbers below are cited as the source of each prediction, never as its
confirmation. A hypothesis whose confirmatory result contradicts its
exploratory source is reported with both numbers side by side.

## A. Mined-history claims (deterministic replay; no agents)

H1. **File-granularity claiming over-blocks.** Of file-pairs edited by both
    sides of a real two-parent merge, the majority do not textually conflict.
    Prediction: over-block rate > 0.5 per repository in the majority of mined
    repositories. Source: `package.json:89` offset dispersion (exploratory);
    STORM's named-but-unquantified false-rejection failure. Test: per-repo
    rate with a whole-merge cluster bootstrap interval (commits are cliques —
    same correction as the co-change study), reported per repo, never pooled
    silently.

H2. **Clean textual merges are usually semantically sound, but not always.**
    Prediction: 0 < silent-breakage rate < 0.10 on evaluated clean merges
    (both parents green, merged tree red), per gated repository. Source:
    speculative-merging literature (method precedent, priors not treated as
    data). Test: exact binomial interval per repo. Either boundary failing is
    reportable: 0 means the integration gate is nearly free insurance; ≥0.10
    means fine-grained claiming is conditional on the gate and must be
    published as such.

    **Timing disclosure:** `exploratory/conflicts/SEMANTIC.md` (Click 0/44)
    landed on disk hours before this draft was written. It was unread at
    drafting time, but by file timestamps H2 cannot count as pre-stated for
    Click. It remains pre-stated for Pygments, for the 354 older Click clean
    merges beyond that run's cap, and for any further repository. The Click
    exposure was also thin — 13/44 merges with same-file concurrency, 4 with
    concurrent source paths — so the confirmatory question is live, not
    answered.

H3. **Conflict probability rises with divergence from the merge base.**
    Prediction: positive association between divergence (commits and changed
    lines per side) and conflict occurrence, interval excluding zero, in the
    majority of mined repositories. Source: two confounded categorical
    production points (5-behind clean vs 467-behind garbage) — explicitly
    downgraded to anecdote; this hypothesis is their proper replacement. This
    is the lane-staleness bound for the isolated-lane mode.

H4. **Within real conflicts, the two sides' changed byte ranges genuinely
    intersect in most cases.** Prediction: >0.5 of conflicted files show true
    byte-range intersection rather than same-file adjacency. Honest note:
    textual conflict is *defined* over overlapping/adjacent lines, so this
    leans expected; the informative quantity is the size of the minority —
    conflicts fine claiming would have missed — and H4's real content is that
    this minority is nonzero and must be quantified, not assumed away.

## B. The arms ladder (agents as subjects) — INCOMPLETE BY DESIGN

H5. **Coordination arms.** Sequential → unmediated concurrent → file locks →
    coordinator → contracts + mechanical claims + harvest, on re-enacted
    mined conflict sites (base from `merge-base`, intent per side from its
    commit, oracle from its tests, green-red-green verified). Directional
    prediction: the full system exceeds file locks on task completion at
    equal correctness, and unmediated concurrency pays for its throughput in
    integration failures. Cell sizes, metrics, and the exclusion rule are
    **fixed with the PI after §9.1a reports** how many task sites exist —
    committing sample sizes before knowing the population would be theater.

Preconditions for any H5 run, non-negotiable:
1. This file approved and committed first.
2. Agent-subject runs meet the LEAN-Bench clean-room bar: environment
   manifest, same-day calibrated planted-marker canary, certificates beside
   the data. Analysis workers are instruments and carry the lighter
   disclosure in `prompts/README.md`; subjects carry the full bar.
3. Prompts frozen in `prompts/` with hashes before launch.
4. The fairness rule: an agent that never finished is excluded and its slot
   redrawn; a finished-but-bad result is data. Everything stays on disk.
