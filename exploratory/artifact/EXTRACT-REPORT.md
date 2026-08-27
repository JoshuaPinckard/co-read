115 files extracted / 6 cited-but-absent / 87 credential hits (1 redacted / 0 excluded / 86 false-positive)

# Pinned engine-extract report

## Verdict

The pinned standalone artifact is complete at `artifact/engine-extract/`. It is a frozen instrument-and-subject source extract, not a runnable ToolsEnabled product. Extraction integrity and the blocking credential scan both pass. Publication is **not approved by this report**: the required final OWNER sign-off remains the publication gate.

No live ToolsEnabled checkout was read. No absent file was fetched or reconstructed. The snapshot, fixtures, prompts, and existing results were not modified. Nothing was committed or pushed.

The canonical deliverables are the [artifact README](../../artifact/engine-extract/README.md), [payload manifest](../../artifact/engine-extract/MANIFEST.json), [claim map](../../artifact/engine-extract/CLAIM-MAP.md), and exact [MIT license](../../artifact/engine-extract/LICENSE).

## Extraction inventory

| Class | Files | Bytes | Verification |
|---|---:|---:|---|
| Study-cited files in `SNAPSHOT-MANIFEST.json` | 109 | included below with the license subtotal | Every path, byte count, and SHA-256 matched the frozen manifest |
| Snapshot `LICENSE` | 1 | included below with the cited-file subtotal | Exact 1,072-byte copy; SHA-256 `e36d99bd9074ba3fb9261e75f04c99db8fd64736de5faf49bed7b15e0dea9602` |
| Main snapshot payload subtotal | 110 | 3,816,996 | 110/110 exact snapshot copies |
| `_transport-state/` observation records | 5 | 1,383,890 | Four exact copies; one documented 19-byte absolute-path redaction |
| **Frozen payload total** | **115** | **5,200,886** | **0 missing, extra, hash, size, or provenance errors** |
| Generated `README.md`, `CLAIM-MAP.md`, `MANIFEST.json` | 3 | 136,591 | Metadata, deliberately outside the frozen payload inventory |
| **Final artifact tree scanned** | **118** | **5,337,477** | **118 strict UTF-8 files; 0 binary/NUL/invalid-UTF-8 files** |

The broad scan of the three citation inputs expanded the user's expected core set to 109 present engine paths; every one was included. All 34 paths in the expected core were present and verified. The complete per-file inventory is `MANIFEST.json`: every payload entry records artifact path, source-relative path, byte size, authoritative source hash, and hash authority. The one transformed entry additionally records original source size/hash and transformed artifact size/hash.

Payload top-level inventory:

| Path group | Files | Bytes |
|---|---:|---:|
| Root (`.gitattributes`, `LICENSE`, `package.json`) | 3 | 24,012 |
| `.githooks/` | 1 | 24,593 |
| `_transport-state/` | 5 | 1,383,890 |
| `config/` | 6 | 771,649 |
| `packages/` | 7 | 50,919 |
| `src/` | 61 | 2,396,645 |
| `tests/` | 10 | 152,170 |
| `tools/` | 22 | 397,008 |

The transport-state inventory is:

- `_transport-state/git-paths.txt`
- `_transport-state/git-status-porcelain-v2.txt`
- `_transport-state/git-diff-unified-zero.patch`
- `_transport-state/git-attributes-repo-sync.txt`
- `_transport-state/git-filter-config.txt`

The last two records were included because `ENGINE-GAP.md:435` cites them in addition to the three records expressly required by the extraction brief.

### Frozen provenance

