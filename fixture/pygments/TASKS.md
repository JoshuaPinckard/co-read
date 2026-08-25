# Pygments causal-task construction

## Selection rule fixed before outcomes

The final rule was fixed after the unmodified-tree gate and static Git/patch
inspection, but before the candidate ledger or any historical pytest run.
Its SHA-256 is
`c98608c8ab1f3168d693a615307c0edb0ff5d36587476b6bd81d225e4d7875bd`.

In summary:

1. Start at anchor `38f426a6b1cd4ffc6429f5808031b7c62ea57b1f` and scan
   first-parent commits since 2024-01-01, newest first.
2. Recognize only non-revert one- or two-parent landings whose subject has an
   unambiguous `(#N)` suffix or `Merge pull request #N` prefix. Diff a merge
   against parent 1; the first occurrence of each PR number wins.
3. Reduce each PR to A/M paths below `pygments/` and `tests/`. Require 2-30
   paths, a production Python path, a changed collectable test target outside
   `tests/contrast`, no more than 10 test paths, a patch no larger than 500
   KiB, and regular blobs only.
4. A `.output` golden file stays in the test patch and test-path count but is
   not itself collectable and cannot make an output-only change qualify.
5. Require the reduced full patch to reverse cleanly from the anchor in a
   temporary Git index. Keep candidates greedily only when their complete path
   set is disjoint from every earlier kept candidate.
6. Write and hash the complete static ledger before pytest. Its 47 patches
   must cumulatively reverse to tree
   `fdba588edb787a757b5f332715bb119d1c11397a` and replay exactly to anchor tree
   `ef5ef11d79315fe64ed6663277d7466c4d065b16`.
7. Screen all kept candidates in ledger order on singleton reversions. The
   changed test targets must be red with tests only and green with the complete
   patch, with red failures mapped exactly to a changed artifact.
8. Take the first 30 provisional tasks. Cumulatively reverse exactly those
   patches into one shared base. On fresh copies, require the complete fixed
   task suite green on the base, red with each tests patch alone, and green
   with each complete patch alone. A final failure would replace only the
   first failing task and restart the entire common-base proof.

The fixed environment was CPython 3.11.9 and pytest 8.4.2 with plugin
autoload disabled. Every process had a 120-second timeout, local Pygments on
`PYTHONPATH`, bytecode disabled, user site disabled, and inherited pytest
options/plugins removed. The fixed command was:

```text
python -m pytest --ignore=tests/contrast
```

## Candidate accounting

- 385 first-parent commits scanned
- 231 unambiguous unique PR landings
- 71 structurally eligible before disjointness
- 47 path-disjoint candidates frozen in the ledger
- 47 candidates screened
- 46 provisional targeted red/green candidates
- 1 screening rejection: PR 3057, whose changed test passed without source
- 30 accepted tasks from the first provisional cohort
- selection yield: 30 / 47 examined = 63.8%
- targeted-screen yield: 46 / 47 = 97.9%
- final cohort attempts: 1; final replacements: 0

The candidate-ledger SHA-256 is
`3746dde7222bcd854ed9d6af816ffaae9ed588345bdc13997928aadded4a51bf`.

## Common base

`base-shared/` is the anchor with the 30 accepted reduced patches reversed.
Its exact tree is `ad20930041fbd242b17a4ce3e84770b63743ef7e` and contains
2,756 regular files. Replaying all 30 complete patches reconstructs the anchor
tree exactly.

The complete fixed suite on that base was:

- 5,274 passed
- 0 failed
- 0 errors
- 16 skipped
- 5,290 total JUnit cases
- exit 0 in 20.607 seconds

That single byte-identical base result is the source-and-test-absent green arm
for every task.

After emission, a fresh copy of the actual vendored `base-shared/` repeated
the same 5,274-passed/16-skipped result in 22.14 seconds using the documented
pytest-only command.

## Per-task green-red-green evidence

