**Does it happen? Yes under the requested broad ABA definition: 1 / 8 (12.500%) multi-agent region-write sequences recurred (denominator `D_seq=8`). However, 0 / 10 (0.000%) eligible transitions reversed prior work (denominator `D_pair=10`), and unflagged textual reversal–reapplication cycles were 0 / 8 (0.000%) (denominator `D_seq=8`).**

# Cross-agent reversal and oscillation

## Coverage caveat

This is one team, one Claude Code harness, and a Node-dominated workload in which agents were largely assigned compatible goals. A low rate is evidence only about this workload; it is not evidence that adversarial task assignment is safe.

The literal `C:/Users/USER/.claude/projects` path did not exist on this host. The run used the current user's equivalent `.claude/projects` tree, opened read-only, and froze 5,943 JSONL byte prefixes (3,995,516,101 bytes) at `2026-08-24T22:17:58.565678Z`. The frozen-prefix SHA-256 is `4dad985fafafdc45a787553a6234eccd13ec2a8821104b62a0111112a18a5aec`.

Structured event counts are lower bounds; ratios are not directionally bounded because missing operations can enter either numerator or denominator. Paths embedded only in Bash/PowerShell commands are not writes in either. Successful subagent Edit/Write results in this corpus lack `toolUseResult`; input `old_string`/`new_string` was deliberately not substituted because the requested hunk ranges and complete pre-image are result evidence.

The frozen scan observed 183,703 Bash and 16,012 PowerShell calls, whose command-string paths remain outside structured write capture. It also quarantined 2,203 globally repeated tool-use IDs whose session-based identities conflicted.

## Verdict and denominators

The broad writer-recurrence (ABA) rate is 1 / 8 (12.500%); denominator `D_seq=8`. The stronger textual reversal-and-reapplication count after cause flags is 0 / 8 (0.000%) against the fixed original denominator `D_seq=8`. Broad ABA is a structural candidate count, not evidence that the file state failed to progress or that agents held opposed objectives. With only 8 eligible sequences and no result-side subagent coverage, this can document the concern as unobserved in this channel, not as design-wide out of scope.

`D_pair = 10`: all deduplicated, successful, strictly serialized, locally state-continuous, adjacent cross-agent write pairs whose result-derived changed ranges overlap after A's post-image is mapped into B's pre-image by symmetric exact-line alignment. Local continuity means every contacted A block maps exactly; unrelated insertions/deletions elsewhere may shift its coordinates.

`D_seq = 8`: all maximal multi-agent region-write sequences formed by one or more contiguous `D_pair` edges. This is the denominator for every sequence classification and headline rate. An oscillation additionally requires one structured-patch block contact path to persist through the repeated-writer subrun.

## Four sequence classifications

The four rows are mutually exclusive and exhaustive over `D_seq`, with precedence oscillation → exact reversal → partial reversal → independent co-editing. Thus an A–B–A exact cycle on one persisted region appears in the oscillation row, not again in exact reversal; writer recurrence across unrelated regions does not count.

| Classification | Count and fraction of all multi-agent region-write sequences |
|---|---:|
| Oscillation (writer recurs after a foreign phase) | 1 / 8 (12.500%); denominator `D_seq=8` |
| Exact reversal, non-oscillating | 0 / 8 (0.000%); denominator `D_seq=8` |
| Partial reversal, non-oscillating | 0 / 8 (0.000%); denominator `D_seq=8` |
| Independent co-editing control | 7 / 8 (87.500%); denominator `D_seq=8` |

As non-exclusive content flags, 0 / 8 (0.000%) sequences contained an exact inverse edge and 0 / 8 (0.000%) contained a baseline partial inverse edge; both use denominator `D_seq=8` and can also be oscillations.

### Pair-transition control

An exact reversal restores every A block contacted by B at its aligned structural location, swapping exact old/new content; B may also have disjoint hunks. Complete whole-file restoration is accepted as a stronger grouping-insensitive case. Different hunk grouping in a merely regional restore remains a conservative false negative.