- Snapshot: `corpus/engine-audit/snapshot-20260825T141104Z/`
- Snapshot manifest SHA-256: `12e9baff91e5dd5b138a9efff55f6160a54fcfef2da626e5b1e71d1d360a9412`
- Snapshot copy interval: `2026-08-25T14:11:05.0697484Z` through `2026-08-25T14:12:56.4300065Z`
- Snapshot records: 2,114 files; changed during the one-read copy: 0
- Observed engine branch/HEAD: `main` at `42ccc286aa3f78fa39e08257e1ea5653c43f579e`
- Observation capture: `2026-08-25T14:13:57.2256479Z`
- Observation manifest SHA-256: `a33ccd6158aec1e5174583c05786de3bcc5c2ab3ad64ef7c8713f697f3a3761a`
- Study repository extraction HEAD: `98d2e46741c3679f63bf2ea928a38999abbe0160`
- Committed citation interval: `b89d8786190c470ca727b1045d76a39ab4af2796..98d2e46741c3679f63bf2ea928a38999abbe0160`, inclusive from `96590ef4506101776d81bbe6b1087723af291d2c` through the extraction HEAD (16 commits)

That Git interval does not identify every working-tree citation byte. `MANIFEST.json` therefore pins independent SHA-256 values for all three scanned study documents and records their Git status.

Independent final verification detected a post-extraction change to `paper/DRAFT-2026-08-25.md`. The extraction-time SHA-256 `48ffc5eaa43dc0c3b32ea2f835d572c7e1e3607847e7987fd1cdb2b04c239b4` remains authoritative; the manifest separately records the recheck SHA-256 `8a8f02bf976e87c00c399ca4e879f1a2d024facb7e64af6aeada81ced0b9e242`, 23,851 bytes, and last-write time `2026-08-27T00:33:06.7846122Z`. Both scans found zero direct engine path/range claims in the paper, so the extract and claim set have no delta.

### Cited but absent from the snapshot

These six paths were absent from the frozen snapshot itself. They were not fetched from anywhere else:

| Path | Citation | Consequence |
|---|---|---|
| `.codex/hooks.json` | `CORPUS.md:163` | Contents cannot be mapped |
| `config/truth-registry.json` | `CORPUS.md:149,163` | Part of C-010 remains partial |
| `docs/full-remote-access.md` | `CORPUS.md:163` | Reported skip cannot be checked against contents |
| `src/lib/providers/hosted-relay-entitlement.js` | `CORPUS.md:166` | Nonexistence remains a frozen-snapshot absence, not a content check |
| `tools/enforcement-report.js` | `CORPUS.md:149,163` | Part of C-010 remains partial |
| `tools/launch-fra-cutover-maintenance.ps1` | `CORPUS.md:163` | Reported skip cannot be checked against contents |

## Credential scan report

### Scope and stable result

The final blocking pass read every byte below `artifact/engine-extract/` after all metadata edits: **118 files / 5,337,477 bytes**. A pre/post full-file inventory fingerprint was identical at `19da3346471bc3dbe6ba97a4ed1128a55a0494302fdbbcd6d0cc6f3a4f95c34c`, demonstrating that the tree did not change during the scan.

There were **87 adjudicated candidates: 1 redacted, 0 excluded, 86 false-positive**. The final bytes contain the 86 false-positive occurrences; the 87th candidate is the source value already replaced by the recorded redaction. There are 0 unresolved hits, 0 authenticated URLs, 0 remaining personalized checkout paths, and 0 real credentials identified.

### Pattern families used

The scan combined a full-byte encoding/NUL pass with these content families:

- Private-key headers: `-----BEGIN [A-Z0-9 _-]{0,64}PRIVATE KEY-----`.
- GitHub forms `gh[pousr]_...` and `github_pat_...`; `sk-...`; AWS `AKIA`/`ASIA`; Slack `xox*`; and literal or interpolated `Bearer` authorization values.
- Expanded token forms: Google `AIza` and `ya29`, Stripe live/test secret/restricted keys, npm, PyPI, GitLab, DigitalOcean, JWT, and Basic authorization.
- Anchored optional-`export` `.env` assignments, no-space key/value assignments, and sensitive-field assignments covering password/passphrase/token/secret/API/access/refresh/identity/client/private-key/auth/cookie/CVC/CVV/security-code/PIN/card/credential/session names.
- Conventional email-address matching.
- All-scheme URL extraction followed by URI UserInfo and sensitive query-name parsing; 35 URLs were parsed. Dynamic `://...@` and sensitive query contexts were also checked.
- Windows drive-root, POSIX root, UNC, arbitrary rooted-slash, and quoted-root path sweeps.
- Sensitive filenames including `.env`, private-key/certificate stores, credential files, and secret-named files.

