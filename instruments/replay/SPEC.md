# Cross-language co-change replay — specification

## Why this exists

The brief's §7A measured co-change prediction on three repositories, all
Node/JavaScript-dominated, and states plainly that portability to Rust,
Terraform, notebooks or vendored blobs is **argued from mechanism and not
measured**. That is the study's stated main validity threat and a live product
risk, since a customer's Terraform repository is a real customer.

It is also load-bearing for a strategic recommendation: that the co-change
estimator is near a structural ceiling and a better formula buys little. If
§7A's numbers are a JavaScript artifact, that recommendation is wrong.

## What this measures

Exactly §7A's protocol, unchanged, on repositories that are not JavaScript.
Comparability with §7A matters more than improving on it. Do not "fix" the
protocol.

**Protocol.** Strict temporal replay. At commit *i*, every model sees only
commits with index < *i*. Task: given one changed file as seed, predict the
other files changed in that same commit. Ground truth **excludes files created
by that commit**, because a file that does not exist cannot be claimed. Seeds
are drawn from commits with at least two surviving ground-truth files.

**Metrics.** P@1, P@10, R@10, R@20, plus empty-radius rate (fraction of queries
returning nothing) and per-query wall-clock. Report all of them per repository.

**Models (pass 1 — git metadata and paths only, no file contents):**

| Model | Definition |
|---|---|
| Co-change, time-decayed | Confidence weighted by exp(−ln2 · age_in_commits / 150) |
| Co-change, plain confidence | count(a,b) / count(a) |
| Path/name similarity | Shared path prefix depth, basename similarity |
| Popularity — control | Rank by global change frequency, ignoring the seed |
| Random draw — chance | Uniform over files existing at commit *i* |

The two controls are not optional. §7A's co-change result is only meaningful
against them: it beat popularity 0.411 to 0.190, and that gap is the evidence it
learns real pairs rather than which files are hot.

Pass 1 deliberately omits the identifier and import features because they need
file contents, which needs blobs, which is slow. Pass 1 answers the single most
important question — does co-change transfer across languages — at minimum cost.

## Corpus

Chosen to span the axes that plausibly break the mechanism, not for popularity.
Each is named with the axis it covers and what is expected to break.

| Repo | Axis | Expected stress |
|---|---|---|
| hashicorp/terraform-provider-random | Go + HCL + YAML, small | Baseline non-JS control |
| BurntSushi/ripgrep | Rust; tests **inline** in source files under `#[cfg(test)]` | Test-to-source path affinity should collapse — there is no separate test file |
| psf/requests | Python; `tests/` directory | Different test convention again |
| apache/commons-lang | Java; parallel `src/test/java` tree | Deep mirrored hierarchy; path similarity should do unusually well |
| gohugoio/hugo | Go; `_test.go` **adjacent** to source | Test file in the same directory, not a different one |
| ansible/ansible | Python + very large YAML surface | Config-heavy; huge file count |
| hashicorp/terraform | Go + HCL, large | Scale |
| redis/redis | C; no standard test convention | Language with no import statement in the scraped sense |
| prometheus/prometheus | Go with vendored tree history | Vendored blobs — near-duplicate content |
| jupyter/notebook | Notebooks + JS | JSON-enveloped code |

## Reporting rules

- **Per-claim confidence with its reason.** Not a document-level label.
- **Scope naming.** Say "10 repositories selected to span named axes", never
  "repositories". This is not a random sample of anything and must never be
  described as one.
- A commit touching *k* files yields a clique of *k(k−1)/2* mutually dependent
  pairs. Queries from one commit are **not independent**. Do not compute naive
  confidence intervals across queries. If an interval is wanted, resample whole
  **commits**, not queries.
- Report failures and empty cells as themselves. A repository the harness cannot
  process is a result, not an omission.

## What would change the conclusion

If co-change P@1 and R@10 on non-JavaScript repositories land within roughly the
same band as §7A's 0.500 / 0.411, the mechanism transfers and the ceiling
argument stands. If they collapse on some languages, then §7A's numbers are a
JavaScript artifact, the ceiling argument is wrong, and formula design becomes
worth real investment for exactly those regimes.
