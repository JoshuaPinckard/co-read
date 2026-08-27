# Conflict-corpus re-analysis for external review

## Scope and headline verdict

This is a re-analysis of the frozen local artifacts only. It reads the per-repository conflict JSONL rows, `corpus/conflicts/_all_merges/*.jsonl`, `exploratory/conflicts/MINING.md`, and the four arms files named in the request. It does not invoke Git, inspect corpus mirrors, fetch repositories, mine new rows, or run agent subjects.

The three answers are:

1. The 19-site arms population contains 16 byte-intersecting merges, 0 same-file-disjoint merges, 2 boundary-only merges, and 1 unclassifiable merge. The same-file-disjoint stratum is therefore plainly single digits: it is exactly zero.
2. The complete-case strict intersection rate is 304 / 372 decidable conflicted merges (81.720%). The 44 / 416 censored conflicted merges (10.577%) are not plausibly missing completely at random, and the direction and magnitude of any resulting complete-case bias are unobservable. The honest full-population bounds are 304 / 416 (73.077%) if all 44 are nonintersecting and 348 / 416 (83.654%) if all 44 are intersecting.
3. Requiring both base-to-parent diffs to be nonempty raises the pooled conflict rate from 416 / 25,073 evaluable merges (1.659%) to 416 / 17,749 conditioned evaluable merges (2.344%). The stricter common-file rate cannot be recovered from `_all_merges` because those rows do not retain the complete changed-path set for either side.

All percentages below put their numerator and denominator in the same sentence or table cell.

## 1. Site stratification

### Population reconciliation

The 19 requested identities reconcile as 11 Python rows with `verdict=VALIDATED` in `sites.json`, 2 Go rows with `verdict=ELIGIBLE` and `runner_eligible=true` in `sites-go.json`, and 6 Java rows with `verdict=passed` in `sites-java.json`. Every one of the 19 repository-plus-full-merge identities matched exactly one mined conflict row.

“Validated” is not uniform source terminology. In particular, both selected Go rows have `validated=false` and describe runner eligibility only. This re-analysis uses the 19-site population implied by the three manifest-specific passing predicates; it does not silently upgrade the Go rows to Python-style source/test red-green validation.

The composition is:

| Mined changed-byte class | Sites / eligible population |
|---|---:|
| `overlap` (strict byte-intersecting) | 16 / 19 |
| `same_file_disjoint` | 0 / 19 |
| `boundary_only` | 2 / 19 |
| `unclassifiable` | 1 / 19 |

Among the 18 / 19 sites with decidable strict status, 16 / 18 are byte-intersecting (88.889%). The remaining site is not put in either strict stratum.

### Per-site join

