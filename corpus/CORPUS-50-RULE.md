# Corpus-50 sampling rule

Rule identifier: **C50-2026-08-23-v1**  
Frozen before downloading any selection frame or selecting any of the forty additions.  
Seed literal: `blast-radius-corpus-50-2026-08-23-v1`

The measurement scope is always named **“50 repositories drawn under Rule
C50-2026-08-23-v1 (10 retained stress anchors, 35 seeded active-frame
additions, and 5 seeded stress-frame additions)”**. The ten retained anchors
were selected earlier to span named axes; they are certainty inclusions, not
random draws. No result licenses an unqualified statement about
“repositories.”

## Retained anchors

The retained names are `hashicorp/terraform-provider-random`,
`BurntSushi/ripgrep`, `psf/requests`, `apache/commons-lang`, `gohugoio/hugo`,
`ansible/ansible`, `hashicorp/terraform`, `redis/redis`,
`prometheus/prometheus`, and `jupyter/notebook`. Pre-existing clones for any
other name confer no selection status.

## Dated base listing

The base listing is the union of the 24 hourly GH Archive files for
**2026-08-22 UTC**, `https://data.gharchive.org/2026-08-22-H.json.gz` for
integer hours H=0..23. Construction aborts rather than silently omitting an
unavailable or corrupt hour. For every hour the acquisition ledger records the
URL, retrieval time, HTTP `Date`, `ETag` when supplied, byte length, and SHA-256.

Only public `PushEvent` records enter the frame. Repositories are deduplicated
by immutable decimal `repo.id`; all observed names are retained and the name in
the event with greatest `(created_at, event.id)` is the clone name. A repository
enters the active subframe when either:

1. it has at least three PushEvent records and at least one non-bot actor, or
2. it has at least two distinct non-bot actors.

An actor is a bot exactly when its login case-insensitively ends in `[bot]` or
equals `github-actions`. The ten retained anchors are removed from this frame.
The base priority key is
`SHA256(UTF8(seed + NUL + "base" + NUL + decimal_repo_id))`, ascending, with
numeric repository ID as the tie-breaker.

Candidates are screened in that order. A candidate must still clone publicly
from GitHub with `--filter=blob:none --no-checkout`, must have at least 500
first-parent commits at the cloned default-branch HEAD, and must not duplicate
the exact HEAD of an already included repository. Clone failures, disappeared
or private repositories, fewer-than-500 histories, duplicate HEADs, and disk
denials are written to the selection ledger before advancing. Once selected,
an extraction or replay failure remains one of the fifty and is not replaced.
Forks and archived repositories are not post-hoc exclusions: the dated public
PushEvent frame and the stated checks define the population.

## Primary-language strata for the 35 base additions

Primary language is measured locally at the frozen HEAD, so selection does not
depend on a mutable GitHub label or API rate limit. It is the plurality of
tracked source-bearing paths under the frozen extension map below; ties break
by the displayed language name. Generated/vendor paths are deliberately not
removed because they are part of the repository shape tested by the replay.

| Stratum | Languages and extensions | Quota |
|---|---|---:|
| C/C++ | C/C++/Objective-C: `.c .h .cc .cpp .cxx .hpp .hh .m .mm` | 4 |
| JVM | Java/Kotlin/Scala/Clojure/Groovy: `.java .kt .kts .scala .clj .cljs .cljc .groovy` | 4 |
| JS/TS | JavaScript/TypeScript: `.js .jsx .mjs .cjs .ts .tsx .vue .svelte` | 5 |
| Python | `.py .pyi .pyx` | 5 |
| Go | `.go` | 4 |
| Rust | `.rs` | 4 |
| .NET | C#/F#/VB.NET: `.cs .fs .fsx .vb` | 3 |
| Ruby/PHP | `.rb .rake .php` | 3 |
| Other/no-code | all other plurality winners or no source-bearing path | 3 |

The complete Other map, used both to calculate source fractions and name the
realised primary language, is: Swift `.swift`; Shell `.sh .bash .zsh .fish`;
Lua `.lua`; Haskell `.hs .lhs`; Elixir/Erlang `.ex .exs .erl .hrl`; R `.r`;
Julia `.jl`; Perl `.pl .pm`; Dart `.dart`; Zig `.zig`; Vim Script `.vim`;
Solidity `.sol`; SQL `.sql`; HTML/CSS `.html .htm .css .scss .sass .less`;
Terraform/HCL `.tf .hcl`; Nix `.nix`; and Wenyan `.wy`. Extension comparison is
case-insensitive.

## Layout strata for the 35 base additions

The classifier uses tracked HEAD paths and the same source-extension table. It
is applied in the following priority order, producing one label per candidate:

1. **artifact/config/docs** when at most 20% of tracked paths are source-bearing;
2. **manifest monorepo** when at least five distinct non-root directories contain
   one of `package.json`, `pyproject.toml`, `setup.py`, `Cargo.toml`, `go.mod`,
   `pom.xml`, `build.gradle`, a `*.csproj` or `*.fsproj`, `Package.swift`,
   `composer.json`, `Gemfile`, or `mix.exs`;
