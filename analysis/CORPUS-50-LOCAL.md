# Local corpus-50 acquisition and selection

`analysis/corpus50.py` implements the external frame and selection operations
for Rule `C50-2026-08-23-v1`. It never invokes or changes
`instruments/replay/`. The frozen rule remains `corpus/CORPUS-50-RULE.md`.

The examples use `D:\Blast-Radius-C50` as the frame and screening root. Keep
screening clones beneath that root, or pass every other clone/stream/result
root with `--account-path`, so the 20 GiB combined guard sees the complete run.
The tool also requires at least 12 GiB free on `D:` and 1.5 GiB on `C:` during
network acquisition.

## Dated listings

Inspect the exact, write-free plan first:

```powershell
python analysis/corpus50.py acquire-gharchive `
  --frame-root D:\Blast-Radius-C50 `
  --account-path corpus\_clones `
  --account-path exploratory\language-hole `
  --dry-run
```

Acquire all 24 hours and build the base frame:

```powershell
python analysis/corpus50.py acquire-gharchive `
  --frame-root D:\Blast-Radius-C50 `
  --account-path corpus\_clones `
  --account-path exploratory\language-hole

python analysis/corpus50.py build-base-frame `
  --frame-root D:\Blast-Radius-C50
```

The acquisition command streams to a partial file, polls the disk guards,
fsyncs, records every response header, records HTTP `Date`/`ETag`, hashes the
compressed bytes, and reads the whole gzip stream to validate its CRC. One
missing or corrupt hour aborts construction. The builder revalidates the 24
files and commits one hour at a time to `state/base-frame.sqlite3`, so an
interruption cannot double-count a partially processed hour. Its durable
listing is `frames/base-active.jsonl` and its construction manifest is
`manifests/base-active.json`.

The 63 Search requests must be made on 2026-08-23 UTC. Put a token in
`GITHUB_TOKEN` or `GH_TOKEN`; it is sent in the request but never printed or
persisted. Then run:

```powershell
python analysis/corpus50.py acquire-search `
  --frame-root D:\Blast-Radius-C50 `
  --account-path corpus\_clones `
  --account-path exploratory\language-hole

python analysis/corpus50.py build-stress-frames `
  --frame-root D:\Blast-Radius-C50
```

The fixed query catalog is written before the first request. Acquisition saves
the exact URL, untouched JSON response bytes, all response-header pairs,
retrieval time, `Date`/`ETag`, byte length, and SHA-256. It rejects
`incomplete_results=true`, preserves failed HTTP bodies, and honors
`Retry-After`/`X-RateLimit-Reset`. Both the recorded retrieval time and HTTP
`Date` must be the frozen UTC date. Verified snapshots resume without a new
request. Outputs are `frames/stress-{config,catalog,import,low_author,
non_english}.jsonl` and `manifests/stress-frames.json`.

## Screening order and local measurements

Stress slots are filled in the fixed order in the rule. Emit the complete
own-frame, union-frame, then base-frame fallback order for one slot with:

```powershell
python analysis/corpus50.py emit-stress-order `
  --frame-root D:\Blast-Radius-C50 `
  --stress-key config `
  --output D:\Blast-Radius-C50\frames\order-config.jsonl
```

Pass prior selected immutable IDs using repeated `--exclude-repo-id` options.
Candidates must be cloned with `--filter=blob:none --no-checkout`. A selected
screening clone is canonical, remains in `corpus/_clones/<owner>__<repo>`, and
is pinned to its manifest HEAD. Only rejected screening clones may be removed,
and only after their HEAD, counts, size, outcome, and reason are durable in the
selector ledger.

Measure common eligibility and both local strata without reading blobs:

```powershell
python analysis/corpus50.py classify-repo --repo <clone-path>
```

Evaluate the required stress predicate separately:

```powershell
python analysis/corpus50.py evaluate-stress `
  --repo <clone-path> --stress-key config --output <evidence.json>
```

`non_english` produces conservative machine evidence but deliberately leaves
`passed` null. A human may reject false positives and finalize only a subset of
the machine-evidenced tokens:

```powershell
python analysis/corpus50.py review-non-english `
  --scan <scan.json> `
  --accepted-token 变量甲 `
  --accepted-token 变量乙 `
  --output <reviewed.json>
```

At least ten accepted tokens are required. The review command refuses any
token absent from the machine evidence, so a machine miss can never be
promoted.

Eligible base-candidate metadata must retain `repo_id`, `base_rank`,
`priority_key`, `head`, the commit counts, and the classifier result. Solve the
two margins with:

```powershell
python analysis/corpus50.py solve-base `
  --candidates D:\Blast-Radius-C50\frames\eligible-base.jsonl `
  --output D:\Blast-Radius-C50\frames\base-selected.json
```

Before feasibility, this returns `status: not_yet_feasible` and selects
nothing. The minimum-deviation fallback is disabled unless the entire active
frame has truly been screened; only then add `--active-frame-exhausted`. The
solver uses bipartite flow for exact feasibility, stops at the first feasible
base rank, and greedily tests completion to obtain the lexicographically
smallest priority tuple. Its exhausted-frame fallback uses convex min-cost
flow and reports every language and layout deviation.

## Durable selector ledger

The canonical ledger is `corpus/CORPUS-50-LEDGER.jsonl`. Append one candidate
outcome from a JSON file (or omit `--input` and pipe JSON on stdin):

```powershell
python analysis/corpus50.py record-ledger --input candidate-outcome.json
python analysis/corpus50.py verify-ledger corpus/CORPUS-50-LEDGER.jsonl
```

A normal candidate event has this shape:

```json
{
  "event_type": "candidate_screened",
  "candidate": {
    "repo_id": 123,
    "name": "owner/repo",
    "url": "https://github.com/owner/repo.git",
    "cohort": "base",
    "priority_key": "..."
  },
  "outcome": {
    "status": "rejected",
    "reason": "fewer_than_500_first_parent_commits"
  },
  "measurements": {
    "head": "...",
    "first_parent_commit_count": 412,
    "clone_size_bytes": 123456
  },
  "artifacts": {}
}
```

The appender supplies `schema_version`, `rule_id`, UTC time, event ID, and a
SHA chain, then flushes and fsyncs. For each record, `record_sha256` is SHA-256
of UTF-8 `json.dumps(record_without_record_sha256, ensure_ascii=False,
sort_keys=True, separators=(',', ':')) + '\n'`. The prior full-record hash is
included as `previous_record_sha256`; line one uses null.

Clone failures, disappeared/private repositories, histories below 500,
duplicate HEADs, disk denials, and all predicate misses are ledger outcomes,
never silent omissions. Once a repository is selected, later extraction or
replay failure does not replace it.

## Canonical selected-members manifest

After the five stress and 35 base selections have durable local measurements:

```powershell
python analysis/corpus50_select.py assemble `
  --frame-root D:\Blast-Radius-C50 `
  --ledger corpus\CORPUS-50-LEDGER.jsonl `
  --account-path corpus\_clones `
  --account-path exploratory\language-hole
```

This is the production assembly route. It verifies the frozen frame hashes,
the selection-ledger chain and deterministic terminal prefixes, recomputes the
stress predicates and base solver result, pins the ten anchors to the original
manifest, and attaches the resulting cross-file provenance hashes. The
lower-level `corpus50.py assemble-manifest` command does not attach that
attestation and is not sufficient for the final report.

The production route atomically writes `corpus/CORPUS-50.json`. It locally measures the ten
retained anchors, requires every addition to have a frozen HEAD and at least
500 first-parent commits, requires five distinct passing stress predicates,
checks immutable IDs/names/HEADs for duplicates, and verifies the base margins
(or a provenance-bearing exhausted-frame deviation result). Every member has
`selection_status: "selected"`; cohorts are exactly `retained_anchor`,
`stress`, or `base`. The manifest carries absolute `frame_root` and
`accounted_paths`, `disk_cap_bytes: 21474836480`, listing dates, seed, realised
strata, selection order, frozen HEADs, and local counts.

Run the focused offline validation at any time:

```powershell
python -m pytest -q analysis/tests/test_corpus50.py
```

