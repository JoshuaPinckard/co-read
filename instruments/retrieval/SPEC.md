# Grep-replacement evaluation — specification

## The question

When an agent searches a repository, does an index-backed retriever return
better results, in fewer tokens, than ripgrep does?

"Better" is not a matter of opinion here. The transcript corpus records what the
agent did *after* each search, which gives implicit relevance labels for free.

## The labels, and why they are sound

For each `Grep` call in the corpus we can observe what followed it in the same
session:

- **Positive.** The agent subsequently `Read` a file. That file was relevant to
  the search. Strongest available signal.
- **Negative.** A path appeared in the Grep's own result list and was never read.
  Surfaced and rejected. This is free negative data and nothing else in this
  project currently uses it.
- **Failure.** The agent issued another `Grep` before reading anything. A search
  followed by another search is a search that did not answer the question.
- **Abandonment.** Neither followed. Ambiguous; excluded rather than guessed.

These are implicit-feedback labels and carry the usual bias: they record what the
agent did given what it was shown, not what it would have done given something
better. That bias is why the ripgrep control is mandatory and why absolute
numbers from this benchmark mean less than the gap between arms.

## The eval set

Extracted from `C:/Users/USER/.claude/projects/` — 13,108 Grep events are
present. One record per Grep call that has a resolvable outcome:

```json
{
  "id": "<sessionId>:<toolUseId>",
  "ts": 1756900000.0,
  "agent": "<agentId or sessionId>",
  "cwd": "C:/Users/USER/Desktop/toolsenabled-current",
  "git_branch": "main",
  "query": {"pattern": "...", "path": "...", "glob": "...", "type": "...",
            "output_mode": "files_with_matches|content|count", "-i": false},
  "returned_paths": ["abs/or/rel/path", "..."],
  "followed_by_read": ["path read within the window", "..."],
  "followed_by_grep": true,
  "seconds_to_next_action": 12.4,
  "result_bytes": 4210
}
```

Rules, all of which must be honoured or the benchmark is meaningless:

1. **Follow-up window: 300 seconds AND same session.** Report sensitivity at 60s
   and 900s. Do not tune the window to make a result look better.
2. **Only same-agent follow-ups count.** A different agent reading the file is a
   different phenomenon (that is the hazard measurement, not this one).
3. **Exclude records with no follow-up of any kind** — abandonment is ambiguous.
   Report how many were excluded; a large fraction is itself a finding.
4. **Resolve paths to absolute, normalised case** before comparing. `cwd` is on
   the record and must be used for relative paths.
5. **Hold out by session, not by record.** Records from one session are
   dependent. Any split is at session granularity.

## The arms

| Arm | Description |
|---|---|
| `ripgrep` | **Control.** Re-run the recorded query with real ripgrep against the repo at that commit if resolvable, else at HEAD. Report which. |
| `bm25` | Inverted index, BM25, identifier-aware tokenisation |
| `ident_first` | Exact identifier postings first; BM25 only when the query has no identifier-shaped token |
| `bm25_pathboost` | BM25 with path and filename weighting |

The control is not optional. An arm that beats nothing is not a result.

## Tokenisation — the load-bearing detail

Code is not prose. Every token must be emitted **and** decomposed:

- `parseLease` → `parselease`, `parse`, `lease`
- `MAX_RETRY_MS` → `max_retry_ms`, `max`, `retry`, `ms`
- `tool-registry.js` → `tool-registry.js`, `tool`, `registry`, `js`
- `a.b.c` → `a.b.c`, `a`, `b`, `c`

The existing `src/lib/search.js` tokeniser is `/[a-z0-9_]+/g`, which does not
split camelCase at all. Fixing that alone is expected to be the single largest
quality lever, and the benchmark should be able to show it.

## Chunking and region identity

Fixed byte windows with overlap, snapped to the nearest blank line or
column-zero line within a tolerance; plain fixed windows where no such structure
exists (minified bundles, degenerate single-line files).

**Region identity is `(path, startByte, endByte)` plus a content hash. Never a
chunk index.** A chunk index shifts whenever anything above it changes, which
makes it useless as a stable address.

## Metrics

Per arm, over the eval set:

- **recall@K** of `followed_by_read` paths, K in {1, 5, 10, 20}
- **precision@K** against the same
- **response bytes** and **estimated tokens** (bytes/4) — the token claim
- **failure rate**: fraction where the arm's top-K misses every read path
- **latency**: median and p95 per query

Report all of them per arm. A retriever that wins recall while tripling response
size has not won.

## What would make this benchmark wrong

- If most Greps have no resolvable follow-up, the eval set is small and biased
  toward searches that worked. **Report the retention rate first**, before any
  metric.
- If the repository state at query time cannot be reconstructed, ripgrep's
  control arm is run against the wrong tree. Report how many records this
  affects.
- The corpus is one team, one harness, Node-dominated. Nothing here generalises
  to a customer without re-measurement.
