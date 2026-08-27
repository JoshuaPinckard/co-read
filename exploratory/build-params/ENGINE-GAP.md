# ENGINE redesign seam audit

## Eight-item gap table

| # | Redesign build item | Verdict in the copied tree | Wiring status | Exact first attachment seam | Estimated attachment blast radius | Confidence |
|---:|---|---|---|---|---|---|
| 1 | Actor/roster event log; byte-interval regions; materialized-byte hashes; append-only events and immutable reports | **PARTIAL primitives; composite absent** | Signed audit and mission launch/terminal events are production-reachable. A robust generic internal-VCS event store, roster JSONL, and exact hashed FRA byte reads also exist, but they are not one schema or authority. Fleet dispatch and review reports are outside that event model. | Add a required private event/region projector after the handler returns at S/src/lib/tool-registry.js:3583-3585 and before success audit/return at :3586-3587. Extend the existing mission LaunchRecord rather than creating a second launch schema; add the Fleet equivalent inside the state-locked claim path at S/src/lib/fleet-supervisor/supervisor.js:404-494. | **6-9 production files plus tests:** new event schema/store/projector; tool-registry; provider receipts; controller launch/outcome adapter; supervisor/state/review. | **High** for static code and wiring; **medium** for lifecycle rules not specified in the checkout. |
| 2 | Inverted read index; claims derived mechanically at the API layer, never agent-declared | **ABSENT as required; misleading substitutes exist** | executeTool is production-live, but records neither arguments nor results. Fleet and spawned CLIs use native filesystem/shell surfaces and report FILES-READ themselves. Search/postings indexes are for retrieval, not live claims. | Register private per-tool claim projectors at S/src/lib/tool-registry.js:590-634 and require their index append between :3585 and :3586. Propagate a unique launch/lane principal into executeTool. Then confine or instrument native CLI/shell access; the registry hook alone is incomplete. | **8-13 production files plus tests:** registry, new inverted index, MCP/context identity, repo/host/FRA adapters, agent-api policy, agent-lane and fleet lane-runner confinement/instrumentation. | **High.** |
| 3 | Write-time validation with repair and N=3 cross-agent region-alternation escalation | **ABSENT; narrow validation/CAS patterns only** | Unique-text patch checks, whole-file shared-write locks, BUILD-QUEUE CAS, and an unrelated three-attempt supervision policy exist. No generic region validator, repair loop, actor alternation counter, or escalation exists. | Run generic admission after permission/egress/approval/P13 at S/src/lib/tool-registry.js:3565 and immediately before handler dispatch at :3583. Perform the authoritative compare after acquiring each provider lock; first wrap repo.patch_file's read/validate/write in withSharedWrite. | **7-11 production files plus tests:** validator/repair/state/event modules, registry, repo-files, host-control, shared-write guard, read-index adapter, escalation reporting. | **High.** |
| 4 | Exact-tier holds for regions in a live read set; every ranked result advisory only | **ABSENT; substantial wrong-scope/test-only stubs** | Public internal-VCS services.claims throws; its implementation is process-local and path/resource-selector based. Territory/worktree leases are path-level and CLI/test-only. Roster ranking is correctly advisory but not a hold authority. | Populate the live read set at the item-2 success hook, acquire/release the exact hold atomically inside FleetSupervisor.claimNext's existing state transaction, and revalidate it under the provider write lock. Do not place authority behind roster ranking. | **8-12 production files plus tests:** read-set/hold store, registry/provider receipts, supervisor lifecycle, controller dispatch, worktree-lease replacement/adapter, reconciliation. | **High** on absence/wrong scope; **medium** on expiry semantics. |
| 5 | Identifier lookup; BM25 in shadow; Grep input narrowing | **PARTIAL** | code.* identifier/LSP lookup is production MCP. GrepSaver orientation is production-used by onboarding and direct CLI, but uses simple term containment and does not invoke/narrow Grep. One BM25 is CLI retrieval; another BM25F module has no product caller. Neither is shadow evaluation of code reads. | Introduce one retrieval orchestrator at S/tools/grepsaver-orient.js:278-287 before loadSystems/ranking, and have S/src/lib/agent-onboarding.js:1037-1051 call it. It should call identifier lookup first, record BM25 shadow output without steering, and emit concrete paths/patterns for a bounded Grep adapter. | **5-8 production/config files plus tests:** new orchestrator, GrepSaver orient, onboarding, code-intel adapter, registry/tool definition if exposed, shadow metrics/config. | **High.** |
| 6 | Silence-rate regime switch per seed file | **ABSENT** | Only a capability-recall evaluation computes false-positive/silence metrics. Its split explicitly has no seed; runtime has one absolute floor per mode, and that runtime itself is unwired. | There is no honest current production seam until item 2 identifies a seed file and item 5 owns retrieval. Add a per-seed regime selector in that orchestrator immediately after seed identity and before BM25/Grep ranking. The closest prototype-only hook is S/src/lib/capability-recall/index.js:131-138. | **3-5 production/data files plus tests:** new regime/state module, retrieval orchestrator, seed/read-index contract, evaluation fixtures and threshold tests. | **High** on absence; **medium** on the exact policy because “seed file” is not defined in code. |
| 7 | Dispatch-time contract-conflict screen; stateless per-task agents | **PARTIAL / STUBBED** | CONTRACT/1 validates syntax on agent.spawn and cloud-batch admission. Several runners use fresh processes; advisory overlap display and dead/test-only allocators exist. No atomic cross-agent region screen covers mission, research, fleet, cloud, wake/review, and direct launchers. Statelessness is path-dependent. | Canonical mission seam: widen S/src/lib/controller-launch-record.js:258-292 to carry normalized contract regions. In `createLaunch`, preserve existing agent-target and depth/fan-out checks through :823, then reserve before record construction at :825; release if record/audit persistence at :825-856 fails. S/src/lib/tool-registry.js:145-156 must stop dropping CONTRACT target. Fleet/cloud/direct paths must converge or call adapters before their first effect. | **13-21 production files plus tests:** contract schema, canonical reservation/LaunchRecord, registry, mission/research callers, lane+wake, fleet dispatch/review, cloud batch/direct, supported direct launchers, presence projection. | **High** on wiring/omissions; **medium** on provider-side state. |
| 8 | Isolated-lane harvest under tests; ~16-commit/~1-week staleness; private index; CAS ref updates | **PARTIAL / STUBBED** | Cloud mirror has exact freshness logic and a private temporary index for mirror-tree construction. Batch harvest only downloads/grades diffs. repo-sync is a direct-CLI/test receiver and is not demonstrably scheduled by shipped config. No tested integrator, threshold-budget mode, or application-level observation-bound CAS/lease exists. | Call a new isolated integrator after S/tools/cloud-lane.js:587 and before success at :588. Add an isolated-lane budget evaluator at S/src/lib/cloud-agent/cloud-mirror.js:1315-1323 while preserving exact default dispatch. Add expected-old observation binding immediately before S/tools/repo-sync.js:220 and at mirror push :1153-1163. | **About 12 production/config files plus 6-8 tests:** new integrator; batch harvest/journal/target; cloud-lane/mirror/launch; repo-sync/transport/status/task config; receiver and mirror tests. | **High** on code gaps; **medium** on external Git/filter behavior. |

## Audit basis and verdict vocabulary

This report describes one immutable copy of the live checkout, not HEAD alone and not earlier reports.