| Successive-pair label | Count and fraction of eligible overlapping pairs |
|---|---:|
| Exact reversal | 0 / 10 (0.000%); denominator `D_pair=10` |
| Partial reversal at 0.75 | 0 / 10 (0.000%); denominator `D_pair=10` |
| Independent co-editing | 10 / 10 (100.000%); denominator `D_pair=10` |

### Partial-reversal definition and sensitivity

Within the contacted blocks, the lexical inverse score is the fraction of A-added tokens B removes and the fraction of A-removed tokens B restores, matched with multiplicity. For a replacement the score is the smaller direction; for a pure insertion/deletion it is the one defined direction. A non-exact edge is “substantial” at the fixed-before-run baseline `score ≥ 0.75`. This prevents `1→2→3` from counting merely because B removed `2` without restoring `1`.

| Threshold | Partial pairs / fixed `D_pair` | Partial-only non-oscillating sequences / fixed `D_seq` |
|---:|---:|---:|
| 0.50 | 0 / 10 (0.000%); denominator `D_pair=10` | 0 / 8 (0.000%); denominator `D_seq=8` |
| 0.75 | 0 / 10 (0.000%); denominator `D_pair=10` | 0 / 8 (0.000%); denominator `D_seq=8` |
| 0.90 | 0 / 10 (0.000%); denominator `D_pair=10` | 0 / 8 (0.000%); denominator `D_seq=8` |

Tokenization sensitivity: using exact logical lines as atoms at the same 0.75 threshold yields 0 / 10 (0.000%) partial pairs; denominator `D_pair=10`.

### Insertion-anchor boundary sensitivity

Primary overlap excludes a zero-width insertion exactly at the boundary of A's changed range. Including those anchors adds 13 pairs, giving denominator `D_pair_boundary=23`. Rebuilding maximal runs from all boundary-inclusive edges gives denominator `D_seq_boundary=19`; it is not derived arithmetically from `D_seq`.

| Boundary-inclusive pair label | Count / `D_pair_boundary` |
|---|---:|
| Exact reversal | 0 / 23 (0.000%); denominator `D_pair_boundary=23` |
| Partial reversal at 0.75 | 0 / 23 (0.000%); denominator `D_pair_boundary=23` |
| Independent co-editing | 23 / 23 (100.000%); denominator `D_pair_boundary=23` |

| Boundary-inclusive sequence label | Count / `D_seq_boundary` |
|---|---:|
| Oscillation | 1 / 19 (5.263%); denominator `D_seq_boundary=19` |
| Exact reversal, non-oscillating | 0 / 19 (0.000%); denominator `D_seq_boundary=19` |
| Partial reversal, non-oscillating | 0 / 19 (0.000%); denominator `D_seq_boundary=19` |
| Independent co-editing | 18 / 19 (94.737%); denominator `D_seq_boundary=19` |

Headline sensitivity: broad ABA is 1 / 8 (12.500%) with denominator `D_seq=8` under primary overlap and 1 / 19 (5.263%) with denominator `D_seq_boundary=19` when boundary anchors count. After excluding oscillations whose repeated-writer witnesses are all definitely mechanical it is 0 / 19 (0.000%) with denominator `D_seq_boundary=19`.

## Oscillation detail

- Exact S0→S1→S0→S1 cycles: 0 / 8 (0.000%); denominator `D_seq=8`.
- Partial/exact reversal followed by re-application: 0 / 8 (0.000%); denominator `D_seq=8`.
- ABA recurrence without textual no-progress proof: 1 / 8 (12.500%); denominator `D_seq=8`.

The longest observed oscillation has 3 writes, 3 agent phases, and **2 distinct agents** over 1.0 d; it is a `coordination_markdown` file (`c:\users\joshp\.claude\projects\c--users-joshp-desktop-toolsenabled\memory\mission-control-dashboard.md`).

## Mechanical-cause separation

