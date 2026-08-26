# Hazard transfer, collision null, and lease rule

This report is generated from frozen local artifacts by [`compute_models.py`](../../instruments/models/compute_models.py). It performs no new mining, does not invoke Git, and uses no network or agent subjects. The hazard model choice was locked before the first fit in [`MODEL_SPEC.md`](../../instruments/models/MODEL_SPEC.md); that file also records two explicit input-definition corrections made during dry-run validation before the production output was accepted.

## 1. Conflict hazard and preregistered prediction

### Model choice stated before fitting

The chosen model is `logit(p) = alpha + beta * log(1 + combined_text_lines_changed); beta >= 0`. The outcome is the stored per-merge conflict indicator and exposure is the stored two-side combined countable changed-line total. The slope is constrained nonnegative. Uncertainty uses the CR1 repository-cluster sandwich; a 2,000-draw repository bootstrap with seed 20260825 is the small-cluster sensitivity.

The fit uses **238 / 23,428 = 1.016%** conflicted countable-text merges from 15 repositories. This reconciles to 23,428 / 25,073 evaluable merges. The excluded exposure-missing stratum is 178 / 1,645 = 10.821%; the fitted relationship is therefore coverage-conditioned.

Fitted parameters:

| Parameter | Estimate | Repo-cluster CR1 SE | Cluster-normal 95% | Repo-bootstrap 95% |
|---|---|---|---|---|
| alpha | -5.95486131 | 0.61404968 | [-7.158377, -4.751346] | [-7.303391, -4.877186] |
| beta | 0.23415068 | 0.06062561 | [0.115327, 0.352975] | [0.140039, 0.369806] |

Inputs/fit checks: log likelihood `-1282.433888`; CR1 correction `1.071474` over 15 repository clusters; bootstrap 2,000 successful / 2,000 requested fits. The monotonicity boundary was not active.

### Sanity check against MINING.md bins

The midpoint below is `(observed minimum + observed maximum)/2` within each fixed MINING.md bin, including the open top bin. Because the curve is nonlinear, the mean fitted probability over all rows in the bin is also shown; neither value is expected to equal a pooled observed rate exactly.

| MINING bin | Observed x range; midpoint | Observed | h(mid), cluster 95% | Mean h(x) in bin | Repos |
|---|---|---|---|---|---|
| 0 | 0-0; mid 0.0 | 0/3 (0.000%) | 0.259% [0.078%, 0.857%] | 0.259% | 3 |
| 1-15 | 1-15; mid 8.0 | 6/5,341 (0.112%) | 0.432% [0.153%, 1.217%] | 0.393% | 15 |
| 16-63 | 16-63; mid 39.5 | 17/3,770 (0.451%) | 0.613% [0.236%, 1.585%] | 0.588% | 15 |
| 64-255 | 64-255; mid 159.5 | 39/4,345 (0.898%) | 0.844% [0.342%, 2.067%] | 0.807% | 15 |
| 256-1,023 | 256-1,023; mid 639.5 | 73/4,307 (1.695%) | 1.164% [0.485%, 2.769%] | 1.105% | 15 |
| 1,024-4,095 | 1,024-4,095; mid 2,559.5 | 52/3,049 (1.705%) | 1.603% [0.665%, 3.811%] | 1.512% | 15 |
| 4,096+ | 4,101-3,889,340; mid 1,946,720.5 | 51/2,613 (1.952%) | 7.149% [2.135%, 21.365%] | 2.529% | 13 |

### (a) Exposure-only prediction for future unselected tasks

`W` is one transcript-corpus write's **aggregate claim lines per write**, not one hunk. For percentile scenarios both concurrent tasks are set to the named percentile. For the distributional estimand, tasks are independent empirical draws with replacement and the mean is taken *after* applying the nonlinear hazard.

| Prediction | Inputs | Point | Cluster/identification interval | Repo-bootstrap sensitivity |
|---|---|---|---|---|
| p50 pair | W1=W2=9 lines; x=18 lines | 0.514% | [0.190%, 1.383%] | [0.170%, 1.319%] |
| p90 pair | W1=W2=56 lines; x=112 lines | 0.778% | [0.312%, 1.928%] | [0.288%, 2.082%] |
| E[h(W1+W2)] | n=1,354; p50/p90/p99/max=9/56/200/452 lines | 0.620% provisional | summary bounds [0.481%, 0.707%] | provisional repo-bootstrap [0.216%, 1.629%] |

