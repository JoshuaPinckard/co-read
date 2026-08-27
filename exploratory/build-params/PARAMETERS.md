# Operational build parameters

**Scope up front:** one team, one Claude Code harness, and a Node-dominated workload. These are observed seeds for `@perrepo` self-calibration, not universal constants. The corpus was opened read-only; no file contents or shell commands are reproduced.

## Parameter table

| Name | Value / distribution | What it sizes | Blind spot |
|---|---|---|---|
| 1. Event volume | 57,610 successful events; active actor-hour n=5,174; p50/p90/p99/max=6/28/68/148; aggregate active UTC hour n=520; p50/p90/p99/max=55/276/788/1,071 | Event-log write rate and count retention | 191,764 shell commands (332.9% of structured-event count) are untyped effects. |
| 2. Read windows | lines n=2,519; p50/p90/p99/max=44/210/1,069/2,076; returned UTF-8 bytes n=2,519; p50/p90/p99/max=2,350/13,901/59,161/73,894 | Default read-claim region granularity | Only 2,519/29,024 successful reads localized after all-channel dedup; 454,596 shell mentions have no line window. |
| 3. Edit regions | change-block lines n=4,489; p50/p90/p99/max=4/16/53/400; LF-normalized UTF-8 bytes n=4,489; p50/p90/p99/max=273/1,104/3,515/18,706 | Write-claim granularity and escalation region | Exact-preimage population is 1,354/28,586 writes; shell write/ambiguous commands have no patch spans. |
| 4. Read multiplicity | rolling 24 h n=1,392; p50/p90/p99/max=1/1/4/11; rolling 7 d n=1,392; p50/p90/p99/max=1/2/5/16 | Inverted-index hot-key fan-out | Treating every recovered shell mention as a read raises the 7-day candidate maximum by up to 1,709 actors; this is an exposure sensitivity, not observed reads. |
| 5. Read→write / linger | first read→absolute first write n=405; p50/p90/p99/max=25.755 s/58.66 min/1.54 d/3.22 d; last file write→observed end n=3,094; p50/p90/p99/max=23.07 h/3.00 d/5.92 d/15.39 d | Lease/renewal and release-at-close behavior | No close event exists; 127/405 eligible pairs also have same-path shell mentions. |
| 6. Observed concurrency | all-tool logical actors/min n=20,319; p50/p90/p99/max=2/8/24/53; same Claude project-bucket proxy n=22,761; p50/p90/p99/max=1/8/23/53; same current Git root in resolved structured slice n=3,356; p50/p90/p99/max=1/1/2/4 | Shim actor concurrency and per-repository contention | Minute co-activity is not true overlap; 2.9% of eligible call-ID groups were quarantined; current Git-root attribution covers 6,448/57,610 structured events and project-bucket results are only a proxy. |
| 7. Index cardinality | 1,392 files; 1,799 file/actor pairs; weekly new files n=7; p50/p90/p99/max=187/443/443/443 | Initial index memory and weekly growth headroom | All shell mentions-as-reads sensitivity reaches 63,829 candidate paths and 162,209 candidate pairs. |
| 8. Capture coverage | 191,764 shell vs 58,250 structured calls (3.29:1); mentions/parser-positive command n=176,442; p50/p90/p99/max=2/5/11/234; weighted parser recall 35.2% | Instrumentation priority and shell adapter budget | Recovered text is a lexical mention, not a successful read/write; 50-command audit is stratified and small. |
| 9. File-type mix | 2,519 localized read events / 6,505 localized writes across source/config/markdown/json/lock/binary/other | Fast-path formats versus tolerated formats | Extension/basename classifier; binary means binary-looking extension, not inspected content. |
| 10. Session length | all tool calls/actor n=5,290; p50/p90/p99/max=31/105/314/6,999; observed wall clock n=5,971; p50/p90/p99/max=6.51 min/35.65 min/10.83 h/16.42 d | One-shot dispatch payload and statelessness envelope | Observed span includes idle gaps and has no launch/close/crash boundary; raw parent session and logical actor populations differ; 2.9% of eligible call-ID groups were quarantined. |

