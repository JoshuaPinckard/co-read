# Co-read versus co-change as change predictors

## Does co-read beat co-change at predicting what changes together?

**No. Co-read loses to co-change on both primary metrics.** On the strict temporal replay, co-read reaches P@1 **0.0931** and P@10 **0.0495**, versus **0.1803** and **0.3426** for time-decayed co-change. The paired differences are -8.72 and -29.31 percentage points. A 10,000-resample whole-commit bootstrap keeps both differences below zero: P@1 `[-14.35, -5.44]` points and P@10 `[-50.11, -4.16]` points.

Co-read also loses both mandatory controls on both primary point estimates: popularity is 0.4247/0.2535 and random is 0.2189/0.2239 at P@1/P@10. Its empty-radius rate is 69.17%. This is a clean negative for the proposed substitution: reads are the better retrieval signal in the earlier work, but they are not the better same-commit change-impact signal under this construction.

Fusion is useful but does not rescue the substitution claim. RRF improves on co-change to P@1 **0.2000** and P@10 **0.3621**; the whole-commit intervals for its gains over co-change are positive. It still loses both controls at P@1, although it beats them at P@10. No learned arm dominates both controls at both primary cutoffs.

**Confidence: high for this fixed repository, corpus snapshot, and operationalization.** The loss appears on both primary metrics, survives whole-commit resampling, remains in the target-read-window sensitivity, and is unchanged by the copied-prefix sensitivity. This is not a claim that co-read can never predict change impact in another repository or under a different preregistered score.

## Primary results

The externally stated transcript window begins `2026-07-17T00:00:00Z`; the exact snapshot endpoint is `2026-08-24T04:43:33.951047Z`. The lower bound comes from the task statement and is not encoded in the compact read header, which records only the exact endpoint. All **1,164** first-parent commits at target HEAD fall inside it. After applying the unchanged replay eligibility rule—exclude files created by the query commit, then require at least two surviving logical files—**556 commits** and **5,929 seed queries** remain. This passes the predeclared minimum of 20 eligible commits.

P@10 uses a fixed denominator of ten, so short or empty lists receive misses. Recall is shown directly beside the query-weighted mean ground-truth size, as required. The mean is the same for every arm because every arm receives exactly the same queries.

| Arm | P@1 | P@10 | Mean ground-truth size | R@10 | R@20 | Empty radius | Median query time |
|---|---:|---:|---:|---:|---:|---:|---:|
| `cochange` | 0.1803 | 0.3426 | 521.56 | 0.1167 | 0.1401 | 0.35% | 962.1 µs |
| `coread` | 0.0931 | 0.0495 | 521.56 | 0.0688 | 0.0838 | 69.17% | 9.5 µs |
| `fused` | 0.2000 | 0.3621 | 521.56 | 0.1374 | 0.1716 | 0.25% | 1,488.2 µs |
| `popularity` | 0.4247 | 0.2535 | 521.56 | 0.0586 | 0.0812 | 0.00% | 43.3 µs |
| `random` | 0.2189 | 0.2239 | 521.56 | 0.0060 | 0.0117 | 0.00% | 50.5 µs |

The ground-truth distribution is extremely skewed. The median eligible commit has ground-truth size 2 per query, but the maximum is 1,529. The largest commit contributes 25.81% of all queries and the five largest contribute 48.36%. That is why popularity and random can have high query-weighted precision while covering little of the ground truth. It is also why recall without the 521.56 mean would be misleading. These commits were not removed or downweighted because doing so would change the replay protocol.

Timing is an implementation diagnostic, not a portable comparison. It covers ranked-list production; fused time includes both source rankings plus RRF. Index construction and transcript extraction are outside the query timer.

## Paired whole-commit uncertainty

No query-level interval was computed. A commit with *k* eligible files creates *k* dependent seed queries, so all of a commit's queries were resampled together and the same sampled commits were used for every arm.