The provisional reconstruction has mean task size 30.173 lines over 1,354 reconstructed values. It is **not** the requested empirical distributional mean: `parameters.json` retains only count, p50, p90, p99, and maximum. Under the retained nearest-rank constraints, integer `W>=1`, and the fitted monotone curve, the sharp probability range is [0.481%, 0.707%]. The amendment should quote the provisional point only with this label, or use the range until a lossless histogram is exported.

This exposure-only quantity is the P6 prediction for the unmediated collision rate on **future, unselected concurrent tasks**. It is not a prediction for the deliberately conflict-selected arms sites.

### (b) Site-conditioned context for the 19 selected arms sites

| Gate | Repository | Merge | P1+P2=x lines | Exposure source | Fitted h(x), cluster 95% | Mined byte class |
|---|---|---|---|---|---|---|
| Python validated | pallets/click | `1697599708...` | 474+14=488 | stored | 1.093% [0.454%, 2.610%] | overlap |
| Python validated | pallets/click | `11abf2bff0...` | 530+978=1,508 | stored | 1.419% [0.592%, 3.363%] | boundary_only |
| Python validated | pallets/click | `22697863f0...` | 534+809=1,343 | stored | 1.381% [0.576%, 3.274%] | overlap |
| Python validated | pallets/click | `8b971f7374...` | 540+162=702 | stored | 1.189% [0.495%, 2.827%] | overlap |
| Python validated | pallets/click | `655918a61e...` | 168+136=304 | side sum; binary present | 0.980% [0.404%, 2.359%] | overlap |
| Python validated | pallets/click | `65eceb08e3...` | 103+154=257 | side sum; binary present | 0.943% [0.387%, 2.278%] | overlap |
| Python validated | pallets/click | `7271763ea3...` | 235+219=454 | stored | 1.075% [0.446%, 2.570%] | unclassifiable |
| Python validated | pallets/click | `d9af5cfa00...` | 871+974=1,845 | stored | 1.486% [0.619%, 3.526%] | overlap |
| Python validated | pallets/click | `3a40e43e8f...` | 90+177=267 | stored | 0.951% [0.391%, 2.296%] | overlap |
| Python validated | pallets/click | `240603f240...` | 326+238=564 | stored | 1.131% [0.470%, 2.694%] | boundary_only |
| Python validated | pygments/pygments | `00a31bcae2...` | 33+35=68 | stored | 0.694% [0.273%, 1.752%] | overlap |
| Go runner-eligible | gohugoio/hugo | `3583dd6d71...` | 1,598+556=2,154 | stored | 1.540% [0.641%, 3.657%] | overlap |
| Go runner-eligible | gohugoio/hugo | `604ddb90c5...` | 1,099+41=1,140 | stored | 1.330% [0.555%, 3.153%] | overlap |
| Java gate passed | apache/commons-lang | `640953167a...` | 1,991+14=2,005 | stored | 1.515% [0.631%, 3.596%] | overlap |
| Java gate passed | apache/commons-lang | `7fae5b0b17...` | 24+60=84 | stored | 0.729% [0.289%, 1.823%] | overlap |
| Java gate passed | apache/commons-lang | `80644cdab9...` | 114+181=295 | stored | 0.973% [0.401%, 2.345%] | overlap |
| Java gate passed | apache/commons-lang | `481137553f...` | 152+115=267 | stored | 0.951% [0.391%, 2.296%] | overlap |
| Java gate passed | apache/commons-lang | `42f2058c83...` | 2,708+21=2,729 | stored | 1.627% [0.675%, 3.870%] | overlap |
| Java gate passed | apache/commons-lang | `6681a34d25...` | 89+168=257 | stored | 0.943% [0.387%, 2.278%] | overlap |

Across the 19 sites, mean fitted exposure hazard = **1.155%** with repository-bootstrap 95% [0.467%, 3.202%]. The historical realized rate is **19/19 = 100.000%**, or 86.56x the mean fitted exposure hazard. That gap measures how strongly selecting known historical conflicts concentrates collision beyond changed-line exposure; it is not a failed forecast, because these outcomes selected the sites.

Exposure provenance: 17/19 sites use the stored combined countable-text field. For 2/19 sites the miner nulls that field because a side also changed a binary file; both stored side-specific text totals remain available, so the table uses their labeled sum. Those hazards are text-component extrapolations outside the fit's eligibility rule.

Accordingly, the arms' realized collision rate is preregistered to land **above** the exposure-only agent-size prediction. For the selected sites the claim is only that ordering. The numerical ladder prediction remains section 1(a), for the unmediated arm on future unselected work. No site-specific future collision probability is claimed.

Hazard caveats: repository clustering does not repair purposive repository selection; only 15 clusters support small-cluster inference; conflict is Git/textual rather than semantic; changed lines are historical branch diffs while agent claims are transcript patch-write regions; and the 1,645 exposure-unavailable rows have a much higher conflict rate.

