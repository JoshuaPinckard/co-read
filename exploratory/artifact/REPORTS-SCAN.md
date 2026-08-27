115 files scanned, 6,610,342 bytes; hits by verdict: 7 redact / 30 exclude / 10 keep (47 grouped hits).

# Harvest-reports publication scan

## Result

**Do not publish the frozen directory unchanged.** The 114 manifest-listed payload files have a conservative file-level disposition of **95 exclude, 7 redact, and 12 keep**. No live private key, requested provider-token shape, Bearer value, credential-bearing URL, or password assignment was found. The blockers are instead dense operational telemetry, exact authorization and credential-boundary behavior, unresolved production-looking security defects, unreleased product/integration facts, stable cloud/audit identifiers, and limited personal or institutional identifiers.

The physical count is 115 because <code>MANIFEST.json</code> inventories 114 payload files but does not inventory itself. The 114 payloads total 6,479,114 bytes; <code>MANIFEST.json</code> is 131,228 bytes. All 114 manifest entries exist and match their declared size and SHA-256. The manifest has no duplicate path, missing payload, extra payload, size mismatch, or hash mismatch.

Every physical file was read byte-for-byte. Six verifier transcripts are UTF-16LE with BOM and were decoded and scanned as such; <code>MANIFEST.json</code> is UTF-8 with BOM; the rest decode as UTF-8. No bytes were skipped or undecodable. The scan covered requested lexical patterns, URL and assignment forms, email and path extraction, high-entropy identifiers, host/port/endpoint forms, and a semantic read-through of every file. Static scanning cannot prove the absence of a proprietary or deliberately low-entropy secret format; that limitation is recorded below.

“Hit” in the first line means one deduplicated finding group in the table. Repeated instances are grouped only when their sensitivity, replacement, and disposition are identical; the location cell gives the applicable files and anchors or full line range. A keep verdict applies to the quoted match itself even where a separate finding excludes the containing file.

### Credential-family check

| Family | Result |
|---|---|
| PEM/OpenSSH/private-key material | 0 |
| JWTs | 0 |
| GitHub <code>gh[pousr]_</code> values | 2 occurrences of one deliberately fake fixture; K01 |
| OpenAI-style <code>sk-</code> values | 0 |
| AWS <code>AKIA</code>/<code>ASIA</code> access IDs and paired secret assignments | 0 |
| Slack <code>xox*</code> values | 0 |
| Live Bearer values | 0; vocabulary and matching logic only |
| Credential-bearing URLs or URL userinfo | 0 |
| Live password/secret/token assignments | 0; explicit fake/empty identifiers are K03-K04 and one synthetic Basic fixture is covered by E11 |
| Plain email-address occurrences | 7, of which 4 are reserved examples and 3 require R01-R02; the institutional address also appears in URL-encoded/regex form |
| Cloud task identifiers | 23 occurrences, 17 distinct; E13 |
| Customer names | 0 identified; public providers and one institutional domain were reviewed separately |

## Full hit table

### Exclude