| Gate | Repository | Merge | Mined class | Contradictory-task status in `SITES.md` |
|---|---|---|---|---|
| Python validated | `pallets/click` | `16975997084a21a37dea351b6967b63591fd991f` | `overlap` | Textually nonconstructible |
| Python validated | `pallets/click` | `11abf2bff0f48b7f7b04b38b6a70fb102ef17662` | `boundary_only` | Textually nonconstructible |
| Python validated | `pallets/click` | `22697863f0a252082226068290eff3d57f79a3ec` | `overlap` | Textually nonconstructible |
| Python validated | `pallets/click` | `8b971f73743aa3bec1f09414377d3d01369de1bd` | `overlap` | Textually nonconstructible |
| Python validated | `pallets/click` | `655918a61e22cade722dacb9bf798e86b13093af` | `overlap` | Textually nonconstructible |
| Python validated | `pallets/click` | `65eceb08e392e74dcc761be2090e951274ccbe36` | `overlap` | Textually nonconstructible |
| Python validated | `pallets/click` | `7271763ea3b33d6b7e089501173fe3234fb1a594` | **`unclassifiable`** | Textually nonconstructible |
| Python validated | `pallets/click` | `d9af5cfa009c927a96d10ed38a3e37979876a12e` | `overlap` | Textually nonconstructible |
| Python validated | `pallets/click` | `3a40e43e8f61761cb7bd4f3e27ae3060690a84eb` | `overlap` | Jointly satisfiable |
| Python validated | `pallets/click` | `240603f240a9ff179d834fede836060d897c6980` | `boundary_only` | Textually nonconstructible |
| Python validated | `pygments/pygments` | `00a31bcae2f61ce74ccfabd05be2731bfc7a5a28` | `overlap` | **Mutually unsatisfiable** |
| Go runner-eligible | `gohugoio/hugo` | `3583dd6d713c243808b5e8724b32565ceaf66104` | `overlap` | Not assessed there |
| Go runner-eligible | `gohugoio/hugo` | `604ddb90c5d6f1ca5583be1ec0ea8e48f014741a` | `overlap` | Not assessed there |
| Java gate passed | `apache/commons-lang` | `640953167adf3580a2c21077d78e7e7ce84ead03` | `overlap` | Not assessed there |
| Java gate passed | `apache/commons-lang` | `7fae5b0b17dbfa46236243cc53f0ea853ed89f5c` | `overlap` | Not assessed there |
| Java gate passed | `apache/commons-lang` | `80644cdab9a77db0a52d09d9ebb1f406912997d1` | `overlap` | Not assessed there |
| Java gate passed | `apache/commons-lang` | `481137553f878e2f69ce05129d4aecbf016a1756` | `overlap` | Not assessed there |
| Java gate passed | `apache/commons-lang` | `42f2058c83f256d8654b349d5249d6f59920f88b` | `overlap` | Not assessed there |
| Java gate passed | `apache/commons-lang` | `6681a34d25b63a47c2d619ae0466825304ea9b1f` | `overlap` | Not assessed there |

The two boundary-only rows are Click `11abf2...` (`CHANGES.rst`) and Click `240603...` (`pyproject.toml` and `uv.lock`). Click `727176...` is unclassifiable because its `setup.cfg` overlap path is `unclassifiable_missing_or_nonblob_stage`. No strict status is inferred for it.

### Mutually-unsatisfiable-test potential

`SITES.md` classifies 1 / 2 constructible validated Python sites as operationally mutually unsatisfiable (50.000%): Pygments `00a31b...`. This supplies one positive identity within the requested 19-site gate population; 19 is the population count, not the joint-test denominator. It is not evidence that the other 18 sites lack such potential.

Coverage within the requested population is one mutually unsatisfiable positive, one jointly satisfiable site, nine textually nonconstructible sites under the prescribed Python construction, and eight Go/Java sites not assessed by that Python-only contradictory-task section.

**Confidence: high for the 19 hash joins and mined classes.** The joins are exact and unique, and the class comes directly from each conflict row. Confidence is high but protocol-limited for the single mutually-unsatisfiable positive. It is unsupported for the nine textually nonconstructible or eight unassessed sites.

## 2. Censoring versus disjointness

### Complete-case claim and honest bounds

Strict-decidable classes contain 304 `overlap`, 46 `same_file_disjoint`, and 22 `boundary_only` merges, for 372 / 416 conflicted merges (89.423%). The censored population contains 41 `unclassifiable` and 3 `boundary_with_unclassifiable` merges, for 44 / 416 conflicted merges (10.577%).

The published complete-case rate is therefore 304 / 372 strict-decidable conflicted merges (81.720%). It does not identify the 44 withheld outcomes.

| Assumption about all 44 censored merges | Intersecting / all conflicted merges |
|---|---:|
| Worst case: every censored merge is nonintersecting | 304 / 416 (73.077%) |
| Best case: every censored merge is intersecting | 348 / 416 (83.654%) |

These are identification bounds, not confidence intervals.

### Where the 44 censored merges occur

| Repository | Censored / repository's conflicted merges | Decidable conflicted merges |
|---|---:|---:|
| `gohugoio/hugo` | 31 / 124 (25.000%) | 93 |
| `ansible/ansible` | 5 / 83 (6.024%) | 78 |
| `pallets/itsdangerous` | 3 / 13 (23.077%) | 10 |
| `pallets/click` | 2 / 73 (2.740%) | 71 |
| `prometheus/prometheus` | 2 / 25 (8.000%) | 23 |
| `redis/redis` | 1 / 24 (4.167%) | 23 |