## 2. Uniform contiguous-span collision null

### Exact null and supported inputs

For measured positive widths `w1,w2` in a base blob of `N` bytes, each contiguous half-open span start is independently uniform over every integer position where it fits. With `D=N-w1-w2`, the exact disjoint probability is `(D+1)(D+2)/((N-w1+1)(N-w2+1))` when `D>=0`, else zero. Each side's multiple refined hunks and insertion anchors supply a unioned changed-byte mass `w`; the null places one contiguous span of that size. This is the committed **contiguous-span** null. A bounding-hull sensitivity is retained separately and counts unchanged gaps. The supplied `exp(-w1*w2/N)` formula instead describes a scattered-byte reference and is reported without fitting, not used as the null.

The paired null has **797** handwritten, strict-classifiable conflict-path pairs across 12 repositories; each has two parent-side base-coordinate span sets and a base blob `N`. Separately, the requested handwritten marker-span distribution has **845** measurable result-blob paths: median/p90 span = 485/2,705 bytes, median/p90 file size = 5,640/44,785 bytes, and median/p90 span fraction = 10.674%/55.945%. Marker spans do not contain two side-specific widths, so they are described but not synthetically paired.

### (a) Whole-file over-block prediction versus Click

Under the fixed contiguous null, expected exact nonoverlap is **691.715 / 797 = 86.790%**. Whole-file claiming would serialize every such pair, so this is its null over-block rate.

The measured anchored Click census is **475 / 684 = 69.444%** textually nonconflicting same-file units (the published 69.4%). Measured minus null = -17.345 percentage points. The direction is consistent with edits clustering more than uniform placement, but the magnitude is contextual: the width sample is conflict-selected and pooled, while 475/684 is Click's full anchored textual census.

Coordinate-size sensitivity: substituting result-blob size for `N` where both changed-byte masses still fit gives 93.351% over 792/797 pairs. Base-blob `N` remains primary because the retained spans are in base coordinates.

### (b) Claim-granularity curve

A `g`-byte empirical claim expands every retained interval/effective insertion byte outward to the fixed, file-origin-aligned `g`-byte blocks it touches. Over-blocking means the miner's exact span sets are strict-disjoint but padded claim unions overlap. Thus exact-span over-block is zero and whole-file over-block equals exact nonoverlap. Null values are exact counts over all uniform integer-start pairs, not Monte Carlo estimates.

| Claim granularity | Pooled handwritten null | Click empirical spans | Same Click-subset null |
|---|---|---|---|
| exact_span | 0.000/797 (0.000%) | 0/138 (0.000%) | 0.000/138 (0.000%) |
| 64B | 37.705/797 (4.731%) | 31/138 (22.464%) | 3.715/138 (2.692%) |
| 256B | 92.751/797 (11.637%) | 34/138 (24.638%) | 13.454/138 (9.749%) |
| 1KB | 237.502/797 (29.800%) | 34/138 (24.638%) | 36.135/138 (26.184%) |
| 4KB | 443.219/797 (55.611%) | 34/138 (24.638%) | 78.716/138 (57.040%) |
| whole_file | 691.715/797 (86.790%) | 34/138 (24.638%) | 122.638/138 (88.868%) |

The empirical Click curve denominator is **138** strict-classifiable paths from conflicted mined merges. It is a real base-coordinate curve, but it is not the 684-unit full census: clean-merge census rows retain changed paths and textual labels, not their base-coordinate spans. That full-census curve therefore cannot be computed without new mining.

### (c) Clustering ratios

| Statistic | Observed overlap | Null expected overlap | Observed/null |
|---|---|---|---|
| Full Click textual proxy | 209/684 (30.556%) | 105.285/797 (13.210%) | 2.313x |
| Click span-supported mined-strict | 104/138 (75.362%) | 15.362/138 (11.132%) | 6.770x |
| Click bounding-hull sensitivity | 125/138 (90.580%) | 68.155/138 (49.388%) | 1.834x |

The same-cohort mined-strict ratio is the internally comparable clustering statistic. The full Click ratio answers the requested paper comparison but mixes a full textual census numerator with a conflict-selected span distribution; it should be described as contextual. The bounding-hull sensitivity deliberately counts unchanged gaps as edit width and overlap; the committed null instead places the measured changed-byte mass as one contiguous span.

For reference only, averaging the supplied scattered birthday expression over the primary pairs gives disjoint probability 67.451%; it is not substituted for the contiguous result. The null was not tuned to 69.4%. Its failure--and especially the same-cohort observed/null excess--is the clustering finding.