| ID | File:line | Quoted snippet | Verdict |
|---|---|---|---|
| E01 | <code>.gitignore:22,42,52,60,99,163-169,207,253</code> | “Machine topology is a per-installation USER SETTING”; “bearer credential for the relay we operate”; “WHICH PADDLE ACCOUNT THIS INSTALLATION BILLS AGAINST”; “full model transcripts”; “absolute paths, live ports” | **Exclude the file.** It is a compact map of secret, billing, peer, topology, transcript, named-pipe, generated-state, and live deployment locations. Line 166 also contains <code>C:\Users\owner\Desktop\ToolsEnabled</code>; if the PI overrides exclusion, replace it with <code>C:\Users\[USER]\Desktop\ToolsEnabled</code>. |
| E02 | <code>config/capability-index.json:1</code>; <code>src/lib/tool-registry.js:916,924,1299,1461,1903,2129-2140,2282-2307,2459-2460,2778-2790,3511</code> | <code>"N":268</code>; <code>"id":"host.exec"</code>; “remote peer came to enumerate 265 tools”; credential issuance, deploy, card, approval, audit, egress, and agent-spawn descriptors | **Exclude both files.** Together they disclose the complete private action surface, destructive operations, credential workflows, permissions, billing actions, providers, and historical boundary failures. |
| E03 | <code>config/settings-registry.json:11,58,946,1136,1473,1481-1489</code> | “192.168.50.1 and 192.168.50.2”; “two ports”; “peer-pinned firewall rules”; identity, API-key, elevation, provider, and direct-link settings | **Exclude the file.** It is a deployable settings, topology, firewall, and security-boundary map. The example RFC1918 addresses are not sensitive by themselves; the dense operational instructions are. |
| E04 | <code>package.json:4,37,86-87,116</code>; <code>tests/suites/orphans-wired-0824.txt:30-35</code>; <code>tests/suites/orphans-wired-w2.txt:5-10</code>; <code>tests/suites/root-suite.txt:8-20</code> | <code>"private": true</code>; <code>fra-token-enrollment-vault.js</code>; <code>full-remote-playwright-mcp-proxy.js</code>; <code>vault-live-acl.js</code> | **Exclude all four files.** They expose a private package and comprehensive unreleased security, remote-control, provider, billing, deployment, and red-team test inventories. |
| E05 | <code>logs/actions.jsonl:1-351</code>; <code>logs/actions.log:1-351</code> | <code>"eventId":"audit-ae0771f7-..."</code>; <code>"invocationId":"invocation-3b2b1f3c-..."</code>; <code>"keyId":"audit-ed25519-de729..."</code>; hashes, signatures, timestamps, targets, and outcomes | **Exclude both ledgers.** They contain 351 correlatable signed activity records, 198 invocation IDs, stable identifiers, targets, and outcomes. The Ed25519 signatures and key ID are not private-key material, but the operational ledger is sensitive. |
| E06 | <code>logs/audit-durability.json:1</code>; <code>logs/audit-emergency.jsonl.quarantine-490a252b24bfff1f:1-35</code> | <code>"code":"AUDIT_SIGNING_KEY_UNAVAILABLE"</code>; <code>"mac":null</code>; <code>"AUDIT_PROJECTION_DIVERGED"</code>; <code>"AUDIT_EMERGENCY_SPOOL_PENDING"</code> | **Exclude both files.** They disclose 74 audit durability breaches, 35 quarantined envelopes, opaque task/event identifiers, null authentication data, and exact failure modes. |
| E07 | <code>logs/clarify-gate-hook.log:1-3081</code>; <code>logs/standing-orders-hook.log:1-3173</code>; <code>logs/status-visibility-hook.log:9</code> | <code>"decision":"allow-withheld-disabled"</code>; <code>"decision":"fail-open"</code>; destructive-git override traces; <code>"context":"stdin-parse"</code> | **Exclude all three files.** These are exhaustive security-gate decision matrices and expose fail-open behavior, exact commands, configuration states, remedies, and override paths. |
| E08 | <code>logs/agent-launch-audit.log:1-56</code>; <code>logs/fra-keeper.log:31</code>; <code>logs/health-observer.log:1-64</code> | “skip: hook payload named no session”; <code>"action":"start_refused"</code>; <code>"status":"failed","code":"PROCESS_VISIBILITY_REFRESH_FAILED"</code> | **Exclude all three files.** They are timestamped runtime/launch/remote-access/observer telemetry whose production-versus-fixture provenance is not established. |
| E09 | all 14 <code>logs/linux-portability-*-after-*.json</code> files, the eight files under <code>logs/portability-{linux,windows}-{baseline,final}/</code>, and <code>logs/portability-windows-remainder.json</code>; representative anchors <code>linux-portability-linux-after-platform-gates.json:11</code>, <code>linux-portability-windows-after-native-gates.json:7-8</code>, <code>portability-linux-final/2026-08-24T12-56-17-100Z.json:4277-4278</code>, <code>portability-windows-final/2026-08-24T13-02-30-690Z.json:4810-4811</code> | “Windows DPAPI vault behavior”; <code>"file":"tests/kernel.audit/vault-hardening.js","status":"timeout"</code>; <code>"file":"tests/secret-store.js","status":"fail"</code> | **Exclude all 23 files.** They enumerate internal features and give measured pass/fail/timeout states for vault, confinement, audit, remote access, and process boundaries. Four <code>latest.json</code> files are exact byte duplicates of their timestamped counterpart and inherit the same verdict. |
| E10 | <code>logs/REPORT-root-baseline.txt:3</code>; <code>logs/REPORT-root-final.txt:2357-2412</code>; <code>logs/REPORT-root-test-body.txt:743-798</code>; <code>logs/REPORT-verifier-baseline.txt:3</code>; <code>logs/REPORT-verifier-main-explicit.txt:781-836</code>; <code>logs/REPORT-verifier-posttest-explicit.txt:1-24</code>; <code>logs/REPORT-verifier-test-body-baseline.txt:3</code> | full internal test chain; <code>https://github.com/joshuapinckard/toolsenabled</code>; <code>engine-private</code>; process failures and exact paths | **Exclude all seven raw transcripts.** Line-level redaction is impractical because they contain dense paths, stack traces, repository destinations, feature inventory, operational failures, and test outcomes. If overridden, replace the personal origin with <code>https://github.com/[USER]/toolsenabled</code> and private repository names with <code>[PRIVATE_REPOSITORY]</code>. |
| E11 | <code>REPORT-C2-settings-count.md:5,17,31,129</code>; <code>REPORT-CO1-backlog-board.md:5,69-72</code>; <code>REPORT-P1-next-contracts.md:3-5,17-18,25-33,39-47,53-75</code>; <code>logs/REPORT-verifier.md:50,58,66,133-144</code> | “old token receiving HTTP 200”; <code>Authorization: Basic base64(owner:basic-password-only-echo-739184)</code>; quote-bearing cloud target; ignored identity authorization | **Exclude all four reports.** They form an actionable playbook for unresolved credential revocation, Basic-password echo, target injection, entitlement, audit-integrity, and authorization failures. The Basic value is synthetic; if exclusion is overridden, replace it with <code>[SYNTHETIC_BASIC_CREDENTIAL]</code> and <code>[SYNTHETIC_PASSWORD]</code>. |
| E12 | <code>REPORT-H1-verify-closed.md:20-22</code>; <code>REPORT-H2-verify-claims.md:5,55,115-119,142,151-160</code> | <code>EMPTY_ARGUMENT_CELL={"status":"VERIFIED"...}</code>; “false-pass guard... actual=VERIFIED”; duplicate Gmail authorities and absent sold-entitlement implementation | **Exclude both reports.** They expose exact false-verification mechanics, confidential routing seams, refuted safety claims, and missing enforcement. |
| E13 | <code>REPORT-H3-harvest-cloud.md:7,9,15,21,30,40,56,60,71,75,86</code>; <code>REPORT-HV1-apply-engine.md:7,9,11,17,26,35,43-45</code>; <code>REPORT-MG1-conflict.md:5,9,13</code> | <code>task_e_6a8c75c5c384832197d010a3c8a8e889</code>; <code>task_e_6a8cab320a908321ac7c843ddf3e6e87</code>; <code>schedulerApprovalBypass(...)</code>; <code>context.internal === 'scheduler-runner-v1'</code> | **Exclude all three reports.** They expose 23 occurrences of 17 stable cloud-task IDs plus exact authorization, machine-routing, spawn, and patch-conflict mechanics. If overridden, replace every <code>task_e_[hex]</code> value with <code>[CLOUD_TASK_ID]</code> before a separate security review. |
| E14 | <code>REPORT-M1-engine-landing.md:7,24,31</code>; <code>REPORT-M2-engine-green.md:3-7</code> | exact lifecycle timing and “four passes and one failure”; “confinement/permission failures... entitlement pointer drift... mission-bridge assertions” | **Exclude both reports.** They reveal unreleased reliability data and current safety, authority, and release-readiness defects. |
| E15 | <code>REPORT-P2-post-cut-refactor.md:3-39</code>; <code>REPORT-P2-refactor-order.md:3-51</code> | “owner email delivery still has two authorities”; duplicate Gmail recipient resolution; surviving Telegram-shaped contracts | **Exclude both reports.** They disclose confidential outbound-routing boundaries, failure behavior, and unreleased refactor plans. |
| E16 | <code>REPORT-W10-engine-commit-prep.md:5,11-12,92-109,142-180</code>; <code>REPORT-W10-spawn-tool.md:5-9,47-51</code> | “Pinned to machine-b”; unknown approval-bypass and credential-removal verdicts; new <code>agent.spawn</code> authority codes and limits | **Exclude both reports.** They are comprehensive security/configuration lane maps and expose an unreleased recursive-delegation contract. |
| E17 | <code>REPORT-W6-docker-fixtures.md:3-45</code>; <code>REPORT-W8-docker-fixtures.md:3-24</code> | “six suites require a paired machine”; private/config fixture paths; missing <code>hosted-relay-entitlement.js</code> | **Exclude both reports.** They reveal topology assumptions, confidential/account preconditions, exact configuration paths, and a missing enforcement point. |
| E18 | <code>REPORT-W7-linux-paths.md:7</code>; <code>REPORT-W7-refusal-copy.md:5,11-21</code>; <code>REPORT-W8-enforcer-audit.md:5,7,29,36,39,49,115-120</code> | <code>host.exec</code> stopped at <code>AUDIT_SIGNING_KEY_UNAVAILABLE</code>; <code>AGENT_MESSAGE_SENDER_FORGED</code>; ignored identity and elevation controls | **Exclude all three reports.** They disclose command-execution audit boundaries, message inspection/refusal vocabulary, and unreachable or ignored controls. |
| E19 | <code>logs/REPORT-codex-audit-retention.md:14-23</code>; <code>logs/REPORT-manager-v02-w21.md:160-171</code> | exact retention symbols and live enforcement path; “projection rewrites precede archive”; projections diverge after forced failure | **Exclude both reports.** They disclose unreleased audit architecture and a reproducible integrity weakness. |
| E20 | <code>logs/REPORT-codex-cloud-dispatch.md:7-9,32-60</code>; <code>logs/REPORT-codex-cloud-worker.md:13-19,25-53</code> | cloud task stdin/account/approval contract; “real, billable, uncancellable remote work” | **Exclude both reports.** They expose private cloud dispatch, account failover, target forwarding, billing, cancellation, and guard behavior. |
| E21 | <code>logs/REPORT-codex-linux-portability.md:7,82-84</code>; <code>logs/REPORT-codex-worker.md:42-45</code>; <code>logs/REPORT-codex.md:13-16</code> | platform failure totals; literal <code>machine-a</code>/<code>machine-b</code>; loopback shipped registry; private process registry, sibling checkout, two Windows principals, cloud project ID | **Exclude all three reports.** They disclose unreleased topology, principal, process, provider, and portability facts. |
| E22 | <code>logs/REPORT-root.md:5,127</code>; <code>logs/REPORT-verifier.md:50-66,98,133-209</code>; <code>logs/REPORT-worker.md:24,37-39</code> | stale personal repository destination; <code>McNair Draft 7.28.pdf</code>; credential and target failures; session identity <code>luna</code> and manager <code>controller</code> | **Exclude all three reports.** They mix current security defects, personal metadata, repository identity, and internal agent/session routing. If overridden, replace the handle with <code>[USER]</code> and filename with <code>[PERSONAL_DOCUMENT.pdf]</code>. |
| E23 | <code>src/lib/agent-approval-policy.js:9,22,31,183-184</code>; <code>src/lib/approvals.js:7-8,29-44</code> | “ORDER IS THE SECURITY PROPERTY”; <code>const TOKEN = /^[A-Za-z0-9_-]{43}$/;</code>; <code>const MAX_TTL_SECONDS = 15 * 60;</code> | **Exclude both files.** They disclose exact authorization ordering, token shape, lifetime, hashing, and one-time-consumption mechanics. |
| E24 | <code>src/lib/cloud-agent/cloud-mirror.js:13,487-489,552,689,863</code>; <code>src/lib/cloud-agent/codex-cloud-launch.js:110,118-128</code> | credential-scanner blind spots; Bearer detector; owner binding; “three signed-in accounts”; public task URL prefix | **Exclude both files.** They reveal private publication filters, scanner blind spots, account behavior, repository binding, task-launch mechanics, and mirror/receipt checks. The role email is separately covered by R01. |
| E25 | <code>src/lib/multi-account/switcher.js:15-18</code>; <code>src/lib/providers/agent-comms.js:14,24-42,119,170,488</code> | “wrong-identity incident”; <code>RELAY_TOKEN_KEY = 'custom.link_bus_bridge_token'</code>; prior port <code>8787</code>; HMAC/Bearer relay framing and fail-open behavior | **Exclude both files.** They disclose a personal operational incident and exact cross-machine identity, topology, session, authentication, endpoint, and drain behavior. |
| E26 | <code>tests/agent-approval-policy.test.js:216-221</code>; <code>tests/agent-comms/provider.js:146-154</code>; <code>tests/agent-spawn-contract.test.js:1-70</code>; <code>tests/approval-boundary-bypasses.test.js:37-49,144,170</code> | optional tier check skipped the ceiling; no relay credential provisioned; recursive-delegation contract; “vulnerable code... reaches system.credential_remove without a token” | **Exclude all four tests.** They are direct authorization, credential, relay, scheduler, handler, mutation, and delegation defect maps. |
| E27 | <code>tests/cloud-mirror.test.js:1-515</code> | mirror-boundary tests, acknowledgement rules, credential-shape scanning, publication, and freshness checks | **Exclude the file.** The exact fake GitHub token is safe as a fixture (K01), but the complete test discloses the private mirror and scan boundary. |
| E28 | <code>tests/capability-recall-demo.js:16-19,124-126</code> | “two cases... this build still gets wrong”; “recall@3”; “false-positive”; “THIS is the honest number” | **Exclude the file pending release confirmation.** It publishes unreleased product-quality metrics, known misses, permissions, and agent-visible prompts. |
| E29 | <code>REPORT-W2-orphan-wire.md:3-24,60-147</code>; <code>REPORT-W6-orphan-wire.md:3-25,61-152</code> | seven orphan tests wired into lifecycle; 114-entry admitted-orphan manifest and reachability change | **Exclude both reports.** They map private test admission, lifecycle wiring, non-green handling, and previously unreachable security coverage. |
| E30 | <code>REPORT-CUT-manager.md:3-34</code>; <code>REPORT-L-docker-recheck.md:5-31,33-106</code>; <code>REPORT-W9-selfcheck-cleanup.md:3-56</code>; <code>REPORT-selfcheck-cleanup.md:3-52</code> | “28 tolerated entries”; “Verdict: HOLD”; new safety-proof failure and 143 failures; generated source hidden from review; dirty-tree and internal-test details | **Exclude all four reports.** They disclose current release readiness, exact failure counts, safety-test gaps, repository hygiene incidents, and private workflow/test inventory. |

