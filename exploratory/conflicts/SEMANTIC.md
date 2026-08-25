# Clean merges: textual over-blocking and silent semantic breakage

## Headline rates

- **`pallets/click`: file-granularity over-block rate = 475 / 684 same-file concurrent `(merge, path)` units = 69.4%; silent semantic breakage rate = 0 / 44 mechanically clean merges evaluated with green-green parents = 0.0%.**
- **`pygments/pygments`: neither rate was measured; the required `corpus/_clones` Git source was absent, so both numerators and denominators are unknown—not zero.**

**Decision:** file-level claiming was expensive in the full Click census, and fine-grained claiming was **nearly free in the measured recent Click replay**: none of 44 clean mechanical merges went red. Direct overlap exposure was limited, however: only 13 of those 44 merges contained same-file concurrency, and all 13 stayed green. Their 16 exact-path units were 10 changelog, 1 documentation, and 5 source-path units across 4 merges. This is not enough evidence to remove a test-verified integration gate from the general architecture: the intended second repository is unmeasured, the Click semantic window is recent and purposive, and a passing suite cannot expose behavior it does not test.

This study was scoped to exactly two determinism-gated Python libraries, Click and Pygments. Numeric merge-history evidence was obtainable for Click only. The semantic sample is the deterministically selected newest-first recent-history window most likely to run in the current environment; it is not a random sample. The result bounds this design decision under those conditions and does **not** measure software in general.

## Merge census and over-block rate

The Click census is complete for commits reachable from gated anchor `2c8cd3ac958a7eb316d67f2d316c27086c4c0369` (3,329 reachable commits). Pygments has no census because its required local Git history was unavailable.

| Census item | `pallets/click` | `pygments/pygments` |
|---|---:|---:|
| Reachable merge commits | 1,183 | Not measured |
| Exactly two parents | 1,183 | Not measured |
| Octopus merge commits | 0 | Not measured |
| Recorded two-parent commits skipped as topologically non-divergent | 664 | Not measured |
| Divergent two-parent merges | 519 | Not measured |
| Mechanically clean under Git 2.46 | 398 (76.7% of divergent) | Not measured |
| Textually conflicted | 121 (23.3% of divergent) | Not measured |
| `merge-tree` errors / unclassifiable merges | 0 | Not measured |
| Same-file concurrent `(merge, path)` units | 684 across 281 merges | Not measured |
| Units without textual conflict | 475 | Not measured |
| Textually conflicted units | 209 | Not measured |
| Unclassifiable units | 0 | Not measured |

“Topologically non-divergent” means one recorded parent was an ancestor of the other, so there was no two-sided concurrency to replay. These 664 records are not observed fast-forward integration events: a true fast-forward creates no merge commit and is therefore unobservable from the commit graph. No octopus merge was present.

The over-block unit is one exact path in the intersection of `git diff --no-renames B..P1` and `B..P2`. A unit enters the numerator when that exact path is absent from Git 2.46's conflict paths. Thus `475 / 684 = 69.444%` is the descriptive cost of file-level claiming over the full anchored Click census. It does not assume that path units or merges are statistically independent.

The result is not an artifact of considering only globally clean merges:

| Merge classification | Same-file units | Nonconflicting units | Textually conflicted units |
|---|---:|---:|---:|
| Globally clean merges | 226 | 226 | 0 |
| Globally conflicted merges | 458 | 249 | 209 |
| **Total** | **684** | **475** | **209** |

Even inside globally conflicted merges, file claims would over-block `249 / 458 = 54.4%` of same-file units. This matters operationally: a merge-level conflict does not imply that every concurrently touched file needs exclusive ownership.

### Census sensitivity and source coverage

Exactly one Click merge, `0e1cd42da33fc40182adc1afd478870269489319`, had two best merge bases. Following the specified plain `git merge-base P1 P2` operation selected `752ff79d…`, producing three same-file units: one conflicted and two nonconflicting. Repeating only the path-intersection calculation with alternate best base `65da5cc5…` produces seven units: one conflicted and six nonconflicting. The full rate would become `479 / 688 = 69.622%`, a change of +0.178 percentage points from the frozen `69.444%`. Git's `merge-tree` handled the criss-cross merge itself; this sensitivity concerns the path-unit census.

The Click copy was non-shallow, had no alternates, passed `git fsck --full`, and had zero missing anchor-reachable objects with `GIT_NO_LAZY_FETCH=1`. It retained promisor/`blob:none` metadata, but the complete object closure required for this anchor was already local; no hydration or network fetch was needed. All work occurred in the marked scratch copy, never in `corpus/_clones`.

## Pilot, budget, cap, and recent-history window

The mandatory pilot used the ten most-recent mechanically clean divergent merges. Every pilot merge was green on P1, P2, and the mechanical merge.