Zero-hit expanded families remain explicit: AWS, Slack, `github_pat_`, Google, Stripe, npm, PyPI, GitLab, DigitalOcean, JWT, Basic-auth, UNC, sensitive filenames, and URL userinfo/auth/query credentials all returned 0.

### Redaction

| File:line | Verdict | Reason and treatment |
|---|---|---|
| `_transport-state/git-paths.txt:3` | **Redact** | The captured source value disclosed non-obvious private checkout structure below the already-public user root. It was replaced with `<REDACTED:absolute-path>`. `MANIFEST.json` preserves the original observation SHA-256 and 164-byte size, the transformed SHA-256 `6493d3bd2ecd0ea33f7bd43e51cf3b4f575ebbda286ef1bf23b03100937ced3d` and 145-byte size, the line number, kind, and replacement. No source module was changed. |

No file was excluded.

### Non-path false positives — 18 hits

Every reference in this table is one separate hit; a line repeated across pattern rows contains multiple separately adjudicated matches.

| Pattern | File:line hits | Verdict and justification |
|---|---|---|
| Private-key header, 4 | `config/cloud-mirror-boundary.json:117`; `config/cloud-mirror-boundary.json:120`; `config/cloud-mirror-boundary.json:121`; `_transport-state/git-diff-unified-zero.patch:44` | **False positive.** Prose-only detector acknowledgements, deliberately unparseable header fixtures, or a named generated canary; no key body or end block is present. The patch occurrence is the captured diff copy of the acknowledgement. |
| GitHub-token shape, 5 | `config/cloud-mirror-boundary.json:116`; `config/cloud-mirror-boundary.json:118`; `tests/cloud-mirror.test.js:69`; `tests/cloud-mirror.test.js:205`; `_transport-state/git-diff-unified-zero.patch:43` | **False positive.** Pattern documentation and an intentionally fake test value used to prove scanning/redaction, plus the captured diff copy. |
| `sk-` shape, 1 | `config/cloud-mirror-boundary.json:118` | **False positive.** Explicitly labeled test sentinel in detector-acknowledgement prose. |
| Bearer shape/template, 4 | `config/cloud-mirror-boundary.json:118`; `config/cloud-mirror-boundary.json:119`; `src/lib/research/runners.js:320`; `src/lib/providers/agent-comms.js:170` | **False positive.** Two documented fake fixtures and two runtime interpolation expressions; no literal credential value is embedded. |
| Secret-style assignment, 1 | `tests/agent-comms/provider.js:276` | **False positive.** The assigned value literally states that it is not real and is used as a refusal/redaction test fixture. |
| Email address, 3 | `src/lib/cloud-agent/cloud-mirror.js:1146`; `tests/desktop.browser/browser-account-selection.test.js:11`; `tests/desktop.browser/browser-account-selection.test.js:12` | **False positive.** One fixed non-person service identity and two synthetic browser-test account fixtures; none is a secret or an undisclosed personal identity. |

### Absolute-path false positives — 68 hits

All 68 are generic interpreter locations, null/proc/tmp/container paths, module lookup roots, syntax fragments, system install paths, or obvious placeholders. They disclose no non-obvious owner-private structure.