### Redact

| ID | File:line | Quoted snippet | Verdict |
|---|---|---|---|
| R01 | <code>src/lib/cloud-agent/cloud-mirror.js:863</code> | <code>'-c', 'user.email=cloud-mirror@toolsenabled.ai'</code> | **Redact** to <code>user.email=cloud-mirror@example.invalid</code>. The file is excluded under E24; this is the required fallback if that decision is overridden. |
| R02 | <code>tests/desktop.browser/browser-account-selection.test.js:12,22-23,42</code>; <code>tests/multi-account-failover.test.js:24</code> | <code>acctc@ucr.edu</code>; <code>acctc%40ucr.edu</code>; matching regex form | **Redact** every form to <code>acctc@example.invalid</code>, <code>acctc%40example.invalid</code>, and <code>acctc%40example\.invalid</code>. The real institutional domain is unnecessary fixture data. |
| R03 | <code>tests/multi-account-failover.test.js:163-167,211-234</code> | “owner has three accounts”; each holds named Claude and Codex subscriptions; “a .edu carrying the school role first” | **Redact** to provider-neutral fixture rationale: <code>Use three synthetic accounts with deterministic roles to test priority and failover.</code> |
| R04 | <code>src/lib/settings-registry.js:226-234</code> | “41 of 51 entries carry enforcedBy: '' and 11 carry derivedFrom: ''” | **Redact** the measured private catalogue debt to: <code>Track absent enforcement and provenance declarations without making existing catalogue debt a load failure.</code> |
| R05 | <code>src/lib/settings.js:180-190</code> | “exactly 3 of these 51 ids appear in any executable file”; “48 controls that change nothing” | **Redact** to: <code>Report declared enforcement and provenance separately; a declaration is not proof that the enforcer runs.</code> |
| R06 | <code>tests/every-module-parses.test.js:5-17</code> | “gemini-agentic.js shipped with a SYNTAX ERROR”; advertised capability entry point threw | **Redact** the incident narrative to: <code>Compile every shipped JavaScript module without executing it so syntax failures cannot bypass text-only checks.</code> |
| R07 | <code>tools/agent-contract.js:130-140,186-200</code>; <code>tools/tool-surface-runner.js:99-111</code> | “NINE returned no diff”; “FULL process.env -- including any ANTHROPIC_API_KEY”; lowercase-case-variant escape | **Redact** metrics and defect history. In both files use: <code>Construct child environments through the shared ambient-credential scrubber before applying explicit overrides.</code> |