## Corpus freeze and measurement contract

The final freeze fixed **6,290 JSONL byte prefixes** totaling **4,151,991,960 bytes** at `2026-08-25T14:54:34.024634Z`. The frozen-prefix SHA-256 is `dcba21bb558eb1cc32dfc29a92b46817c2b8050d56c685bec7234ba2501c7458`. All 6,290 prefixes were read; read failures=0, truncations=0, malformed JSONL lines=0, and frozen manifest prefixes resolved=6,290; live JSONL files outside the frozen manifest at rescan=12.

The freeze hashes ordinal, frozen byte length, and exact prefix bytes, matching the oscillation study. It does not hash path names and is not a transactional filesystem snapshot: a same-length concurrent mutation could produce a mixed-time view, although the digest commits exactly to the bytes read. Percentiles are observed nearest-rank values (`x[ceil(p*n)-1]`); every `n` below is its denominator. UTC day/week/hour/minute buckets are half-open, and ISO weeks begin Monday.

Calls and results are paired source-locally by tool-use ID, then copied IDs are reconciled globally; conflicts are quarantined. The coordination identity is `agentId` when nonempty, otherwise `sessionId`. Structured path metrics use result-side paths only. Repository concurrency uses current extant `.git` ancestors where resolvable and labels Claude project-directory grouping separately as a proxy.

## Seed defaults for `@perrepo` self-calibration

These are percentile seeds, not safety limits. Long tails should renew or escalate; observed maxima should remain diagnostics.

- Event log: p99 **68 events/active actor-hour** and **788 aggregate events/active UTC hour**; observed maxima 148 and 1,071. Byte retention remains unverified until the final event record is serialized.

- Read claim: seed p90 **210 lines / 13,901 returned UTF-8 bytes**; retain p99 **1,069 lines / 59,161 bytes** as escalation/tail telemetry.

- Write claim change block: seed p90 **16 lines / 1,104 LF-normalized UTF-8 bytes**; p99 is **53 lines / 3,515 bytes**.

- Lease proxy: use **1.00 h renewable** as the initial self-calibration seed, rounded from the observed p90 first-read→first-write interval of **58.66 min**; p99 is **1.54 d**. The p90 last-file-write→observed-end tail is **3.00 d**; require renewal/heartbeat and explicit close rather than installing that censored tail as an unattended lease.

- Concurrency: global p99/max is **24/53 logical actors/active minute**. For an initial `@perrepo` seed, the all-call Claude project-bucket proxy is p99/max **23/53**. Keep the current-Git-root resolved structured slice p99/max **2/4** as diagnostics only because attribution covers 6,448/57,610 events.

- Read index: structured seed **1,392 files / 1,799 file-actor pairs**, with p99 weekly increments **443/566**. The all-shell-mentions-as-reads sensitivity is 63,829 candidate paths / 162,209 pairs.

- Capture adapter: shell calls are **3.292:1** versus structured file-tool calls; the audited lexical parser's weighted recall/precision is only **35.2%/39.5%**, so mentions must not create mechanical claims.

- Format fast path: source plus Markdown account for **73.8%** of localized reads and **85.4%** of localized writes. Lock and binary events were both zero in the localized slice; tolerate them, but do not infer they are absent from shell activity.

- One-shot dispatch: provision the p99 envelope at **314 all-tool calls/actor** and **97 successful structured events/core-active actor**; observed maxima are 6,999/941. The p99 observed wall span is 10.83 h, but includes idle time and is not a timeout default.

## 1. Event volume

Corpus total: **57,610** successful Read/Edit/Write results: Read=29,024, Edit=19,209, Write=9,377. Calls=58,250; result-linked operations including errors=58,229; failed results=619.

Global-ID quarantine diagnostics: operation session-identity conflicts=2,407, operation metadata conflicts=22, call session-identity conflicts=8,554, and call tool/time conflicts=3. These are excluded, not guessed or double-counted.