The other conflicted repositories have zero censored rows: Commons Lang has 0 / 25, Hutool 0 / 14, Terraform 0 / 17, Notebook 0 / 2, Requests 0 / 13, and Pygments 0 / 3. The four repositories with no conflicted merges have undefined censoring rates.

Hugo supplies 31 / 44 censored merges (70.455%). This clustering matters: pooled censored-versus-decidable contrasts are not independent observations and should not be interpreted as a repository-controlled effect.

### Actual overlap-censoring reasons

The overlap censor is `overlap.paths[].status`. It must not be conflated with `conflicts[].range_status`, which describes availability of marker-backed regions in the merge result.

At merge level, 42 / 44 censored merges (95.455%) lack one or more of the three stage entries needed for a base-coordinate comparison, 1 / 44 (2.273%) has all three stages recorded as gitlinks (`mode=160000`), and 1 / 44 (2.273%) exceeds the bounded refinement limit. No censored merge has an `unclassifiable_binary` or `unclassifiable_binary_by_git` overlap status.

At path level, the 44 merges contain 1,188 conflict paths. Four paths are classifiable in the three `boundary_with_unclassifiable` merges: three are boundary contacts and one is classifiable-disjoint. Of the remaining 1,184 unknown paths, 1,182 / 1,184 (99.831%) have absent stage entries, 1 / 1,184 (0.084%) is the all-gitlink path, and 1 / 1,184 (0.084%) hits the refinement limit.

The 1,182 absent-stage paths break down as follows:

| Recorded stage pattern | Unknown paths |
|---|---:|
| No stage 1, 2, or 3 entry | 499 |
| Only stage 1 present | 81 |
| Only stage 2 present | 81 |
| Only stage 3 present | 121 |
| Only stage 1 missing | 10 |
| Only stage 2 missing | 18 |
| Only stage 3 missing | 372 |

“Absent stage” is not evidence that repository objects failed to hydrate. `MINING.md` reports zero required objects missing in the final audit. It means the conflict did not supply the full base/P1/P2 blob-stage triple required by this coordinate definition. Likewise, `unavailable_no_result_blob` below means that the conflict result path has no ordinary result blob; it is not an object-fetch failure.

### Range-status co-occurrences

These mutually exclusive path-occurrence statuses describe marker-range availability. The merge-level columns overlap because one merge can contain paths with several statuses.

| Result range status | Path occurrences in the 44 censored merges | Path occurrences in the 372 strict-decidable merges | Censored merges containing status | Strict-decidable merges containing status |
|---|---:|---:|---:|---:|
| `measured_text_markers` | 10 / 1,188 (0.842%) | 889 / 8,472 (10.493%) | 7 / 44 (15.909%) | 372 / 372 (100.000%) |
| `unavailable_binary_result` | 296 / 1,188 (24.916%) | 795 / 8,472 (9.384%) | 10 / 44 (22.727%) | 37 / 372 (9.946%) |
| `unavailable_no_result_blob` | 581 / 1,188 (48.906%) | 3,546 / 8,472 (41.856%) | 34 / 44 (77.273%) | 75 / 372 (20.161%) |
| `unavailable_no_text_markers` | 301 / 1,188 (25.337%) | 3,242 / 8,472 (38.267%) | 37 / 44 (84.091%) | 87 / 372 (23.387%) |

Binary-result paths are more prevalent descriptively among censored rows, but binary result is never the recorded strict-overlap censor in this corpus. Of the 1,184 unknown paths, 296 co-occur with a binary-result range status, 581 with no result blob, 301 with no text markers, and 6 with measured markers; their overlap-censor reasons remain absent/nonblob stages or the refinement limit.

The exact merge-level presence sets are:

| Range-status presence set | Censored merges | Strict-decidable merges |
|---|---:|---:|
| Measured only | 3 | 280 |
| No result blob + no text markers | 19 | 0 |
| No text markers only | 7 | 0 |
| Binary result + no result blob + no text markers | 7 | 0 |
| Measured + no result blob + no text markers | 4 | 33 |
| Binary result + no result blob | 3 | 0 |
| No result blob only | 1 | 0 |
| Measured + binary result + no result blob + no text markers | 0 | 37 |
| Measured + no text markers | 0 | 17 |
| Measured + no result blob | 0 | 5 |
| **Total** | **44** | **372** |

### Observable file-count and artifact mix

The stored side counts are base-to-parent changed-file counts. Their sum is not a distinct-file union because `_all_merges` does not retain the complete path sets.

| Observable | 44 censored merges | 372 decidable merges |
|---|---:|---:|
| Conflict-file occurrences | 1,188 total; median 2; mean 27.000; range 1-514 | 8,472 total; median 1; mean 22.774; range 1-1,348 |
| Parent 1 changed files | median 2,339; range 1-3,865 | median 64.5; range 1-3,876 |
| Parent 2 changed files | median 22.5; range 1-564 | median 6; range 1-1,242 |
| Smaller-side changed files | median 20 | median 4 |
| Larger-side changed files | median 2,339 | median 74 |
| Sum of the two side counts | median 2,381; mean 2,069.591; range 3-4,429 | median 79.5; mean 818.602; range 2-4,762 |

The pooled side-count contrast is strongly confounded by repository composition and cannot identify a within-repository size effect. Within Hugo, censored versus decidable medians are 4 versus 15 conflict paths, 31 versus 50 smaller-side files, 2,901 versus 2,938 larger-side files, and 2,936 versus 2,995 summed side counts.

Using `MINING.md`'s operational classifier, artifacts are generated, lockfile, or vendored files; “handwritten” is the residual and is not human-authorship ground truth.

| Origin mix | 44 censored merges | 372 decidable merges |
|---|---:|---:|
| Handwritten-residual occurrences | 1,103 / 1,188 (92.845%) | 7,054 / 8,472 (83.263%) |
| Artifact occurrences | 85 / 1,188 (7.155%) | 1,418 / 8,472 (16.737%) |
| Generated / vendored / lockfile occurrences | 53 / 32 / 0 | 681 / 692 / 45 |
| Handwritten-only merges | 38 / 44 (86.364%) | 313 / 372 (84.140%) |
| Mixed handwritten/artifact merges | 4 / 44 (9.091%) | 56 / 372 (15.054%) |
| Artifact-only merges | 2 / 44 (4.545%) | 3 / 372 (0.806%) |
| Merges containing any artifact | 6 / 44 (13.636%) | 59 / 372 (15.860%) |

The observed merge-level any-artifact share is slightly lower among censored merges, 6 / 44 (13.636%), than among strict-decidable merges, 59 / 372 (15.860%); these clustered counts do not establish equality or absence of association. Occurrence shares are likewise highly clustered and should not be treated as independent file samples.

### Is censoring related to disjointness?

Its correlation with **true** disjointness cannot be computed: true status is the missing value. The observable censoring mechanism is nevertheless not plausibly missing completely at random.

First, absent-stage censoring arises from structural conflict forms in which the classifier's required ordinary-blob stage-1/stage-2/stage-3 triple is unavailable. Second, refinement censoring is explicitly a function of edit size. Third, the merge rule is asymmetric: one proven intersecting path makes an `overlap` merge decidable even if other paths are unknown, whereas a strict-negative classification requires the relevant paths to be classifiable. Indeed, 103 / 372 decidable merges (27.688%) contain at least one unknown path, and all 103 are among the 304 proven-overlap merges; 103 / 304 proven-overlap merges (33.882%) are positive despite other unknown paths.

That asymmetry makes upward complete-case bias plausible, but it does not prove it or supply a correction. The data support only the 304 / 416 (73.077%) to 348 / 416 (83.654%) identification bounds above.

