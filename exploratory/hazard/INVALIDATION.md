# Invalidation join: the rate is not identifiable

## Coverage caveat

This is one team, one harness, and a Node-dominated workload. The structured-tool
join does not parse paths embedded in command strings: 167,459 Bash calls and
15,414 PowerShell calls are outside the measurement. Raw operation,
path-bearing-event, read, and write totals are therefore lower bounds. Fractions
and first-response category counts are **not** directionally bounded: omitted
operations can add to either numerator or denominator, or change which event is
first.

The transcript corpus was opened read-only. The final run froze 5,867 JSONL byte
prefixes (3,945,204,612 bytes) at `2026-08-24T05:42:54.120719Z`; the frozen-prefix
SHA-256 is `e03a33bdf511474c17a9852de1746dbfe0723621b8884152a6926f8802b0da91`.
Opening Read/Write endpoints were restricted to the historical cutoff
`2026-08-23T11:15:09.101Z`; follow-up was observed through the frozen end of each
transcript. The historical replication had no byte manifest, so the cutoff is a
post-hoc forensic reconstruction, not proof that the original live scan was
atomic.

## Verdict

**No confirmed-invalidation rate can be reported.** The requested fraction is
undefined, not 0%: among pre-image-verified, region-overlapping hazards there
were **zero outcome-eligible reader edits**, hence neither a rework-followed nor
a clean-followed denominator.

The corpus therefore still does not answer the central question, **“is reading
depending?”** It establishes exposure, and it supports a narrow structural
overlap audit, but the result schema removes the reader's exact downstream patch
in precisely the actor topology that supplies classifiable openings.

## Population and attrition

The committed exposure result is reproducible at the historical cutoff: 553 code
paths, 24,138 ordered Read/foreign-Write pairs, 854 readers, and 392 writers at
one hour. That population contains copied transcript prefixes. Its 65,194
path-bearing occurrences reduce to 56,889 global tool-use IDs; 2,257 IDs repeat,
adding 8,305 occurrences, and 2,196 repeated IDs disagree on actor or session.

The invalidation join first links successful call/result records source-locally,
then deduplicates by global tool-use ID. It retains the original call-to-call
one-hour window but also requires `Read.result < Write.call` to exclude concurrent
tools.

| Join stage | Pairs | Paths / sessions | What the stage establishes |
|---|---:|---:|---|
| Historical file-level exposure | 24,138 | 553 paths | Forensic replication, including copied occurrences and cross-session hazards |
| Successful, globally deduplicated exposure | 12,530 | 522 paths | 12,347 pairs are same-session; 183 are cross-session |
| Strict same-session ordered pairs | 12,346 | 512 historical paths | One overlapping tool-call interval removed |
| Read window + exact B patch | 2,258 | 169 paths, 20 sessions | Offset-only structural contact can be computed |
| Read content exactly matches B pre-image slice | 103 | 51 paths, 14 sessions | Opening contact is version-verified |
| Verified reader-edit outcomes | **0** | 0 paths, 0 sessions | Rework/clean contingency has no denominator |

Of the 12,346 strict pairs, 9 reads were unlocalized. After retaining localized
reads, 10,079 B writes lacked an exact patch. Among the 2,258 offset-classifiable
pairs, 2,108 lacked a usable B pre-image and 47 failed the exact Read-content to
B-pre-image comparison, leaving 103 matches.

All 2,258 offset-classifiable pairs—and all 103 pre-image-verified pairs—have an
explicit sidechain reader A and MAIN writer B. Excluding endpoints whose copied
tool IDs disagree on actor/session reduces the classifiable cohorts from 2,258
to 2,038 and from 103 to 72 respectively; it does not create any reader-edit
outcomes.

## Measurement definitions

- **Opening-overlap unit:** one deduplicated, strict, same-session ordered
  `(Read tool ID, foreign Write tool ID)` pair. Repeated reads before one write
  remain distinct because this is the unit behind the original hazard count.