Post-quarantine tool-ID groups kept/excluded: all calls 288,580/8,557 of 297,137 (2.9% excluded); result-linked operations 58,229/2,429 of 60,658 (4.0% excluded); shell commands 191,764/4,795 of 196,559 (2.4% excluded). All distributions are conditional on the kept slice.

Active logical-actor/UTC-hour buckets: n=5,174; p50/p90/p99/max=6/28/68/148. Aggregate active UTC hours: n=520; p50/p90/p99/max=55/276/788/1,071. Per-tool active actor-hours: Read n=4,279; p50/p90/p99/max=4/16/38/114; Edit n=1,897; p50/p90/p99/max=6/25/52/105; Write n=2,513; p50/p90/p99/max=2/8/18/44. Combined write-family: n=2,918; p50/p90/p99/max=5/25/55/112.

**Shell-channel qualification:** 191,764 deduplicated shell commands equal 332.9% of the structured-event count; parser-positive shell commands equal 304.3%. They cannot be added as events because direction, effect, and success are unknown. The event count sizes count throughput only, not serialized bytes.

## 2. Read window sizes

Eligible localized reads: **2,519/29,024** (8.7%). `startLine`: n=2,519; p50/p90/p99/max=1/700/3,585/15,130 one-based lines. `numLines`: n=2,519; p50/p90/p99/max=44/210/1,069/2,076 logical lines.

Same-result returned UTF-8 payload bytes: n=2,519; p50/p90/p99/max=2,350/13,901/59,161/73,894 bytes; bytes/declared line: n=2,519; p50/p90/p99/max=51/102.152/3,205/58,659 bytes/line. The byte denominator is every localized read with a same-result content string: 2,519/2,519. Under the tool's observed terminal-separator-as-empty-line convention, 2,482/2,519 strings align with metadata; the aligned-only byte sensitivity is n=2,482; p50/p90/p99/max=2,410/13,988/59,613/73,894 bytes. Alignment is diagnostic and does not gate the primary bytes. These are transcript-decoded payload bytes, not physical disk bytes or encoding/line-ending truth. Another event's `originalFile` was not joined to a read because state can change.

**Shell-channel qualification:** 454,596 canonical shell path mentions, or 18046.7% of the structured-window count, have no recoverable line/byte window; 136,220 commands were heuristically read-like/mixed. Neither quantity can repair the distribution.

## 3. Edit region sizes

Successful Edit/Write denominator=28,586; localized writes=6,505; valid nonempty patches before pre-image validation=4,319; exact string-preimage/applying patches=1,354. The exact slice contains 1,618 raw hunks and 4,489 contiguous `+/-` change blocks.

Raw hunk declared old lines (including context): n=1,618; p50/p90/p99/max=7/29/142/206; declared new lines: n=1,618; p50/p90/p99/max=15/53/180/457; exact transcript-UTF-8 old span: n=1,618; p50/p90/p99/max=454/1,893/8,173/9,824 bytes.

Parsed change-block removed lines: n=4,489; p50/p90/p99/max=1/6/20/130; added lines: n=4,489; p50/p90/p99/max=2/14/49/400; claim span `max(removed,added)`: n=4,489; p50/p90/p99/max=4/16/53/400. LF-normalized UTF-8 claim bytes: n=4,489; p50/p90/p99/max=273/1,104/3,515/18,706 bytes. Pure insertions=970/4,489; pure deletions=174/4,489. Insertions remain zero-width old-side anchors.

Per exact write: change blocks n=1,354; p50/p90/p99/max=1/8/31/50; aggregate claim lines n=1,354; p50/p90/p99/max=9/56/200/452; aggregate claim bytes n=1,354; p50/p90/p99/max=680/3,733/13,888/22,714 bytes. Full Write creates with null pre-image/empty patch=2,186; result-content size n=2,186; p50/p90/p99/max=4,147/9,692/24,447/59,358 bytes.

**Shell-channel qualification:** structured exact patch blocks=4,489; heuristic shell write/mixed commands with canonical paths=87,628; ambiguous canonical-path commands=29,410. Shell commands supply no trusted hunks, pre-images, or byte spans.