| Paired difference | Point estimate | Whole-commit 95% percentile interval |
|---|---:|---:|
| Co-read minus co-change, P@1 | -0.0872 | [-0.1435, -0.0544] |
| Co-read minus co-change, P@10 | -0.2931 | [-0.5011, -0.0416] |
| Co-read minus co-change, R@10 | -0.0479 | [-0.0874, -0.0282] |
| Co-read minus co-change, R@20 | -0.0563 | [-0.0979, -0.0352] |
| Fused minus co-change, P@1 | +0.0197 | [+0.0057, +0.0449] |
| Fused minus co-change, P@10 | +0.0194 | [+0.0089, +0.0394] |

These are descriptive intervals for this one time-ordered repository. Whole-commit resampling respects within-commit dependence, but it does not remove serial dependence between adjacent commits or turn this selected repository into a population sample.

## Controls are part of the verdict

At the primary cutoffs, co-read is below popularity by 33.16 points at P@1 and 20.40 points at P@10. It is below random by 12.58 and 17.44 points. Therefore co-read does not merely lose the head-to-head comparison; it beats neither control on either primary point estimate.

Co-change and fused have a mixed control result. Both beat popularity and random at P@10, but both lose them at P@1. Their recall is substantially above random and above popularity, but recall is conditioned by the unusually large mean ground-truth set. The correct reading is metric-specific, not “all learned models work”: co-change has useful breadth at ranks 1–10, fusion improves it, and neither establishes control dominance at every primary cutoff.

**Claim—co-read beats neither control on the primary point estimates: high confidence for this artifact.** The comparisons are direct and use identical queries. Confidence about a population-level control gap is lower because the commit bootstrap is wide where a few bulk commits dominate.

## Strict temporal construction

The replay keeps the stock protocol's first-parent order, logical identities, ground truth, controls, metrics, and current co-change scorer. Every query is made against one immutable pre-commit state; then the commit is folded exactly once. Additions are excluded from that commit's seeds and ground truth but enter later history. Renames retain identity, while delete-then-re-add receives a new identity.

The additional co-read rules are deliberately conservative:

- The commit cutoff is Git committer time (`%ct`), which is the timestamp stored by the unchanged replay extractor. Author time differs for 47 commits, by at most 2,587 seconds; it is retained as a diagnostic rather than substituted into the protocol.
- A read becomes available at `max(tool-use timestamp, successful-result timestamp)`. Thus both the call and its confirming result must precede the commit. This matters: result lag reaches 130.153 seconds.
- A read path is mapped only to the logical file live in the first-parent tree when its result becomes available. Future aliases and the final HEAD tree are never used. Of 7,937 primary usable events, 7,312 map and 625 remain unmapped; unmapped reads still advance inactivity boundaries but cannot create associations.
- Reads are grouped by effective agent and split only when the consecutive call-time gap is strictly greater than 300 seconds. Each mapped logical file counts once per task. A pair becomes available only after both files' first successful reads are available.
- At every eligible commit, `bisect_left` admits only pair incidences with availability strictly less than the cutoff. Code asserts both that the last admitted pair is `< cutoff` and the next omitted pair is `>= cutoff`. There were 556 prefix assertions and 5,929 query-level temporal assertions.
- Co-read ranks positive neighbors by prior task-pair count. For one fixed seed this is the same order as forward confidence, without introducing an outcome-selected decay or normalization. Co-change is the current time-decayed confidence baseline with a 150-commit half-life.
- Fusion uses standard RRF with `k=60` over each source arm's complete positive-support ranking, not just its top 20. Missing candidates contribute zero.
- Popularity and random call the unchanged replay implementations. Random is deterministic, uniform without replacement over the live pre-commit universe.

There are nine equal-committer-time groups containing 30 commits, so the strict `<` check is load-bearing. Committer timestamps are otherwise nondecreasing. All five arms produced exactly 5,929 queries; 29,645 predictions were asserted to contain only live pre-commit candidates. The replayed final tree equals HEAD.