- **Downstream-response unit:** one reader/path episode opened by the first
  definite foreign write. This avoids multiplying one response over many prior
  reads. Later foreign writes are competing exposures, not merged amendments to
  the opening label.
- **Coordinates:** 1-based, half-open logical-line intervals. Patch context is
  excluded; only exact added/deleted runs from `structuredPatch` count.
- **Strict region overlap:** B deleted or replaced a line A read, or inserted
  strictly inside A's read window. An insertion exactly at a window boundary is
  a separately reported sensitivity, not primary overlap.
- **Pre-image verification:** every logical line returned to A must equal the
  corresponding slice of B's non-null `originalFile`. This is exact logical-line
  content, not raw newline-byte identity.
- **Reader response:** after B completes, compare A's first read/write with the
  next foreign write using strict interval ordering. Overlapping calls are
  ambiguous; an earlier foreign write censors the response. Actions after that
  censor are presence-only and cannot be attributed to the original B write.
- **Rework proxy:** after a localized A index edit touching the propagated read
  region, an exact inverse, a later A edit, or a foreign supersession must touch
  that edit's propagated footprint. Central-rate eligibility additionally
  requires A's index edit to touch B's propagated changed footprint; the broader
  requested contingency retains any read-region index edit as a comparison.
  “Clean” additionally requires an intact exact-patch chain, at least five
  minutes of observed follow-up, and a quiescent session. Reaching transcript
  end without an edit is **not** clean.

## Read/write region overlap

The offset-only diagnostic classifies 2,258 pairs: 992 strict overlaps and 1,266
file-only contacts, or **43.9%** strict line-offset overlap. Five file-only pairs
are boundary insertions; including them changes the diagnostic to 44.2%. This is
not an “actual overlap” estimate because the Read window and B patch may describe
different file versions. Dependence is also material: its session-clustered 95%
interval is 27.5%–64.0%, and its path-clustered interval is 31.2%–59.8%.

Exact Read-content/B-pre-image agreement leaves 103 pairs: 101 strict overlaps,
2 file-only contacts, and no boundary contacts. **No overlap fraction is reported
for this verified cohort.** It spans only 14 sessions, every pair has the same
sidechain-reader → MAIN-writer topology, 31 of the 103 pairs disappear under the
actor-identity-conflict sensitivity, and the reporting gate requires at least
20 contributing sessions as well as 100 classifiable pairs and adequate
clustered precision.

For clarity, the 43.9% number answers only “do the reported line offsets
intersect?” The verified 101/2 counts are the closest evidence for “did the
actual version A read intersect B's exact change?”, but the selected population
is too narrow to support a rate.

## What A did next

Among the 103 pre-image-verified pairs:

| Opening class | Competing foreign write first | Changed-region reread first | Observed end, no reader action | Reader edit |
|---|---:|---:|---:|---:|
| Region-overlapping (101) | 78 | 1 | 22 | **0** |
| File-only (2) | 2 | 0 | 0 | **0** |

The larger offset-only sensitivity gives the same substantive result: 1,938 of
2,258 pairs encounter a competing foreign write first; 10 overlap B's own tool
interval and are ambiguous; 287 reach observed transcript end with no reader
action; 1 first reread touches B's changed region; and 22 first reread elsewhere.
Ignoring the competing-write censor, 66 pair rows contain some later A write and
67 contain a localized reread, but **none contains an exact A patch**. Thus the
instrument cannot determine whether any later A write touched the previously
read region.

As a dependence-reducing sensitivity, the first-write episode construction
produces 1,568 response episodes. Of these, 101 have verified opening classes
(100 overlap, 1 file-only), again with zero exact reader-edit outcomes. The unit
changes the overlap statistic, so episode overlap is not presented as the
original hazard overlap rate.

## Requested contingency table