Definite mechanical overlap means exact whitespace-only change, import-order-only change, or contact solely through a recognized `modified`/`updated` timestamp metadata line. Suspected causes are generated/lock/build paths, an observed formatter/codegen/git command strictly between writes, or a Write/Edit whose changed-line coverage reaches 80% of the pre- or post-image. The 80% flag is a disclosed audit heuristic, not a fitted classifier. Oscillation filtering is computed on persisted contact paths, so a one-edge neighboring hunk cannot change the cause assigned to the recurring region. Changed or ambiguously aligned contacted regions are excluded before `D_pair`; exact unrelated changes elsewhere may shift the region without excluding it.

| Cause partition | Count and fraction of all sequences | Treatment |
|---|---:|---|
| `definite_mechanical_only` | 3 / 8 (37.500%); denominator `D_seq=8` | excluded from substantive numerator |
| `mixed_definite_mechanical` | 1 / 8 (12.500%); denominator `D_seq=8` | flagged; not silently called substantive |
| `suspected_generated_or_codegen` | 3 / 8 (37.500%); denominator `D_seq=8` | flagged suspected artifact |
| `suspected_wholesale_or_git` | 1 / 8 (12.500%); denominator `D_seq=8` | flagged suspected tree rewrite |
| `suspected_formatter_or_linter` | 0 / 8 (0.000%); denominator `D_seq=8` | flagged suspected formatter/linter |
| `no_detected_mechanical_cause` | 0 / 8 (0.000%); denominator `D_seq=8` | retained as unflagged, not proven semantic |

Raw oscillations: 1 / 8 (12.500%); after excluding only oscillations whose repeated-writer witnesses are definitely mechanical: 0 / 8 (0.000%); with every detected/suspected mechanical cause removed: 0 / 8 (0.000%). Each is a count against the fixed original denominator `D_seq=8`. Conditional on the unflagged population, the oscillation rate is 0 / 0 (undefined) with denominator `D_seq_unflagged=0`.

There were 3 serialized cross-agent exact-metadata raw-coordinate contacts excluded because the contacted A region could not be symmetrically aligned into B's pre-image; 1 had an observed mutating git command between the two structured writes. A break can also come from Bash/PowerShell, another unlocalized writer, or stale/copy artifacts.

## Reversal timing

For all 0 exact-or-baseline-partial reversal edges (denominator `D_reversal=0`), A-completion to B-invocation latency was: median not observed, Q1 not observed, Q3 not observed, p90 not observed, and maximum not observed.

| Latency bin | Reversal edges / `D_reversal` |
|---|---:|
| Under 1 minute | 0 / 0 (undefined); denominator `D_reversal=0` |
| 1–5 minutes | 0 / 0 (undefined); denominator `D_reversal=0` |
| 5 minutes–1 hour | 0 / 0 (undefined); denominator `D_reversal=0` |
| 1–24 hours | 0 / 0 (undefined); denominator `D_reversal=0` |
| 24 hours or more | 0 / 0 (undefined); denominator `D_reversal=0` |

## Did the reverting agent read the region?

The categories below partition every exact-or-baseline-partial reversal edge. “No observed localized Read” is not literal unawareness: Grep, prompts, shared messages, Bash, PowerShell, and unstructured subagent results can expose content.

| Reverter's strongest observed Read evidence | Count / all reversal edges |
|---|---:|
| Post-A region Read; content verified against B pre-image | 0 / 0 (undefined); denominator `D_reversal=0` |
| Post-A region Read; offset only | 0 / 0 (undefined); denominator `D_reversal=0` |
| Only a pre-A region Read | 0 / 0 (undefined); denominator `D_reversal=0` |
| Post-A file Read outside reverting region | 0 / 0 (undefined); denominator `D_reversal=0` |
| No observed localized Read | 0 / 0 (undefined); denominator `D_reversal=0` |
| Unclassified | 0 / 0 (undefined); denominator `D_reversal=0` |

## File-type concentration