**Claim—the implemented index does not consume a future read pair: high confidence.** Availability is computed from both Read records, path mapping is historical, and the boundary is asserted in code for every eligible commit and query. **Claim—the mapped historical tree is the exact tree the agent read: low confidence.** The compact transcript does not retain checkout SHA, and that limitation cannot be repaired by an assertion.

## Coverage and cold start

The fresh compact stream contains 8,005 accepted target-repository Reads. The primary run excludes 68 copied-prefix events whose fallback agent identity depends on transcript filesystem creation time that is absent from the content hash. The remaining 7,937 events form 1,521 inferred tasks; 947 tasks have at least two temporally mapped files and produce 30,147 pair incidences.

Although the global corpus window begins July 17, the first usable target Read is not available until `2026-08-03T06:18:02.301Z`, and the first co-read pair until `2026-08-03T08:07:56.058Z`. There are 83 eligible commits before the first target Read. They remain in the primary run because moving the boundary to the first observed target event would condition on co-read activity and favor that arm. Co-read returns a nonempty radius for only 30.83% of primary queries.

This cold start is part of the operational result, but not its sole cause. In the secondary run beginning at the first usable target Read, 1,026 commits remain, of which 473 are eligible and produce 2,895 queries. Mean ground-truth size falls to 53.13. Co-read still loses:

| Arm | P@1 | P@10 | Mean ground-truth size | R@10 | R@20 |
|---|---:|---:|---:|---:|---:|
| `cochange` | 0.2615 | 0.1299 | 53.13 | 0.1964 | 0.2298 |
| `coread` | 0.1907 | 0.1013 | 53.13 | 0.1408 | 0.1716 |
| `fused` | 0.3019 | 0.1697 | 53.13 | 0.2388 | 0.2943 |
| `popularity` | 0.1679 | 0.1319 | 53.13 | 0.1000 | 0.1365 |
| `random` | 0.0183 | 0.0215 | 53.13 | 0.0050 | 0.0094 |

The secondary co-read-minus-co-change P@1 interval is wholly negative, `[-0.1082, -0.0303]`; its P@10 interval is `[-0.0587, +0.0050]`. The point verdict is unchanged, while P@10 uncertainty is wider around the smaller loss.

Restoring all 68 copied-prefix events leaves co-read P@1 unchanged and raises P@10 by only 0.00017; fused P@1 rises by 0.00084. This sensitivity also leaves the verdict unchanged.

## Per-claim confidence

| Claim | Confidence | Reason |
|---|---|---|
| Co-read does not beat co-change on this replay | **High** | It loses both primary point estimates; both primary whole-commit intervals are below zero; the narrower-window and copied-prefix sensitivities do not reverse it. |
| Co-read is a better retrieval signal but not a better change-impact signal here | **High for the tested distinction** | The retrieval result comes from the prior study; this downstream replay directly supplies the previously missing change-impact comparison. Generalization remains untested. |
| RRF improves the co-change baseline on this artifact | **Medium-high** | It improves P@1, P@10, R@10, and R@20; paired whole-commit intervals for the two primary gains are positive. It still lacks P@1 control dominance and depends on the fixed `k=60` fusion rule. |
| Co-read beats neither mandatory control | **High for point estimates; medium for a broader inference** | Both primary point estimates are lower than both controls, but bulk commits make some commit-resampled control intervals wide. |
| The temporal cutoff is enforced correctly in the implementation | **High** | Result availability, historical identity mapping, strict boundary selection, query-before-fold order, and live-candidate checks are all asserted. |
| The measured effect generalizes beyond ToolsEnabled | **Low / not claimed** | This is one repository, one transcript population, one task proxy, and one co-read score. |

## Claims that could NOT be verified