### Publish as-is for the quoted match

| ID | File:line | Quoted snippet | Verdict |
|---|---|---|---|
| K01 | <code>tests/cloud-mirror.test.js:69,205</code> | <code>ghp_abcdefghijklmnopqrstuvwxyz0123456789</code> | **Keep the match.** It is an obvious alphabetic placeholder created in an isolated temporary repository and the test asserts it is absent from published output. The file still has E27’s exclude verdict. |
| K02 | <code>tests/approval-boundary-bypasses.test.js:55</code>; <code>tests/desktop.browser/browser-account-selection.test.js:11</code>; <code>tests/multi-account-failover.test.js:25-26</code> | <code>owner@example.invalid</code>; <code>accta@example.com</code>; <code>acctb@example.com</code> | **Keep the matches.** They use reserved example/non-routable domains and do not identify a person. |
| K03 | <code>REPORT-W6-docker-fixtures.md:19</code>; <code>tests/agent-comms/provider.js:276</code>; <code>src/lib/cloud-agent/cloud-mirror.js:552</code> | <code>ANTHROPIC_API_KEY is not set</code>; <code>api_key=not-a-real-value</code>; <code>/^Bearer\s/i</code> | **Keep the matches.** These are an environment-variable name with no value, an explicitly fake assignment, and matching logic with no Bearer credential. File-level exclusions come from other content. |
| K04 | <code>logs/clarify-gate-hook.log:1-3081</code>; <code>tests/agent-comms/provider.js:64-75</code> | <code>test-session-r1518</code>; <code>unknown-session</code>; <code>test-link-bus-token-value</code> | **Keep as lexical token matches.** They are explicit repeated test/unknown labels, not usable session or relay tokens. The log/test remain excluded for boundary disclosure. |
| K05 | <code>MANIFEST.json:4-5</code> and deep-path occurrences in raw reports/logs | <code>C:\Users\joshp\Desktop\...</code> | **Keep.** The user expressly identified <code>C:/Users/joshp</code> as already public; all occurrences under that prefix were treated as allowed. This does not rescue files excluded for their surrounding operational detail. |
| K06 | <code>tests/multi-account-failover.test.js:18-19</code>; <code>logs/clarify-gate-hook.log</code>; <code>logs/REPORT-verifier-posttest-explicit.txt:7</code>; <code>logs/REPORT-manager-v02-w21.md:122</code> | <code>C:\repo\...</code>; <code>C:\somewhere\file.txt</code>; <code>C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe</code>; <code>/Users/...</code>, <code>/home/...</code> | **Keep the path matches.** They are synthetic repository paths, a generic OS path, or literal placeholders rather than user or live deployment paths. |
| K07 | <code>logs/fra-keeper.log:31</code>; <code>tests/agent-comms/provider.js:64-75</code>; <code>src/lib/single-source.js</code>; <code>config/settings-registry.json:1473</code> | <code>192.0.2.10</code>; <code>203.0.113.1/2</code>; <code>http://relay.test</code>; <code>127.0.0.1</code>; <code>192.168.50.1/2</code> | **Keep the endpoint matches.** TEST-NET and <code>.test</code> are reserved; loopback is generic; the RFC1918 pair is tutorial text. Containing files may still be excluded for operational detail. No scheme URL contains credentials or an explicit live port. |
| K08 | corpus-wide provider/entity review; representative <code>package.json:86-116</code> and <code>src/lib/tool-registry.js</code> | OpenAI, Codex, Anthropic, Claude, Google, Firebase, Gemini, Gmail, GitHub, Docker, DigitalOcean, Paddle, Telegram, AWS, Cloudflare, Vercel, Stripe | **Keep the names as names.** They are public vendors/integration vocabulary, not identified customers. Files are excluded where the combination discloses an unreleased roadmap or security surface. No named customer was found. |
| K09 | <code>REPORT-L-docker-recheck.md:17,19,106</code> | <code>d62c1f55c04c:/evidence/latest.json</code>; <code>18938a0bc690:/evidence/latest.json</code> | **Keep.** These are ephemeral container IDs and an internal container path, not credentials, customer identities, or reachable endpoints. |
| K10 | <code>MANIFEST.json:11</code> and every <code>files[]</code> entry | <code>"fileCount":114</code>; declared byte sizes and SHA-256 values | **Keep.** These are integrity metadata. The count is accurate for payloads but excludes the manifest itself; publication should state “114 payload files plus MANIFEST.json” to avoid ambiguity. |

## THE READ-THROUGH PACKAGE for the PI