## 4. Read multiplicity

One observation per read file: rolling 24-hour maximum n=1,392; p50/p90/p99/max=1/1/4/11; rolling 7-day maximum n=1,392; p50/p90/p99/max=1/2/5/16; denominator=1,392 files. Calendar nonempty file/day cells: n=1,667; p50/p90/p99/max=1/1/4/11; file/ISO-week cells: n=1,481; p50/p90/p99/max=1/2/5/16.

Top 20 rank by rolling 7-day maximum, then rolling 24-hour maximum, lifetime actors, read-event count, and normalized path:

| # | Redacted path | Category | 24 h max | 7 d max | Lifetime actors | Read events |
|---:|---|---|---:|---:|---:|---:|
| 1 | `<home>\desktop\toolsenabled\context\systems.md` | doc | 11 | 16 | 17 | 17 |
| 2 | `<home>\desktop\toolsenabled\standing-orders.md` | doc | 6 | 16 | 24 | 29 |
| 3 | `<home>\desktop\toolsenabled\config\agent-org.json` | config | 5 | 12 | 15 | 15 |
| 4 | `<home>\desktop\toolsenabled-current\standing-orders.md` | doc | 5 | 11 | 13 | 21 |
| 5 | `<home>\desktop\toolsenabled\reports\open-gates.md` | doc | 3 | 10 | 11 | 14 |
| 6 | `<home>\desktop\portfolio dashboard\app\db.py` | source | 9 | 9 | 9 | 10 |
| 7 | `<home>\desktop\toolsenabled\build-queue.md` | doc | 4 | 8 | 9 | 18 |
| 8 | `<home>\desktop\toolsenabled-current\build-queue.md` | doc | 5 | 7 | 9 | 12 |
| 9 | `<home>\desktop\servercontrol\servers.json` | config | 6 | 6 | 7 | 7 |
| 10 | `<home>\desktop\llmbenchmarking\lean-bench\harness\models.py` | source | 6 | 6 | 6 | 6 |
| 11 | `<home>\desktop\toolsenabled-current\docs\agent-coordination-protocol.md` | doc | 6 | 6 | 6 | 6 |
| 12 | `<home>\desktop\toolsenabled-current\src\lib\mission-bridge\actions.js` | source | 3 | 6 | 7 | 22 |
| 13 | `<home>\.claude\projects\c--users-<user>-desktop-toolsenabled\memory\memory.md` | doc | 2 | 6 | 8 | 10 |
| 14 | `<home>\.claude\projects\c--users-<user>-appdata-local-temp-claude-c--users-<user>-desktop-toolsenabled-legal-b6a617e2-5c2f-4d0c-b7d7-6689f4a4b19f-scratchpad-credexp-armoff-workspace\memory\<credential-path-redacted>` | doc | 5 | 5 | 5 | 5 |
| 15 | `<home>\.claude\projects\c--users-<user>-appdata-local-temp-claude-c--users-<user>-desktop-toolsenabled-legal-b6a617e2-5c2f-4d0c-b7d7-6689f4a4b19f-scratchpad-credexp-armon-workspace\memory\<credential-path-redacted>` | doc | 5 | 5 | 5 | 5 |
| 16 | `<home>\desktop\toolsenabled-current\docs\coordinator\dispatch-queue-2026-08-13.md` | doc | 5 | 5 | 5 | 5 |
| 17 | `<home>\desktop\toolsenabled\context\portfolio-dashboard.md` | doc | 5 | 5 | 5 | 5 |
| 18 | `<home>\desktop\toolsenabled\tools\<credential-path-redacted>` | source | 3 | 5 | 5 | 5 |
| 19 | `<home>\desktop\toolsenabled-current\claude.md` | doc | 2 | 5 | 6 | 7 |
| 20 | `<home>\desktop\toolsenabled-current\scratch\r1177-first-swarm-evidence.md` | doc | 4 | 4 | 4 | 4 |