- The compact transcript does not retain the checkout SHA or a reliable branch for each Read. Mapping a path to the first-parent tree live at result time is conservative, but it cannot prove that this was the tree the agent actually inspected.
- The 625 unmapped primary reads cannot be aligned to a logical file without guessing about uncommitted work, side branches, case collisions, or future paths.
- The 300-second inactivity windows are not true task IDs. They can merge adjacent tasks or split one long task.
- A Read shows what an agent inspected, not what was necessary, understood, or causally relevant.
- Same-commit co-occurrence is an operational change-impact target, not a defect oracle or proof of semantic blast radius.
- Git author and committer dates can be rewritten. The unchanged replay defines `%ct` as the cutoff, but no external clock proves when the work actually occurred.
- The externally supplied July 17 corpus start is day-level. Its exact intraday boundary is unavailable here; this does not affect inclusion because the target's first replayed commit is July 22.
- The result does not establish the best possible temporal co-read formula. Time decay, direction, PMI, line overlap, or a supervised combination were not outcome-selected and tested here.
- Deterministic raw-path tie breaking is reproducible but not uniquely correct. Ties are frequent, particularly in co-change.
- Whole-commit bootstrap intervals do not model serial dependence between commits and do not support population generalization from one repository.
- Query latencies are not portable across machines or implementations and exclude index construction.

## What would change this verdict

- A preregistered temporal co-read score that exceeds co-change on both P@1 and P@10, with paired whole-commit intervals excluding zero in its favor, would reverse the direct verdict.
- Exact task IDs plus checkout SHAs could recover currently unmapped reads and change task membership. A rerun on that stronger corpus could reverse or strengthen the result.
- A longer read history that predates the repository's evaluated commits could remove the 83-commit cold start. The first-target-read sensitivity shows that removing the observed cold start alone is not enough here.
- A preregistered alternative treatment of bulk/import/governance commits could change the query-weighted precision regime. The current largest commit supplies 25.81% of queries; excluding it after seeing the result would not be a clean test.
- Replication on independently selected repositories could show that ToolsEnabled is an outlier and that co-read wins in other development regimes.
- A predeclared tie-averaged evaluation or another deterministic support rule could move boundary-heavy rankings. A reversal on both primary metrics would narrow this conclusion.
- If an author-time, call-time-only, or exact-checkout replay materially reversed both primary metrics, the current committer/result-time verdict would become protocol-dependent rather than negative.

Until one of those falsifiers succeeds, use co-read for retrieval and keep time-decayed co-change as the change-impact foundation. RRF is worth further testing as an augmentation, but this run does not support replacing co-change with co-read.

## Provenance and reproducibility

Target: `C:\Users\joshp\Desktop\toolsenabled-current` at HEAD `1cf47ea2cfcf83f403822a08dab82955b48d1f8b`. The same five pre-existing dirty worktree paths were present before and after; models use the committed Git stream, not dirty contents.

Fresh transcript snapshot: 5,867 JSONLs, 3,944,152,465 bytes, content snapshot SHA-256 `8b68045bd20e2f79299663f095a5bd3e7691e1d50b833e0751a91808fe765aeb`. Compact read stream SHA-256: `23a0ee6c18c5a0bc35ca4574266084272c8df7a279e2a7b2f39f916a8ae6f103`. Git stream SHA-256: `9ef0e65958ce156f1e4806743b0a15bddfb111feb09fdb71e63d0287a030a5f3`. Predictor script SHA-256 recorded by the run: `f5067cd9419c9fade830630da95a8f6b5401811bfdbf5856c251e47fb5d44e5a`.

Machine-readable outputs:

- [Primary metrics](predictor-metrics.json)
- [First-target-read-window sensitivity](predictor-metrics-target-read-window.json)
- [Copied-prefix sensitivity](predictor-metrics-copied-prefix-sensitivity.json)
- [Read extraction metadata](predictor-artifacts/read-events.meta.json)

Implementation and tests:

- [`predictor.py`](../../instruments/unification/predictor.py)
- [`test_predictor.py`](../../instruments/unification/test_predictor.py)

The protected `extract_reads.py`, `analyze.py`, and every file under `instruments/replay/` were not modified. Thirty-six inherited and new replay/unification tests pass.