**Confidence: high for counts, joins, status distributions, and bounds; medium for the substantive censoring assessment.** The arithmetic is direct from frozen rows. The assessment that missing-at-random is implausible follows from classifier asymmetry and observable clustering, but repo/history confounding and the unobserved outcomes prevent estimating either direction or magnitude.

## 3. Conditional base rate

### Both sides have a nonempty base-to-parent diff

Eligibility here means `evaluation_status` is `clean` or `conflicted` and both `diffs.parent1.files > 0` and `diffs.parent2.files > 0`. The 10 no-merge-base rows are excluded, not counted as clean.

This conditioning retains 17,749 / 25,073 evaluable merges (70.789%) and all 416 / 416 conflicted merges (100.000%). It excludes 7,324 / 25,073 evaluable merges (29.211%): 7,311 have only parent 1's diff empty, 11 have only parent 2's diff empty, and 2 have both diffs empty.

| Repository | Conflicted / evaluable merges with both side diffs nonempty |
|---|---:|
| `BurntSushi/ripgrep` | 0 / 23 (0.000%) |
| `ansible/ansible` | 83 / 3,803 (2.182%) |
| `apache/commons-lang` | 25 / 178 (14.045%) |
| `chinabugotech/hutool` | 14 / 620 (2.258%) |
| `gohugoio/hugo` | 124 / 202 (61.386%) |
| `hashicorp/terraform` | 17 / 4,784 (0.355%) |
| `hashicorp/terraform-provider-random` | 0 / 36 (0.000%) |
| `jupyter/notebook` | 2 / 1,932 (0.104%) |
| `pallets/click` | 73 / 379 (19.261%) |
| `pallets/itsdangerous` | 13 / 73 (17.808%) |
| `prometheus/prometheus` | 25 / 2,694 (0.928%) |
| `psf/requests` | 13 / 443 (2.935%) |
| `pygments/pygments` | 3 / 148 (2.027%) |
| `redis/redis` | 24 / 889 (2.700%) |
| `superfluid-org/protocol-monorepo` | 0 / 0 (undefined) |
| `ydb-platform/ydb` | 0 / 1,545 (0.000%) |
| **Pooled** | **416 / 17,749 (2.344%)** |

The local rows also reproduce the unconditioned baseline as 416 / 25,073 evaluable two-parent merges (1.659%) and the topologically trivial dilution point as 0 / 5,356 evaluable merges with one commit of combined divergence (0.000%). Requiring two nonempty sides removes that one-commit population, but the pooled result remains below the reviewer's high-single-digit-to-teen comparison. Repository rates vary widely, so the pooled selected-corpus rate is not a literature-wide population estimate.

### Both sides touch at least one common file

**This rate is not computable from the allowed local inputs.** Each `_all_merges` side records an aggregate `files` count and partial path lists for `test_files` and `binary_paths`. It does not record the complete base-to-parent changed-path list, a precomputed intersection, or even enough information to recover the clean-row denominator. `conflicted_paths` cannot stand in for the missing changed-path sets.

No numerator, denominator, or rate is reported for this condition. Obtaining one would require complete frozen per-side path lists or new repository diff mining, which the request forbids.

### Relationship to prior literature

The cited papers and their method sections are not present in the allowed local inputs. The following is a provisional comparability map from general knowledge, not a verified description of any paper's denominator:

| Study | Closest local conditioning, provisionally | Verification status |
|---|---|---|
| Ghiotto et al. (TSE) | Both-sides-nonempty if their merge scenarios exclude empty contributions; otherwise the unconditioned baseline is closer | **Citation verification required** |
| Kasi and Sarma (Cassandra) | Both-sides-nonempty is likely closest for pairs of change-bearing tasks; common-file would be closer if their candidate pairs were prefiltered by shared files | **Citation verification required** |
| Accioly et al. | Common-file is likely closest if the remembered population requires concurrent changes to common Java files | **Citation verification required** |
| Brun et al. | Both-sides-nonempty is likely closest for speculative integration of change-bearing workspaces or branches; common-file would be closer if only shared-artifact pairs enter the denominator | **Citation verification required** |