## 3. Quantile lease rule

### Objective, inputs, and default costs

The fixed rule is `P(T>L)*C_reacquire + E[min(L,D)]*C_block`. `T` is the 405-pair first-read-result to absolute-first-write interval: p50/p90/p99/max = 25.755/3519.388/133320.741/278233.521 seconds. `D` is the 3,094-claim last-write-result to observed-end interval: p50/p90/p99/max = 83035.187/259336.939/511917.379/1329345.269 seconds. `E[min(L,D)]` assumes an explicit release at observed end and otherwise expiry `L` after last use.

Default `C_block` = 1.000 agent-minute per waiting minute. No launch-overhead measurement exists, so default `C_reacquire` = 3.3275 agent-minutes, the measured p50 structured active span 199.652 seconds divided by 60. This is a workload-cost proxy, not a startup benchmark.

On the declared quantile reconstruction, **L* = 0.429250 minutes (25.755 seconds)**. At L*, false expiry = 49.630%, expected dangling = 0.429111 minutes, and objective = 2.080554 agent-minutes per claim.

This is **provisional, not an exact empirical optimum**. `parameters.json` contains only the nearest-rank summaries, so the script reconstructs piecewise-linear quantile functions through minimum zero, p50, p90, p99, and maximum. The four summary-consistent low/high scenarios put L* between 0.429250 and 0.429250 minutes under the default costs; that scenario range is not claimed to be a sharp identification set.

### Default objective curve

| L (min) | FalseExpiry | E dangling (min) | Reacquire term | Blocking term | Objective |
|---|---|---|---|---|---|
| 0.000000 | 99.753% | 0.000000 | 3.319317 | 0.000000 | 3.319317 |
| 0.166667 | 80.494% | 0.166613 | 2.678459 | 0.166613 | 2.845072 |
| 0.250000 | 70.864% | 0.249919 | 2.358030 | 0.249919 | 2.607949 |
| 0.429250 | 49.630% | 0.429111 | 1.651442 | 0.429111 | 2.080554 |
| 0.500000 | 49.630% | 0.499838 | 1.651442 | 0.499838 | 2.151281 |
| 1.000000 | 49.383% | 0.999643 | 1.643226 | 0.999643 | 2.642869 |
| 2.000000 | 48.642% | 1.998929 | 1.618578 | 1.998929 | 3.617507 |
| 5.000000 | 46.667% | 4.994644 | 1.552849 | 4.994644 | 6.547493 |
| 10.000000 | 43.210% | 9.980310 | 1.437823 | 9.980310 | 11.418134 |
| 15.000000 | 39.753% | 14.956930 | 1.322797 | 14.956930 | 16.279727 |
| 30.000000 | 29.630% | 29.832639 | 0.985936 | 29.832639 | 30.818575 |
| 60.000000 | 9.630% | 59.340394 | 0.320429 | 59.340394 | 59.660823 |
| 120.000000 | 9.630% | 117.380975 | 0.320429 | 117.380975 | 117.701404 |
| 240.000000 | 9.136% | 229.562700 | 0.303997 | 229.562700 | 229.866697 |
| 480.000000 | 8.148% | 438.328401 | 0.271132 | 438.328401 | 438.599533 |
| 720.000000 | 7.160% | 626.297102 | 0.238268 | 626.297102 | 626.535370 |
| 1440.000000 | 4.198% | 1065.756831 | 0.139674 | 1065.756831 | 1065.896506 |
| 2880.000000 | 0.741% | 1633.464813 | 0.024648 | 1633.464813 | 1633.489461 |
| 4320.000000 | 0.247% | 1919.029984 | 0.008216 | 1919.029984 | 1919.038200 |
| 10080.000000 | 0.000% | 2163.798928 | 0.000000 | 2163.798928 | 2163.798928 |

### Cost-ratio sensitivity (1:10 to 10:1)

| Numeric C_reacquire:C_block | L* (min) | FalseExpiry | E dangling (min) | Objective |
|---|---|---|---|---|
| 0.1000:1 | 0.000000 | 99.753% | 0.000000 | 0.099753 |
| 0.1995:1 | 0.000000 | 99.753% | 0.000000 | 0.199034 |
| 0.5012:1 | 0.000000 | 99.753% | 0.000000 | 0.499950 |
| 1.0000:1 | 0.429250 | 49.630% | 0.429111 | 0.925408 |
| 1.9953:1 | 0.429250 | 49.630% | 0.429111 | 1.419353 |
| 3.3275:1 (default) | 0.429250 | 49.630% | 0.429111 | 2.080554 |
| 5.0119:1 | 0.429250 | 49.630% | 0.429111 | 2.916485 |
| 10.0000:1 | 0.429250 | 49.630% | 0.429111 | 5.392074 |