Credential-bearing paths redacted=3/20. **Shell-channel qualification:** if every recovered shell mention is treated as a possible read, 62,988 path keys rise in rolling-24-hour maximum and 63,053 rise in rolling-7-day maximum; maximum absolute lifts are 769 and 1,709 actors. This is a noisy all-recovered-mentions scenario, neither an upper nor lower bound on total shell exposure, and includes directories/non-effects.

## 5. Read-to-write intervals and claim linger

Pairs with both operations=554; literal eligible pairs whose absolute first write was not before the first read=405; first-write-before-first-read pairs=149. Literal first-read result→absolute first-write call: n=405; p50/p90/p99/max=25.755 s/58.66 min/1.54 d/3.22 d. The separately labeled first-subsequent-write sensitivity is n=521; p50/p90/p99/max=21.858 s/25.73 min/1.51 d/3.22 d.

One linger value per actor/file claim: n=3,094; p50/p90/p99/max=23.07 h/3.00 d/5.92 d/15.39 d; actor-level final write→observed end: n=122; p50/p90/p99/max=57.803 s/2.49 h/13.83 h/13.65 d. Observed end is the last timestamped record, not a close event; pauses count and live/crashed sessions are right-censored.

**Shell-channel qualification:** same-path shell mentions touch 127/405 literal eligible pairs; before first read=76, between read/write=36, after first write=108. The heuristic structured+shell read/write union yields 107,912 candidate pairs with n=107,912; p50/p90/p99/max=0.000 s/0.000 s/24.39 min/6.35 d, but shell intent is not effect evidence.

## 6. Observed concurrency

Logical actors with successful structured invocations per active UTC minute: n=12,136; p50/p90/p99/max=1/5/10/25; all deduplicated tool calls: n=20,319; p50/p90/p99/max=2/8/24/53. Raw parent `sessionId` structured sensitivity: n=12,136; p50/p90/p99/max=1/2/3/6. Primary identity remains `agentId` else `sessionId`, so parent-session aggregation does not hide sideagents.

Same current Git root, only where the structured target resolved at `2026-08-25T15:54:47.984213Z`: n=3,356; p50/p90/p99/max=1/1/2/4 over 3,356 active repo-minutes. Attribution coverage=6,448/57,610 (11.2%); unresolved event targets=51,162. Claude project-bucket proxy same-project actor counts are n=22,761; p50/p90/p99/max=1/8/23/53 for all calls.

**Shell-channel qualification:** on the union of structured-plus-shell active minutes, the shell-attributable actor-count delta is n=19,401; p50/p90/p99/max=1/5/18/52; positive in 14,666/19,401 minutes. On same-project-bucket minute cells it is n=21,240; p50/p90/p99/max=1/4/17/52; positive in 15,598/21,240 cells. This uses timestamped shell calls, not command effects, non-tool thinking time, or duration overlap.

## 7. Index cardinality

Structured localized slice: **1,392 distinct files** and **1,799 distinct file/logical-actor pairs**. Weekly new files: n=7; p50/p90/p99/max=187/443/443/443; weekly new pairs: n=7; p50/p90/p99/max=220/566/566/566. Denominator=7 ISO weeks including leading, trailing, and internal zero-growth weeks across the frozen timestamp span; boundary weeks are partial calendar coverage. Span rule: every ISO week from the first to last timestamped logical-actor record in the frozen corpus, including leading, trailing, and internal zero-growth weeks.

| ISO week | New files | Cumulative files | New file/actor pairs | Cumulative pairs |
|---|---:|---:|---:|---:|
| 2026-W29 | 4 | 4 | 4 | 4 |
| 2026-W30 | 86 | 90 | 86 | 90 |
| 2026-W31 | 215 | 305 | 340 | 430 |
| 2026-W32 | 443 | 748 | 566 | 996 |
| 2026-W33 | 415 | 1,163 | 527 | 1,523 |
| 2026-W34 | 187 | 1,350 | 220 | 1,743 |
| 2026-W35 | 42 | 1,392 | 56 | 1,799 |

**Shell-channel qualification:** treating every canonical shell mention as a possible read adds 62,437 candidate path keys and 160,410 candidate path/actor pairs, producing union totals 63,829/162,209; lifts are 4485.4%/8916.6%. These are possible endpoints, not proven reads or necessarily files.