The index is intentionally ordered by attention level rather than pathname. **EXCLUDE** means do not publish the frozen file; **REDACT** means apply the proposed replacements to a publication copy and re-scan; **KEEP** means no publication-blocking content was found in the static review. This is exactly 114 one-line payload entries. <code>MANIFEST.json</code> was scanned too, but is listed separately afterward because it is not one of its own 114 payload entries.

### Highest attention — exclude

- **HIGH / EXCLUDE** <code>.gitignore</code> — 11,099 B — enumerates ignored topology, vault, billing, peer, transcript, generated-state, and pipe artifacts.
- **HIGH / EXCLUDE** <code>REPORT-C2-settings-count.md</code> — 17,793 B — audits 68 settings and identifies identity authorization as the costliest ineffective control.
- **HIGH / EXCLUDE** <code>REPORT-CO1-backlog-board.md</code> — 12,454 B — classifies a 28-slot backlog around credential reload, identity, topology, and delivery work.
- **HIGH / EXCLUDE** <code>REPORT-CUT-manager.md</code> — 3,830 B — exposes current lifecycle tolerance counts, known failures, and release-workflow bounds.
- **HIGH / EXCLUDE** <code>REPORT-H1-verify-closed.md</code> — 8,540 B — demonstrates that empty required-input probes can be falsely labeled verified.
- **HIGH / EXCLUDE** <code>REPORT-H2-verify-claims.md</code> — 22,551 B — rechecks ten reports and separates confirmed, refuted, and untestable claims.
- **HIGH / EXCLUDE** <code>REPORT-H3-harvest-cloud.md</code> — 8,548 B — classifies eleven cloud-task returns and their retained, rejected, or reverted patches.
- **HIGH / EXCLUDE** <code>REPORT-HV1-apply-engine.md</code> — 5,707 B — evaluates six later cloud returns and records retained and reverted security-related diffs.
- **HIGH / EXCLUDE** <code>REPORT-L-docker-recheck.md</code> — 7,167 B — records a HOLD verdict, safety-proof gaps, internal test inventory, and exact failure-state deltas.
- **HIGH / EXCLUDE** <code>REPORT-M1-engine-landing.md</code> — 6,512 B — reports unreachable mechanisms and an intermittent agent-engine process timeout.
- **HIGH / EXCLUDE** <code>REPORT-M2-engine-green.md</code> — 6,652 B — lowers one known-failure baseline while retaining safety and authority failures.
- **HIGH / EXCLUDE** <code>REPORT-MG1-conflict.md</code> — 7,783 B — resolves collisions among cloud authorization, spawn, and shared-tree ownership changes.
- **HIGH / EXCLUDE** <code>REPORT-P1-next-contracts.md</code> — 15,251 B — ranks implementation briefs for authentication, injection, redaction, audit, and entitlement defects.
- **HIGH / EXCLUDE** <code>REPORT-P2-post-cut-refactor.md</code> — 15,949 B — plans owner-email, retired-transport, escaping, and digest-projection refactors.
- **HIGH / EXCLUDE** <code>REPORT-P2-refactor-order.md</code> — 19,901 B — expands the ordered confidential-delivery and escaping refactor plan.
- **HIGH / EXCLUDE** <code>REPORT-W10-engine-commit-prep.md</code> — 18,899 B — divides 184 dirty paths into landing lanes including approval and credential custody.
- **HIGH / EXCLUDE** <code>REPORT-W10-spawn-tool.md</code> — 4,594 B — documents the new agent-spawn contract, authority checks, workspace fence, and limits.
- **HIGH / EXCLUDE** <code>REPORT-W2-orphan-wire.md</code> — 7,698 B — wires seven orphan tests while retaining lifecycle failures.
- **HIGH / EXCLUDE** <code>REPORT-W6-docker-fixtures.md</code> — 10,553 B — turns missing credentials, accounts, paired machines, and services into named non-passing skips.
- **HIGH / EXCLUDE** <code>REPORT-W6-orphan-wire.md</code> — 7,599 B — reports a 114-entry internal suite manifest and reachability increase.
- **HIGH / EXCLUDE** <code>REPORT-W7-linux-paths.md</code> — 8,480 B — validates portable fixtures while documenting a Windows-only audited host-control boundary.
- **HIGH / EXCLUDE** <code>REPORT-W7-refusal-copy.md</code> — 6,254 B — adds explanations for sixteen agent-message safety and identity refusal codes.
- **HIGH / EXCLUDE** <code>REPORT-W8-docker-fixtures.md</code> — 8,047 B — names missing confidential/configuration fixtures while preserving an entitlement failure.
- **HIGH / EXCLUDE** <code>REPORT-W8-enforcer-audit.md</code> — 20,803 B — audits declared enforcers and exposes ignored or unreachable security controls.
- **HIGH / EXCLUDE** <code>REPORT-W9-selfcheck-cleanup.md</code> — 3,523 B — documents hidden generated source, cleanup hardening, and private dirty-tree/test details.
- **HIGH / EXCLUDE** <code>REPORT-selfcheck-cleanup.md</code> — 3,515 B — independently records the same repository-hygiene incident and preserved private worktree state.
- **HIGH / EXCLUDE** <code>config/capability-index.json</code> — 648,604 B — contains a generated index of 268 tool descriptors, providers, parameters, and effects.
- **HIGH / EXCLUDE** <code>config/settings-registry.json</code> — 99,539 B — contains 67 settings with risks, consequences, enforcers, and direct-link setup instructions.
- **HIGH / EXCLUDE** <code>logs/REPORT-codex-audit-retention.md</code> — 6,525 B — documents audit-retention wiring and mutation checks.
- **HIGH / EXCLUDE** <code>logs/REPORT-codex-cloud-dispatch.md</code> — 7,771 B — documents the cloud-task contract, transport, account registry, and approval route.
- **HIGH / EXCLUDE** <code>logs/REPORT-codex-cloud-worker.md</code> — 7,294 B — validates cloud dispatch and names missing account and target forwarding.
- **HIGH / EXCLUDE** <code>logs/REPORT-codex-linux-portability.md</code> — 7,565 B — records cross-platform failure censuses and remote-access lifecycle gaps.
- **HIGH / EXCLUDE** <code>logs/REPORT-codex-worker.md</code> — 9,919 B — compares builder-private and shipped-neutral registries and attributes topology failures.
- **HIGH / EXCLUDE** <code>logs/REPORT-codex.md</code> — 12,677 B — gives a broader builder-versus-shipped configuration and private-boundary review.
- **HIGH / EXCLUDE** <code>logs/REPORT-manager-v02-w21.md</code> — 9,283 B — compares lifecycle runs and demonstrates audit projection divergence.
- **HIGH / EXCLUDE** <code>logs/REPORT-root-baseline.txt</code> — 84,606 B — is a raw pretest transcript containing the complete internal check-chain inventory.
- **HIGH / EXCLUDE** <code>logs/REPORT-root-final.txt</code> — 441,154 B — is the full final lifecycle transcript with failures, paths, and repository destinations.
- **HIGH / EXCLUDE** <code>logs/REPORT-root-test-body.txt</code> — 361,710 B — is the explicit 44-step main-test transcript with internal suite failures.
- **HIGH / EXCLUDE** <code>logs/REPORT-root.md</code> — 9,385 B — summarizes lifecycle failures, registry topology, and a repository-origin mismatch.
- **HIGH / EXCLUDE** <code>logs/REPORT-verifier-baseline.txt</code> — 169,142 B — is a raw verifier pretest transcript with six failures.
- **HIGH / EXCLUDE** <code>logs/REPORT-verifier-main-explicit.txt</code> — 735,126 B — is the largest raw main-chain transcript with 29 internal failures.
- **HIGH / EXCLUDE** <code>logs/REPORT-verifier-posttest-explicit.txt</code> — 2,314 B — records provider checks and a dashboard PowerShell timeout.
- **HIGH / EXCLUDE** <code>logs/REPORT-verifier-test-body-baseline.txt</code> — 169,170 B — repeats the raw pretest baseline with different timings.
- **HIGH / EXCLUDE** <code>logs/REPORT-verifier.md</code> — 28,164 B — documents unresolved token reuse, Basic-password leakage, and cloud-target injection.
- **HIGH / EXCLUDE** <code>logs/REPORT-worker.md</code> — 5,784 B — documents internal agent identity, reporting line, session, and message routing.
- **HIGH / EXCLUDE** <code>logs/actions.jsonl</code> — 283,655 B — contains 351 signed audit events with identifiers, hashes, signatures, timestamps, and details.
- **HIGH / EXCLUDE** <code>logs/actions.log</code> — 122,337 B — contains the plaintext projection of the same 351 operational audit events.
- **HIGH / EXCLUDE** <code>logs/agent-launch-audit.log</code> — 3,416 B — records 56 exact launch-hook activity timestamps.
- **HIGH / EXCLUDE** <code>logs/audit-durability.json</code> — 16,826 B — records 74 audit durability breaches and emergency-spool state.
- **HIGH / EXCLUDE** <code>logs/audit-emergency.jsonl.quarantine-490a252b24bfff1f</code> — 28,502 B — contains quarantined task-transition envelopes with opaque IDs, null MACs, and signing errors.
- **HIGH / EXCLUDE** <code>logs/clarify-gate-hook.log</code> — 700,693 B — contains 3,081 clarify-gate decisions across mutations, configuration states, and remedies.
- **HIGH / EXCLUDE** <code>logs/fra-keeper.log</code> — 19,900 B — traces remote-access keeper starts and refusals with lifecycle outcomes.
- **HIGH / EXCLUDE** <code>logs/health-observer.log</code> — 10,944 B — records repeated process-visibility and observer failures.
- **HIGH / EXCLUDE** <code>logs/linux-portability-linux-after-backup.json</code> — 380 B — gives Linux coordinator-backup observer test outcomes.
- **HIGH / EXCLUDE** <code>logs/linux-portability-linux-after-paths.json</code> — 1,462 B — gives Linux path-portability outcomes across eleven tests.
- **HIGH / EXCLUDE** <code>logs/linux-portability-linux-after-platform-gates.json</code> — 10,714 B — names 35 Windows-only security and process tests skipped on Linux.
- **HIGH / EXCLUDE** <code>logs/linux-portability-linux-after-refusal-pin.json</code> — 235 B — records the Linux unsupported-vault-platform refusal outcome.
- **HIGH / EXCLUDE** <code>logs/linux-portability-linux-after-runner-reconciliation.json</code> — 234 B — records the Linux test-run reconciliation outcome.
- **HIGH / EXCLUDE** <code>logs/linux-portability-windows-after-backup.json</code> — 381 B — gives Windows coordinator-backup observer test outcomes.
- **HIGH / EXCLUDE** <code>logs/linux-portability-windows-after-confinement.json</code> — 238 B — gives the Windows agent-session confinement outcome.
- **HIGH / EXCLUDE** <code>logs/linux-portability-windows-after-native-gates.json</code> — 2,270 B — gives 17 native/security outcomes including failures and timeouts.
- **HIGH / EXCLUDE** <code>logs/linux-portability-windows-after-paths.json</code> — 1,471 B — gives Windows path-portability and confinement outcomes.
- **HIGH / EXCLUDE** <code>logs/linux-portability-windows-after-process-gates.json</code> — 873 B — gives Windows process-boundary and visibility-consumer outcomes.
- **HIGH / EXCLUDE** <code>logs/linux-portability-windows-after-reclassified-fra.json</code> — 227 B — records the reclassified remote-access workspace-handles outcome.
- **HIGH / EXCLUDE** <code>logs/linux-portability-windows-after-refusal-pin.json</code> — 237 B — records the Windows unsupported-vault-platform refusal outcome.
- **HIGH / EXCLUDE** <code>logs/linux-portability-windows-after-runner-reconciliation.json</code> — 236 B — records the Windows test-run reconciliation outcome.
- **HIGH / EXCLUDE** <code>logs/linux-portability-windows-after-vault-gates.json</code> — 1,669 B — gives vault/security outcomes including failures and timeouts.
- **HIGH / EXCLUDE** <code>logs/portability-linux-baseline/2026-08-24T10-51-12-803Z.json</code> — 108,280 B — inventories 759 Linux tests and their pass/fail/timeout/skip states.
- **HIGH / EXCLUDE** <code>logs/portability-linux-baseline/latest.json</code> — 108,280 B — is an exact byte duplicate of the timestamped Linux baseline.
- **HIGH / EXCLUDE** <code>logs/portability-linux-final/2026-08-24T12-56-17-100Z.json</code> — 108,795 B — inventories 763 Linux final tests and their states.
- **HIGH / EXCLUDE** <code>logs/portability-linux-final/latest.json</code> — 108,795 B — is an exact byte duplicate of the timestamped Linux final result.
- **HIGH / EXCLUDE** <code>logs/portability-windows-baseline/2026-08-24T10-38-51-372Z.json</code> — 108,889 B — inventories 759 Windows tests and their states.
- **HIGH / EXCLUDE** <code>logs/portability-windows-baseline/latest.json</code> — 108,889 B — is an exact byte duplicate of the timestamped Windows baseline.
- **HIGH / EXCLUDE** <code>logs/portability-windows-final/2026-08-24T13-02-30-690Z.json</code> — 109,266 B — inventories 763 Windows final tests and their states.
- **HIGH / EXCLUDE** <code>logs/portability-windows-final/latest.json</code> — 109,266 B — is an exact byte duplicate of the timestamped Windows final result.
- **HIGH / EXCLUDE** <code>logs/portability-windows-remainder.json</code> — 7,510 B — gives 55 Windows remainder outcomes including vault and remote-access failures.
- **HIGH / EXCLUDE** <code>logs/standing-orders-hook.log</code> — 576,756 B — contains 3,173 standing-order and destructive-git decisions including fail-open traces.
- **HIGH / EXCLUDE** <code>logs/status-visibility-hook.log</code> — 1,297 B — contains advisory output checks and an internal parse-error fail-open event.
- **HIGH / EXCLUDE** <code>package.json</code> — 22,097 B — exposes private package metadata, the internal test chain, providers, remote-access suites, and dependencies.
- **HIGH / EXCLUDE** <code>src/lib/agent-approval-policy.js</code> — 14,679 B — implements owner preferences inside a permission-tier authorization ceiling.
- **HIGH / EXCLUDE** <code>src/lib/approvals.js</code> — 1,892 B — implements one-time approval-token validation and state-store consumption.
- **HIGH / EXCLUDE** <code>src/lib/cloud-agent/cloud-mirror.js</code> — 57,646 B — implements private cloud-mirror classification, scanning, publishing, and receipt verification.
- **HIGH / EXCLUDE** <code>src/lib/cloud-agent/codex-cloud-launch.js</code> — 43,410 B — implements cloud task launch, status, diff, list, account failover, and mirror checks.
- **HIGH / EXCLUDE** <code>src/lib/multi-account/switcher.js</code> — 9,897 B — implements account health, failover, persisted routing, and active-account pin synchronization.
- **HIGH / EXCLUDE** <code>src/lib/providers/agent-comms.js</code> — 24,238 B — implements authenticated cross-machine relay topology, HMAC envelopes, and durable drains.
- **HIGH / EXCLUDE** <code>src/lib/tool-registry.js</code> — 295,034 B — contains the live 268-tool registry and dispatch chokepoint across permissions, audit, providers, and billing.
- **HIGH / EXCLUDE** <code>tests/agent-approval-policy.test.js</code> — 17,494 B — tests fail-closed preferences, tier ordering, guided behavior, and missing-ceiling defects.
- **HIGH / EXCLUDE** <code>tests/agent-comms/provider.js</code> — 14,518 B — tests relay-provider authentication and agent-message CLI behavior.
- **HIGH / EXCLUDE** <code>tests/agent-spawn-contract.test.js</code> — 2,420 B — tests recursive-delegation policy, malformed refusal, and bounded launch parameters.
- **HIGH / EXCLUDE** <code>tests/approval-boundary-bypasses.test.js</code> — 9,295 B — contains adversarial scheduler, token, handler, browser, and descriptor bypass regressions.
- **HIGH / EXCLUDE** <code>tests/capability-recall-demo.js</code> — 10,985 B — prints measured capability recall, permission filtering, known misses, and agent-visible prompts.
- **HIGH / EXCLUDE** <code>tests/cloud-mirror.test.js</code> — 23,808 B — maps mirror boundaries, credential acknowledgements, publication, and freshness checks.
- **HIGH / EXCLUDE** <code>tests/suites/orphans-wired-0824.txt</code> — 4,429 B — lists 114 admitted internal relay, enrollment, vault, remote-control, and red-team tests.
- **HIGH / EXCLUDE** <code>tests/suites/orphans-wired-w2.txt</code> — 468 B — lists seven approval, remote-access, and fail-closed boundary tests.
- **HIGH / EXCLUDE** <code>tests/suites/root-suite.txt</code> — 5,243 B — lists 154 root-suite security, provider, billing, deployment, and removed-product tests.