The machine-readable file contains the full 41-point log-spaced sensitivity plus the measured default. Numeric ratios compare agent-minutes per false expiry with agent-minutes per dangling minute, so the ratio has an implicit minute scale; it is a design tradeoff, not a universal constant.

Lease caveats: read does not establish causal need for the later write; session end is not an explicit close and the linger tail is right-censored; no launch-overhead benchmark was retained; ties inside percentile summaries are unknown; and online renewal/heartbeat behavior is outside this static objective.

## Claims that could NOT be verified

- The exact empirical `E[h(W1+W2)]`: the 1,354 task sizes or a lossless histogram are absent from `parameters.json`.
- The exact empirical lease curve or L*: the 405 read-to-write and 3,094 linger observations are absent; only five order-statistic constraints per distribution remain.
- Actual agent relaunch overhead: p50 structured active span is only a workload-cost proxy.
- An uncensored last-use-to-close distribution: observed session end is not a close event.
- A 64B-to-4KB empirical curve on all 684 Click same-file census units: clean-census rows do not retain base-coordinate spans.
- A common-population estimate that directly compares the pooled conflict-selected span null with the complete Click census.
- Side-specific widths from the 845 handwritten marker spans: marker regions measure disputed result text, not the two parent edits.
- Semantic conflict probability, causal effects of exposure, or generalization beyond these selected histories and one transcript workload.

## What would change this verdict

- Exporting a lossless histogram (or the 1,354 values) for aggregate claim lines would replace the distributional hazard range/reconstruction with the exact push-through mean.
- Exporting the 405 and 3,094 interval values plus explicit close/crash/heartbeat events would identify the lease objective and remove the quantile reconstruction.
- Measuring agent startup/relaunch latency directly would replace the structured-active-span cost proxy and could move L* materially.
- Retaining base-coordinate interval sets and blob sizes for every Click same-file census unit would produce the requested full empirical granularity curve and a common-cohort null ratio.
- More independently sampled repositories or a future unselected-agent task cohort could materially move the hazard curve and its transfer calibration.
- A different declared null--scattered hunks, nonuniform hot regions, syntax-aware placement, or file-type conditioning--would change the reference. It must be preregistered, not tuned to 69.4%.
- If future selected-site outcomes do not exceed the exposure-only agent-size prediction, the preregistered ordering claim would fail.

## Per-claim confidence

| Claim | Confidence | Reason |
|---|---|---|
| 23,428-row logistic hazard parameters and MINING-bin reconciliation | High for this frozen corpus | Every evaluable JSONL row is streamed; counts are asserted against 238/23,428 and all seven published bins; model and seed were locked before fit. |
| Repository-cluster uncertainty | Moderate | CR1 and repository bootstrap agree/disagree visibly, but only 15 clusters exist and repositories were purposively selected. |
| p50 and p90 exposure-only predictions | Moderate | Inputs 9 and 56 lines are retained exactly and the curve is reproducible; cross-population hazard transfer remains an untested hypothesis. |
| Distributional exposure-only point | Low as a point; high for stated bounds | The point uses an explicit quantile reconstruction. Bounds follow the retained nearest-rank constraints and monotonicity, but depend on minimum-one-line support. |
| Nineteen site exposures, hazards, and 100% historical realization | High descriptively | All 19 full identities join uniquely and all rows are conflicted; 17 use the stored combined field and 2 use a labeled side-text sum because binary changes null the fitted-population field; selection prevents predictive interpretation. |
| Contiguous null formulas and granularity probabilities | High mathematically; moderate for input representativeness | Closed-form and aligned-block counts are exhaustively unit-tested on small files; measured widths come only from classifiable conflict-selected paths. |
| Click conflict-selected empirical granularity curve | High for its retained denominator | Actual base-coordinate interval/anchor sets are used with a deterministic block rule; it excludes census units without retained spans. |
| Clustering magnitude against the full Click census | Low-to-moderate | Direction is informative, but the observed textual census and null width distribution are different cohorts; the same-span-subset ratio is stronger internally. |
| Lease L* | Low-to-moderate provisional | Objective and costs are explicit, but both distributions are reconstructed, linger is censored, and relaunch cost is proxied. |

## Reproduction

Run `python instruments/models/compute_models.py --root . --bootstrap 2000`, `python -m unittest instruments.models.test_model_lib -v`, and `python instruments/models/verify_models.py`. The JSON records SHA-256, byte size, and row counts for the fitted merge inputs, plus hashes for the other load-bearing artifacts.