## 8. Capture coverage

Deduplicated channel calls: Bash/PowerShell=191,764; structured Read/Edit/Write=58,250; ratio **3.292:1**. Raw copied-record occurrences are 212,511/70,557, or **3.012:1**. Thus the earlier approximate 5:1 dominance was not reproduced under either disclosed denominator. Commands with any parser path mention=176,442; commands with a canonical non-pattern path=175,336; canonical `(command,path)` mentions=454,596; mentions/parser-positive command n=176,442; p50/p90/p99/max=2/5/11/234; unresolved/glob mentions=14,382; distinct canonical shell paths=63,141; distinct shell path/actor pairs=160,806.

Parser method: quote-aware token scan; absolute/UNC, explicit relative, path-flag, common-basename, and known-extension candidates; resolves literals against recorded cwd; retains home/environment aliases as unresolved symbolic mentions; excludes URLs, switches, unresolved variables, and globs from canonical-file counts. Intent classification is heuristic and never treated as filesystem-effect proof.

Hand audit: exactly **50 commands**, disproportionately stratified to exercise Bash/PowerShell positives and negatives. Raw mention counts TP/FP/FN=31/42/65; raw recall=32.3%; raw precision=42.5%. Population-weighted point estimates: recall **35.2%**, precision **39.5%**, complete-recovery commands **16.9%**.

Weighting denominators: Bash_positive population/sample=165,253/20; Bash_negative population/sample=11,353/10; PowerShell_positive population/sample=11,189/10; PowerShell_negative population/sample=3,969/10. Weighted estimated mention totals TP/FP/FN=170,417.15/261,306.3/313,712.4; these are inverse-stratum point estimates, not observed integer effects. Reviewer=independent Codex manual audit.

Among 41 audited commands with a manual-audit-confirmed reference, raw any-recovery=24/41 (Wilson 95% 43.4%–72.2%); complete recovery=8/41 (Wilson 95% 10.2%–34.0%). Mention-level intervals are not presented because references cluster within commands.

Structured result metadata coverage by source occurrence (successful result denominator):

| Source | Tool | Successful results | Exact structured metadata | Coverage | `originalFile` key | String pre-image |
|---|---|---:|---:|---:|---:|---:|
| main | Read | 5,887 | 5,456 | 92.7% | 0 | 0 |
| main | Edit | 9,876 | 3,410 | 34.5% | 9,876 | 3,463 |
| main | Write | 5,675 | 887 | 15.6% | 5,675 | 887 |
| direct_subagent | Read | 13,244 | 0 | 0.0% | 0 | 0 |
| direct_subagent | Edit | 9,421 | 0 | 0.0% | 0 | 0 |
| direct_subagent | Write | 3,215 | 0 | 0.0% | 0 | 0 |
| workflow_subagent | Read | 12,978 | 0 | 0.0% | 0 | 0 |
| workflow_subagent | Edit | 5,733 | 0 | 0.0% | 0 | 0 |
| workflow_subagent | Write | 3,712 | 0 | 0.0% | 0 | 0 |

The earlier 99.6% Read and 98.7% Edit/Write field-presence claims could not be reproduced as corpus-wide usable-metadata rates. Main-result field/key presence is a different population; successful direct/workflow subagent results in this freeze have no exact result metadata. Captured structured event counts are incomplete as total file activity; physical-file cardinalities, percentile tails, and concurrency are not directionally bounded because aliases and proxies can also inflate them.

## 9. File-type mix

Categories are mutually exclusive: binary → lock → markdown → JSON → config → source → other. No contents are inspected; binary is extension evidence only.