| Pre-image-verified opening class | Opening pairs | Rework-followed | Clean-followed | Outcome-eligible |
|---|---:|---:|---:|---:|
| Region-overlapping | 101 | 0 | 0 | **0** |
| File-only | 2 | 0 | 0 | **0** |

These are `0/0` outcome cells, not evidence of no rework. There was no localized
reader index edit from which either rework or clean follow-up could be measured.
Consequently:

> Of hazards where the read and write regions were pre-image-verified as
> overlapping, the fraction showing downstream rework is **not estimable**.

## Why the join failed structurally

The two schema-coverage premises in the finding do not hold over the joined,
deduplicated population:

| Successful result type | Coverage observed |
|---|---|
| Read | 25,915 / 28,411 localized (91.2%): 2,941 structured file objects plus 22,974 numbered visible-result fallbacks |
| Edit | MAIN: 4,827 exact patches; explicit sidechain: 14,424 results with no structured patch |
| Write | MAIN: 364 exact update patches and 2,276 empty create/no-op patches; explicit sidechain: 6,068 results with no structured patch |
| Edit pre-image | 1,433 nonempty `originalFile`; 3,394 null among MAIN structured results |
| Write pre-image | 264 nonempty `originalFile`; 2,376 null among MAIN structured results |

The earlier 99.6% Read figure was conditional on already having a structured
Read `file` object; it was not coverage of all successful Read results. Numbered
visible output recovers much of the remainder, but total localization is 91.2%.
The earlier 98.7% Edit/Write statement counted key presence and treated
`originalFile: null` as a complete pre-image. More importantly, every explicit
sidechain Edit/Write result in this cohort lacks the top-level exact patch.

This creates a reciprocal observation problem. A classifiable B write must be a
MAIN write, making A the sidechain reader. A's later sidechain write then lacks
the exact patch required to localize the edit and follow its footprint. In the
103 verified pairs A did not write at all, but even the 66 later-write rows in
the offset sensitivity are structurally unlocalizable.

## Rework is a proxy, not wrongness

Even with a nonzero denominator, rework would not equal semantic wrongness.

- **Upward bias (overstates wrongness):** planned iteration, formatting,
  refactoring, requirement changes, ownership handoff, or an unrelated foreign
  edit can produce a re-edit or supersession even when A's first work was sound.
- **Downward bias (understates wrongness):** stale work may remain unrepaired,
  be abandoned, be fixed in another file or session, or be repaired through
  Bash, PowerShell, Git, or another unparsed tool. Missing sidechain patches also
  hide real rework.

Conditioning on observable exact patches can move the rate in either direction:
it can select unusually structured/easy edits, while conditioning on agents who
continue editing can select unusually difficult cases. A future measured rate
must retain both caveats.

## Confidence by claim

| Claim | Confidence | Reason |
|---|---|---|
| The historical 553-path / 24,138-pair exposure result is forensically reproducible at the cutoff | High, conditional | Two independent implementations agree; the original live scan lacks a byte manifest, so historical atomicity is not provable |
| The frozen join contains 12,346 strict same-session ordered pairs | High | Byte-prefix manifest, deterministic tool-ID deduplication, strict interval test, independent recount, and 36 passing tests |
| The offset-only contact diagnostic is 992 / 2,258 | High as coordinate arithmetic; low as semantic overlap | Exact hunk-run parsing and independent reproduction; file-version identity is not established for most rows |
| The verified opening counts are 101 overlap and 2 file-only | High within the selected cohort | Exact logical-line comparison to B's pre-image and independent reproduction; only 14 sessions and one actor topology contribute |
| There are zero exact reader edits after B in both the 2,258 and 103 cohorts | High within the frozen structured-tool corpus | Full forward scan through frozen transcript end, including post-cutoff actions; independently reproduced |
| A confirmed-invalidation rate is not identifiable here | High | Outcome denominator is zero under both pair and response-episode constructions |
| Reading is or is not depending in general | No supported claim | The observation channel needed to discriminate the alternatives is missing |
| The result generalizes beyond this team/harness and Node-dominated workload | Low / unverified | No independent teams, harnesses, or balanced language populations were measured |