| File type | Oscillations / sequences in that type | Share of all oscillations |
|---|---:|---:|
| `config_data_lock` | 0 / 1 (0.000%); denominator `D_seq_type=1` | 0 / 1 (0.000%); denominator `D_oscillation=1` |
| `coordination_markdown` | 1 / 4 (25.000%); denominator `D_seq_type=4` | 1 / 1 (100.000%); denominator `D_oscillation=1` |
| `generated_build` | 0 / 0 (undefined); denominator `D_seq_type=0` | 0 / 1 (0.000%); denominator `D_oscillation=1` |
| `other` | 0 / 0 (undefined); denominator `D_seq_type=0` | 0 / 1 (0.000%); denominator `D_oscillation=1` |
| `other_documentation` | 0 / 1 (0.000%); denominator `D_seq_type=1` | 0 / 1 (0.000%); denominator `D_oscillation=1` |
| `source_code` | 0 / 2 (0.000%); denominator `D_seq_type=2` | 0 / 1 (0.000%); denominator `D_oscillation=1` |

Boundary-inclusive file-type sensitivity:

| File type | Oscillations / boundary-inclusive sequences in that type |
|---|---:|
| `config_data_lock` | 0 / 2 (0.000%); denominator `D_seq_boundary_type=2` |
| `coordination_markdown` | 1 / 12 (8.333%); denominator `D_seq_boundary_type=12` |
| `generated_build` | 0 / 0 (undefined); denominator `D_seq_boundary_type=0` |
| `other` | 0 / 0 (undefined); denominator `D_seq_boundary_type=0` |
| `other_documentation` | 0 / 2 (0.000%); denominator `D_seq_boundary_type=2` |
| `source_code` | 0 / 3 (0.000%); denominator `D_seq_boundary_type=3` |

## Measurement attrition

The requested literal reuse of `extract_hazards.py` was not possible without violating the result-only requirement: that historical iterator reads call input paths and exposes neither tool IDs nor results (and importing it has an output directory side effect). It remains unchanged. This instrument uses the same JSONL traversal shape but performs its own source-local call/result pairing and uses only result paths. This is an explicit instrument deviation, not claimed reuse.

| Stage | Count |
|---|---:|
| `deduplicated_successful_write_operations` | 27,244 |
| `write_operations_without_result_path` | 21,037 |
| `result_localized_non_noop_write_events` | 6,207 |
| `usable_exact_write_events` | 1,270 |
| `adjacent_result_localized_write_pairs` | 3,462 |
| `cross_agent_adjacent_pairs` | 210 |
| `strictly_serialized_cross_agent_pairs` | 210 |
| `serialized_pairs_missing_exact_write_metadata` | 149 |
| `serialized_exact_metadata_pairs` | 61 |
| `mapped_coordinate_disjoint_exact_metadata_pairs` | 32 |
| `unmapped_or_ambiguous_region_pairs` | 3 |
| `coordinate_contact_exact_metadata_pairs` | 23 |
| `shifted_coordinate_contacts_recovered` | 1 |
| `full_file_state_continuous_contact_pairs` | 21 |
| `contacted_region_alignment_failures` | 3 |
| `local_state_continuity_breaks` | 3 |
| `locally_state_continuous_contact_pairs` | 23 |
| `boundary_anchor_only_pairs_sensitivity` | 13 |
| `eligible_overlapping_pairs_D_pair` | 10 |
| `multi_agent_region_sequences_D_seq` | 8 |

### Result-metadata coverage by source

These are successful result *occurrences* before global duplicate quarantine. A usable write requires a result path, a nonempty valid patch, and a string pre-image; field presence or `originalFile: null` is not counted as usable.

| Transcript source | Localized Reads / successful Reads | Usable nonempty writes / successful writes |
|---|---:|---:|
| Main transcript | 5322 / 5750 (92.557%); denominator `5,750` successful Read results | 4126 / 14925 (27.645%); denominator `14,925` successful Edit/Write results |
| Direct subagent | 0 / 13232 (0.000%); denominator `13,232` successful Read results | 0 / 12586 (0.000%); denominator `12,586` successful Edit/Write results |
| Workflow subagent | 0 / 11992 (0.000%); denominator `11,992` successful Read results | 0 / 8451 (0.000%); denominator `8,451` successful Edit/Write results |