Counts are `passed / failed / errors / skipped`. `Mapped` is the number of red
failure/error cases mapped exactly to a changed Python test, example input, or
snippet input. All red arms exited 1; all base and complete-patch arms exited
0. All files present at arm start remained byte-identical after pytest.

| # | PR | Commit | Base green | Tests-only red | Mapped | Complete-patch green |
|---:|---:|---|---|---|---:|---|
| 1 | 3225 | `d3441d00a334` | 5274 / 0 / 0 / 16 | 5272 / 12 / 0 / 16 | 9 | 5288 / 0 / 0 / 16 |
| 2 | 3217 | `8549c5a79686` | 5274 / 0 / 0 / 16 | 5273 / 1 / 0 / 16 | 1 | 5274 / 0 / 0 / 16 |
| 3 | 3209 | `bc57a65d6154` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 4 | 2969 | `82e3442fa3c7` | 5274 / 0 / 0 / 16 | 5275 / 1 / 0 / 16 | 1 | 5276 / 0 / 0 / 16 |
| 5 | 3215 | `2f0d713b396d` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 6 | 3216 | `6a7aa837d500` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 7 | 3214 | `5b57beacc211` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 8 | 3213 | `57c372782935` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 9 | 3211 | `7a45519e886f` | 5274 / 0 / 0 / 16 | 5273 / 1 / 0 / 16 | 1 | 5274 / 0 / 0 / 16 |
| 10 | 3206 | `5b7a089b1578` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 11 | 3210 | `86ee7c61a533` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 12 | 3204 | `ee289546df0a` | 5274 / 0 / 0 / 16 | 5274 / 6 / 0 / 16 | 6 | 5280 / 0 / 0 / 16 |
| 13 | 3201 | `b6aaaca75fd3` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 14 | 3199 | `9c436b34ccaf` | 5274 / 0 / 0 / 16 | 5274 / 2 / 0 / 16 | 1 | 5276 / 0 / 0 / 16 |
| 15 | 3197 | `f1a91515811a` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 16 | 3195 | `8882fe3238a3` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 17 | 3190 | `ce720cb81614` | 5274 / 0 / 0 / 16 | 5274 / 2 / 0 / 16 | 2 | 5276 / 0 / 0 / 16 |
| 18 | 3185 | `c97c0b54f58d` | 5274 / 0 / 0 / 16 | 5273 / 2 / 0 / 16 | 2 | 5275 / 0 / 0 / 16 |
| 19 | 3177 | `dab45b6e440a` | 5274 / 0 / 0 / 16 | 5273 / 2 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 20 | 3163 | `28e58aa93c0d` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 21 | 3164 | `857e46ae5bf7` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 22 | 3140 | `760627e5455f` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 23 | 3143 | `c1376e051cb6` | 5274 / 0 / 0 / 16 | 5275 / 2 / 0 / 16 | 2 | 5277 / 0 / 0 / 16 |
| 24 | 3160 | `27afd50c783a` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 25 | 3159 | `0d558fb180ad` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 26 | 3165 | `1e376e81debe` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 27 | 3167 | `3b4be4abd4a8` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 28 | 3176 | `b073d8adf1d6` | 5274 / 0 / 0 / 16 | 5273 / 2 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 29 | 3172 | `510a9c227e2d` | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 30 | 3168 | `797c4c4481da` | 5274 / 0 / 0 / 16 | 5274 / 5 / 0 / 16 | 5 | 5279 / 0 / 0 / 16 |

`TASKS.json` records full commit subjects and dates, all changed source/test
paths, runnable targets, patch byte counts and hashes, arm runtimes, normalized
JUnit hashes, mapped failure identities, and exact expected trees.

## Patch semantics

For every task:

- `pr-N.patch` contains the reduced production and test changes together.
- `pr-N.tests.patch` contains only the exact test subset.
- both are generated from the landing's first-parent diff with binary/full
  indexes and 50% rename detection, after requiring A/M-only reduced changes.
- the complete patch is applied to a fresh base, never layered on the
  tests-only arm.

No perturbation sweep was run during fixture construction.