| Category | Read events / share | Distinct read files / share | Write events / share | Distinct write files / share | Read events/active actor p50/p90/p99/max | Write events/active actor p50/p90/p99/max |
|---|---:|---:|---:|---:|---|---|
| source | 1,180 / 46.8% | 581 / 41.7% | 3,628 / 55.8% | 1,583 / 55.1% | n=140; p50/p90/p99/max=1/18/119/178 | n=122; p50/p90/p99/max=1/71/434/530 |
| config | 35 / 1.4% | 33 / 2.4% | 6 / 0.1% | 4 / 0.1% | n=140; p50/p90/p99/max=0/0/2/33 | n=122; p50/p90/p99/max=0/0/1/3 |
| markdown | 678 / 26.9% | 369 / 26.5% | 1,925 / 29.6% | 796 / 27.7% | n=140; p50/p90/p99/max=2/12/46/50 | n=122; p50/p90/p99/max=2/47/154/191 |
| json | 212 / 8.4% | 119 / 8.5% | 332 / 5.1% | 203 / 7.1% | n=140; p50/p90/p99/max=0/2/26/56 | n=122; p50/p90/p99/max=0/6/37/61 |
| lock | 0 / 0.0% | 0 / 0.0% | 0 / 0.0% | 0 / 0.0% | n=140; p50/p90/p99/max=0/0/0/0 | n=122; p50/p90/p99/max=0/0/0/0 |
| binary | 0 / 0.0% | 0 / 0.0% | 0 / 0.0% | 0 / 0.0% | n=140; p50/p90/p99/max=0/0/0/0 | n=122; p50/p90/p99/max=0/0/0/0 |
| other | 414 / 16.4% | 290 / 20.8% | 614 / 9.4% | 287 / 10.0% | n=140; p50/p90/p99/max=0/6/46/91 | n=122; p50/p90/p99/max=0/9/88/107 |

## 10. Session length

Logical actors (`agentId` else `sessionId`) with any deduplicated tool call=5,290. All tool calls/actor: n=5,290; p50/p90/p99/max=31/105/314/6,999. Successful Read/Edit/Write events/core-active actor: n=3,765; p50/p90/p99/max=7/36/97/941. Structured active span/core-active actor: n=3,765; p50/p90/p99/max=3.33 min/33.20 min/8.62 h/15.65 d.

Observed wall clock over every timestamped logical actor: n=5,971; p50/p90/p99/max=6.51 min/35.65 min/10.83 h/16.42 d, denominator=5,971. Raw parent sessions=875; wall clock n=875; p50/p90/p99/max=2.11 min/1.05 h/6.75 d/16.42 d; all tool calls/raw session including zeros n=875; p50/p90/p99/max=0/43/8,169/59,279. Parent sessions can aggregate many sideagents, so the logical-actor distribution is the coordination substrate's primary dispatch unit.

Shell commands/shell-active actor: n=4,865; p50/p90/p99/max=23/77/231/5,448. This quantifies how structured event counts understate the one-shot workload. Wall clock remains first-to-last observed record, includes idle gaps, and is not launch-to-close lifetime.

## Claims that could NOT be verified

- True physical byte offsets, original encodings, raw on-disk byte counts, or identity across symlinks/hardlinks/renames.

- Historical Git repository roots for targets that no longer resolve to an extant current .git ancestor.

- Actual simultaneous execution rather than co-activity within the same UTC minute.

- Explicit session close times or uncensored last-write-to-close claim linger.

- Files actually read or written by shell subprocesses, scripts, git, package managers, formatters, code generators, variables, or glob expansion.

- Reads through Grep/search, prompts, shared context, or other non-Read tools.

- That a first read caused a later write or that a claim had to remain live throughout the measured interval.

- Binary workload completeness when tools refused or omitted binary payloads.

- Event-log byte retention or byte bandwidth before the final serialized event schema is benchmarked.

- Sub-minute peak throughput or operation-overlap durations.

- The earlier approximate 5:1 shell dominance: this freeze measures 3.292:1 after deduplication and 3.012:1 over raw copied-record occurrences.

- The earlier 99.6% Read and 98.7% Edit/Write metadata figures as corpus-wide usable-metadata coverage; source-specific result shapes differ materially.

- Generalization beyond this one team, Claude Code harness, compatible-goal history, and Node-dominated workload.

## What would change this verdict