## Concrete oscillation sequences

Only 1 distinct raw oscillation sequence exists across the primary population (1 in `D_seq`) and additional boundary-inclusive sensitivity candidates (0); all are printed. Three examples do not exist.

### Example 1: `c:\users\joshp\.claude\projects\c--users-joshp-desktop-toolsenabled\memory\mission-control-dashboard.md`

Classification: `oscillation` / `ABA_only`; cause partition `definite_mechanical_only`; file type `coordination_markdown`; population `primary`; persisted-region witness cause(s) `definite_mechanical_only`; 3 writes by 2 distinct agents over 1.0 d.

Write 1: agent `9001a6af-de18-4b47-b197-6f00ee60b72e`, `Edit`, call `2026-08-05T01:44:35.856000Z`, result `2026-08-05T01:44:36.911000Z`.

````diff
@@ -8,1 +8,1 @@
-  modified: 2026-08-04T19:31:21.494Z
+  modified: 2026-08-05T01:44:35.882Z
@@ -16,5 +16,5 @@
-**Paused 2026-08-04 mid-execution of an approved phased polish plan.** The full plan —
-request-review checklist, 22 remaining audit findings, per-phase agent prompts, and a
-RESUME STATE header saying exactly what to run next — is at
-`C:\Users\joshp\.claude\plans\mossy-humming-mochi.md`. Phase A committed (`b153556`);
-resume by launching the Phase B prompt verbatim, then C1→C2→C3→D→E→F.
+**Polish plan COMPLETE 2026-08-04** — all 8 phases committed (final commit `8be2fe5`),
+full probe suite green, 75-image gallery reviewed and its findings fixed. The plan
+file (`C:\Users\joshp\.claude\plans\mossy-humming-mochi.md`) records what was
+deliberately left: home files (parallel session's territory), the black-theme
+node-sphere design divergence (flagged, owner's call), two documented transients.
````

Write 2: agent `4d31c982-3bbf-4edb-85f1-e86b9fb503bb`, `Edit`, call `2026-08-05T22:25:52.205000Z`, result `2026-08-05T22:25:53.261000Z`.

````diff
@@ -8,1 +8,1 @@
-  modified: 2026-08-05T01:44:35.882Z
+  modified: 2026-08-05T22:25:52.242Z
@@ -16,0 +16,5 @@
+**R1133 (2026-08-05): mission-control is the OFFICIAL successor dashboard** —
+AgentActivityVisualizer is decommissioned from ToolsEnabled ("AAV is no longer a part
+of toolsenabled, we are switching over to mission control"). AAV is preserved at its
+first local commit but excluded from V1 and from future dashboard work.
+
````

Write 3: agent `9001a6af-de18-4b47-b197-6f00ee60b72e`, `Edit`, call `2026-08-06T01:59:06.409000Z`, result `2026-08-06T01:59:07.457000Z`.

````diff
@@ -8,1 +8,1 @@
-  modified: 2026-08-05T22:25:52.242Z
+  modified: 2026-08-06T01:59:06.417Z
@@ -27,0 +27,8 @@
+**DEVELOPMENT HANDED OFF 2026-08-05 at commit `14aad6b`** — the finalized build is
+`c6f73aa` (feature wave → brace framing → perf surgery → sanctioned one-time Opus
+visual wave → cold-read loop closed on all metrics modules); `HANDOFF.md` in the repo
+is the complete transfer document for the next manager (laws, protected surfaces,
+codex-CLI standing order, gate battery, cold-read loop, gotchas). Read it — not this
+memory — before doing any further website work. `V1safe` on the Desktop stays the
+frozen fallback (commit `3992fa8`).
+
````

Edge 1: `independent_coediting`, lexical inverse score `0.000`, latency 20.7 h, Read status `not_applicable_non_reversal`.
Edge 2: `independent_coediting`, lexical inverse score `0.000`, latency 3.6 h, Read status `not_applicable_non_reversal`.

## What I got wrong

| Prior premise | Verdict | Correction |
|---|---|---|
| “98.7% of Edit/Write results have patches and complete pre-images” | Wrong population | That figure describes main-transcript field presence and includes null/empty cases; successful subagent results have no `toolUseResult` object. The primary rate is conditional on usable result evidence. |
| The historical event walker could supply region writes | False | It walks call inputs only. Result-local pairing and global duplicate quarantine were required. |
| Any ABA sequence proves opposed goals | Too strong | ABA is a structural candidate; compatible iteration, rollback after tests, and handoff remain viable explanations. |

## Claims that could NOT be verified

- That any structural reversal or ABA recurrence was caused by genuinely opposed task objectives.
- The reversal/oscillation behavior of metadata-less subagent Edit/Write results.
- Writes performed through Bash, PowerShell, git, formatters, linters, code generators, or other tools without structured write results.
- Actual reverter awareness when no localized Read was observed; prompts, Grep, shell output, and shared context are not joined.
- Semantic equivalence reversals that restore behavior without restoring lexical tokens or exact lines.
- Generalization beyond this team, harness, compatible-goal workload, and Node-dominated corpus.
- That absence of a detected formatter/codegen/git command proves a change was non-mechanical.

## What would change this verdict

1. Persist result-side `filePath`, `structuredPatch`, and non-null `originalFile` for subagent writes, then rerun the same fixed-denominator classifier.
2. Persist structured paths and pre/post state for Bash, PowerShell, git, formatter, linter, and codegen mutations so continuity breaks become observable writes.
3. Attach immutable task-objective IDs and explicit incompatibility labels to agents; then compare structural cycles under compatible versus opposed assignments.
4. Replicate on intentionally adversarial assignments, independent teams/harnesses, and a language-balanced corpus.
5. Blind-review every unflagged candidate with task prompts, tests, and repository state; genuine opposed-goal cycles would raise the substantive numerator.

## Confidence by claim

| Claim | Confidence | Reason |
|---|---|---|
| `D_pair=10` and `D_seq=8` within the frozen structured-result slice | High | Byte-prefix snapshot, result-only paths, source-local pairing, global identity-conflict quarantine, exact patch application, strict ordering, and symmetric exact-line mapping at contacted blocks; ambiguous repeated-line alignments are rejected. |
| Exact reversal counts within `D_pair` | Moderate-high, conservative | Exact regional inverse blocks and complete-restoration cases are unambiguous; a regional restore expressed with different hunk grouping can still be missed. |
| Partial reversal counts | Moderate | The construct is explicit and threshold sensitivity is reported, but lexical tokens are an imperfect proxy for intent. |
| Structural ABA/oscillation counts | High structurally; low as goal conflict | Agent recurrence and state continuity are exact in-slice; objectives are not recorded or labeled. |
| Boundary-inclusive ABA sensitivity | High structurally in-slice | Boundary edges are fully materialized and maximal runs are rebuilt; the sensitivity is not inferred from pair counts. |
| Observed localized Read categories | High as recorded-tool evidence; low as actual awareness | Result paths/ranges/content are exact where present, but other information channels are omitted. |
| Mechanical separation | Moderate for whitespace/import; low-to-moderate for codegen/git | Definite predicates are content-based; command/path and 80% flags are incomplete heuristics. |
| Low rate applies outside this workload | No supported claim | One team, one harness, compatible goals, Node dominance, and missing subagent/shell patches. |

## Reproduction

```powershell
python instruments/oscillation/analyze.py `
  --corpus "$env:USERPROFILE\.claude\projects" `
  --output exploratory/oscillation/results.json `
  --manifest-output exploratory/oscillation/corpus-manifest.json `
  --report-output exploratory/oscillation/RESULTS.md
python -m unittest instruments.oscillation.test_analyze -v
```

Diagnostic context: the frozen scan observed 14,925 successful main Edit/Write result occurrences. The sideagent result channel was separately counted in schema diagnostics; its successful writes cannot enter `D_pair` without result paths/ranges/pre-images.