### High attention — redact, then re-scan

- **HIGH / REDACT** <code>src/lib/settings-registry.js</code> — 17,722 B — validates settings declarations while comments disclose measured private catalogue debt.
- **HIGH / REDACT** <code>src/lib/settings.js</code> — 10,581 B — loads runtime settings while comments quantify ineffective controls.
- **HIGH / REDACT** <code>tests/desktop.browser/browser-account-selection.test.js</code> — 4,542 B — tests Google/Firebase account routing and contains a real-domain institutional email fixture.
- **HIGH / REDACT** <code>tests/every-module-parses.test.js</code> — 5,007 B — provides a parse gate alongside a detailed previously shipped syntax-defect account.
- **HIGH / REDACT** <code>tests/multi-account-failover.test.js</code> — 22,239 B — tests routing/failover and contains an institutional email plus personal account-count history.
- **HIGH / REDACT** <code>tools/agent-contract.js</code> — 11,468 B — parses agent contracts while comments expose internal metrics and ambient-credential defect history.
- **HIGH / REDACT** <code>tools/tool-surface-runner.js</code> — 19,974 B — adapts the surface runner to CLI/worker use while comments expose ambient-credential defect history.

### Lower attention — publish as-is
- **LOW / KEEP** <code>logs/REPORT-verifier-audit-scale-isolated.exit</code> — 3 B — contains the isolated audit-scale exit code 0.
- **LOW / KEEP** <code>logs/REPORT-verifier-audit-scale-isolated.txt</code> — 138 B — records a 1,159-record audit migration pass and runtime.
- **LOW / KEEP** <code>logs/REPORT-verifier-baseline.exit</code> — 3 B — contains the verifier baseline exit code 1.
- **LOW / KEEP** <code>logs/REPORT-verifier-luna-isolated.exit</code> — 3 B — contains the isolated Luna exit code 1.
- **MED / KEEP** <code>logs/REPORT-verifier-luna-isolated.txt</code> — 3,802 B — contains an EBUSY temporary-directory cleanup stack trace under the approved user root.
- **LOW / KEEP** <code>logs/REPORT-verifier-main-explicit.exit</code> — 3 B — contains the explicit main-chain exit code 1.
- **LOW / KEEP** <code>logs/REPORT-verifier-posttest-explicit.exit</code> — 3 B — contains the posttest exit code 1.
- **LOW / KEEP** <code>logs/REPORT-verifier-test-body-baseline.exit</code> — 3 B — contains the repeated baseline exit code 1.
- **MED / KEEP** <code>logs/session-autoregister.log</code> — 17,778 B — repeats synthetic negative-test skips for absent, malformed, undefined, and traversal-shaped session payloads.
- **MED / KEEP** <code>src/lib/single-source.js</code> — 34,369 B — checks declared authorities against mirrors, generated artifacts, and discovery results.
- **MED / KEEP** <code>tests/tool-surface-runner.test.js</code> — 8,402 B — uses synthetic fixtures to test classification, reversible execution, refusals, and permission ceilings.
- **MED / KEEP** <code>tools/lib/tool-surface-runner.js</code> — 17,164 B — supplies a reusable census and execution harness for desktop, Docker, web, and mobile surfaces.

