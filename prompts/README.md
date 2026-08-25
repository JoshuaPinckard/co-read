# Frozen worker prompts

Every result document in this project was produced by a `codex exec` worker
driven by one of these prompt files. LEAN-Bench freezes every prompt byte-wise
with a SHA-256; this directory transfers that rule. `HASHES.txt` carries the
digests. Prompts are frozen at launch time from this point forward; a result
whose prompt is not in this directory says so in its provenance line.

## Prompt → result mapping

| Prompt | Worker model / effort | Result |
|---|---|---|
| `job-bootstrap.txt` | gpt-5.6-sol / ultra | `exploratory/language-hole/BOOTSTRAP.md` |
| `job-normalization.txt` | gpt-5.6-sol / ultra | `exploratory/unification/NORMALIZATION.md` |
| `job-coread-predictor.txt` | gpt-5.6-sol / ultra | `exploratory/unification/PREDICTOR.md` |
| `job-hazard-upgrade.txt` | gpt-5.6-sol / ultra | `exploratory/hazard/INVALIDATION.md` |
| `job-retrieval-fix.txt` | gpt-5.6-sol / ultra | `exploratory/retrieval/RESULTS-V2.md` |
| `job-corpus.txt` | gpt-5.6-sol / ultra | `corpus/` build (long-running) |
| `job-recover-tasks.txt` | gpt-5.6-sol / ultra | `exploratory/causal/RECOVERY.md` |
| `job-abort-rate.txt` | gpt-5.6-sol / ultra | none — terminated at a clarification question before any draw; superseded by the mined-conflict design |
| `job-second-repo.txt` | gpt-5.6-sol / ultra | `exploratory/causal/SECOND-REPO.md`, `fixture/pygments/` |
| `job-oscillation.txt` | gpt-5.6-sol / ultra | `exploratory/oscillation/RESULTS.md` |
| `job-harvest-corpus.txt` | gpt-5.6-sol / ultra | `exploratory/harvest/CORPUS.md`, `collisions.json` |
| `job-conflict-mine.txt` | gpt-5.6-sol / ultra | `exploratory/conflicts/MINING.md` (running at freeze) |
| `job-semantic-merge.txt` | gpt-5.6-sol / ultra | `exploratory/conflicts/SEMANTIC.md` (running at freeze) |

## What is NOT here — recorded, not hidden

The first-wave prompts predate this freeze and were lost with their session
scratchpad: the cross-language replay (`exploratory/language-hole/RESULTS.md`),
unification pass one, hazard pass one, the causal pilot/sweep/expansion, and
retrieval v1. Their protocols survive only as restated methodology inside the
result documents themselves, which is testimony, not the artifact. Cloud-task
prompts are additionally recorded in the Codex Cloud task system. This is a
known provenance defect of the first wave; it cannot be repaired retroactively
and is why the freeze-at-launch rule now exists.

## Worker environment disclosure

Workers ran via `codex exec` with the working directory inside this project.
The Codex CLI reads agent-instruction files from exactly two locations — its
home directory and the working directory (channel forensics: LEAN-Bench
cleanroom, 2026-08-21, calibrated planted markers). During every run to date,
`~/.codex/AGENTS.md` existed (943 bytes): a ToolsEnabled bootstrap pointer
whose operative line outside ToolsEnabled is "use the current project's own
instructions." No project-root `AGENTS.md` or `CLAUDE.md` exists in
Blast-Radius. Judgment: no behavioral shaping of measurement workers; the
channel is disclosed because it was open, not because it fired. Any future run
in which agents are **subjects** (the arms ladder) requires the LEAN-Bench
clean-room bar instead: environment manifest plus a same-day planted-marker
canary, certificates beside the data.