| Pilot order | Merge | Wall seconds |
|---:|---|---:|
| 1 | `2c8cd3ac958a` | 69.420 |
| 2 | `cbd7a4109da1` | 65.561 |
| 3 | `5aa8ac43527f` | 54.664 |
| 4 | `42235de05e38` | 60.579 |
| 5 | `398f9154317f` | 81.218 |
| 6 | `333c28d79cd9` | 75.321 |
| 7 | `7df2f82305f2` | 77.676 |
| 8 | `8a4ce842564a` | 64.116 |
| 9 | `16fc00e2f4a2` | 78.871 |
| 10 | `dbfb10aba610` | 55.126 |

Pilot total was 682.554 seconds; mean 68.255, median 67.490, maximum 81.218 seconds per merge. The four-hour total budget was split without outcome-dependent reallocation: 7,200 seconds per requested repository. Reserving 10% left 6,480 seconds usable for Click. The outcome-blind planning cost was the worst-case four attempts multiplied by the pilot's maximum observed attempt plus maximum per-attempt overhead:

`4 × (23.159 s + 13.422 s) = 146.325 seconds per merge`.

This froze the Click cap at **44 most-recent mechanically clean divergent two-parent merges out of 398**. The window runs from 2026-08-20 back to 2026-04-13 and spans sparse merge-stream ordinals 1 through 134. **The remaining 354 older clean merges were not evaluated.** They are beyond-cap, not exclusions. The window is intentionally biased toward recent trees because they are most likely to run in the current environment.

All 44 completed in 3,330.953 seconds (55.516 minutes) of recorded per-merge wall time, within Click's allocation. Pygments' allocation was not reassigned: its required source was absent, so no Pygments pilot ran and no cap was set. A cap of zero would falsely imply that a candidate population had been enumerated.

## Semantic replay result

For every selected clean Click merge, the replay checked out P1, P2, and the exact tree emitted by `git merge-tree --write-tree P1 P2` in fresh worktrees. The current isolated Windows environment was CPython 3.11.9 with pytest 8.4.2, plugin autoload disabled, deterministic environment variables, and a 120-second timeout per suite attempt. The command was the repository-configured `python -m pytest`: Click's default `not stress` marker selected the ordinary suite and deselected 30,000 or 31,000 stress cases, depending on the tree, matching the [strict Click gate](../../fixture/click/GATE.md). No installed Click code was on the import path; each worktree's `src` tree won resolution. A metadata-only fixed `click 8.5.0.dev` distribution record—containing no Click code—allowed every historical checkout's tests that query `importlib.metadata.version("click")` to collect. The frozen protocol also included the project's `collections.Iterable` compatibility shim, although none of the 88 selected parent checkouts referenced that alias.

Only the anchor itself has the project's strict five-run determinism evidence. At historical commits, the required mitigation was to rerun a red mechanical merge once: disagreement would be `FLAKY` and excluded; two agreeing red runs with comparable JUnit or normalized failure evidence after green parents would count as semantic breakage, including a merged-tree import or collection error. Empty or otherwise unverifiable failure evidence would be an infrastructure exclusion. No merged tree went red, so no red-result rerun was triggered.

| Semantic disposition | Count |
|---|---:|
| Selected clean merges | 44 |
| P1 green and P2 green | 44 |
| Mechanical merged tree green | 44 |
| Stable silent semantic breakage | 0 |
| Flaky merged result | 0 |
| Parent exclusions | 0 |
| Merged-tree infrastructure exclusions | 0 |
| **Evaluated denominator** | **44** |

All 132 initial suite attempts exited zero and emitted raw status, stdout, stderr, JUnit, and a pytest summary line. The merge-level exclusion rate was `0 / 44 = 0.0%`; current-environment dependency drift did not narrow this recent window.

Only 13 of the 44 evaluated merges directly exercised exact-path concurrency. Their 16 same-file units comprised 10 changelog paths, 1 documentation path, and 5 source paths across 4 merges. All 13 merged green, so the supplemental directly relevant subset is `0/13`; `0/44` remains the prescribed merge-level rate, but it is not 44 trials of overlapping source code.

### Failing tests and byte distance

There were **no silent semantic breakages**, so there are no failing tests, parent subjects, or nearest-edit byte distances to list. This is “not applicable,” not a claim that semantic interactions always occur at zero or infinite distance. The byte-distance machinery remains recorded in the instrument for any stable green-green/red case; it uses the minimum gap between the sides' edits in base-file byte coordinates and reports cross-file edits categorically because they have no shared exact-path coordinate.

## Developer-intervention check

The recorded tree differed from Git 2.46's clean mechanical tree for **1 / 398 clean Click merges = 0.251%**; 397 matched exactly. The sole case was:

- Merge `466d0add86f48502db9c02c32e30287d39ab866b`, `Merge branch '7.x'`.
- P1 `4a5ef3cd…`, `Merge pull request #1479 from pallets/zsh-autoload-backport`.
- P2 `856f0b72…`, `Merge pull request #1480 from pallets/zsh-autoload-backport`.
- The actual tree removes a duplicated two-line `CHANGES.rst` entry—“Fix autoloading for ZSH completion and add error handling. :pr:\`1348\`”—that the mechanical tree retains.

This is concrete evidence consistent with manual reconciliation after a textually clean merge, but tree inequality is a proxy, not proof: historical merge strategy, configuration, or Git-version differences are alternate explanations. This merge is outside the recent 44-merge semantic window; none of the 44 selected merges had a tree inequality.

## Exclusions, flakes, and unavailable source

| Repository / reason | Count | Denominator treatment |
|---|---:|---|
| Click parent test failure | 0 | Would exclude merge |
| Click parent collection/import error | 0 | Would exclude merge |
| Click parent missing dependency / infrastructure failure | 0 | Would exclude merge |
| Click merged-tree infrastructure failure | 0 | Would exclude merge |
| Click disagreeing red rerun (`FLAKY`) | 0 | Would exclude merge and report separately |
| Click older clean merges beyond frozen cap | 354 | Not evaluated; not exclusions |
| Pygments required Git source absent | Repository-level | No census, candidate population, pilot, cap, numerator, or denominator |

The mandated `corpus/_clones` junction contained no Pygments directory or matching Git remote/config. Retained project reports explicitly say the task-created clone and virtual environment were removed after fixture validation. A concurrent job's bare `corpus/_conflict_mirrors/pygments__pygments` repository was deliberately not used or modified: [its preparation record](PREPARATION.json) says it was cloned directly from GitHub, not copied from the required `corpus/_clones` source. The surviving eligible `fixture/pygments/base-shared` is a plain source export; its compressed history is a first-parent SHA/timestamp/name-status stream, with no P2s, complete DAG, trees, blobs, or Git object database. Substituting it would fabricate the inputs required by `merge-base`, `merge-tree`, parent checkout, conflict classification, and mechanical-versus-recorded tree comparison.

The retained gate evidence is still useful but narrower: at anchor `38f426a6b1cd4ffc6429f5808031b7c62ea57b1f`, five CPython 3.11.9 full-suite runs each had 5,330 passed, 16 skipped, exit 0, and the same normalized signature. It establishes determinism at that anchor only; it cannot supply a merge-history rate today. See [the machine-readable unavailability record](semantic-runs/pygments/unavailable.json), [Pygments gate](../../fixture/pygments/GATE.md), and [second-repository report](../causal/SECOND-REPO.md).

## Implication for claim architecture

At the measured rates, **file-granularity claiming is unnecessarily expensive, and finer-grained claiming is nearly free in the recent Click window**. The full anchored census shows that a file claim would serialize 69.4% of same-file concurrent units that Git can combine textually. Separately, none of the 44 recent clean merges went red, but only 13 contained exact-path concurrency and only 4 contained concurrent source paths. The clean-tree intervention proxy was also rare at 1/398.

The implementation recommendation is nevertheless: **use byte/line-granularity claims, but keep a test-verified integration gate for now**. That gate is cheap insurance against the unobserved tail. The evidence is strong enough to reject “same file always means unsafe” for Click, but not strong enough to assert that fine-grained integration is unconditionally safe across the intended two-repository scope. This is not a “genuinely risky” signal; no stable semantic breakage was observed. It is a bounded “nearly free” result with a deliberately conservative deployment rule.

No binomial confidence interval is reported. The over-block rate is a complete anchored Click census under the operational definition, while the semantic observations are a purposive recent prefix with clustered repository history—not a random independent sample from a software population.

## Claims that could NOT be verified

- No Pygments merge census, over-block rate, intervention count, exclusion rate, or silent-breakage rate; therefore no two-repository aggregate estimate.
- No semantic outcome for the 354 older clean Click merges beyond the frozen cap.
- No outcome for latent semantic bugs outside Click's suite coverage; “green” means test-observable green, not proof of behavioral equivalence.
- No determinism claim at historical parents or merges beyond the prescribed red-result rerun mitigation; only the anchor was five-run gated.
- No reconstruction of each merge's historical Python, dependency, platform, Git configuration, or merge strategy.
- No count of true fast-forward integration events, because they do not leave merge commits.
- No logical-file result across renames; the operational identity is exact path under `--no-renames`.
- No byte-distance distribution for semantic conflicts, because no stable green-green/red case occurred.
- No proof that the one clean-tree inequality was manual semantic work rather than historical tool/configuration behavior.
- No generalization to other repositories, languages, application types, or software overall.

## What would change this verdict

- Restore the exact audited Pygments clone—or a complete immutable local clone/bundle containing the gated anchor's reachable DAG and objects—under the mandated `corpus/_clones` provenance rule (or explicitly authorize an equivalent source), then pass non-shallow, object-closure, and `git fsck --full` checks and run the same newest-first protocol. A materially different Pygments rate would directly change the two-repository decision.
- Reconstruct hermetic historical Click environments and expand into the 354 older clean merges. A high parent-exclusion rate or a different older-history rate would narrow or reverse the present conclusion.
- Any stable green-P1/green-P2/red-merge case would move the operational verdict to **safe only behind a test-verified integration gate**. A cross-file case or a large nearest-edit byte distance would particularly weaken the idea that any textual claim granularity can prevent the interaction.
- Sustained zero breakage across both gated repositories and a substantially wider, still-green-parent window would strengthen “nearly free” enough to reconsider whether every integration needs the full gate.
- Better behavioral or integration tests could reveal interactions that the current repository suite cannot; those observations would supersede the present numerator.

## Per-claim confidence

| Claim | Confidence | Reason |
|---|---|---|
| Click merge census and clean/conflicted counts | High within the frozen anchor | Complete reachable enumeration; non-shallow copy; `fsck` and anchored object closure passed; zero `merge-tree` errors. Conditioned on Git 2.46 behavior. |
| Click over-block rate `475/684 = 69.444%` | High as an exact-path descriptive rate | Every classifiable `(merge, path)` unit is in the frozen census; unknown count is zero. Exact-path/no-renames identity and one multiple-base choice bound interpretation; alternate-base sensitivity is only +0.178 points. |
| Click semantic classification `0/44` | High for these 44 executions | All 132 raw attempts exited zero, emitted JUnit and summaries, preserved tracked state, and used hash-bound trees/protocol. |
| Click repository-level silent-breakage propensity | Low-to-moderate | The selected window is a purposive recent prefix under the current environment; only 13/44 merges directly exercised same-file overlap, 354 older clean merges were not tested, and suite coverage is finite. |
| Developer-intervention count `1/398` | High for tree inequality; moderate for manual-semantic interpretation | Tree hashes and the one diff are exact, but historical strategy/configuration/version differences can also change a clean merge tree. |
| Pygments is presently unmeasurable under the mandated `corpus/_clones` provenance rule | High | Complete checks of the allowed corpus root and named retained paths found no eligible clone, and the retained fixture constructor/artifacts demonstrably omit the required Git DAG and objects. A direct-fetch mirror from another job was intentionally out of scope. |
| Historical Pygments anchor passed its gate | Medium-high | Two internally consistent retained reports and hashes record the five runs, but the deleted source/object database prevents independent replay now. |
| “Fine-grained claiming is nearly free” beyond the measured Click window | Low | Pygments and older Click history are unmeasured; two gated Python libraries were intended but only one supplied numeric evidence. |
| Architecture recommendation to retain a test gate | Moderate | It follows conservatively from the observed low cost plus missing second-repository/generalization evidence; new stable breakage or broader zero evidence would change it. |

## Method and evidence

This is a modern replay of the speculative/proactive conflict-detection shape exemplified by [Brun et al., “Proactive Detection of Collaboration Conflicts,” ESEC/FSE 2011](https://doi.org/10.1145/2025113.2025139). The method is not claimed as novel; the measurements reported here are the contribution. The instrument attaches byte-range evidence when a stable breakage exists.

The frozen instrument is [semantic_merge_replay.py](../../instruments/conflicts/semantic_merge_replay.py), SHA-256 `f0580b8bff70adf34ff92cd42a1cf99c73c64612281a9e6b6bbf70cbc8e1e568`. Its fail-closed outputs are:

- [Click census](semantic-runs/click/census.json), SHA-256 `71ca69982c1b002016a7967e2f12ed3211c0caa9c79a851790ec174a3c8e8450`.
- [Frozen cap](semantic-runs/click/cap.json), SHA-256 `82742457abf449162f560daca2daf13772e39a0726f821f43ebf9581fe7fcf41`.
- [Replay ledger](semantic-runs/click/replay.json), whose frozen protocol SHA-256 is `021a620bdcec20d3f59f59c4496599c45256aa66a241e5e3cd81bd01a2f01822`.
- [Computed summary](semantic-runs/click/summary.json).
- [Per-merge raw attempts](semantic-runs/click/merges/), containing every exit status, stdout, stderr, JUnit XML, pytest summary line, result record, and merge-level record.
- [Pygments source-unavailability record](semantic-runs/pygments/unavailable.json).
- [Strict Click determinism gate](../../fixture/click/GATE.md).