3. **multi-module tree** when at least four top-level directories each contain
   at least five source-bearing paths;
4. **single-package tree** otherwise.

The fixed margins are respectively **6, 8, 9, and 12**. Candidate metadata are
accumulated in base-priority order until a 35-member set satisfies both these
layout margins and the language margins exactly. At the first feasible prefix,
choose the lexicographically smallest sorted tuple of priority keys among all
feasible sets. If the full active frame is exhausted without exact feasibility,
choose first the set with minimum total absolute margin deviation, then the
lexicographically smallest priority tuple; report every deviation. No human
layout relabeling is allowed.

## Five seeded stress additions

Before base selection, five distinct stress members are selected in this fixed
order: `config`, `catalog`, `import`, `low_author`, `non_english`. Each stress
frame is a union of dated GitHub REST Search snapshots acquired on 2026-08-23.
For each query, page 1 with `per_page=100` is in the frozen listing, except that
the deliberately rare `import` and `low_author` screens include pages 1 through
10 (at most 1,000 results per query). The ledger saves every
exact URL, raw JSON, retrieval time, HTTP `Date`/`ETag`, byte length, and SHA-256
and requires `incomplete_results=false`. Repository searches
use `fork:false archived:false size:<200000`, `sort=stars`, and `order=desc`;
within a stress frame, deduplicate by repository ID and rank by
`SHA256(UTF8(seed + NUL + stress_key + NUL + decimal_repo_id))`, tie by ID.

The query families are fixed before acquisition:

- `config`: repository-search terms `configuration`, `dotfiles`, and `gitignore`,
  each in name, description, or topics;
- `catalog`: repository-search terms `monorepo`, `registry`, and
  `package-manager`, each in name, description, or topics;
- `import`: one broad repository search for at least 1,000 stars; selection uses
  only the structural history predicate below, not a commit-message search;
- `low_author`: repository searches with at least 100 stars for each primary
  language C, C++, Lua, and Vim Script;
- `non_english`: repository-search terms `classical-chinese`,
  `chinese programming-language`, `japanese`, `korean`, `arabic`, `cyrillic`,
  and `unicode`, each in name, description, README, or topics.

All common eligibility checks for base candidates apply. A predicate miss or
acquisition failure is recorded and advances to the next hash-ranked candidate.
If a stress-specific frame exhausts, its fixed fallback is the union of all five
stress frames under that stress key, then the base active frame; the predicate
never widens. If those exhaust, the slot is reported unfilled rather than
hand-picked.

The stress predicates are:

- **config:** at most five source-bearing paths and at least 90% of tracked paths
  use `.gitignore .json .yaml .yml .toml .ini .cfg .conf .config .xml
  .properties .lock .md .rst .txt`, or are extensionless README/LICENSE files;
- **catalog:** beneath one prefix at depth zero or one, at least 60% of all
  tracked files belong to at least 100 identity-named component directories at
  one fixed depth (one optional one- or two-character alphanumeric shard level
  is allowed), with a median of at most 20 files per component. The resulting
  label is “flat/sharded catalog: hierarchy carries component identity or shard,
  not category semantics”;
- **import:** within the oldest 20 first-parent commits, one commit adds at least
  500 paths from a parent tree with at most 10 paths and supplies at least 80%
  of the paths live after first-parent commit 20. Its message is recorded but is
  not part of the predicate;
- **low_author:** one to four unique `(mailmapped author name, email)` identities
  across all reachable commits, obtained with
  `git log --all --use-mailmap --format=%aN%x00%aE`; email is case-folded;
- **non_english:** at least ten distinct identifier tokens containing a
  non-ASCII Unicode letter in UTF-8 source files, with each accepted token
  evidenced in a declaration/assignment context and a later executable use.
  Evidence paths and lines are saved. Human review may reject syntax/string/
  comment false positives but may never promote a predicate miss.

Stress members are removed before solving the base margins. A repository cannot
fill two slots.

## Scale, ordering, persistence, and disk

The unchanged replay cap applies: when reachable history exceeds 20,000 commits,
extract and replay only the most recent 5,000 first-parent commits in
chronological order. The live-file universe starts at the boundary parent tree,
but all learned indexes start empty. Every applied cap is logged, and capped
rows are explicitly marked non-comparable for conclusions sensitive to warm
history.

After selection, repositories run in ascending first-parent commit count. Each
repository completes clone metadata, extraction, and replay before the next
begins; harness atomic files are the durable unit. Stage status is read from the
written metadata, not inferred from process exit status.

The hard cap is **20 GiB combined** for the dated-frame files, screening and
selected clones, compressed streams, and result JSONs. The run also stops if
`D:` has less than 12 GiB free or `C:` has less than 1.5 GiB free. These guards
are polled during clone and extraction because rename similarity detection may
hydrate blobs. A cap denial is a recorded candidate result, never permission to
skip ahead silently. Non-selected screening clones may be deleted only after
their ledger records, sizes, HEADs, counts, and rejection reasons are durable.

No path, language, extension, contributor, or commit-size filtering is applied
inside replay. The selection classifiers above affect corpus construction only.
