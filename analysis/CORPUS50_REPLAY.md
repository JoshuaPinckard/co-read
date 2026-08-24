# Corpus-50 replay runner

`analysis/corpus50_replay.py` is the external orchestration layer for the
unchanged harness in `instruments/replay/`. Its scope is **50 repositories drawn
under Rule C50-2026-08-23-v1 (10 retained stress anchors, 35 seeded active-frame
additions, and 5 seeded stress-frame additions)**.

The selector writes `corpus/CORPUS-50.json`. Before any work, inspect the exact
smallest-first plan; plan-only is the default and writes nothing:

```powershell
python analysis/corpus50_replay.py --manifest corpus/CORPUS-50.json --dry-run
```

Run the forty additions, one complete repository at a time:

```powershell
python analysis/corpus50_replay.py --manifest corpus/CORPUS-50.json --execute
```

The same command is resumable. A stage is reused only when its durable JSON and
artifacts reverify against the frozen HEAD, current cap decision, stream hash,
and—at replay—current harness hash. Use a repeated `--force-stage` to rerun an
otherwise valid stage.

The selector retains each selected screening promisor clone as the canonical
`corpus/_clones/<slug>` clone at the manifest HEAD. The runner is intentionally
fail-closed if that clone is missing or drifts: it never moves a branch ref to
make a mutable fresh clone look like the frozen selection. Rejected screening
clones are a selector-ledger cleanup concern, not a runner action.

To rerun the original ten from their verified streams under the current
harness, without cloning or extracting:

```powershell
python analysis/corpus50_replay.py `
  --manifest corpus/CORPUS-50.json `
  --members anchors `
  --start-stage replay `
  --stop-stage replay `
  --force-stage replay `
  --execute
```

The runner preserves all pre-existing `corpus/CORPUS.json` records but replaces
its static harness order with the canonical 50-member selection order. Execution
uses a separate ascending first-parent-count order and never rewrites the frozen
selection manifest.

Durability and guards:

- `exploratory/language-hole/corpus-50-run.json` is atomically replaced and
  fsynced after every stage decision, cap event, and disk observation. A state
  file cannot be silently reused with a different manifest hash.
- Clone and extraction are polled while running. If combined accounted storage
  exceeds 20 GiB, D: falls below 12 GiB free, or C: falls below 1.5 GiB free,
  tracked Git subprocess trees are terminated and the run stops.
- Replay does not grow disk until its result is ready. The runner checks the
  exact replacement-size projection before the atomic result write.
- Every reachable-history cap is logged with the 20,000-commit trigger, the
  5,000-commit left-truncated window, empty learned-index boundary, and explicit
  warm-history non-comparability marker.
- Function return values are never treated as completion. The runner rereads
  `corpus/CORPUS.json`, stream metadata, or result JSON after each stage. Normal
  clone/extraction failures also receive downstream failed metadata/result JSON
  when those stages are in the requested run.

Focused tests are local and perform no Git/network work:

```powershell
python -m unittest analysis.test_corpus50_replay -v
```

`--allow-incomplete-manifest` and `--skip-volume-guards` exist only for tests or
driver development. Production execution should use neither.