- POSIX interpreter paths, 21 hits: `.githooks/pre-push:1` (`/bin/sh`); `src/mcp-server.js:1`, `src/playwright-gateway.js:1`, `tests/docker-precondition-refusals.test.js:1`, `tests/invocation-guard.test.js:294`, `tools/agent-contract.js:1`, `tools/agent-onboarding.js:1`, `tools/agent-territory-claim.js:1`, `tools/check-chain-runner.js:1`, `tools/cloud-lane.js:1`, `tools/cloud-mirror.js:1`, `tools/gemini-agentic-run.js:1`, `tools/gemini-fleet.js:1`, `tools/lane-territory-gate.js:1`, `tools/recall.js:1`, `tools/repo-sync.js:1`, `tools/role-sweep-runner.js:1`, `tools/run-vertex-report-wave.js:1`, `tools/tool-surface-runner.js:1`, `_transport-state/git-diff-unified-zero.patch:884`, and `_transport-state/git-diff-unified-zero.patch:895` (`/usr/bin/env`). Verdict: **false positive**, standard shebangs or their captured diff copies.
- Null-device paths, 6 hits: `.githooks/pre-push:184,262,270,326,327`; `_transport-state/git-diff-unified-zero.patch:1225`. Verdict: **false positive**, `/dev/null` shell redirection.
- Process paths, 2 hits: `src/lib/fleet-supervisor/luna-executor.js:715,718`. Verdict: **false positive**, generic `/proc` process inspection.
- Temporary/container cache paths, 8 hits: `src/lib/providers/agent-sandbox.js:447,477,597` (`/tmp`); `src/lib/providers/agent-sandbox.js:448,581,582,583` (`/home/sandbox/.cache`); `src/lib/providers/agent-sandbox.js:452` (`/home/sandbox`). Verdict: **false positive**, sandbox-local generic paths.
- Workspace paths, 8 hits: `src/lib/mission-bridge/actions.js:28,544,901`; `src/lib/providers/agent-sandbox.js:449,451,572,1343`; and the interpolated workspace construction at `src/lib/providers/agent-sandbox.js:1322`. Verdict: **false positive**, generic container workspace paths or code constructing one.
- Tool module roots, 6 hits: `src/lib/providers/agent-sandbox.js:453,459,481,1345` (`/opt/toolsenabled/node_modules`); two separate matches on `src/lib/providers/code-intel.js:218` (`/usr/local/lib/node_modules` and `/usr/lib/node_modules`). Verdict: **false positive**, generic dependency search roots.
- Rooted syntax/example fragments, 8 hits: `src/lib/action-guards.js:102` (`/dev`); `src/lib/audit-store.js:283,284` (`/data`); `tests/repo-sync.test.js:162` (`/lib`); `tests/mcp-initialize-instructions.test.js:201,285` (`/sys`); `tests/desktop.browser/browser-account-selection.test.js:48` (`/project`); `tools/check-single-copy-work.js:348` (`/repo`). Verdict: **false positive**, regex/syntax fragments and generic test roots.
- Windows paths, 9 hits: `.githooks/pre-push:144`, `src/lib/providers/code-intel.js:679`, `tools/check-single-copy-work.js:340`, and `tools/gemini-agentic-run.js:7` are generic examples/placeholders; `_transport-state/git-filter-config.txt:2,3,4,5,6` are standard Git installation paths. Verdict: **false positive**.

The false-positive subtotals reconcile exactly: 18 non-path + 59 POSIX-path + 9 Windows-path = 86.

## Claim-map coverage

One claim unit is one evidence-bearing physical Markdown line; compound clauses on that line remain together. Repeated claims remain separate. Provenance-only `ENGINE-GAP.md:23-25` was excluded. The complete row-level map validated 405 evidence references with 0 missing files and 0 out-of-bounds ranges.

| Citing document | Claims mapped / total | Verified | Partial | Defect | Wholly not extractable |
|---|---:|---:|---:|---:|---:|
| `exploratory/build-params/ENGINE-GAP.md` | **181 / 181** | 152 | 27 | 2 | 0 |
| `exploratory/harvest/CORPUS.md` | **12 / 12** | 5 | 6 | 1 | 0 |
| `paper/DRAFT-2026-08-25.md` | **0 / 0** | 0 | 0 | 0 | 0 |
| **Total direct engine-code claims** | **193 / 193** | **157** | **33** | **3** | **0** |

Six individual cited paths are absent, as itemized above. A compound claim can therefore be partial while no whole claim row is classified as wholly not extractable.

## Claims that could NOT be verified

Thirty-six direct claim rows are not fully verified: 33 are partial and 3 contain a material citation defect.

