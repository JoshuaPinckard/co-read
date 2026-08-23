# Formulation: correcting §4.3, and what is identifiable

Source: independent theory-first planning pass (Claude Opus 5), 2026-08-23.
Status: **design input, not owner-approved.** The lemmas are short and checkable.
Check them before relying on them.

This document corrects a claim repeated several times earlier in this project by
the orchestrator — that the union in §4.3 makes the constraint a submodular
coverage function and therefore no scalar threshold exists. That is wrong, and
the reason it is wrong is instructive.

---

## 1. §4.3 is a resource-attribution error, not an algebra error

§4.3 charges a **shared** resource (the fraction of the repository under shadow)
against a **private** decision (agent *i*'s radius `B_i`). That mismatch is what
manufactures the coverage function. Fix the attribution and the coverage
structure disappears — it was never in the problem.

- **Blocking posture.** The scarce resource is the repository's writable surface.
  A region is blocked once, however many agents flagged it. Cost is a function of
  `S = ∪ᵢ Bᵢ` alone: `c(S) = Σ_{R∈S} c_R`, **modular in S**.
- **Advisory posture.** The scarce resource is each agent's context. Cost is
  `Σᵢ c(Bᵢ)`, **separable and modular** in the tuple, with no fleet coupling.

Under neither posture is the cost a coverage function.

A consequence worth stating: §4.2's convex superlinear aggregate cost is an
artifact of the **blocking** posture only. Under advisory there is no shared
denominator, so §4.2 and §7A's "must never block" cannot both hold. The owner's
ruling that it should block selects the shared-resource formulation, and §4.2
survives — but it belongs in the operator's utility over β, not in the
per-selection cost.

## 2. The corrected objective

Regions `𝓡`, size `c: 𝓡 → ℝ₊` in bytes, `M = c(𝓡)`. Live claims `C₁…C_N` with
weights `wᵢ ≥ 0`. Miss cost `κ_R ≥ 0`. Risk

    π_iR = Pr[ R ∈ T(Cᵢ) | 𝓕 ],   𝓕 = index state at decision time.

Note what is **absent**: `|T(Cᵢ)|`. The decision variable is the union itself.

    (P_β)   max_{S ⊆ 𝓡}  U(S) = Σ_{R∈S} g_R,
            where g_R = κ_R Σᵢ wᵢ π_iR,
            subject to  Σ_{R∈S} c_R ≤ β M.

Agent *i* is shown `Bᵢ = {R ∈ S : π_iR > 0}`, or its top-`Kᵢ` by `π_iR` where the
advisory channel is cardinality-limited.

**Why the unobservable denominator is gone.** `U(S)` is an expected weighted
*count*, obtained by linearity of expectation — which needs no independence
assumption. A recall *ratio* requires the joint law of numerator and denominator
and puts an unobservable in the denominator. An expected count requires only the
marginals `π_iR`.

**Proposition 1 (the threshold, correctly located).** The LP relaxation of (P_β)
is solved by density order `g_R / c_R`, and its dual has a single multiplier
`λ ≥ 0` with

    R ∈ S  ⟺  g_R / c_R ≥ λ

up to one boundary item. If `max_R c_R ≤ ε β M` the integrality gap is at most ε.
*Proof:* Dantzig (1957). Citation, not new work.

**Corollary — what the brief got wrong, precisely.** A single scalar threshold
*does* exist, but it is a threshold on the **pooled density** `g_R / c_R`, not on
any per-claim score `π_iR`. With per-agent radii as the decision variables and a
union constraint, the marginal price of `R` to agent *i* is
`c_R · 1[R ∉ ∪_{j≠i} B_j]`, which is not a function of `π_iR` alone. §4.3's
sentence is true of the reparameterised problem and false of the problem as
written.

**Corollary — what the operator controls.** β (throughput budget), κ (relative
miss costs), `Kᵢ` (advisory channel width). The operator does **not** control θ.
A per-claim confidence threshold is a **dependent variable — the dual price.**
Exposing it as a knob is a category error, and it explains §12.3's puzzle that
the same policy gave a mean radius of 11.4 files on one repository and 2.2 on
another: a fixed confidence level is not a fixed operating point.

**The control loop.** `V*(β)` is the LP value function, concave and
nondecreasing, so `λ*(β) = ∂V*/∂β` exists a.e. and is nonincreasing. The update
`λ_{t+1} = [λ_t + η_t (c(S_t)/M − β)]₊` is scalar monotone stochastic
approximation; Robbins–Monro conditions on `η_t` suffice. That is the entire
formal content of "a control loop rather than a constant."

## 3. The cover form, which may be the real problem

    (P_ρ)   min_{S ⊆ 𝓡} Σ_{R∈S} c_R   s.t.   Σ_{R∈S} g_R ≥ ρ Σ_{R∈𝓡} g_R.

Same admission rule, different stopping condition, different operator control.
**Which of (P_β) and (P_ρ) is the right primitive is empirical, with a one-line
diagnostic: is λ > 0?** §7A's empty-radius rates — 1.7% mature, 49.2% and 67.8%
thin-history — and RESULTS.md's 16.7% and 19.7% on Ansible and Terraform all say
that for a new customer the binding problem is **too little radius, not too
much.** If β is slack, §4's budgeted-maximisation apparatus is idle and the
brief's constraint points the wrong way.

## 4. Non-identification, and what survives it

Observable `Y_iR = 1` if R co-changes with `Cᵢ`. Target `Z_iR = 1` if
`R ∈ T(Cᵢ)`. Sensitivity `α(X) = Pr[Y=1 | Z=1, X]`, manufactured-pair rate
`γ(X) = Pr[Y=1 | Z=0, X]`. Then

    q(X) = Pr[Y=1|X] = γ + (α − γ) π(X)   ⟹   π = (q − γ)/(α − γ).

**Proposition 2 (non-identification).** From co-change alone, `q` is identified
and `π` is not. For any target `π† ∈ (0,1)` there exist admissible `(α, γ)` with
`0 ≤ γ ≤ α ≤ 1` reproducing the observed `q` exactly: take `γ=0, α=q/π†` when
`π† ≥ q`; `α=1, γ=(q−π†)/(1−π†)` when `π† < q`. The identified set for `π(X)` is
all of (0,1). **No sample size closes this.**

**What is identified.** If `α, γ` are constant in X with `α > γ`, then π is a
strictly increasing affine transform of q, so **the ranking induced by q is the
ranking induced by π.** Ranking is identified; levels are not. Homogeneity of γ
is violated in a known direction — γ grows with the sizes of commits a pair
participates in — so that assumption is the thing to stress-test.

Three consequences, each answering an open question in §12 with no compute:

- **§12.6 answered: no calibrated probabilities.** Levels are unidentified, so a
  calibrated number is false precision in the literal sense — its value is set by
  an unidentified nuisance parameter. Ranking plus a cost-derived threshold is
  not a pragmatic compromise; it is *the identified functional*.
- **§12.3 answered negatively.** A cross-repository operating point is not
  identifiable from co-change. The defensible transport rule is to match
  **quantiles of the pooled density** across repositories, not confidence levels.
- **§12.10 promoted to first.** A validation subsample where both Y and Z are
  observed identifies α and γ, and hence π on the whole population — the standard
  two-sample measurement-error correction. **Read instrumentation is not an
  optimisation; it is the only thing that makes any level statement identifiable
  at all.**

Caveat: a read log is a noisy measure of Z, giving one-sided identification
(a recorded read implies a read, hence a lower bound on α) rather than a point.

## 5. Lemma 3 — no scalar commit-size weight can fix commit-size bias

Model a commit as a union of m latent coherent work-items of sizes `k₁…k_m`
summing to k. Observed pairs `C(k,2)`; true pairs `Σⱼ C(kⱼ,2)`. Then

    SNR(k) = Σⱼ C(kⱼ,2) / ( C(k,2) − Σⱼ C(kⱼ,2) ) = Θ(1/k)

for i.i.d. item sizes with fixed mean. **Any scalar reweighting `w(k)` of all
pairs from that commit cancels in this ratio.** Weighting only chooses how much
of a fixed-SNR body of evidence to admit; it cannot change the SNR.

Corollaries:

- Under a linear-Gaussian surrogate where each pair from a size-k commit has
  signal fraction Θ(1/k), minimum-variance pooling weights by precision, giving
  `w(k) = Θ(1/k)`. This **derives** the `1/(k−1)` family — a legal `@derived`
  tag rather than a magic number.
- The alternative `2/(k(k−1))` over-corrects by a factor of k and drives true-pair
  mass to zero. It is **wrong**, not merely blunt.
- The brief's current practice of excluding commits over 20–50 files is the
  hard-thresholded version of the same rule, justified when the marginal commit's
  SNR falls below the prior's noise floor.
- **To beat Θ(1/k) you need the topic decomposition itself** — within-commit
  clustering, not a weight. This redirects §5.3 away from the reweighting
  question the brief poses.

## 6. "Cannot un-read": real for retrieval, rhetoric for blast radius

- **One-shot retrieval:** monotonicity is vacuous, there is one decision.
  Irreversibility adds nothing to the mathematics. Framing only.
- **Sequential retrieval with observation:** genuinely adaptive submodular
  maximisation (Golovin–Krause). It converts a sequence of independent per-call
  budgeted problems into **one** budgeted problem over a shared, non-replenishing
  resource, so the per-call rule is *not* "spend Γ/T per call." And it yields a
  **falsifiable prediction: λ must be nondecreasing along a session.** A
  retriever whose effective threshold does not tighten as context fills is
  provably leaving value on the table.
- **Blast radius: it does not apply.** Under blocking the radius is a gate;
  nothing enters context. Under advisory it enters as a list of region
  *identifiers*, cost `a·|B|` — a **cardinality** constraint, not a knapsack.
  §7A's "a hard top-K cap, not merely a confidence threshold" follows directly
  from the cost function's shape, and NWF applies with no partial enumeration.

Asserting irreversibility for both uses conflates a byte budget with a
cardinality budget and gets the wrong algorithm for one of them. The orchestrator
made exactly that conflation earlier in this project.

## 7. What is genuinely new here: nothing

Lemma 3 appears to be an original small observation but is elementary and would
be a remark, not a theorem. Everything else is citation — Dantzig, Nemhauser–
Wolsey–Fisher, Sviridenko, Khuller–Moss–Naor, Feige, Wolsey, Minoux,
Golovin–Krause, Manski, Carroll–Ruppert–Stefanski, Cameron–Gelbach–Miller,
Chen–Goodman, Teh.

**That is the case for doing it first, not a concession.** It is cheap, it is
Gate 1 ("zero model calls"), and three of Gate 1's four open defects are defects
in the mathematics. Feige's result — that (1−1/e) is optimal unless P=NP — is
itself an argument against spending compute searching algorithm space.

## 8. The cost already incurred

`exploratory/language-hole/RESULTS.md` spent ten repositories and returned P@1
varying by a factor of 2.3 and R@10 by a factor of 5.6, and the report itself
attributes the worst case to ground-truth set size. The identification argument
implies, *before any run*, that a recall endpoint is not comparable across
repositories and a rank endpoint is. **Ten repositories were spent producing a
headline number the theory says is not a comparable quantity.** That is a dated,
in-tree cost of measuring before specifying.

## What would change this verdict

1. **The cluster bootstrap returns "nothing is distinguishable."** Then the
   choice among objectives is unfalsifiable at achievable sample sizes and the
   binding constraint is data, not formulation.
2. **Redundancy measures near-modular.** Then the submodular apparatus collapses
   to sort-and-threshold and the primary work was always the estimator.
3. **Reads turn out recordable completely and cheaply.** Then Z is observed,
   Proposition 2 is moot, and supervised learning on read logs is correct — an
   empirical-first programme. This is the most plausible of the four.
4. **λ = 0 always.** If the fleet never approaches β, the budgeted-maximisation
   frame is idle, §4 is dead weight, and the real problem is coverage (P_ρ) — a
   different algorithm with a different control variable. The 16.7%–67.8% silence
   rates make this live, and it is the one to watch.
