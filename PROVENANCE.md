# Provenance index

Where every imported artifact came from and how it was verified on import.
Per-artifact records live beside the artifacts; this is the index. (Pattern:
LEAN-Bench `PROVENANCE.md`.)

| Artifact | Origin | Verification | Record |
|---|---|---|---|
| `brief/BRIEF-v1-2026-08-23.md` | Problem statement authored on this machine by another session | sha256 `d2cbad73…5162b` frozen at 29,952 bytes / 586 lines; v0 was never hashed and three design passes ran against it — recorded as a defect | `brief/FREEZE.md` |
| `fixture/click/` | `pallets/click` @ `2c8cd3ac958a7eb316d67f2d316c27086c4c0369`, BSD-3-Clause | Five-run suite determinism (identical normalised result hash), green-red-green per task | `fixture/click/GATE.md`, `TASKS.md`, `README.md` |
| `fixture/click/history/commit-stream.txt.gz` | Same clone, first-parent stream | 1,378 commits (an earlier README draft said 3,329 — caught and corrected) | `fixture/click/README.md` |
| `fixture/pygments/` | `pygments/pygments` @ `38f426a6b1cd4ffc6429f5808031b7c62ea57b1f`, BSD-2-Clause | Five-run determinism gate; 30 tasks green-red-green | `exploratory/causal/SECOND-REPO.md` |
| `corpus/harvest-reports/` | Read-only copy from the live ToolsEnabled engine checkout | Per-file SHA-256 at freeze; metadata compared during single read; 23 recovered entries lack before/after metadata (stated) | `corpus/harvest-reports/MANIFEST.json` |
| `corpus/_clones/` | Cloned corpora (mixed quality: includes scrape-derived repositories) | **Unvetted at large.** Only repositories individually gated or justified in a result document carry evidentiary weight; presence in this directory confers none | result docs that select from it |
| `corpus/conflicts/` | Derived by the deterministic miner from named clones | Miner determinism check (double run, byte-identical) required by the job spec | `exploratory/conflicts/MINING.md` when it lands |
| `prompts/*.txt` | This project's worker prompts, frozen from session scratchpads | SHA-256 per file; first-wave prompts lost and disclosed | `prompts/HASHES.txt`, `prompts/README.md` |
| `LICENSE`, `NOTICE` | Apache-2.0 text fetched from apache.org | 202 lines; sha256 recorded at fetch | commit message at introduction |
| Transcript corpus (read in place, never copied) | `~/.claude/projects` | Frozen byte-prefixes per analysis with corpus-level SHA-256 stated in each result | e.g. `exploratory/oscillation/RESULTS.md` |

Development history: the full working record is the `main` branch; `public` is
the curated branch; `_local-record` is the safety ref. Nothing in this table is
regenerated silently — an artifact that changes gets a new row or an updated
record, never an in-place overwrite of its verification claim.