### Scanned support file, outside the 114-entry payload index

- **MED / KEEP** <code>MANIFEST.json</code> — 131,228 B — records the freeze roots, 114 payload hashes/sizes, timestamps, and copy-change provenance; state the inclusive physical count as 115.

## Claims that could NOT be verified

| Claim | Why it could not be verified | Per-claim confidence | What would change this verdict |
|---|---|---:|---|
| No secret exists in an unspecified or proprietary token format. | Exact requested formats and common variants were scanned, but arbitrary low-entropy prose and undocumented encodings have no universal signature. | **Medium, 0.80** that no credential is present; **high, 0.99** for the named lexical families. | Independent secret-manager correlation, provenance review of opaque assignments, or a second scanner with organization-specific patterns. |
| <code>acctc@ucr.edu</code> and <code>cloud-mirror@toolsenabled.ai</code> are non-live fixtures. | Both are syntactically valid addresses on real domains; no mailbox or directory lookup was attempted. | **Low, 0.30** for UCR; **low, 0.35** for the role address. | Written owner confirmation that each is intentionally public, non-personal, and sanctioned test/Git metadata; redaction remains safer. |
| The <code>task_e_...</code> values are inert and cannot retrieve or correlate cloud work. | The corpus exposes a public task URL prefix but static text cannot test reachability or access control. | **Medium, 0.65** that they are operationally sensitive; **high, 0.99** for exact occurrence counts. | Provider-side confirmation that all IDs are expired, non-enumerable, non-sensitive, and approved for disclosure. |
| Audit event IDs, invocation IDs, hashes, signatures, and key IDs cannot correlate real activity. | Their cryptographic appearance does not establish provenance, customer linkage, or irreversibility. | **Medium, 0.70** that the ledger is correlatable; **high, 0.99** for textual extraction. | Schema/provenance proof that identifiers are fixture-only or one-way and a privacy review approving the complete activity sequence. |
| The signed audit ledger is authentic. | No trusted public key, certificate provenance, or canonicalization verifier was supplied. | **Medium-high, 0.80** that authenticity remains unverified. | A trusted verification key and documented canonicalization procedure that validates every record. |
| Audit, health, launch, and hook logs are wholly synthetic. | Some use test sessions, documentation IPs, and temp roots, but several lack explicit fixture provenance and look production-derived. | **Low-medium, 0.45** for all logs being synthetic; **high, 0.95** for the clarify/session test matrices. | Reproducible isolated-fixture generation producing matching hashes with no production input. Security-boundary disclosure would still need approval. |
| The disclosed authorization, revocation, Basic-redaction, fail-open, audit, and confinement defects are fixed in every live deployment. | This was a static corpus scan; deployment/version state and coordinated-disclosure status were not checked. | **Low, 0.30** for current remediation; **medium-high, 0.85** that the text would aid an attacker if still applicable. | Versioned deployment evidence for every affected environment plus PI/security approval for coordinated disclosure. |
| Cloud mirror, multi-account failover, full remote access, AICalendar, the 268-tool registry, and named provider workflows are already public released facts. | The corpus calls the package private and contains roadmap-like tests; no public release record was supplied. | **Low, 0.35** that the whole feature set is already public. | Product-owner confirmation mapping each fact to an already public release and approving implementation-level disclosure. |
| Exact H1/H3/HV1/W8/W10 mutation, cloud-apply, timing, path-count, and restoration histories are independently reproducible. | Several reports say the prose is the only retained record or that no raw mutation/snapshot artifact survives. | **Low-medium, 0.40** for independent reproducibility; **high, 0.95** that the corpus itself lacks the cited proof. | Signed before/after trees, raw task returns, timestamped path manifests, invocation-tied transcripts, and mutation logs. |
| Docker baseline/recheck totals and every skip reason are independently verified. | The cited containers and <code>/evidence/latest.json</code> ledgers are not in this corpus. | **Medium-high, 0.80** that the claims remain unverified from this freeze. | Freeze both evidence ledgers or provide signed hashes and a durable retrieval location. |
| No customer name appears. | The semantic review found providers, institutions, account-role language, and personal metadata but no string explicitly identified as a customer; an unnamed provider could also be a commercial counterparty. | **Medium-high, 0.85**. | PI/customer-list comparison or entity-resolution review against confidential customer records. |
| “114 frozen files” means every regular file under the directory. | Direct enumeration gives 115 because the manifest does not list itself. | **High, 0.99** that the distinction is real. | Publish the wording “114 payload files plus MANIFEST.json,” or regenerate metadata with both payload and inclusive counts. |
| The recovered <code>.gitignore</code> has verified original-copy provenance. | Its manifest record has <code>sourceBefore:null</code> and says source content was not re-read. | **High, 0.95** that provenance is incomplete. | An original source snapshot or signed acquisition record matching its hash. |