- Persist normalized repository/worktree IDs, real file IDs, encodings, raw byte ranges, and before/after hashes on every file operation.

- Emit structured shell/subprocess effects with expanded paths, read/write direction, success, and byte intervals.

- Emit explicit session start, heartbeat, close, and crash events plus operation start/end intervals.

- Capture result-side path/range/patch/pre-image metadata uniformly in main, direct-subagent, and workflow-subagent transcripts.

- Serialize and benchmark the finalized event record to convert event-count retention into bytes and sustained write bandwidth.

- Validate the shell parser on a larger independently reviewed sample, especially opaque inline-code, heredoc/here-string, variable, and glob cases.

- Replicate across teams, harnesses, languages, repository sizes, and intentionally adversarial workloads.

## Confidence by claim

| Claim | Measurement confidence | Scope confidence | Reason |
|---|---|---|---|
| `corpus_freeze` | High | `in_slice` | Sorted byte lengths were fixed before reads; exact prefixes and per-file/global SHA-256 values are recorded, with read/truncation/growth diagnostics. |
| `event_volume` | High for paired structured events; low as total file activity | `workload_default_only` | Direct timestamps and exact active-hour denominators, but shell and non-Read channels are not typed file events. |
| `read_window_sizes` | High for lines; Moderate for bytes | `workload_default_only` | Result startLine/numLines are direct; UTF-8 payload bytes are re-encoded transcript text, not disk bytes. Subagent metadata is absent. |
| `edit_region_sizes` | High in exact-preimage slice; Moderate for byte interpretation | `workload_default_only` | Patches parse and old blocks validate against same-result originalFile; most successful subagent writes lack this evidence. |
| `read_multiplicity` | High for localized lexical paths; Moderate as total reader fan-out | `workload_default_only` | Rolling/calendar windows and identities are deterministic, but shell/search exposures are omitted and aliases are unresolved. |
| `read_to_write_intervals` | Moderate-low | `workload_default_only` | Endpoints are observed, but causality is unproven, many operations lack paths, and observed end is a censored last record rather than close. |
| `observed_concurrency` | High for minute buckets; Moderate-low for true overlap/repository grouping | `workload_default_only` | Actor sets are deterministic for conflict-free post-quarantine calls; excluded ambiguous IDs may move tails. Same-minute activity is not duration overlap and historical Git-root attribution is incomplete. |
| `index_cardinality` | High in localized structured slice | `workload_default_only` | First-seen files/pairs and zero-growth ISO weeks are deterministic; completeness is limited by missing structured paths and aliases. |
| `capture_coverage` | Moderate-low for recovered shell paths; High for channel counts | `workload_default_only` | Shell/tool counts are exact after quarantine; lexical path recovery is conditioned on a 50-command manual audit and does not prove effects. |
| `file_type_mix` | Moderate | `workload_default_only` | Mutually exclusive extension/basename rules are deterministic; binary means binary-looking path, not inspected content. |
| `session_lengths` | High as observed timestamp span; Low as launch-to-close lifetime | `workload_default_only` | Spans and kept-call counts are direct after identity reconciliation and quarantine, but excluded ambiguous IDs may move call tails, idle gaps remain, and no close/crash marker exists. |
| `universal_defaults` | Not verified | `unsupported_universal` | Only one team, harness, and Node-dominated workload was measured. |

## Reproduction

```powershell
python instruments/build-params/extract_parameters.py `
  --corpus "$env:USERPROFILE\.claude\projects" `
  --freeze-manifest-input exploratory/build-params/corpus-manifest.json `
  --output exploratory/build-params/extraction.json `
  --manifest-output exploratory/build-params/corpus-manifest.json `
  --sample-output exploratory/build-params/shell-validation-sample.json
python instruments/build-params/render_parameters.py `
  --extraction exploratory/build-params/extraction.json `
  --labels exploratory/build-params/shell-validation-labels.json `
  --json-output exploratory/build-params/parameters.json `
  --report-output exploratory/build-params/PARAMETERS.md
python -m unittest instruments/build-params/test_extract_parameters.py -v
```