### Citation defects

- **EG-037 / `ENGINE-GAP.md:120`:** the frozen policy retains Read/Write/Edit/Glob/Grep, but Bash and PowerShell both have `keep:false`; the claim that it retains shell families is contradicted.
- **EG-169 / `ENGINE-GAP.md:552`:** host execution records exit/timed-out/output-size information, but the cited result audit contains no duration/timing field.
- **C-001 / `CORPUS.md:53`:** `agent-contract.js:211` prints the expanded brief; it cannot establish the repository-wide assertion that no later observer binds effects to an actor.

### Partial claims

- Repository-wide negative, caller-census, wiring, reachability, uniqueness, or comparative conclusions whose cited local ranges verify only the narrower mechanism or attachment seam: **EG-001–EG-007, EG-013, EG-014, EG-023, EG-025, EG-032, EG-036, EG-053, EG-057, EG-058, EG-066, EG-073, EG-094, EG-108, EG-114, and EG-179**.
- Facts dependent on deployed language support, external scheduling/configuration, inherited environment, unavailable optional tool packs, or a comparative “strongest” judgment: **EG-070, EG-120, EG-136, EG-143, and EG-181**.
- Historical outcome/count comments without linked raw task rows, a shared metric, or a disjoint population: **C-002, C-003, C-005, and C-011**.
- A comparison whose other report is outside this engine extract: **C-009**.
- A compound claim relying in part on two frozen-snapshot-absent files: **C-010**.

The paper has no direct engine path/range claim to map. Its production-corpus conclusions are indirect and are not silently promoted into direct-code verifications.

## What would change this verdict

- A new frozen snapshot containing any of the six absent paths could make their contents extractable. It would require a new artifact and manifest; the present frozen artifact cannot be retroactively supplemented from another tree.
- Linked raw harvest/task/roster records with explicit population identity and a common metric could resolve the historical-outcome partials.
- A complete, frozen importer/caller census plus deployment manifests or executed reachability evidence could resolve many tree-wide negative and production-wiring partials.
- Frozen environment evidence for schedulers, `GIT_INDEX_FILE`, language servers, and optional tool packs could resolve the corresponding external-state partials.
- Correcting the three study statements, or supplying different frozen evidence that directly supports them, could remove the three citation defects. The current snapshot does not.
- A newer paper version that adds direct engine citations would require rescanning and a new claim-map revision. The post-extraction version observed here still adds none.
- The credential verdict would change if OWNER review rejects a false-positive classification or the single redaction policy. No unresolved credential-shaped value remains in the current extract.
- Publication status changes only when the OWNER signs off. A passing scan is necessary but is not that sign-off.

## Per-claim confidence with reasons

The authoritative per-claim confidence record is the final column of [CLAIM-MAP.md](../../artifact/engine-extract/CLAIM-MAP.md). It contains one confidence judgment and a concrete reason for each of all 193 claim IDs; this report treats that table as its per-claim annex rather than duplicating 193 rows and risking divergence.

Mechanically, 191 rows begin at **High** confidence in the stated frozen-code determination. Two begin at **Medium-high**:

- **EG-162, Verified:** the entire bounded module is the support unit because the study cites it without a line range.
- **EG-181, Partial:** the module contains the claimed primitives, but “strongest” is a comparative audit judgment and the citation covers the whole module.

For partial rows, “High” applies to the narrower code fact while the reason explicitly identifies the unverified broader clause; it is not a high-confidence endorsement of that broader clause. The three defect judgments are High because the frozen cited lines directly contradict the material clause identified above. The 157 verified rows give the exact supporting extract range and why it is sufficient.

## Final owner gate

Extraction integrity: **PASS**. Credential scan: **PASS with one documented redaction and no unresolved exposure**. Claim-map coverage: **193/193 direct claims mapped**, with 33 partials and 3 defects disclosed rather than repaired or hidden.

Publication remains **BLOCKED PENDING FINAL OWNER SIGN-OFF**. This operation did not commit or push the artifact.