## What would change this verdict

The frozen bundle can move toward publication only after all of the following applicable gates:

1. Remove the 95 **EXCLUDE** files, or obtain explicit PI plus security/product-owner approval for each specific implementation, operational log, unresolved defect, task/audit identifier, and unreleased feature it exposes.
2. Apply R01-R07 to a separate publication copy, then re-run the full byte scan because replacements can change line numbers and introduce accidental residue.
3. Obtain deployment evidence and coordinated-disclosure approval before reconsidering any file that documents an authorization, revocation, credential-redaction, fail-open, audit-integrity, confinement, or billing/cancellation boundary.
4. Obtain provider-side or owner confirmation before retaining cloud-task IDs, non-example email addresses, personal repository/document metadata, audit identifiers, or private repository names.
5. Establish fixture provenance for operational-looking ledgers and logs; synthetic provenance alone does not clear exact security-boundary behavior.
6. Confirm which integrations and capabilities are already public releases before retaining provider combinations, internal test names, registry entries, or product-quality metrics.
7. Clarify the release inventory as **114 payload files plus MANIFEST.json**.

Until those gates are met, confidence is **high (0.95)** that publishing the directory unchanged creates avoidable operational-security and privacy exposure, and **high (0.99)** that the requested named credential families contain no live value detectable in this static corpus.