These conditionings alone do not establish cross-study equivalence. Merge unit, reachability, branch selection, conflict definition, Git configuration, language, project selection, and era can all change the denominator.

**Confidence: high for the both-sides-nonempty arithmetic; no verified result for common-file; low for the literature map until citations are checked.** Every evaluable row contains both aggregate file counts, and the conditioned counts reconcile to the baseline. Complete path sets and literature inclusion rules are absent locally.

## Per-claim confidence

| Claim | Confidence | Reason |
|---|---|---|
| The 19-site composition is 16 intersecting, 0 same-file-disjoint, 2 boundary-only, and 1 unclassifiable | High | All 19 exact repository/full-hash identities join uniquely to one conflict row, whose frozen `overlap.classification` supplies the class. |
| One of the 19 has operational mutually-unsatisfiable-test potential | High within the exact Python joint-source protocol | `SITES.md` and the structured site record agree on the Pygments identity and outcome; the claim does not extend to withheld or unassessed sites. |
| Strict overlap is 304 / 372 decidable merges (81.720%) and bounded by 304 / 416 (73.077%) to 348 / 416 (83.654%) over all conflicted merges | High | The five merge classes sum exactly to 416, and the bounds make only the two stated extremal assignments. |
| Censoring is not plausibly missing completely at random | Medium | It is repo- and conflict-form-clustered, refinement depends on size, and the merge classifier has a verified positive/negative decidability asymmetry; true censored outcomes remain unknown. |
| Censoring caused an upward bias of a particular size | Unsupported | The 44 true statuses are unobservable, so neither direction nor magnitude can be estimated from these rows. |
| The both-sides-nonempty conflict rate is 416 / 17,749 (2.344%) | High | The condition uses complete aggregate fields present on all 25,073 evaluable rows and retains all 416 conflict identities. |
| A common-file conditional conflict rate | Not verified | Complete per-side changed-path sets are absent from `_all_merges`. |
| Exact comparability to the four literature populations | Low / unverified | The papers' inclusion criteria are outside the allowed local evidence and require citation verification. |

## Claims that could NOT be verified

- The strict overlap status of Click `727176...` or of any of the 44 censored conflicted merges.
- Whether the 44 censored merges are truly more or less disjoint than the 372 decidable merges, or the direction and magnitude of complete-case bias.
- That binary merge-result paths caused strict-overlap censoring; no binary overlap-censor status occurs in these rows.
- Human authorship ground truth for the operational handwritten/artifact classification.
- Mutually-unsatisfiable-test status for the 9 textually nonconstructible Python sites or the 8 Go/Java sites outside `SITES.md`'s joint-task analysis.
- The conflict rate conditional on a nonempty intersection of the two complete changed-file sets.
- The reported populations, denominators, and conflict definitions of Ghiotto et al., Kasi and Sarma, Accioly et al., or Brun et al.; every literature mapping above requires citation verification.
- Semantic overlap, semantic conflict, historical developer experience, or generalization beyond the selected frozen repository histories.

## What would change this verdict

- Complete base/P1/P2 blob-stage coordinates for the absent-stage and gitlink cases, plus a preregistered coordinate definition for structural conflicts, could resolve 43 / 44 censored merges. A scalable independently pinned refinement method could resolve the remaining 1 / 44.
- Complete frozen changed-path lists for both sides of all 25,073 evaluable rows would make the common-file denominator directly computable. Re-diffing repository histories would also do so, but would be new mining and is outside this task.
- The four papers' exact method sections or authoritative population definitions would replace the provisional literature map with a citation-verified comparison.
- Joint contradictory-task evaluation for the 8 Go/Java sites, or a preregistered alternative construction for the 9 textually nonconstructible Python sites, could change the one-positive coverage statement; each would be a new experiment.
- A corrected corpus row or site manifest that breaks an exact identity/classification reconciliation would require rerunning this read-only analysis against the corrected frozen inputs.

## Reproducibility

`python instruments/conflicts/reanalysis.py` emits the machine-readable audit used for these counts. The script reads the frozen JSONL and site manifests only and performs no repository or network operation.