- Source observed read-only: C:/Users/joshp/Desktop/toolsenabled/engine.
- Evidence root, abbreviated **S/** below: C:/Users/joshp/Desktop/Blast-Radius/corpus/engine-audit/snapshot-20260825T141104Z/.
- Copy interval: 2026-08-25T14:11:05Z through 14:12:56Z. The manifest records 2,114 copied files and changedMidReadCount 0.
- Copied snapshot manifest: S/SNAPSHOT-MANIFEST.json. SHA-256: 12e9baff91e5dd5b138a9efff55f6160a54fcfef2da626e5b1e71d1d360a9412.
- Read-only Git observations, abbreviated **O/** below: S/_observations/. O/OBSERVATIONS-MANIFEST.json SHA-256: a33ccd6158aec1e5174583c05786de3bcc5c2ab3ad64ef7c8713f697f3a3761a.
- Observed live HEAD/main: 42ccc286aa3f78fa39e08257e1ea5653c43f579e, recorded at O/git-paths.txt:1-5. The snapshot deliberately includes uncommitted working-tree content.
- .git, state, logs, captures, scratch, vault, and node_modules were excluded from the corpus. Git status/diff/worktree/filter facts were captured separately with GIT_OPTIONAL_LOCKS=0.
- Per the boundary, no ENGINE test, build, package, or repository script was run. All line evidence below is from the copy.

Verdicts use these meanings:

- **Exists:** the required behavior, not merely a similarly named module, is reachable on a production path.
- **Partial:** one or more required behaviors are production-live, but the composite contract is not.
- **Stubbed:** a facade, declaration, test-only implementation, or unwired implementation exists.
- **Absent:** two independent static searches plus caller tracing found no implementation of the required behavior in the copied tree.

Line counts are physical copied-file line counts. The prior “about 69 files” estimate for packages/internal-vcs is stale: the copied package contains **73 files**.

## 1. Event log, actor/roster identity, byte regions, and immutable reports

### What exists

| Module | Size | Load-bearing evidence | Actual wiring |
|---|---:|---|---|
| S/src/lib/audit.js | 3,067 LOC | eventInput builds timestamp/action/target/details at :2151-2160; record is at :2425 onward; requireRecord is :2571-2576. | Production-wide audit facade. |
| S/src/lib/audit-store.js | 1,569 LOC | appendEvent at :896-937 assigns sequence, previousHash, eventHash and Ed25519 signature; verification is :1222-1320. | Production canonical audit store. |
| S/src/lib/controller-launch-record.js | 1,002 LOC | normalizeLaunchRequest requires requestingActor/targetAgentId/tier/model/objectiveRef/cap at :258-290. createLaunch at :781-866 writes actor, target, model, objective, parent/depth and durable audit receipt. | Production mission/agent.spawn launch path. |
| S/src/lib/launch-outcome.js | 263 LOC | recordTerminal at :190-254 conditionally appends a separate signed terminal event against the immutable launch. | Production mission lifecycle completion. |
| S/packages/internal-vcs/src/m1/control-store.js | 394 LOC | eventBody at :42-51; buildEvent hashes canonical bytes at :54-57; appendEvent at :251-302 uses an exclusive lock, optional expectedSnapshotId CAS, dedupe conflict check, fsync and atomic publish; snapshots/projectors are :319-365. | Strong generic substrate, but no production constructor/caller found outside the internal-VCS package/tests. |
| S/src/lib/fleet-supervisor/roster/events.js | 701 LOC | eventId is SHA-256 of kind/laneId/attempt/time at :120-125; appendEvent is a read-then-append JSONL operation at :648-660. | CLI/backfill/tests; no FleetSupervisor import. |
| S/src/lib/fleet-supervisor/roster/scoreboard.js | 455 LOC | Pure fold and mutable derived scoreboard; rebuild is exposed to the roster CLI. | CLI/backfill/tests, not dispatch authority. |
| S/src/lib/fleet-supervisor/state.js | 309 LOC | State history is capped and the file is atomically replaced at :132-155. | Production FleetSupervisor mutable state, not append-only event authority. |
| S/src/lib/providers/fra-workspace-handles.js | 615 LOC | read(args, context) at :488-563 returns offset, bytes, totalBytes, fileSha256, contentSha256 and content after stable descriptor checks at :501-538. | Production registry tool workspace.read at S/src/lib/tool-registry.js:1414-1421. |
| S/src/lib/build-queue-slice.js | 289 LOC | BUILD-QUEUE source intervals and phase hash at :137-150; interval/hash verification at :204-239. | Production/narrow BUILD-QUEUE artifact only. |

Representative load-bearing signatures, quoted from the copy, are `appendEvent(input = {}, signer = {})` at S/src/lib/audit-store.js:896, `createLaunch(input, dependencies = {})` at S/src/lib/controller-launch-record.js:781, `read(args = {}, context = {})` at S/src/lib/providers/fra-workspace-handles.js:488, and `claimNext(stateFile, {` at S/src/lib/fleet-supervisor/supervisor.js:383.

The mission route is stronger than a generic “mutable roster” description. S/src/lib/tool-registry.js:1921-1929 exposes agent.spawn; spawnSubagent requires transport-bound context.agentActor at :158-161 and calls mission actions at :179-184. S/src/lib/mission-bridge/actions.js:1129-1151 creates the signed launch before S/src/lib/mission-bridge/actions.js:1233-1246 starts the lane; :1249-1264 records terminal outcome. That is an actor-attributed, tamper-evident launch/terminal lifecycle.

It is not universal. FleetSupervisor.claimNext records laneId/itemId/supervisorId/PID in mutable state at S/src/lib/fleet-supervisor/supervisor.js:383-497 and dispatches later at :1604, without using LaunchRecord. The fleet roster event modules are not imported by the supervisor. Their JSONL event ID is not a digest of complete event bytes, a duplicate ID is not content-compared, and append has no common cross-process lock.

The production audit is a signed hash chain, but its event schema does not require an actor, launch/lane identity, dispatch roster data, canonical file, byte interval, or materialized-byte hash. Hot audit rows may be retired only after cold persistence and a signed boundary at S/src/lib/audit-store.js:1065-1161; describe this as tamper-evident archival continuity, not a literal promise that the live database never deletes a row.

The exact byte primitive is narrow. workspace.read materializes and hashes bytes, but the public API deliberately exposes an opaque handle rather than a canonical path. Whole-file repo.read_file and host.read_file return path/content/byte count without hashes or intervals. BUILD-QUEUE slices do not generalize to arbitrary files.

Reports are not immutable. S/src/lib/fleet-supervisor/review.js:189-309 writes lane.diff, copies changed files without content hashes, and writes a replaceable manifest containing the lane's own claims at :277-304.

There are also two hook-side launch recorders whose rows must not be conflated with enforced LaunchRecord. S/src/lib/agent-launch-audit.js:115-164 records delayed, gated:false observations. S/tools/agent-onboarding.js:235-309 also writes best-effort controller.agent.launch-shaped observations after start. Those are not pre-dispatch gates and do not share the full LaunchRecord schema.

### Gap and first code

The missing object is a canonical event envelope joining:

    actorPrincipal + task/launch/lane + dispatch roster
    canonical resource + [startByte,endByte)
    materialized-byte SHA-256 + read/write/repair outcome
    append sequence/hash + immutable report/evidence references

The first required code belongs after entry.handler returns at S/src/lib/tool-registry.js:3583-3585 and before auditInvocation/return at :3586-3587. It must be **required**, not best-effort: FRA provider auditing swallows failures at S/src/lib/providers/fra-workspace-handles.js:539-546 and auditInvocation catches audit failure at S/src/lib/tool-registry.js:2955-2959. If read-index/event persistence fails, the materialized read result cannot be released as if it were claimable.

workspace.read needs private provider-to-registry receipt metadata carrying canonical resource identity and [offset, offset+bytes), because neither its public arguments nor result contains a path. Put the projector/receipt on the private definition created by define at :590-634; do not add it to publicToolDescriptor at :2523-2544.

Extend the existing controller LaunchRecord for mission traffic. Add equivalent roster events inside FleetSupervisor's existing state-locked claim transaction, then project both into the same immutable event/report model.

### Safety seams to preserve

Keep tool-registry's permission-session refusal, confined-path check, action/model/identity gates, outward gate, approval/P13 checks, and mediated-executor provenance intact. Keep LaunchRecord's durable pre-spawn audit and launch-outcome's conditional parent/terminal binding. New event capture is additive behind those checks.

**Confidence:** High on code and reachability. The coordinator decision-document sentence that ClaimAuthority “is NOT a fencing authority” could not be verified because that decision document is absent from the copied corpus; the code independently supports the non-authority verdict in item 4.

## 2. Inverted read index and mechanical claims

### What exists and what does not

S/src/lib/tool-registry.js, 3,771 LOC, is the production mediation point. define is at :590-634; executeTool is :3369-3598; MCP reaches it at S/src/mcp-server.js:334-358. Private handlers are deliberately removed from public descriptors so callers cannot skip executeTool at S/src/lib/tool-registry.js:2523-2544.

Its two load-bearing signatures are exactly `function define(name, description, inputSchema, handler, options = {})` at S/src/lib/tool-registry.js:590 and `async function executeTool(name, args = {}, context = {})` at :3369. The first is the private provider-metadata seam; the second is the enforced invocation seam.

Current auditInvocation proves why claims cannot be reconstructed:

    auditInvocation(outcome, entry, startedAt, context, error, invocationId, profileHash)

At S/src/lib/tool-registry.js:2945-2956 it stores effect, provider, duration, optional requestId/invocationId/error. It stores no arguments, result, actor principal, path, region, bytes, or content hash. Its meter at :2961-2973 likewise records tool name/times/audit references only.

The current actor field is insufficient even if it were recorded. MCP passes only family-level agentActor plus request/session context at S/src/mcp-server.js:347-357. It does not carry unique agentId, launchId, laneId, or runId, so two Codex agents collapse to the same nominal actor.

The strongest mechanical source is workspace.read. repo.read_file returns {path, content, bytes} at S/src/lib/providers/repo-files.js:237-251. host.read_file returns the same shape at S/src/lib/providers/host-control.js:374-385. These can support private projectors, but none currently appends postings.

No live inverted read-set index was found. The existing inverted/vector indexes are retrieval corpora:

- S/tools/prior-work-index.js uses postings for docs/reports/context, not live actor reads.
- S/src/lib/search.js indexes file chunks for semantic query, not live read claims.
- Internal-VCS ClaimAuthority stores caller-supplied claims, not reads derived from mediated API results.

Production fleet claims are explicitly agent-declared. S/src/lib/fleet-supervisor/lane-runner.js:215-237 parses FILES-READ and FILES-CHANGED from model output. Supervisor only checks named paths exist at S/src/lib/fleet-supervisor/supervisor.js:1946-1964 and passes the raw laneClaims into review at :1987.

The registry is also not the complete current capture surface:

- Fleet lane-runner launches Gemini CLI with auto_edit in the lane worktree at S/src/lib/fleet-supervisor/lane-runner.js:252-296 and :401-409.
- S/src/lib/agent-lane.js:679-723 directly spawns Codex/Claude CLI in a worktree. It already supplies unique TOOLSENABLED_AGENT_ID/session/thread/lane-scope environment at :705-718, but those native file/shell actions do not flow through executeTool.
- S/src/lib/agent-api-policy.js:18-28 explicitly says native built-ins bypass registry permission, approval, audit, and kill-switch; its census retains native Read/Write/Edit/Glob/Grep/shell families at :144-167.
- host.exec and sandbox.exec are opaque file-touching execution routes, detailed in the capture inventory.

### First code and blast radius

Add an entry-private claimProjector contract to define, with schema-specific projectors for repo/host/FRA/code/search tools. Invoke it after successful materialization and before success is observable. Append inverted postings keyed by canonical resource and byte interval to unique actor/task/invocation identity.

That is necessary but not sufficient. The build must either:

1. confine production builders so filesystem reads/writes go through mediated tools; or
2. instrument native CLI/worktree access and explicitly label unobservable shell/provider actions opaque and unclaimable.

Do not accept agent-emitted FILES-READ as a fallback claim source. It can remain advisory diagnostics.

Keep capture behind the complete existing permission/confinement/action/identity/egress/`assertOutwardGate`/approval/P13/`schedulerApprovalBypass` chain and inside the mediated-executor provenance boundary. Do not expose claim projectors through public descriptors or release a read result when required index/event persistence fails.

**Confidence:** High. Static caller tracing establishes both the live mediator and the bypass paths.

## 3. Write validation, repair, and N=3 alternation escalation

### Existing narrow mechanisms

| Mechanism | Evidence | Why it does not satisfy item 3 |
|---|---|---|
| Exact text patch | S/src/lib/providers/repo-files.js:281-324 requires one unique oldText match, computes replacement, writes temp and renames. | Detects missing/ambiguous stale text only. No region claim, actor alternation, repair, or escalation. The read/validate/write sequence is not inside withSharedWrite. |
| Whole-file shared-write lock | S/src/lib/shared-write-guard.js:59-105 acquires a canonical-target cross-process lock. repo.write_file uses it at S/src/lib/providers/repo-files.js:269-278; host.write_file at S/src/lib/providers/host-control.js:398-405. | Whole-file write/write exclusion only. It is bypassed when fleet.concurrent_shared_writes is true at S/src/lib/shared-write-guard.js:63. |
| BUILD-QUEUE CAS | S/src/lib/build-queue-writer.js:251-280 atomicReplace rereads exact text and verifies persisted bytes; :387-452 and :477-540 use expected SHA/lock. | Strong but artifact-specific, not generic source-file validation. |
| Three-attempt supervision pattern | S/src/lib/supervision/policy.js:41-43 sets MAX_RESTARTS=3; decide escalates at :219-228. | Managed-process restart/quarantine only; no files, regions, actors, reads, writes, or repair. |

The current generic write signatures are `function patchFile({ path: relativePath, oldText, newText } = {})` at S/src/lib/providers/repo-files.js:285 and `function withSharedWrite(target, operation, dependencies = {})` at S/src/lib/shared-write-guard.js:59. Their present separation is the first concrete race to close.

No repair engine, cross-agent region-alternation record, or N=3 escalation budget was found by searches for the concepts and by tracing every generic write handler.

### Exact attachment order

At the registry, generic admission belongs after existing required authority:

1. permission and confined-path checks, S/src/lib/tool-registry.js:3422-3439;
2. schema/action/provider/model/identity/outward gates, :3458-3505;
3. approval and P13, ending at :3565;
4. **new region validation/repair admission**;
5. handler at :3583-3585.

The final compare cannot be only a registry precheck because another writer can race it. Each provider must acquire its canonical write lock, re-read the current bytes, validate the planned byte interval/content hash, perform bounded repair if allowed, append the alternation event, and only then publish. The first provider change is to wrap repo.patch_file's entire :296-324 sequence in withSharedWrite.

The N=3 counter must key overlapping region plus ordered actor transitions, not “three total failures.” Store it in the canonical event/index authority and reset it only by the redesign's explicit rule. The existing supervision policy is a reusable bounded-policy shape, not the same state machine.

### Safety seams

Do not move the validator ahead of permission/egress/P13, reinterpret schedulerApprovalBypass, weaken provider path/reparse/protected-file checks, or use the fleet.concurrent_shared_writes setting as permission to bypass exact region validation.

**Confidence:** High.

## 4. Exact-tier live-read-set holds; ranking remains advisory

### Internal-VCS ClaimAuthority: verified, but not a production hold

The copied packages/internal-vcs tree has 73 files. Its public and implementation surfaces are deliberately different:

- S/packages/internal-vcs/src/index.js:26-29 exports services.claims from services/claim-service.
- Every public claim method calls unboundService at S/packages/internal-vcs/src/services/claim-service.js:5-12.
- unboundService throws VCS_ADAPTER_UNAVAILABLE at S/packages/internal-vcs/src/unbound-service.js:5-11.
- The concrete implementation is separately exposed as implementations.m4 at S/packages/internal-vcs/src/index.js:46-49.

S/packages/internal-vcs/src/m4/claim-authority.js is 407 LOC. Its scope selectors are namespace/kind/canonicalId/ancestorIds/actions/resourceVersion at :40-63, not file byte intervals or materialized hashes. ClaimAuthority owns process-local Maps at :127-128. acquireClaim at :157-195 compares those caller-supplied scopes and issues a TTL/fence. holderId is an opaque caller string, not a transport-bound identity.

The decisive existing signature is `acquireClaim({ holderId, scope, ttlMs, policyRevisionId, expectedAbsent = true })` at S/packages/internal-vcs/src/m4/claim-authority.js:157. The durable adjacent prototype exposes `reserveLease(stateFile, proposal, { repoRoot, holderId, expectedRevision, ttlMs = DEFAULT_LEASE_TTL_MS, now = () => Date.now(), fsImpl = fs } = {})` at S/src/lib/fleet-supervisor/worktree-lease-state.js:141, but that proposal is path-level and the module is test-only/unwired.

The narrow binding at S/packages/internal-vcs/src/claim-service-binding.js:93-125 is real and callable, but no production caller was found. Its own header says the default state is process-local and does not survive restart at :35-62. Even with a shared FileControlStore, ClaimAuthority makes decisions only from its own Map at S/packages/internal-vcs/src/m4/claim-authority.js:152-174; the control store is appended after decisions and is never replayed/hydrated into claims. Two processes can therefore both grant overlapping claims. expectedAbsent is accepted at :157 but not used after destructuring. validateFence treats any UNSAFE overlap as “covered” at :225-233 rather than proving selector containment.

The known prior finding “public claims export throws” is therefore **verified**. The stronger conclusion is: the adjacent implementation is not safe to promote unchanged into a cross-process exact-region authority.

### Other path-level prototypes

- S/tools/agent-territory-claim.js, 290 LOC, compares path/glob claims at :117-170 and writes a per-agent JSON file at :174-195. Its header admits it does not lock the filesystem at :39-42. Malformed individual claim JSON is silently skipped at :141-143 despite the fail-closed comment, and read-check-write has no common lock. Only CLI/tests call it.
- S/src/lib/fleet-supervisor/worktree-lease.js, 206 LOC, normalizes path claims and rejects prefix overlap at :48-102 and :161-192. Its header calls it future coordinator integration.
- S/src/lib/fleet-supervisor/worktree-lease-state.js, 166 LOC, provides revisioned lock/CAS and reserve/heartbeat/release at :84-164, but has no non-test production importer.
- S/src/lib/fleet-supervisor/luna-executor.js, 2,009 LOC, has signed exact-path allowlists and an executor-local reservation/evidence system, including collision refusal at :1073-1108. executeLunaLane has no non-test caller. It is path/write-proposal based, evidence-root local, and has no reader lifecycle or byte intervals.
- S/tools/lane-territory-gate.js:341-347 passes NOT_APPLICABLE when no launch/presence record identifies a scoped lane. The pre-push hook converts the corresponding indeterminate code to success at S/.githooks/pre-push:338-346. This is post-write scope enforcement, not a dispatch/read hold.
- S/src/lib/shared-write-guard.js is a whole-file write/write lock, not a hold on regions present in a live read set.

### Ranking is already advisory, but unwired

S/src/lib/fleet-supervisor/roster/decisions.js is 485 LOC. It states suspension is advice only at :20-26, returns advice/null at :249-255, and returns dormant advice when allotment is absent at :285-291. No production supervisor caller was found. This is the correct authority direction—ranking must remain advisory—but it supplies no exact-tier hold.

### First code

Use the item-2 mechanical read index to maintain read-set lifecycle. Generalize the durable worktree-lease-state lock/CAS pattern into a system-wide region-hold store, rather than adopting ClaimAuthority's process-local Map or Luna's executor-private reservation.

For FleetSupervisor, acquire the hold inside claimNext's existing locked state transition before a lane record/worktree side effect. For mission, acquire it in the canonical LaunchRecord admission described in item 7. Revalidate at write time under the provider lock. Ranking can observe the same state but cannot grant, widen, or bypass a hold.

Preserve provider containment/protected-path checks, existing whole-file writer locks, territory/pre-push gates, and lifecycle terminal evidence as independent defenses. Do not route exact authority through roster scoring or the public internal-VCS stub.

**Confidence:** High on current implementations and wiring; medium on intended read-set expiry/heartbeat because the redesign summary does not define it.

## 5. Identifier lookup, shadow BM25, and Grep narrowing

### What exists

| Module | Size | Load-bearing signature/data model | Wiring and verdict |
|---|---:|---|---|
| S/src/lib/providers/code-intel.js | 1,479 LOC | `gotoDefinition` :1094, `findReferences` :1126, `documentSymbols` :1187, `workspaceSymbols` :1231, `diagnostics` :1271, and `hover` :1372 return LSP-style file locations/symbols. | Production through the `code.*` registry tools. This is the existing identifier-lookup leg. Results carry paths and line/column ranges, not materialized-byte hashes or live read claims. |
| S/tools/grepsaver-orient.js | 615 LOC | `tokenize` :89-93; `scoreAgainst` :121-128; `orient` :278-421; optional `priorWorkFor` :521-527. | Direct CLI and production onboarding. It ranks existing system/card metadata; it does not execute Grep or return a bounded Grep query plan. |
| S/src/lib/agent-onboarding.js | 1,719 LOC | Resolves and invokes `grepsaver-orient.js`.orient at :1037-1051, recording an unknown when it fails. | Production/harness onboarding route. It calls only `orient`, not the CLI-only prior-work branch. |
| S/tools/prior-work-index.js | 665 LOC | Builds a term-to-file posting map at :337 onward and queries it at :481 onward with a custom inverse-frequency score. | CLI/tooling retrieval. It is not BM25 and is not in the GrepSaver onboarding call path. |
| S/tools/retrieval/fts-index.js | 383 LOC | SQLite FTS5 query invokes `bm25(...)` at :321 and converts negative-better rank to a relevance value at :357 onward. | Used by S/tools/retrieval/index.js (354 LOC) and S/tools/recall.js (136 LOC) for ledger/agent-board retrieval. It actively orders returned results; it is neither shadow-only nor a code-search/GrepSaver ranker. |
| S/src/lib/capability-recall/score.js | 462 LOC | Header describes BM25F at :9-13; `rank` and the absolute floor are applied at :437-450. | The scoring library is used by build/prune/evaluation tooling. No product caller of S/src/lib/capability-recall/index.js `recommend`/:103-175 or `find`/:184 onward was found. If wired unchanged it would steer output, not run in shadow. |
| S/src/lib/search.js | 330 LOC | Stores root/path/mtime/size/chunkIndex/text/vector at :77-83; `indexPath` is :217-294 and `query` :297-318. | Production `search.*` registry tools. This is vector chunk retrieval, not identifier lookup or BM25. Index freshness uses mtime and size, not byte hashes. |

The current retrieval signatures show the available composition points: `async function gotoDefinition(args = {})` at S/src/lib/providers/code-intel.js:1094, `function orient(query, options = {})` at S/tools/grepsaver-orient.js:278, and `function rank(artifact, queryText, { floor, limit, allowedIds, constants } = {})` at S/src/lib/capability-recall/score.js:437.

GrepSaver's present score is simple containment: each distinct query term contained in a lower-cased haystack contributes one point at S/tools/grepsaver-orient.js:121-128. `orient` boosts identifiers/names and exact identifiers at :287-334, then returns cards. Its guidance at :392-397 tells the reader to prefer `code.*` and search before raw Grep, but no code path constructs an `rg` pattern, narrows input paths, or invokes Grep. Thus "Grep input narrowing" exists only as prose advice.

The production identifier route is real: S/src/lib/tool-registry.js:1639-1693 defines `code.status`, `code.goto_definition`, `code.find_references`, `code.document_symbols`, `code.workspace_symbols`, `code.diagnostics`, and `code.hover`; each calls code-intel. The provider advertises language-server and supplemental strategies, but the evidence does not justify treating every advertised fallback as available for every language.

No one pipeline currently performs the settled order:

    identifier lookup -> observe BM25 without steering -> construct bounded Grep inputs

The two BM25-family implementations serve different corpora and neither is shadow telemetry for this path.

### First code

Create a retrieval orchestrator at the start of S/tools/grepsaver-orient.js `orient`, after query normalization/tokenization and before `loadSystems`/card ranking at :278-287. Make S/src/lib/agent-onboarding.js:1037-1051 invoke that orchestrator through the existing dependency seam. Its contract should:

1. ask the existing `code.*`/code-intel adapter for identifier candidates;
2. compute and record BM25 candidates as observation-only data, never allowing them to add/remove/narrow production candidates;
3. emit explicit, bounded canonical roots/paths plus literal/pattern inputs for a Grep adapter; and
4. preserve an explicit unknown state when identifier or index service is unavailable.

If agents need this as a mediated runtime capability, add a registry descriptor rather than shelling out from onboarding. The common result must feed item 2's mechanical capture; code-intel line/column locations are not substitutes for byte receipts.

### Safety seams and blast radius

Do not turn score/rank into an authority and do not silently fall back from an unavailable typed lookup to an unbounded repository scan. Preserve GrepSaver's distinction between no match and unavailable orientation, and preserve code-intel's typed unavailable/error behavior.

Expected production changes: one new orchestrator/telemetry module, GrepSaver orient, onboarding, a code-intel adapter, and—only if runtime-exposed—a registry definition. Configuration/index-generation changes likely add two or three generated/config files. Tests need to prove shadow non-interference and Grep bounds.

**Confidence:** High on the static call graph and scoring behavior. Medium on deployed language-server coverage because that depends on runtime binaries/configuration not exercised under the read-only boundary.

## 6. Silence-rate regime switch per seed file

### What exists

No production object in the copied tree binds all three required concepts: a seed file identity, a measured silence rate for that seed, and a regime selector.

The closest evaluation vocabulary is test-only. S/tests/capability-recall-eval.js is 467 LOC. It defines false-positive behavior on prompts that deserve silence at :20-22, computes false positives and a silence margin at :134-170, and rejects an excessive false-positive rate at :176-184. Its split is explicitly "no shuffle, no seed" at :63-64. That seed is an evaluation randomization concept anyway, not a source/seed file.

The closest runtime threshold is global per invocation mode, not per file. S/src/lib/capability-recall/index.js:131-138 selects one absolute floor for the mode and S/src/lib/capability-recall/score.js:437-450 filters the ranked set against it. That capability-recall entry point has no product caller in the copied tree and receives prompt text, not a seed-file/read receipt.

The nearest runtime signatures are `recommend(promptText, options = {})` at S/src/lib/capability-recall/index.js:103 and `find(queryText, options = {})` at :184. Neither accepts a seed identity, history, or regime.

Two independent tree searches for `silence-rate`/`silence rate`/`per-seed`/`seed file`/`regime switch`, followed by caller tracing of all BM25-family modules, found no implementation. This item is **absent**, not merely disabled.

### First code

There is no valid standalone production attachment before items 2 and 5 exist. Item 2 must supply a canonical seed-file identity from an actual mediated read, and item 5 must own the retrieval decision. The first new line belongs in that orchestrator after seed identity is resolved and before identifier/BM25/Grep candidate ranking. Add a state/config object keyed by canonical seed identity, with explicit sample counts, regime, version, and unknown/default behavior.

The nearest prototype-only hook is the absolute-floor selection at S/src/lib/capability-recall/index.js:131-138. Reusing its shape is reasonable; wiring that dead module directly would not satisfy per-seed behavior.

The redesign excerpt does not define the observation window, minimum sample count, thresholds, hysteresis, or whether "seed file" means the first file, dispatch target, or each file that seeds a query. Those are policy inputs, not facts to infer from this tree.

Expected production changes after items 2/5: a regime/state module, retrieval orchestrator, seed/read-index contract, and configuration. Evaluation fixtures should include per-seed cold start, hysteresis, and no-data behavior.

**Confidence:** High that the implementation is absent. Medium on the proposed attachment contract because the settled design excerpt leaves seed semantics and thresholds unspecified.

## 7. Contract-conflict screen at dispatch and stateless per-task agents

### The contract that actually exists

S/tools/agent-contract.js is 216 LOC. Its CONTRACT/1 grammar at :21-29 allows `role`, `target`, `do`, `because`, `done`, `report`, and optional `api`/`allow`. Parsing starts at :72; validation at :97-116 requires only role/target/do/because/done/report and checks the declared role. It does not canonicalize the target to a repository identity, express byte intervals/read sets, bind an actor, consult a live roster, reserve anything, or compare another contract. The known prior description of this narrow schema is **verified**.

The signatures are exactly `function parse(text)` at S/tools/agent-contract.js:72 and `function validate(fields)` at :97. The canonical launch entry is `function createLaunch(input, dependencies = {})` at S/src/lib/controller-launch-record.js:781; cloud batch's independent entry is `async function admitBatch(input, { mirrorApi, registryPath = null, changedSince = null, now = null } = {})` at S/src/lib/cloud-agent/batch-target.js:403.

The uncommitted registry path validates syntax but loses the key field:

- S/src/lib/tool-registry.js:106-111 wraps parser/validator errors as AgentContractRefusal.
- `spawnSubagent(args, context, dependencies)` at :132-184 validates the contract at :137.
- It constructs the mission request at :145-156, but does not carry `fields.target`; `objectiveRef` is only a digest-derived label and `territory` is an unrelated root/objective label.
- It then calls `actions.dispatch(request)` at :184.
- The public `agent.spawn` descriptor is at :1921-1929.

Thus this path proves contract syntax was present, not that its target was admitted or conflict-free.

### Dispatch surface and wiring census

| Dispatch surface | What happens now | First conflict-screen seam |
|---|---|---|
| Registry `agent.spawn` -> mission bridge | Production-reachable. CONTRACT/1 syntax is checked, target discarded, then mission dispatch called. | Preserve target in `spawnSubagent` :145-156 and feed the canonical admission below. |
| S/src/lib/mission-bridge/actions.js `dispatch` | Production action at :1061 onward, exposed directly as `/v1/actions/dispatch` by S/src/lib/mission-bridge/server.js:56. Creates LaunchRecord at :1129, constructs/starts the lane at :1152-1246, then records terminal outcome. It does not require CONTRACT/1 or target/read regions for direct callers. | Widen S/src/lib/controller-launch-record.js `normalizeLaunchRequest` :258-292. In `createLaunch` :781-866, keep existing agent-target resolution at :813 and depth/fan-out validation at :815-823; resolve the newly carried canonical contract regions, reserve after :823 and before record construction at :825, and release the reservation if record construction or `auditApi.requireRecord`/:850-856 fails. This is the primary seam; a separate hold store and LaunchRecord are not atomic merely because both calls occur here. |
| Research agent -> mission dispatch | S/src/lib/research/runners.js `defaultDispatch` :307-332 POSTs to `/v1/actions/dispatch`; `runAgent` selects/calls it at :338-357. It sends only `{brief, objectiveRef}` at :321. Server forwards the body unchanged at S/src/lib/mission-bridge/server.js:508-516, while S/src/lib/mission-bridge/actions.js:1061-1075 requires `rootId`, `tier`, `objectiveRef`, `brief`, and `cap`. This copied path is therefore statically guaranteed to be refused before launch. | Repair this caller to supply the canonical mission request plus contract regions and let `createLaunch` perform the conflict screen. Do not add a research-only bypass. |
| Fleet build lanes | S/src/lib/fleet-supervisor/supervisor.js `claimNext` :383-497 selects and writes a mutable claim; the tick claims at :1547 and dispatches at :1604. It uses item/lane metadata and later self-reported files, not a canonical contract conflict screen. | Perform the same reservation inside `claimNext`'s locked mutation before it returns a runnable claim. Preserve the existing worktree creation, materialization/credential fencing, stale-snapshot refusal, baseline capture, and provider launch in their present order at :1842-1920; the credential/staleness gates precede provider execution, not worktree effects. |
| Fleet review lanes | `reviewTick` claims separately at S/src/lib/fleet-supervisor/supervisor.js:1737-1773. Review providers can write: S/src/lib/fleet-supervisor/review.js:972-1006 enables Codex workspace writes/Gemini auto-edit. | Screen reviewer target/read/write scope when `claimLaneForReview` reserves the review; do not classify all review as read-only. |
| Fleet planning | S/src/lib/fleet-supervisor/supervisor.js `ensurePlans` :1636-1657 calls `planning.planPhase`. The default runner is S/src/lib/fleet-supervisor/planning.js:502-506 and is invoked at :536-557. The planner receives inputs embedded in the brief and runs in a disposable scratch cwd at :540-549. | Treat this as an isolated advisory/read-only dispatch exemption from write-conflict reservation, but still require a fresh provider session and, if reads are claimed, attach read-hold/claim identity to the planning task. Do not silently count it as a build lane. |
| Cloud batch | S/src/lib/cloud-agent/batch-target.js is 473 LOC. It reuses CONTRACT/1 at :281-291 and requires a task target, but rejects only an exact lower-cased duplicate target at :296-309. `admitBatch` runs structural and source gates at :403-433. Per-task execution is S/src/lib/cloud-agent/batch-runner.js:270 onward. | Static declaration binding can occur only after all findings are refused, before `admitBatch` returns at :435. The live reservation belongs per dispatch in `batch-runner.worker`: preserve seal and pacing at :278-286, select task/account at :289-291, then reserve before the durable intent/provider call at :293-303. Terminalize/release it on every terminal outcome, including ambiguous `UNKNOWN`; reserving before source validation would leak or require rollback on admission refusal. |
| Cloud single launch | Registry `cloud.task_launch` at S/src/lib/tool-registry.js:2325-2330 accepts an optional target. Mission `/v1/actions/cloud-launch` at S/src/lib/mission-bridge/server.js:81 reaches `actions.cloudLaunch`/:2006, whose exact input omits target and calls the registry at :2112-2114; S/src/lib/cloud-agent/codex-cloud-launch.js:378-384 therefore falls back to repository root. It runs freshness at :400-426 and is not routed through CONTRACT/1/reservation. | Add target to the mission action/receipt, then reserve after target/repository/branch/freshness normalization and before provider launch. Preserve binding, freshness, confirmation, credential, and approval gates. |
| Agent lane and wake/resume | S/src/lib/agent-lane.js submits/claims/starts the task at :761-773, registers initial presence at :788-818, writes the launch spec at :823, actually spawns at :847, and heartbeats at :862-888. Wake reads an existing launch at S/src/lib/agent-wake.js:465, writes a respawn reservation record at :478-487, and starts a new lane at :490; that record is not region-conflict authority. | Bind the canonical region reservation to the immutable launch and revalidate it on wake before `launchLane`/:490. Preserve the separate duplicate-agentId presence fence and respawn record semantics. |
| Direct mutating fleet/agent CLIs | S/tools/gemini-fleet.js normalizes allowed paths at :130-181, rejects exact duplicate declarations, then creates worktrees/spawns around :328-390. S/tools/gemini-agentic-run.js:18,43-45 calls S/src/lib/providers/gemini-agentic.js `runAgenticTask`; the provider performs the kill check at :581 and source/worktree effects at :583-588. S/tools/codex-lane-dispatch.ps1 is another launcher. | Either route supported launchers through canonical admission or make each call a small adapter immediately before its first effect. For Gemini Agentic, insert after the existing kill check at :581 and before repo/worktree effects at :583. Unsupported legacy paths must fail closed, not silently bypass. |
| Direct ephemeral Luna lane | S/tools/run-luna-lane.ps1:27-28 invokes Codex with `--ephemeral` and a read-only sandbox. | Exempt it from write-conflict reservation only while those constraints remain enforced. Give it a fresh task identity and exact read holds/claims if its reads participate in the redesign. |
| Legacy/manual launchers | S/tools/cloud-lane.js has a raw query launch path around :426-438; S/tools/run-vertex-report-wave.js and S/tools/role-sweep-runner.js are direct/manual surfaces. | Inventory/deprecate explicitly or add the same adapter. Static code presence alone does not prove deployment use. |

S/src/lib/agent-onboarding.js does compute path-overlap pairs at :545-554 from presence and claim inputs and renders collision warnings around :1567-1572. It is production-used as context, but it runs after/around onboarding and only advises; it is not atomic admission. S/src/lib/agent-presence.js says observed presence is not authority at :3-5; `register` at :569-596 rejects a duplicate agentId, not territory overlap.

Stronger nearby allocators are not wired to these dispatchers. S/src/lib/fleet-supervisor/worktree-lease-state.js has durable revision/lock/CAS but no non-test importer. S/src/lib/fleet-supervisor/luna-executor.js has signed exact-path reservations but `executeLunaLane` has no non-test caller. Internal-VCS ClaimAuthority is process-local and wrong-scope as item 4 establishes.

### Statelessness verdict

"Stateless per-task" is not a system invariant.

- Codex/Claude mission lanes and several fleet paths spawn a new OS process for a task, which is a useful primitive.
- S/src/lib/agent-lane.js nevertheless persists launch/presence/checkpoint/mailbox state and explicitly supports wake/resumption of an earlier launch. A fresh process can resume old task state.
- Provider-side sessions and hosted cloud state are not observable from the copied repository.
- Direct/manual runners have different lifecycle semantics, and no central gate rejects session reuse.

The build therefore needs a precise definition: fresh process, fresh provider conversation/session, no inherited task memory, and whether durable controller checkpoint/mailbox state is allowed. The current tree only proves the first property on some paths.

### First code and safety seams

Make S/src/lib/controller-launch-record.js the canonical mission admission because it already validates actor/target/tier/model, resolves the target agent, and durably records before spawn. Add exact normalized contract regions and a reservation receipt to its request/record. After the existing depth/fan-out checks at :815-823, resolve those regions and reserve before record construction at :825. Reservation must be atomic against the live item-4 set; if record construction or durable audit at :825-856 fails, release it, and otherwise terminalize it through the existing launch outcome lifecycle. Registry, research, Fleet, cloud batch/single, review, wake, and supported direct launchers then call that authority or a shared lower-level admission module before their first effect.

Do not weaken or move around:

- the complete tool-registry permission, confinement, action/model/identity, `assertOutwardGate`, approval/P13, `schedulerApprovalBypass`, and cancellation chain at S/src/lib/tool-registry.js:3422-3570;
- LaunchRecord's pre-spawn durable audit and child-cap/parent validation;
- mission and Fleet kill switches, provider binding, credential rescue, stale-snapshot refusals, review independence, and batch seal/intent checks;
- cloud mirror freshness and credential/exposure classification; or
- the duplicate-agentId registration fence and territory observations as independent evidence, without treating either as region-conflict authority.

Estimated production blast radius is 13-21 files because there are multiple non-converged dispatchers, including the currently incompatible research caller. The smallest safe sequence is canonical request/reservation first, registry target propagation and research-call repair second, then mission/Fleet/cloud adapters, then supported direct launchers, explicit read-only exemptions, and explicit deprecation/refusal for the rest.

**Confidence:** High on the schema, call graph, missing target propagation, and absence of a common screen. Medium on end-to-end statelessness because hosted provider/session behavior and deployment invocation are external to the snapshot.

## 8. Isolated-lane harvest, staleness budget, private index, and CAS refs

### Existing cloud-lane pieces

S/src/lib/cloud-agent/cloud-mirror.js is 1,391 LOC and is the strongest production-adjacent substrate:

Its load-bearing entries are `async function publishMirror({` at S/src/lib/cloud-agent/cloud-mirror.js:1086 and `async function checkMirrorFreshness({` at :1272. Harvest enters through `function readWave(journalFile, fsImpl)` and `async function harvestBatch({ journalFile, outDir, fetchTask, fsImpl = fs, concurrency = 4 })` at S/src/lib/cloud-agent/batch-harvest.js:57/:127. The protected-main receiver enters through `function runOnce(deps = {})` at S/tools/repo-sync.js:101 and the transport mutation is `fastForward()` at S/packages/internal-vcs/src/m6/protected-main-receiver-transport.js:131.

- It publishes a filtered tree made from **committed blobs**, not the source worktree. `selectMirrorEntries` at :445-460 and `assertSelectable` at :463-480 refuse non-regular, unclassified, and empty selections. `scanBlobsForCredentials` at :502-552 reads selected committed Git blob payloads through `git cat-file --batch`, skips NUL-containing binary payloads, UTF-8 decodes the rest, and refuses credential-shaped content. It does not inspect post-checkout/smudge materialization.
- `temporaryIndexFile`/:569-570 and `buildMirrorTree`/:588-600 set `GIT_INDEX_FILE` for `update-index` and `write-tree`. The real checkout index is not used by this tree-building operation; only additive objects enter the shared object database.
- `registerMirrorProject`/:912-996 records a binding plus named verification results and publishes registry JSON by temp file/rename at :983-986. `verifyRegistration`/:774-885 checks local checkout, boundary presence/syntax, optional remote reach/dry-run push, and privacy only when `providerVisibility` is supplied. With `--no-network`, reach/write are NOT CHECKED; environment-to-repository matching remains UNVERIFIED in this module. The registry write has no common lock or expected-old revision, so concurrent registrations can lose an update.
- Publication receipts are replaceable JSON. `recordPublication`/:1038-1048 writes latest plus a bounded, de-duplicated history; it is not append-only or immutable.
- `publishMirror`/:1086 onward classifies/scans, refuses an unaccounted remote head as `CLOUD_MIRROR_UNHARVESTED_WORK` at :1119-1129, creates a commit, then pushes an exact commit-ID refspec at :1153-1163 and reads the remote ref back at :1166-1174. The normal push is non-forced, but `supersede` uses `--force`, not `--force-with-lease` or another expected-old CAS.
- `checkMirrorFreshness`/:1272-1362 verifies a receipt or commit-trailer witness, requires publication.sourceCommit to equal the configured source checkout's current `HEAD` at :1315-1323, and compares the current boundary-selected tree ID at :1325-1348.

The freshness refusal is real in the copied source path, but its single-launch wiring is **uncommitted in this snapshot**: O/git-diff-unified-zero.patch:289-321 adds the whole S/src/lib/cloud-agent/codex-cloud-launch.js:400-426 block. A process loading this working-tree version verifies provider repository binding and calls `checkMirrorFreshness` before transport/task creation; committed/shipped activation is not established. Batch admission calls the same logic at S/src/lib/cloud-agent/batch-target.js:323-352, but that batch path is reached through direct S/tools/cloud-lane.js while `cloud-lane` is labelled unregistered in S/config/capability-features.json:72-74.

The current rule is exact equality, not the redesign's approximately 16-commit/one-week budget. It computes a diagnostic count for `publication.sourceCommit..localHead` but no elapsed time; distance is null only if that diagnostic Git command fails, not merely because commits diverge. More importantly, the checked base/branch is not the dispatched base/branch. Single launch validates `request.branch` at S/src/lib/cloud-agent/codex-cloud-launch.js:376 and dispatches it at :456-462, but calls freshness with only `{ cloudRepository }` at :424-426. Batch dispatch uses `provider.branch` at S/tools/cloud-lane.js:537-551 while `gateMirror` supplies only projectKey at S/src/lib/cloud-agent/batch-target.js:323-338. `against.publishedCommit` is not proved to be that provider branch's head. The current gate can therefore prove the registered mirror branch/current local checkout relationship while the task is sent to a different branch.

S/src/lib/cloud-agent/batch-harvest.js is 187 LOC. `readWave`/:57-82 parses launched tasks and targets from journal JSONL. `harvestBatch`/:127-184 clamps concurrency to 1-16, retries provider 429s after 2/6/15/40 seconds, records `PENDING`, `THROTTLED`, or `FETCH_ERROR` per task without failing the whole wave, writes only non-empty `.diff` files, syntactically grades touched paths, and writes a manifest. Module refusal codes are `CLOUD_HARVEST_NO_JOURNAL`, `CLOUD_HARVEST_NOTHING_LAUNCHED`, and `CLOUD_HARVEST_MISCONFIGURED`; S/tools/cloud-lane.js:369 can also throw `CLOUD_HARVEST_FETCH`. It does **not** create an isolated worktree/index, apply diffs, run tests, perform mutation sampling, commit, or update any integration ref. `readWave`/`harvestBatch` have only S/tools/cloud-lane.js:574/:580/:587 as non-module callers and no test importer in the copied tree.

Execution isolation exists elsewhere but is not a harvest integrator. FleetSupervisor creates a per-lane worktree after its first dispatch effects around S/src/lib/fleet-supervisor/supervisor.js:1821-1842, applies a stale-snapshot refusal at :1887-1906, and retains reviewable worktrees for separate review. S/src/lib/fleet-supervisor/luna-executor.js provides a stronger signed exact-path reservation/evidence lane, but `executeLunaLane` has no non-test caller. Neither path consumes cloud batch diffs into a tested integration ref.

There is a concrete declaration/consumer mismatch. S/src/lib/cloud-agent/batch-target.js:225-267 admits `harvest.branch` (default `harvest/batch`) and `mutationSample` (default 5). S/src/lib/cloud-agent/batch-journal.js:73-88 seals that spec in the ADMITTED header. `batch-harvest.readWave` ignores the header and returns only launched rows/targets, so neither setting controls harvest behavior. Comments claiming the harvester consumes the header are contradicted by code.

Cloud-mirror consumers are split:

- S/tools/cloud-mirror.js implements `plan`/:127-175, `publish`/:178-219, `list`/:222-227, `register`/:230-246, and `check`/:249-255, with direct CLI dispatch at :258-270. The package aliases for plan/publish/check at S/package.json:129-131 are uncommitted; list/register have no alias.
- S/src/lib/mission-bridge/actions.js imports the mirror at :21, exposes list at :1780-1794 and register at :1797-1872 (call :1842-1855); S/src/lib/mission-bridge/server.js:55-81 exposes those action routes.
- `publishMirror` has CLI/tests but no registry or mission production caller. `checkMirrorFreshness` is called by the CLI check, the uncommitted single-launch default, and the batch gate.
- Batch harvest remains only the direct cloud-lane path described above.

The cloud-mirror module's refusal vocabulary is: `CLOUD_MIRROR_ALREADY_REGISTERED`, `CLOUD_MIRROR_BOUNDARY_ABSENT`, `CLOUD_MIRROR_BOUNDARY_DRIFTED`, `CLOUD_MIRROR_BOUNDARY_INVALID`, `CLOUD_MIRROR_BOUNDARY_MISSING`, `CLOUD_MIRROR_BRANCH_ABSENT`, `CLOUD_MIRROR_BRANCH_MALFORMED`, `CLOUD_MIRROR_CREDENTIAL_IN_PAYLOAD`, `CLOUD_MIRROR_EMPTY_SELECTION`, `CLOUD_MIRROR_GIT_FAILED`, `CLOUD_MIRROR_GIT_UNAVAILABLE`, `CLOUD_MIRROR_INPUT_INVALID`, `CLOUD_MIRROR_NONREGULAR_REFUSED`, `CLOUD_MIRROR_NOT_REGISTERED`, `CLOUD_MIRROR_PROJECT_KEY_MALFORMED`, `CLOUD_MIRROR_PUBLICATION_UNKNOWN`, `CLOUD_MIRROR_PUSH_FAILED`, `CLOUD_MIRROR_PUSH_UNCONFIRMED`, `CLOUD_MIRROR_RECEIPT_INVALID`, `CLOUD_MIRROR_REGISTRATION_INCOMPLETE`, `CLOUD_MIRROR_REGISTRY_INVALID`, `CLOUD_MIRROR_REMOTE_NOT_WRITABLE`, `CLOUD_MIRROR_REMOTE_UNREACHABLE`, `CLOUD_MIRROR_REMOTE_UNREADABLE`, `CLOUD_MIRROR_REPOSITORY_ALREADY_BOUND`, `CLOUD_MIRROR_REPOSITORY_MALFORMED`, `CLOUD_MIRROR_REPOSITORY_NOT_PRIVATE`, `CLOUD_MIRROR_SOURCE_ROOT_ABSENT`, `CLOUD_MIRROR_SOURCE_ROOT_NOT_A_CHECKOUT`, `CLOUD_MIRROR_STALE`, `CLOUD_MIRROR_UNCLASSIFIED`, and `CLOUD_MIRROR_UNHARVESTED_WORK`. S/tools/cloud-mirror.js:38-65/:264-269 does not map every one to its documented exit-3 registration/refusal class; notably `CLOUD_MIRROR_REPOSITORY_NOT_PRIVATE` falls through to exit 1. Several boundary/Git/input/push/receipt/registry failures are also absent from the explicit mapping.

The first isolated-integration line goes after S/tools/cloud-lane.js:587 returns fetched artifacts and before :588 reports success. A new integrator should read and verify the journal header, materialize each candidate in a private index plus disposable worktree, apply in a deterministic order, run the required target tests and declared mutation sample, and publish only an evidence-bound successful integration through expected-old CAS. The present harvester should remain a collector/grader, not quietly become an in-place checkout editor.

### Dedicated repo-sync receiver audit

#### Modules and actual consumers

| Module | Size | Role and reachability |
|---|---:|---|
| S/tools/repo-sync.js | 287 LOC | Protected-main state machine and direct CLI entry at :283-286. Imported by S/tests/repo-sync.test.js. No production JavaScript caller or package-script invocation was found. |
| S/packages/internal-vcs/src/m6/protected-main-receiver-transport.js | 145 LOC | Typed Git adapter used by repo-sync at :15/:61-66 and its own tests. Header :3-7 explicitly disclaims admission, claim, policy, receipt, and consumption attestation. |
| S/tools/check-single-copy-work.js | 853 LOC | Commit/worktree/branch/uncommitted containment detector. repo-sync calls it with `network:true, strictUncommitted:true` at :180-185; it also has a direct CLI and tests. |
| S/tools/repo-sync-task.ps1 | 127 LOC | Windows scheduled-task registrar. It requires `config/managed-processes.json.processes.repo-sync` at :31-40 and registers the task at :91-124. The copied shipped registry has only dashboard/jarvis/uac-delegation-helper/fleet-supervisor, so this registrar refuses unless external setup has replaced the file. |
| S/src/lib/repo-sync-status.js | 290 LOC | Read-only projection of `state/repo-sync.json`; default maximum record age is 15 minutes at :13. `readRepoSyncStatus` is :181-279. |
| S/src/lib/system-status.js | 359 LOC | `status` at :187-216 calls the status reader at :191. Registry `system.status` reaches it at S/src/lib/tool-registry.js:873-885. `doctor` includes `status(...)` at :345, so registry `system.doctor` at :923 also exposes it. These are observations; neither gates writes or launch. |

S/config/capability-features.json:61-69 labels Filekeeper/repo-sync `tier: "unregistered"`. There is a registrar, tests, status projection, and direct CLI, but the copied managed-process configuration cannot register it. An already-created Windows scheduled task or a rewritten external registry could make it live; that external state was not inspected. The accurate wiring verdict is **direct-CLI/test reachable, observably surfaced, not demonstrably scheduled by shipped configuration**.

#### Receiver state machine: observes, refuses, writes

`runOnce(deps)` is S/tools/repo-sync.js:101-268. It is hard-coded to `origin/main` at :17-22 and records a bounded state shape at :122-135 with `countsAreContainmentProof:false`.

Execution order is load-bearing:

1. Stop-file refusal at :137-142.
2. Observe branch and require `main` at :144-155.
3. `fetch --prune origin` at :157-162.
4. Observe `git status --porcelain=v1 --untracked-files=all` and ahead/behind counts at :164-178 through transport :109-128.
5. Run network-verified, strict single-copy containment at :180-192. The detector fetches/prunes every configured remote and fetches advertised refs at S/tools/check-single-copy-work.js:507-534; it examines the primary checkout, other worktrees, commits, local branches, untracked and strict tracked edits at :557-715.
6. Refuse, in order: `diverged-main` :194-198, `ahead-main` :199-203, `dirty-worktree` :204-208, `containment-unknown` :209-213, or `single-copy-stranded` :214-218.
7. If behind, call transport `fastForward` at :220-227. That is exactly `git merge --ff-only origin/main` at S/packages/internal-vcs/src/m6/protected-main-receiver-transport.js:131-133. Reobserve and require 0/0 at :228-250. Otherwise return `in-sync` at :253-256.
8. In every exit, overwrite state JSON at :261-266; a write failure is swallowed so the reader later sees missing/stale.

Top-level action/refusal codes are: `stopped`, `inspection-failed`, `wrong-branch`, `fetch-failed`, `diverged-main`, `ahead-main`, `dirty-worktree`, `containment-unknown`, `single-copy-stranded`, `fast-forward-failed`, `post-fast-forward-unknown`, and catch-all `error`; successes are `fast-forward` and `in-sync`. The nested containment record additionally preserves/synthesizes `NOT_CHECKED`, `SINGLE_COPY_RESULT_INVALID`, `SINGLE_COPY_CHECK_FAILED`, `SINGLE_COPY_WORK_PRESENT`, `CONTAINMENT_REMOTE_MISSING`, `CONTAINMENT_NOT_NETWORK_VERIFIED`, and `SINGLE_COPY_INDETERMINATE` at S/tools/repo-sync.js:69-99, or a detector-supplied code.

The status reader separately returns reasons `READER_OPTIONS_INVALID`, `STATE_MISSING`, `STATE_UNREADABLE`, `STATE_SIZE_INVALID`, `STATE_MALFORMED`, `STATE_TIME_INVALID`, `STATE_STALE`, and `LAST_ACTION_FAILED`, plus schema reasons `STATE_SHAPE_INVALID`, `STATE_SCHEMA_MISSING`, `STATE_SCHEMA_UNSUPPORTED`, `STATE_GENERATED_AT_INVALID`, `STATE_ACTION_INVALID`, `STATE_DETAIL_INVALID`, `STATE_BRANCH_INVALID`, `STATE_COUNTS_INVALID`, `STATE_DIRTY_PATHS_INVALID`, `STATE_CONTAINMENT_INVALID`, `STATE_CONTAINMENT_CLAIM_INVALID`, `STATE_ACTION_RESULT_MISMATCH`, and `STATE_SUCCESS_WITHOUT_CONTAINMENT`; see S/src/lib/repo-sync-status.js:147-279.

What it writes is broader than the header's "one mutation path" if Git metadata counts, though only one path changes checked-out source content:

- fetch/prune updates the object database, remote-tracking refs, and normal Git fetch metadata;
- network containment performs further fetch/prune/ref fetches for every remote;
- `merge --ff-only` updates local `main`, the worktree, and the checkout's real index;
- every attempt **tries** to replace `state/repo-sync.json`; write failure is swallowed at :261-266.

It does not push, reset, rebase, create a merge commit, apply a harvested diff, consume/verify a publication receipt, or write an immutable receiver receipt. The transport's own header says so.

There is also a writer/reader root split. Writer defaults are hard-coded to install-root `ROOT/state/repo-sync.json` and `ROOT/state/repo-sync.stop` at S/tools/repo-sync.js:17-19. Reader default uses `statePath('state', 'repo-sync.json')` at S/src/lib/repo-sync-status.js:9/:17, whose runtime root can be redirected. In a packaged or `TOOLSENABLED_STATE_ROOT` deployment, the writer may write—or silently fail to write—the install-root file while the status reader watches a different state root. They coincide in the source checkout. This makes the current receipt/status path non-universal even if an external scheduled task exists.

#### Exposure to the three redesign findings

**1. Line-ending/filter smudging.** repo-sync reasons in Git states and commit reachability, not raw materialized bytes. `git status` compares the worktree/index through Git's conversion rules; ahead/behind compares commit graph. There is no post-fast-forward manifest/hash of worktree bytes. S/.gitattributes:1-15 sets `* -text`, and the copied read-only `git check-attr text` observation reports `unset` for repo-sync files and `.gitattributes` at O/git-attributes-repo-sync.txt:1-4, which disables EOL conversion for those paths. O/git-filter-config.txt:1-6 nevertheless shows global Git-LFS clean/smudge/process configuration and `core.autocrlf=true`. The audit did not capture `filter` attributes for every path or `.git/info/attributes`; therefore it cannot prove that no path-specific clean/smudge filter affects the checkout. Current receiver code assumes Git's checkout is the desired materialization and does not attest byte identity. Cloud mirror's tree/blob comparison likewise proves Git-object equality, not bytes produced in the cloud checkout: a remote clone can have different attributes or clean/smudge filter availability/configuration. `* -text` helps EOL stability but is not a remote materialization attestation.

**2. Staleness measurement.** repo-sync measures current commit divergence as integer counts against the fetched `origin/main` in the receiver checkout. It measures no wall-clock age, no lane/publication base, and no 16-commit/one-week policy. S/src/lib/repo-sync-status.js:239-266 computes age only for the last state record and labels it stale after 15 minutes; that is monitor freshness, not repository/lane staleness. Cloud mirror uses exact sourceCommit-versus-configured-source-HEAD equality and an optional commit count, with no time. For batch `against.publishedCommit`, S/tools/cloud-lane.js `createGitChangedSince`/:253-294 verifies the object exists and unions committed base-to-HEAD changes, worktree changes, and untracked paths; S/src/lib/cloud-agent/batch-target.js:355-393 then refuses declared targets present in that set. The caller-supplied base is not shown as a provider-branch-head or receipt-bound ref.

**3. Shared-index use and ref CAS.** cloud mirror's filtered-tree construction is private-index safe at :569-600. repo-sync sets no per-actor `GIT_INDEX_FILE` in transport `_run` at :88-95, so absent an externally inherited override its `status` and `merge` default to the receiver checkout's real `.git/index`; containment likewise invokes ordinary Git. O/git-paths.txt records that captured default index. The code therefore provides no index isolation from other ordinary Git users of that checkout. There is no receiver lock proving a dedicated single actor, and branch/dirty/ref state can change between observation and merge.

Ordinary Git still supplies internal ref/index locks and protocol race/non-fast-forward checks; the missing property is an **application-level expected-old binding to the earlier observation and containment proof**. `merge --ff-only` receives no expected local/remote OIDs from repo-sync. More sharply, repo-sync observes ahead/behind, then containment refetches remotes. If the first `behind` was zero and the second fetch advances `origin/main`, the code reaches :253 and returns `in-sync` without reobserving. Mirror's ordinary non-force push has server-side ref transaction/non-FF protection, but supersede uses unconditional `--force`, not a lease tied to `remoteHeadBefore`. Neither path supplies the redesign's observation-bound explicit CAS/lease.

#### First code, receiver seams, and safety seams

Attachment order:

1. Add the isolated harvest integrator after S/tools/cloud-lane.js:587; make `batch-harvest.readWave` consume and verify the sealed ADMITTED header before integration.
2. Add an explicit isolated-lane budget mode/evaluator at S/src/lib/cloud-agent/cloud-mirror.js:1315-1323 **only after** binding requested/provider branch and immutable source base to a receipt. Preserve exact equality as the default for normal dispatch; do not globally relax the current fail-closed gate.
3. Reuse the private-index construction pattern at cloud-mirror :569-600 for staging, but materialize/run tests in a disposable worktree whose index is not the receiver's.
4. In repo-sync, obtain local/remote OIDs and lock/reobserve immediately before S/tools/repo-sync.js:220. Extend transport `fastForward(expectedLocalOid, expectedRemoteOid)` so the mutation refuses if either changed; rerun the containment proof against that exact remote OID. Reobserve even for the current `behind === 0` in-sync route.
5. At mirror push :1153-1163, use expected-old remote-head semantics, including supersede; never replace `--force` with another unconditional write.
6. Produce an immutable receiver/integration receipt bound to input diff hashes, test evidence, source/base OIDs, resulting tree/commit, boundary/filter policy, and CAS result. Keep `repo-sync-status` a projection, not the receipt authority.

Do not weaken cloud mirror classification, credential scan, unharvested-work refusal, provider-repository binding, receipt/trailer witness, boundary-tree comparison, repo-sync dirty/ahead/diverged/unknown/stranded refusals, network containment, or `--ff-only`. The new integrator must route around the live checkout and protected main until tests and CAS succeed.

Estimated production/config blast radius: new isolated-integrator and receipt modules; batch harvest/journal/target; cloud-lane; cloud mirror; cloud launch adapter; repo-sync; protected-main transport; repo-sync status/task/managed-process configuration. Expect 12 or more production/config files plus focused cloud-harvest, staleness, filter-materialization, private-index, CAS-race, and receiver tests.

**Confidence:** High on current repository behavior, caller tracing, index use, and missing integration/CAS/budget. Medium on actual filter activation and scheduled-task deployment because those require excluded/local Git and OS state.

## Mediated file-touch capture inventory

### Scope and common chokepoint

Every `executeTool` invocation also causes implementation-private reads of policy/configuration and usually audit/state writes. Treating those as agent source-file reads would make every registry entry "file-touching" and produce unusable claims. This inventory is exhaustive for the copied **core caller-addressable code/artifact surface**: a public tool can select, enumerate, materialize, mutate, package, upload, or execute over local/sandbox project files. Fixed control-plane persistence is called out separately below.

S/src/lib/tool-registry.js contains 268 core `define(...)` calls. `TOOL_DEFINITIONS` merges these with `LOADED_TOOL_PACKS.definitions` at :2521-2527, but the snapshot has no `src/lib/tool-packs/` content from which optional preloaded personal-pack tools could be enumerated. Completeness therefore means the copied core registry, not an unknowable external/personal pack.

The common production path is S/src/mcp-server.js:334-358 (`tools/call` -> `executeTool`) into S/src/lib/tool-registry.js:3369-3598. `define` at :590-634 has no private capture metadata. The handler result exists at :3583-3585, but `auditInvocation` at :2945-2981 records effect/provider/duration/request/invocation/error only; it records neither arguments nor result. Consequently **none** of the rows below currently creates a mechanical file claim at the mediation layer, even when a provider returns enough data.

Legend: "path" means a canonicalizable file identity is available to the handler; "range" distinguishes byte ranges from textual or LSP locations; "content/hash" asks whether the exact materialized bytes or their digest are available without rereading.

### Direct file and directory tools

| Registry tool | Handler/provider evidence | Path exposed to handler/result | Range exposed | Exact content/hash exposed | Mechanical-capture status |
|---|---|---|---|---|---|
| `sandbox.workspace_write` | S/src/lib/providers/agent-sandbox.js:1243-1271 | Relative path in and out; sandbox/workspace identity is handle-derived. | Whole replacement only; result byte count. | Input UTF-8 content is exact; no digest or preimage. | Projectable write intent/result, but no current hook and no canonical cross-sandbox resource ID. |
| `sandbox.workspace_read` | S/src/lib/providers/agent-sandbox.js:1274-1298 | Relative path in and out plus sandbox ID. | Whole file only; result byte count. | Whole returned text; no digest. | Projectable after provider hashes the exact Buffer; currently absent. |
| `repo.read_file` | S/src/lib/providers/repo-files.js:237-251 | Repository-relative path in and out. | Whole file only; byte count. | Whole returned UTF-8 content; no digest. | Projectable, but current provider reads a string and common audit drops args/result. |
| `repo.write_file` | S/src/lib/providers/repo-files.js:254-278 | Repository-relative/canonical path is known; relative path returned. | Whole replacement; byte count only. | Exact input content, no pre/post digest. | Projectable only if finalized inside existing `withSharedWrite`; current common capture absent. |
| `repo.patch_file` | S/src/lib/providers/repo-files.js:285-324 | Repository-relative/canonical path is known; path returned. | Caller supplies exact `oldText`/`newText`; provider finds one JS-string index at :306-312 but returns no byte interval. | Old/new text known; full pre/post bytes and hashes absent. | In-flight provider is the best patch seam, but it first needs a write lock and Buffer-derived byte offsets/hashes. |
| `repo.list_dir` | S/src/lib/providers/repo-files.js:327 onward | Directory path in/result and entry names/metadata. | Not applicable. | No file content/hash. | Can claim a directory enumeration event, not byte materialization. |
| `workspace.list` | S/src/lib/providers/fra-workspace-handles.js; registry :1406-1413 | Public API uses opaque handles; provider privately resolves a canonical protected-root path and returns child handles/names. | Not applicable. | No file content/hash. | Needs a private provider receipt if directory enumerations are claim-bearing. |
| `workspace.read` | S/src/lib/providers/fra-workspace-handles.js:488-563 | Public call/result has an opaque handle and identity version, not path; provider knows the canonical file. | **Exact byte offset/length** in; actual offset/bytes/total bytes out. | **Yes:** returned content, `fileSha256`, and selected-region `contentSha256` at :547-558. | Only tool already carrying the required byte evidence. Missing private path/principal/event projection; provider audit omits region hash. |
| `host.read_file` | S/src/lib/providers/host-control.js:374-385 | Resolved owner-profile path in/result. | Whole file only; stat byte count. | Whole returned UTF-8 content; no digest. The stat precedes the read. | Requires hashing the exact read Buffer; a later registry reread would race. |
| `host.write_file` | S/src/lib/providers/host-control.js:387-405 | Resolved/canonical owner-profile path; path returned. | Whole replacement; byte count. | Exact input content, no pre/post digest. | Projectable inside existing `withSharedWrite`; absent now. |
| `host.list_dir` | S/src/lib/providers/host-control.js:408 onward | Resolved directory path and entries. | Not applicable. | No file content/hash. | Directory enumeration only; no current claim. |

Byte fidelity differs materially: repo/host/sandbox text readers decode UTF-8 strings and report either re-encoded length or a pre-read stat size; none proves the original materialized byte buffer. FRA `workspace.read` reads a Buffer, uses strict decoding, and hashes the exact full and sliced buffers before exposure. It is the only ready byte-receipt prototype.

### Derived/index/semantic readers

| Registry tool(s) | What the handler really sees | Path | Range/content/hash | Capture consequence |
|---|---|---|---|---|
| `search.index` | S/src/lib/search.js `indexPath` :217-294 recursively opens text/code under caller `root`. | Handler sees every selected path; public result is aggregate/index metadata. | Stores path/root/mtime/size/chunkIndex/text/vector at :77-83. No byte offset or content hash. | This is many hidden reads behind one call. The provider must emit one receipt per exact opened file/chunk; arguments/result alone are insufficient. |
| `search.query` | S/src/lib/search.js `query` :297-318 reads the local index and returns ranked source path/chunk index/snippet. | Result has source paths. | Chunk ordinal/snippet, not byte interval; no materialized source hash. | A claim against the index snapshot is possible; a claim that current source bytes were read is not. |
| `code.goto_definition`, `code.find_references`, `code.document_symbols`, `code.workspace_symbols`, `code.diagnostics`, `code.hover` | S/src/lib/providers/code-intel.js functions at :1094, :1126, :1187, :1231, :1271, :1372. Language server and supplements may read workspace files outside the immediate JS handler. | Inputs/results carry roots/files and result paths. | LSP 1-based/0-based line-column ranges and bounded source previews; no byte ranges or materialized hashes. `document_symbols` reports file byte size, not a digest. | Must either instrument the language-server/file-reader boundary or mark the result as derived/opaque. Converting line/column after the fact is racy. |

`search.status` and `code.status` read service/index readiness rather than caller-selected source bytes, so they are not source-claim rows.

The code-intel preview helper can additionally open up to 40 returned target files at S/src/lib/providers/code-intel.js:692-715; those materializations are not individually reported. `find_references` also reports aggregate scan counts rather than every supplemental CommonJS read path.

### Opaque project/subprocess tools

| Registry tool(s) | Declared locator | Actual possible file effects | Path/range/content exposure |
|---|---|---|---|
| `host.exec` | `cwd`, shell, exact command | Arbitrary host reads/writes/deletes performed by PowerShell/cmd and descendants. | Only command/cwd and stdout/stderr are returned; no touched-path list, ranges, bytes, or hashes. **Known general capture gap.** |
| `sandbox.exec` | Sandbox handle plus `scriptPath`/args | Arbitrary workspace reads/writes performed by the bounded Node script and children inside the container. | Script path known; all other touched paths/content opaque; only exit/stdout/stderr returned. |
| `firebase.deploy` | Project `cwd`, project ID, target | Firebase CLI may traverse project files and create local metadata while performing external deployment. | No per-file inventory, range, materialized content, or hash at the registry/provider result. |
| `terraform.init`, `terraform.validate`, `terraform.plan`, `terraform.apply` | `cwd`; plan/apply also `planFile` | Terraform/plugin subprocesses read modules/state/config and may write lock/cache/plan/state files. | Cwd/plan filename only; per-file effects opaque. |
| `extension.validate`, `extension.package` | `cwd`; package optionally `outputPath` | Recursively reads an extension; package writes a ZIP. | Roots/destination known, but no complete input-file receipts, byte ranges, or content hashes in the common result. |
| `launch.detect`, `deployment.detect`, `launch.plan` | Project `cwd` | Provider modules inspect known and discovered project/config files. | Root and derived metadata only; exact opened bytes/paths are not projected. |
| `deployment.execute`, `launch.execute` | Project `cwd` plus provider/options | Install/build/test/deploy subprocesses can touch an unbounded project set. | Root/options only; no per-file receipts. |
| `agent.spawn` | CONTRACT/tier/workspace root -> mission bridge/native agent lane | The spawned CLI can read/write the workspace through its own built-ins and shell for the lane lifetime. | No child touched-file inventory/ranges/hashes in the spawn result; outside registry capture. |

These bulk tools use provider modules that ultimately invoke S/src/lib/runtime.js `run`/:195-245 or equivalent child-process APIs. `runtime.run` captures executable/argv/cwd/output, not filesystem access. A registry projector cannot recover exact touches from those fields.

`launch.execute` can also generate a package path inside its handler. The existing `findOutwardFileFields` preflight scans caller arguments before execution and cannot inspect handler-produced nested artifact values. That is an egress/capture gap distinct from subprocess opacity.

### Artifact generation/read and file egress

| Registry tool(s) | File-bearing behavior | Path/range/content/hash visible |
|---|---|---|
| `sandbox.artifacts` | S/src/lib/providers/agent-sandbox.js:1380 onward enumerates bounded regular files under the leased workspace artifact directory. | Returns name/hostPath/byte-count metadata, not content/ranges/hashes. |
| `screen.capture`, `screen.capture_region`, `screen.capture_monitor`, `screen.capture_window` | S/src/lib/desktop.js writes PNGs under the fixed captures root. | Destination path/dimensions/byte count are returned; no content digest or byte region of the PNG. Screen rectangle/monitor/window is a visual source region, not a file byte interval. |
| `screen.read_capture` | Reads one direct capture PNG and returns an MCP image/thumbnail. | Input path and image payload are available to the handler; no file byte range/hash in the result contract. |
| `ocr.read` | Reads one capture path and returns derived OCR text. | Path and derived text only; source image bytes/range/hash absent. |
| `browser.playwright_call` | Generic wrapper. S/src/playwright-gateway.js:278-317 identifies nested upstream `browser_file_upload.paths` and `browser_take_screenshot.filename`; all other reviewed browser tools have no local path field. | Gateway sees those nested paths and enforces egress preflight, but registry audit records neither nested args nor bytes. Upload materialization/hash/range absent; screenshot is a generated file. |
| `gmail.send` | Attachment entries can name local `path`; S/src/lib/providers/google.js:75-133 reads each entire file into the MIME body. | Handler has source path and exact bytes transiently; result/audit keeps only count/encoded-message size at :324-348. No source digest/range. Inline Buffer/base64 attachments have content but no filesystem path. Registry's shallow array scanner can see `attachments[].path`, but it performs egress gating, not capture. |
| `drive.upload` | S/src/lib/providers/drive.js:51-85 resolves `filePath` and reads the whole file before upload. | Handler has path and exact bytes transiently; returned metadata includes uploaded byte count but no local path digest/range. |

### Fixed control-plane storage is a separate class

Settings, consent, audit, kill-switch, owner prompts, R-ledger, task/research/memory, scheduler, payment/vault, sandbox lifecycle/profile, workstation configuration, and similar tools read or mutate implementation-owned files/databases. Their public contract does not select arbitrary source/artifact paths. Examples include `settings.read`, `ide.consent_*`, `audit.*`, `system.kill_switch_*`, `r_ledger.*`, `memory.*`, `task.*`, `research.*`, and `scheduler.*`.

The file-affecting managed-lifecycle tools are still exceptions to any "one mediated call means one file" assumption:

- `sandbox.create` creates a managed workspace and returns `workspacePath`; `sandbox.status` reports it. `sandbox.cleanup` and `sandbox.reap` remove an owned scratch workspace by handle/ID but return category-level removal state, not every deleted path/hash. `sandbox.auth_profile_*` touches managed encrypted profile/control files, not caller-selected source content.
- `workstation.status` reads fixed editor/client configs and inventories. `workstation.install_cursor`, `workstation.sync_cursor_extensions`, `workstation.configure_agent_clients`, `workstation.initialize_cursor_state`, and `workstation.launch_cursor` install/launch tools or read/rewrite fixed client configuration/state. They expose registered roots or semantic results, not exact touched paths/ranges/content hashes; later editor activity is outside the invocation.

Those operations still need item-1 logical events, but treating their backing SQLite/JSON paths as an agent's live source read set would expose implementation details and create false holds. If the redesign intends to claim **all physical storage reads**, the registry is too high: every invocation also reads policy/audit/config, and capture must move to storage adapters. For the stated API-derived agent claims, the tables above are the complete caller-addressable capture surface.

### Shell and native-agent capture gap

The exact mediated host shell path is:

    MCP tools/call
      -> src/mcp-server.js:334-358
      -> tool-registry.executeTool:3369-3598
      -> host-control.exec:475-514
      -> execFile(powershell.exe|cmd.exe, command)

S/src/lib/providers/host-control.js:486-511 durably records exact command/cwd before execution and later exit/timing/output sizes, but it never observes opened/written paths. Parsing command text cannot fix this: redirection, scripts, subprocesses, dynamic paths, and native programs make it non-authoritative. `findOutwardFileFields` at S/src/lib/tool-registry.js:3147-3168 is an egress gate over shallow `*Path`/`*FileName` fields, not a file-access tracer; it misses cwd, opaque handles, shell effects, and most nested structures.

There is also a non-mediated gap: mission/Fleet/direct launchers spawn Codex, Claude, and Gemini CLIs that can use their own filesystem/shell surfaces. Those calls never enter this registry. Mechanical claims can be complete only if such lanes are confined to mediated tools, instrumented below the OS/container file boundary, or explicitly labeled opaque/incomplete. Agent prose such as `FILES-READ:` cannot close the gap.

No copied core tool provides a generic `git.*`, Grep/ripgrep, glob/recursive-list, copy/move/delete, or archive-extraction API. Those operations remain possible through `host.exec`, inside `sandbox.exec`, through `agent.spawn`, or as hidden behavior of project wrappers. Grep narrowing in item 5 therefore needs a new bounded adapter rather than wiring an existing registry Grep tool.

### Capture implementation consequence

Add private capture metadata to `define`, keep it out of public descriptors, and require a provider receipt after the handler and before result release. A receipt needs principal/run/launch, canonical resource, operation, half-open byte interval, SHA-256 of the exact bytes materialized or committed, and pre/post identity for writes. Do not reread after the fact. Bulk/semantic/shell tools need child receipts or an explicit `opaque` event; absence of receipts must never be projected as an empty read set.

## In-flight collision notes

The copied porcelain-v2 observation contains **33 tracked changes and 4 untracked paths** at O/git-status-porcelain-v2.txt:2-38. These are sequencing facts only. The audit did not attribute ownership, assess quality, or attempt resolution.

| Dirty path at snapshot time | Observed edit/symbol | Redesign seam exposed | Build sequencing note |
|---|---|---|---|
| `src/lib/tool-registry.js` (`+134/-3`) | Adds `AgentContractRefusal`, `validatedAgentContract`, `spawnSubagent`, `repo.patch_file`, `agent.spawn`, its P13 semantic override, and one `executeTool` context-list entry; see O/git-diff-unified-zero.patch:383-531. | Items 1-3 success/prewrite capture; item 5 possible runtime tool; item 7 target propagation/admission. | Rebase/compose around the named helpers and `CORE_TOOLS`/P13/executeTool symbols; do not overwrite the new contract work. |
| `src/lib/providers/repo-files.js` (`+47/-1`) | The entire `patchFile` implementation and export are new at O/git-diff-unified-zero.patch:329-380. | Item 3's first patch lock/validation seam and item 2 write receipt. | Treat `patchFile` as in-flight, then add locking/receipts against its landed form. |
| `src/mcp-server.js` (`+117/-3`) | Adds MCP initialize/session-instructions behavior in `dispatch`, O/git-diff-unified-zero.patch:532-660. It does **not** change the copied `tools/call` context block. | Item 1/2 launch/run principal propagation is in the same file, different branch of `dispatch`. | File-level collision; symbol-level merge should preserve `sessionInstructions` and separately extend tools/call context. |
| `src/lib/agent-api-policy.js` (`+2/-2`) and `src/lib/agent-engine/claude-cli-adapter.js` (`+32/-5`) | Edits built-in Bash/PowerShell policy and adds `agentApiToolArgs`/argv propagation at O/git-diff-unified-zero.patch:78-135. | Item 2/7 native-CLI mediation and stateless/confinement plan. | Decide native shell/file coverage after these policy/argv edits land; they control whether the registry is actually the only API. |
| `src/lib/agent-tool-summary.js` (`+136/-5`), `config/capability-index-spec.json` (`+8/-8`), `config/capability-index.json` (`+1/-1`) | Changes tool-surface steering/index generation. | Item 5 new retrieval/tool exposure and any item-2 capture status surfaced to agents. | Regenerate/merge indexes after tool names/contracts settle; do not hand-edit generated output over the in-flight revision. |
| `src/lib/cloud-agent/codex-cloud-launch.js` (`+28`) | Adds the default launch-path `checkMirrorFreshness` gate, still uncommitted at the observation instant, at O/git-diff-unified-zero.patch:289-321. | Items 7 and 8 admission/staleness seam. | Extend the landed hard gate; preserve exact mode for normal launches and do not move network launch ahead of it. |
| `config/cloud-mirror-boundary.json` (`+2`) and `package.json` (`+5/-2`) | Boundary classifications changed; package adds cloud-mirror scripts/test coverage at O/git-diff-unified-zero.patch:38-71. | Item 8 filter boundary and runnable/scheduled wiring. | Sequence integration tests/config against the new boundary and scripts. These edits do not demonstrate repo-sync scheduling. |
| `src/lib/action-guards.js` (`+1`) and `src/lib/fra-capability-manifest.js` (`+1/-1`) | Add `repo.patch_file` to guarded/excluded sets at O/git-diff-unified-zero.patch:72-77 and :322-328. | Item 3 patch validation and safety perimeter. | Preserve the new tool in both safety inventories when changing its provider. |
| Related tests and suite lists | Dirty tests cover agent policy/approval/tool summary, cloud launch, FRA manifest, mission bridge, Gmail attachments, repo-files, and runner/suite wiring. Four untracked paths include `tests/agent-spawn-contract.test.js`, `tests/docker-precondition-refusals.test.js`, `tests/mcp-initialize-instructions.test.js`, and a generated `.repo-files.../from-executeTool.txt`. | Same seams as above. | Treat fixture/suite changes as in-flight evidence, not authoritative production wiring. Do not delete the generated/untracked artifact in this audit. |

No dirty entry was observed for `tools/repo-sync.js`, `tools/repo-sync-task.ps1`, `src/lib/repo-sync-status.js`, `tools/check-single-copy-work.js`, protected-main receiver transport, `cloud-mirror.js`, `batch-harvest.js`, `batch-journal.js`, `batch-target.js`, `cloud-lane.js`, internal-VCS ClaimAuthority/control store, audit store, Fleet supervisor/state/review, or worktree-lease modules. That statement is limited to the observation instant; the checkout remained live after copying.

## Claims that could NOT be verified

1. **The coordinator decision document's exact sentence that ClaimAuthority "is NOT a fencing authority."** No such decision document or exact phrase was found in the copied corpus. The code independently supports that conclusion, but this audit cannot authenticate the quoted document/report.
2. **An active Windows scheduled task or external service running repo-sync.** The repository contains a registrar, but shipped `managed-processes.json` lacks the required entry. OS Task Scheduler and running-process state were intentionally not inspected.
3. **Deployment/runtime reachability beyond the static tree.** Static import/call paths establish that MCP registry, mission, Fleet, and some cloud paths are reachable in source. They do not prove a particular installation enables a route, has provider credentials, or is currently running it.
4. **Live runtime state and historical receipts.** `state/`, logs, captures, scratch, vault, and `.git` contents were excluded from the corpus. Cloud publication receipts, batch journals, Fleet state, audit rows, and repo-sync state could therefore not be used as evidence of actual historical execution.
5. **Remote/cloud materialized-byte equality and all Git filters.** The copy contains `.gitattributes` and bounded read-only Git config observations, but not remote checkout config, `.git/info/attributes`, every path's `filter` resolution, provider filter availability, or cloud clone bytes.
6. **Hosted-provider statelessness.** A fresh local process does not prove a fresh Codex/Claude/Gemini provider conversation or absence of provider-side retained context.
7. **Complete native CLI/shell file access.** The source shows those paths bypass or become opaque to the registry; without OS/container instrumentation, their actual read/write sets cannot be reconstructed.
8. **Behavioral/test claims.** The boundary expressly prohibited running ENGINE tests, builds, package scripts, and repository scripts. All verdicts are static, with test code used only as wiring evidence.
9. **Optional tool-pack capture surface.** The registry can merge preloaded pack definitions, but no copied `src/lib/tool-packs/` implementation was available. The mediated inventory is complete for the 268 copied core definitions only.
10. **Post-snapshot changes.** The copy had zero detected mid-read changes, but the live tree continued to be edited after 2026-08-25T14:12:56Z. This report makes no claim about later content.
11. **Unspecified redesign policy values.** The excerpt does not define seed-file identity, silence-rate window/hysteresis, live-read hold expiry/recovery, exactly when the one-week clock starts, or which branch/base qualifies for the isolated-lane budget. The report identifies code seams without inventing those rules.

## What would change each verdict

| Item | Evidence that would change this audit verdict |
|---:|---|
| 1 | A production-wired typed event envelope that requires actor/run/dispatch, canonical byte interval, materialized-byte hash, append identity, and immutable report-manifest references across both mission and Fleet. A private external event service would need code/config/receipts proving the same. |
| 2 | A required post-handler projector plus a durable inverted file/region->principal index; exact provider receipts for repo/host/FRA/search/code; and confinement/instrumentation proving native CLI and shell coverage or explicit opaque claims. |
| 3 | A generic write preflight/final in-lock validator that consumes read receipts, performs bounded repair, records alternating overlapping actors/regions, and demonstrably escalates on the third qualifying alternation. |
| 4 | A durable cross-process hold store keyed by live mechanically derived byte readsets, atomically consulted under write locks and bound to launch/lane terminal lifecycle, with ranking unable to grant/bypass it. |
| 5 | One production retrieval path that calls identifier lookup first, records BM25 outputs without affecting results, and emits/executes bounded Grep paths/patterns; tests must prove shadow non-interference. |
| 6 | Per-seed identity/state and measured silence rates feeding a versioned regime selector in the live retrieval path, with cold-start/window/hysteresis tests. A global prompt threshold would not change the verdict. |
| 7 | All supported mission/Fleet/review/cloud/wake/direct dispatchers converging on one atomic normalized-region conflict admission, CONTRACT target preserved into LaunchRecord, and explicit proof of the chosen per-task statelessness definition. |
| 8 | Harvest that consumes the sealed spec, applies in a private isolated lane, runs declared tests/mutation checks, emits immutable evidence, enforces a branch/base-bound commit-and-time budget, and updates integration/mirror/receiver refs with observation-bound expected-old semantics. Shipped scheduling and unified writer/reader state roots would change repo-sync's wiring verdict. |

## Confidence ledger

| Claim | Confidence | Reason |
|---|---|---|
| Item 1 is partial, not complete | **High** | Signed audit/LaunchRecord/FRA primitives are directly present; required common actor-region schema and immutable Fleet report contract are directly absent from their load-bearing schemas/callers. |
| Item 2 mechanical inverted claims are absent | **High for copied core; medium system-wide** | Registry args/results are not persisted and no inverted read projector/store was found. Native/external instrumentation or optional tool packs outside the corpus could change system-wide completeness. |
| Item 3 repair/N=3 alternation is absent | **High** | Writer call graphs show whole-file locks, unique-text validation, and queue-only CAS; the only N=3 found is unrelated supervision restart policy. |
| Item 4 exact live-read holds are absent | **High** | Every adjacent authority is keyed by semantic scope, declared path/territory, or write reservation; none consumes mechanical byte readsets. |
| Item 5 is partial | **High** | Production code-intel and GrepSaver calls are traceable; scoring functions show containment/custom/FTS/BM25F behavior and no identifier->shadow->Grep orchestrator. |
| Item 6 is absent | **High on absence; medium on seam** | Searches and threshold call graphs find only global/test silence metrics. Seed semantics are not specified, so the precise new contract remains policy-dependent. |
| Item 7 is partial/stubbed | **High on schema/wiring; medium on statelessness** | CONTRACT/1 and all dispatch constructors are explicit. Hosted provider session state and external launcher use are not observable. |
| Item 8 is partial/stubbed | **High on code gaps; medium on deployed Git/OS behavior** | Harvest, mirror, repo-sync, transport, and status implementations are explicit; remote filters, scheduled tasks, external environment variables, and remote ref behavior were not inspected. |
| Public internal-VCS `services.claims` throws | **High** | S/packages/internal-vcs/src/index.js:26-29 exports the facade; claim-service routes to unboundService; S/packages/internal-vcs/src/unbound-service.js:5-11 throws `VCS_ADAPTER_UNAVAILABLE`. |
| ClaimAuthority is not cross-process fencing authority | **High from code; unavailable for quoted document provenance** | Decisions depend on instance-local Maps and never replay/CAS the optional control store. The alleged coordinator-document sentence itself was not found. |
| `packages/internal-vcs/` is about 69 files | **Refuted with high confidence** | Snapshot manifest/file count is 73 files: 48 source, 22 tests, 3 other. The prior count is stale for this tree. |
| repo-sync is a protected-main receiver | **High** | `runOnce` and the typed transport allow only observed clean main -> `merge --ff-only origin/main`, with named refusal states and no push/reset/rebase. |
| repo-sync is actively scheduled in the shipped product | **Not verified; low confidence if asserted** | Registrar exists, but shipped managed-process registry has no repo-sync entry and capability tier is unregistered. External OS setup could override this. |
| Current working-tree single-cloud-launch calls freshness | **High statically; deployed status unverified** | The default call path exists in copied working-tree content, but the entire gate is an uncommitted edit at the observation instant. |
| Mirror/receiver equality proves materialized bytes | **Low; claim rejected** | Current proofs operate on Git objects/status/commit graph. Filters and remote checkout materialization are not attested. |
| repo-sync uses the default shared checkout index | **High for code default; medium at runtime** | It sets no `GIT_INDEX_FILE` and captured default is `.git/index`; an externally inherited override was not inspected. |
| Static wiring classifications | **High for direct imports/calls; medium for deployed activation** | Callers were traced from the immutable copy. No runtime telemetry or allowed test execution corroborated deployment state. |

## Bottom line for the build team

The redesign is not an incremental promotion of ClaimAuthority. The shortest safe build spine is: typed event/provider receipts -> durable inverted read index -> exact region holds -> in-lock write validation/repair/escalation -> canonical dispatch conflict admission. Retrieval/silence consume that read identity but remain advisory; isolated harvest/repo-sync consume the same evidence and add private staging plus observation-bound ref updates. The existing outward, approval, credential, mirror, containment, and protected-main refusals remain independent gates and must stay in front of new effects.