## What I got wrong

| Prior assumption | Verdict | Correction |
|---|---|---|
| “99.6% of Read results are localized from structured result metadata” | Wrong denominator | 99.6% was conditional on structured `file` objects; full successful-result localization is 91.2% after visible-output fallback |
| “98.7% of Edit and Write results carry an exact patch and complete pre-image” | False | Key presence included null pre-images, and explicit sidechain results lack the top-level patch/pre-image metadata needed here |
| A first-write response episode could stand in for the original hazard unit | Wrong estimand | Ordered pairs give 992/1,266 offset classes; response episodes give a different selected statistic because reads are unioned and later writes censor |
| Reported line offsets alone established actual overlap | Too strong | Actual-version verification requires the Read content to equal B's pre-image slice; only 103 pairs pass |
| Raw transcript occurrences could be treated as independent agent events | Too strong | Copied prefixes repeat tool IDs and can rewrite actor/session identity; deduplication and an identity-conflict sensitivity are required |

## Claims that could NOT be verified

- A confirmed-invalidation or confirmed-clean rate.
- An actual-overlap rate representative of the original 553 hazard paths.
- That any reader edited a stale region without first rereading it.
- That any observed rework was caused by B's intervening write.
- That an absence of observed rework means A's work was correct.
- Semantic wrongness, test failure, or behavioral breakage for any hazard.
- Coverage of path-bearing Bash and PowerShell operations.
- Generalization to another team, harness, language mix, or repository sample.
- The novelty claim that no published work reports this rate; no literature
  review was part of this measurement.

## What would change this verdict

1. Persist the same structured Read window, exact patch, and non-null pre-image
   metadata for sidechain tool results that MAIN results receive.
2. Re-run on an atomic, immutable transcript snapshot and retain its manifest
   from the start.
3. Obtain at least 100 verified region-overlap edit outcomes across at least 20
   sessions and 20 paths, where A's index edit touches B's changed footprint,
   with at least five observations in both the rework and clean cells and
   session/path-clustered uncertainty no wider than 20 percentage points.
4. Parse and audit path-bearing Bash and PowerShell commands, then rerun the
   join with those operations in both the exposure and follow-up timelines.
5. Add test outcomes or blinded semantic labels so rework can be calibrated
   against wrongness rather than treated as its unvalidated proxy.
6. Replicate across independent teams, harnesses, and non-Node-dominated
   workloads before making a general claim.

## Reproduction and artifacts

Run from the repository root:

```powershell
python instruments/hazard/invalidation.py `
  --corpus "$env:USERPROFILE\.claude\projects" `
  --output exploratory/hazard/invalidation-results.json `
  --manifest-output exploratory/hazard/invalidation-corpus-manifest.json

python -m unittest discover -s instruments/hazard -p 'test_invalidation*.py'
```

Artifacts:

- [Aggregate results](./invalidation-results.json) — SHA-256
  `529baad7138e49e5d8b191e5bc9e58e02b77267774142c6716c53ebf9d38042b`
- [Frozen byte-prefix manifest](./invalidation-corpus-manifest.json) — SHA-256
  `4e78cd9ca358034ce61c38e760993a9eb24cd84a24be415286ea46a1dddda1c3`
- [Join instrument](../../instruments/hazard/invalidation.py)
- [Line/patch core](../../instruments/hazard/invalidation_core.py)
- [Join tests](../../instruments/hazard/test_invalidation.py)
- [Core tests](../../instruments/hazard/test_invalidation_core.py)

`extract_hazards.py` was not modified.

The public artifacts contain aggregates plus per-prefix ordinals, byte lengths,
cutoff flags, and hashes. Local corpus roots, transcript filenames, filesystem
timestamps, session/tool IDs, and source-path hashes are deliberately omitted.
