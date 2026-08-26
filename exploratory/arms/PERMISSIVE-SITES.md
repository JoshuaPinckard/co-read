6 validated / 14 attempted / 77 census with both-sides tests.

# PERMISSIVE sites

## Census

The `_all_merges` rows retain `evaluation_status` and both-side test-touch metadata, but they do **not** retain complete base-to-parent changed-path sets. I therefore recovered the two path sets directly from the task-owned bare mirrors with read-only Git plumbing and intersected them. No corpus working tree was created, no corpus mirror was used for validation, and validation ran only against physical scratch copies.

| Repository | CLEAN merges | CLEAN with a common changed path | Of those, both sides touch tests |
|---|---:|---:|---:|
| `pallets/click` | 745 | 133 | 34 |
| `pygments/pygments` | 236 | 43 | 29 |
| `gohugoio/hugo` | 95 | 15 | 6 |
| `apache/commons-lang` | 194 | 29 | 8 |
| **Total** | **1,270** | **220** | **77** |

The reported census follows the prompt-literal, rename-aware `git diff --name-only` path names. The exact command forms used were:

```text
# Click and Pygments final rename-aware audit
git --git-dir=<absolute-task-owned-bare-mirror> -c core.quotepath=false diff --name-only -z <merge_base>..<parent> --

# Hugo census
git -C <task-owned-bare-mirror> diff --name-only <merge_base>..<parent> --

# Commons Lang census
GIT_NO_LAZY_FETCH=1 GIT_TERMINAL_PROMPT=0
git --git-dir=<task-owned-bare-mirror> diff --name-only <merge_base>..<parent>
```

No command created a working tree or mutated a corpus mirror. The Python final audit inherited the environment unchanged on Git 2.46.0.windows.1, with `diff.renames` unset; the language-specific Go and Java evidence records their own exact invocations. The exhaustive no-renames recovery first supplied a candidate superset, then the literal command was checked on every member of that superset: Click remained 133 / 34 and Pygments 43 / 29, with identical common-path arrays. Rename detection cannot add a pathname absent from its delete-plus-add representation, so rows outside that superset cannot enter the rename-aware intersection.

As a miner-coordinate sensitivity, explicit `--no-renames` changes Hugo from 15 to 49 and the total from 220 to 254. The extra 34 are entirely docs-subtree rename/delete-versus-edit histories, not pairs of byte edits to an unchanged pathname, and none is a both-sides-tests row. For example, `f8fd5796…` is `R100 content/templates/homepage.md -> docs/content/templates/homepage.md` on one side and `M content/templates/homepage.md` on the other; no-renames exposes the old-path deletion and makes that endpoint common. The complete no-renames sensitivity artifact SHA-256 is `72bcce55909bba1719ea2f2cf7d695fe137707d81ea3afb2132d3d92c254e6a1`. This sensitivity does not change the 77-candidate validation pool, any attempt, or the verdict.

The source JSONL SHA-256 values are `43353975e39a05974ec225c2293d563b9effe504c8c811ae6025086fd1c964b8` (Click), `986cf6bab99f8489d680f1d758ab64ab94c78b182088768f1586061ca610d464` (Pygments), `30de1c45d450f5d6ac5eac4314099ddab0a17a01f370b0efbf817d75327504aa` (Hugo), and `15a361434ee0a353a5e10fc079bd53ced867cee7548743f02bd65becd3abe120` (Commons Lang).

## Verdict

Byte-disjoint same-file concurrency is **not sufficient** to preserve the two sides' focal contracts. All six validated sites pass the required per-side green-red-green (GRG) gate, and all six are strictly byte-disjoint on every common file under `conflict-byte-overlap-v4-pinned-bounded-linear`. Five canonical integrated source patches are `JOINTLY_SATISFIABLE`; one is `MUTUALLY_UNSATISFIABLE`.

The counterexample is Click merge `c63c70dabd3f86ca68678b4f00951f78f52d0270`. Both sides validate independently, Git merges the common files cleanly, and a byte-exclusive claim system using the pinned ranges would permit all writes. Nevertheless, the exact canonical integrated sources fail parent 2's focal `tests/test_options.py::test_show_default_boolean_flag_value` in both joint reruns. This is a focal-contract safety failure, not a claim about full-suite or whole-program correctness.

| Integrated result among the 6 GRG-validated sites | Count |
|---|---:|
| `JOINTLY_SATISFIABLE` | 5 |
| `MUTUALLY_UNSATISFIABLE` | 1 |
| `NOT_CONSTRUCTIBLE_TEXTUAL_SOURCE_CONFLICT` | 0 |

The machine-readable, schema-compatible rows and their complete raw checks and byte ranges are in `exploratory/arms/sites-permissive.json`. Only validated rows are included there.

## Gate and stopping rule

The six validations all came from Click before the global stop condition fired. Pygments had no attempted candidate before that stop. Hugo produced 0 validated / 1 attempted, and Commons Lang produced 0 validated / 2 attempted. Apparatus-only setup retries and candidates stopped before a terminal gate verdict are not counted as attempts.

| Lane | Terminal attempts | Validated | Rejected |
|---|---:|---:|---:|
| Click / frozen Python gate | 11 | 6 | 5 |
| Pygments / frozen Python gate | 0 | 0 | 0 |
| Hugo / focal-map + five-run gate | 1 | 0 | 1 |
| Commons Lang / compile + focal + five-run gate | 2 | 0 | 2 |
| **Total** | **14** | **6** | **8** |

The Python protocol is the frozen `exploratory/arms/protocol.json`, SHA-256 `fb1c0a9f9c7b48c30178d8a7e737250e18535ada63959c49c73c180505c69828`. The unchanged validator SHA-256 before and after the runs is `e04bc99409e584636ce8cb81cc22f6a7ca9c917f9ec9e46ab68a4c2328b6b6c6`; the fixed environment is CPython 3.11.9 and pytest 8.4.2 with the recorded environment and compatibility fingerprints.

The frozen Python selector only admits conflicted rows. It was not rewritten and CLEAN rows were not relabeled. Two scratch-only adapters supplied the unchanged CLEAN rows directly to the frozen preparation and validation functions. Their only semantic boundary change was accepting the required clean `git merge-tree` exit `0` while retaining the exact invocation, result-tree identity, stdout hash, and empty-stderr checks. Adapter SHA-256 values are `9912cdc641001028341b52933d52d470fef4c7e1abeb8689536c3c85e72c48f6` and `f8533ba2abb1e6fd732bbfb12a86d9ed525b7e0216011381933ef7c0aec12960`.

The existing source/test partition, focal selection, five untouched-base runs, red test-patch-only predicate, green source-plus-test predicate, red-leaf identity check, and exact joint-source check were unchanged. Mining stopped as soon as the sixth GRG site validated.

## Validated site evidence

Counts below are pytest summary counts. Every untouched base was green in five identical runs; every red had at least one test-level focal failure; every corresponding green passed the red leaf identities. The complete attempts, normalized leaves, JUnit records, runtime fingerprints, patch manifests, and hashes are embedded in `sites-permissive.json`.

### 1. `3bb230dcd5d751f8605b46e9df5a541639d5fd4e`

- Repository/corpus line: `pallets/click`, 775
- Base: `7c99ebe23b931f27562d926814423cce85fd9766`
- Parents: `63274a79d08fdc5c19220696144489f7144a8547`, `0551bf53588ae87f462d336f24f853a156fefe3a`
- Raw result SHA-256: `240d32cbbeccfa7391c10d52be4ddf2f03fae790708a609de9ef97b96407862e`
- Prepared manifest SHA-256: `0637ff01f23bb924a962c3f4cca7cb3b099d8eac7f33317c3c92f2c10726f657`

| Side | Frozen focal target / overlay leaves | Untouched B ×5 | Test patch only (red) | Exact red leaves | Source + test (green) |
|---|---|---|---|---|---|
| P1 | `tests/test_termui.py` / 237 | 220 passed, 4 skipped | 2 failed, 224 passed, 11 skipped | `test_get_pager_file_flushes_stream_on_exception`; `test_get_pager_file_nullpager_keeps_stringio_stream` | 226 passed, 11 skipped; both red leaves passed |
| P2 | `tests/test_formatting.py` / 35 | 27 passed | 7 failed, 28 passed | seven `write_usage` cases, recorded in full in the JSON row | 35 passed; all seven red leaves passed |

Exact joint source/result patch: `JOINTLY_SATISFIABLE`; P1 had 226 passed / 11 skipped and P2 had 35 passed.

### 2. `1c3dfacddcb73bb256d0e604e239cfd4c75f71f2`

- Repository/corpus line: `pallets/click`, 94
- Base: `4a7fe69f942bd02b811548ff8f02a08fff7429c1`
- Parents: `3a2585cb70bb43897b9fa2d749421d8b99db1e87`, `48b899155ae212d87c6a004d4f50e4daa0486ba7`
- Raw result SHA-256: `98a0b84fde0de1fd1ceffb7713cae85fccc3048890de6fad5a86095c88d85a8b`
- Prepared manifest SHA-256: `67a1e73dbf309d127c8c2b6a06ed8653345f960b63b9ad65bb96ae7b1a7f7537`

| Side | Frozen focal target / overlay leaves | Untouched B ×5 | Test patch only (red) | Exact red leaves | Source + test (green) |
|---|---|---|---|---|---|
| P1 | `tests/test_options.py` / 14 | 13 passed, 5 warnings | 1 failed, 13 passed, 5 warnings | `test_multiple_default_help` | 14 passed, 5 warnings; red leaf passed |
| P2 | `tests/test_options.py` / 15 | 13 passed, 5 warnings | 2 failed, 13 passed, 5 warnings | `test_argument_custom_class`; `test_option_custom_class` | 15 passed, 5 warnings; both red leaves passed |

Exact joint source/result patch: `JOINTLY_SATISFIABLE`; P1 had 14 passed and P2 had 15 passed.

### 3. `be28b6c6f9d001f230614b5f9be2c50b30c6cb3a`

- Repository/corpus line: `pallets/click`, 270
- Base: `084da90d9bd55c082dec70376f718ee6a7f622fc`
- Parents: `31d19ba9c3e04d518888f68d8ebe3add6e5abc4b`, `fbd18ece3215e924c1a050a365c02924f1c3a4be`
- Raw result SHA-256: `4956c8b8274a9dc3592be3e4bca796c0660560cda123b5405917e095bb66e6ae`
- Prepared manifest SHA-256: `f1847f31f5ee8396e426fa89f315dda0aa560374b34ea0155ac7d0fa114dd151`

| Side | Frozen focal target / overlay leaves | Untouched B ×5 | Test patch only (red) | Exact red leaves | Source + test (green) |
|---|---|---|---|---|---|
| P1 | `tests/test_arguments.py` / 18 | 17 passed | 1 failed, 17 passed | `test_multiple_param_decls_not_allowed` | 18 passed; red leaf passed |
| P2 | `tests/test_bashcomplete.py` / 17 | 15 passed | 5 failed, 12 passed | `test_argument_choice`; `test_chaining`; `test_long_chain_choice`; `test_variadic_argument_choice`; `test_variadic_argument_complete` | 17 passed; all five red leaves passed |

Exact joint source/result patch: `JOINTLY_SATISFIABLE`; P1 had 18 passed and P2 had 17 passed.

### 4. `8df9a6b2847b23de5c65dcb16f715a7691c60743`

- Repository/corpus line: `pallets/click`, 265
- Base: `a0e0328e142f63a6e98e69ae20220a51142251d3`
- Parents: `011fd621967b740080965946ee4938bb6c31fe25`, `a94c0be3b53997b55ce5a0808da4ca3b0a0f44dc`
- Raw result SHA-256: `542b16ca2b8ef0de9207e4e968b50e84bb6276828a26692ffa707bca562c2bbf`
- Prepared manifest SHA-256: `2a5c69248373b903e4922395a7638c2300fc7d1bc8073d922240062ddd83c263`

| Side | Frozen focal target / overlay leaves | Untouched B ×5 | Test patch only (red) | Exact red leaves | Source + test (green) |
|---|---|---|---|---|---|
| P1 | `tests/test_formatting.py` / 12 | 11 passed | 1 failed, 11 passed | `test_truncating_docstring` | 12 passed; red leaf passed |
| P2 | `tests/test_context.py`, `tests/test_testing.py` / 28 | 26 passed, 1 skipped | 1 failed, 26 passed, 1 skipped | `test_exit_not_standalone` | 27 passed, 1 skipped; red leaf passed |

Exact joint source/result patch: `JOINTLY_SATISFIABLE`; P1 had 12 passed and P2 had 27 passed / 1 skipped.

### 5. `c70d4636831e391016895587f7ed10e96f49773e`

- Repository/corpus line: `pallets/click`, 233
- Base: `bd597172d7d6c7bd6d0a89ea89c0fd234452b290`
- Parents: `a00e01845100ce2b3d5288a2b655aad260346361`, `138a6e3ad1dbe657e09717bc05ebfbc535f4770d`
- Raw result SHA-256: `587fcc720a73491e7644493ba8bf9665be00bed6477fd0363e6506d6cba3ce96`
- Prepared manifest SHA-256: `d15f11eac37bb4a377c7212ca65e0512843e298a25931e7029a873706e368298`

| Side | Frozen focal target / overlay leaves | Untouched B ×5 | Test patch only (red) | Exact red leaves | Source + test (green) |
|---|---|---|---|---|---|
| P1 | `tests/test_commands.py`, `tests/test_options.py` / 50 | 48 passed | 3 failed, 47 passed | `test_auto_shorthelp`; `test_dynamic_default_help_text`; `test_dynamic_default_help_unset` | 50 passed; all three red leaves passed |
| P2 | `tests/test_options.py` / 34 | 33 passed | 1 failed, 33 passed | `test_case_insensitive_choice` | 34 passed; red leaf passed |

Exact joint source/result patch: `JOINTLY_SATISFIABLE`; P1 had 50 passed and P2 had 34 passed.

### 6. `c63c70dabd3f86ca68678b4f00951f78f52d0270` — counterexample

- Repository/corpus line: `pallets/click`, 525
- Base: `490ac01891e726c8b70a700b91da6c137699382a`
- Parents: `051d57cef4ce59212dc1175ad4550743bf47d840`, `ee5fdbf1f9e267247d6de765329d2cc9bdd76206`
- Raw result SHA-256: `20e2b86542275462afc538ff48da43f8500776dd66215084241412f689ff6a10`
- Prepared manifest SHA-256: `890b4163498972e8773448aa495980c31e9c492c5b55ac3e3cd224fa275179e4`

| Side | Frozen focal target / overlay leaves | Untouched B ×5 | Test patch only (red) | Exact red leaves | Source + test (green) |
|---|---|---|---|---|---|
| P1 | `tests/test_formatting.py`, `tests/test_options.py`, `tests/test_shell_completion.py` / 146 | 145 passed | 2 failed, 144 passed | `test_global_show_default`; `test_hide_false_default_boolean_flag_value[False]` | 146 passed; both red leaves passed |
| P2 | `tests/test_options.py` / 96 | 95 passed | 1 failed, 95 passed | `test_dynamic_default_help_special_method` | 96 passed; red leaf passed |

The exact joint source patch is constructible and has no textual source-conflict path, but the joint status is `MUTUALLY_UNSATISFIABLE`:

| Joint focal set | Attempt 1 | Attempt 2 | Stability |
|---|---|---|---|
| P1 | 146 passed | 146 passed | `STABLE_GREEN` |
| P2 | 1 failed, 95 passed | 1 failed, 95 passed | `STABLE_QUALIFYING_RED` |

The failing identity in both P2 joint attempts is `tests/test_options.py::test_show_default_boolean_flag_value`, with assertion detail `assert '[default: False]' in 'Enable the cache.'`.

The canonical CLEAN tree is `fceeff9682963158a9941706ac593fd440a194a0` and independently equals the merge commit tree. Both reruns applied joint-source patch SHA-256 `d6018f5d2800aea972002bb4d3bef501470e96679572251422b0a73e45aeef6e`. The two P2 normalized attempts share SHA-256 `3798f95ca07e8382997e78d1071055c1e7024fbcf1cb6448edba1547ec9ed62f`, leaf signature `c1319941b3e3d245bc0d5713894a73318848ff74e5d029a79ae0ef535e578c45`, and JUnit outcome signature `66e705f77bd09eceef692540465f57a09237d7c22285fbbc972771b762d2f1da`.

The semantic interaction is distant in the files. P1 changes Click so a single boolean option with false/none default omits the default from help, and replaces the old test with tests expecting the default to be hidden. P2 separately changes callable-default detection from general `callable(...)` to `inspect.isfunction(...)` and adds a special-method test earlier in `tests/test_options.py`; its focal patch retains the base test that expects `[default: False]`. Each side passes against its own source. The clean integrated source incorporates P1's hide-false behavior while P2's retained focal expectation remains, so the old P2 test fails. Git accepted the merge because the physical edit ranges are disjoint; it has no model of this cross-range behavioral dependency.

Scope caveat: the joint gate applies the exact canonical **joint-source** patch and then each parent's test patch separately. The upstream merged `tests/test_options.py` adopts P1's renamed/revised Boolean tests, so it no longer contains the old failing node ID. The verified counterexample is that the canonical integrated sources cannot satisfy both parents' exact focal obligations; it is not evidence that the merge commit's own integrated test tree or full suite fails.

## Byte-overlap classification

Coordinates are zero-based offsets into the base blob. Consumed intervals are half-open `[start,end)`; zero-width insertions are anchors. The exact implementation is the unchanged miner, SHA-256 `07e695c89e34024e24ed8313f57711e130534052713a9342315460df43cb9170`, rule `conflict-byte-overlap-v4-pinned-bounded-linear`. Every result blob below differs from the base and both parent blobs, consistent with Git incorporating both sides.

| Site | Common files | Strict-overlap paths | Boundary-contact paths | Classification | Consequence for byte-exclusive claims / why Git accepted |
|---|---|---:|---:|---|---|
| `3bb230dc…` | `CHANGES.rst` | 0 | 0 | `strictly_disjoint_bytes` | Permit; distinct insertion anchors 2958 and 3139 |
| `1c3dfacd…` | `tests/test_options.py` | 0 | 0 | `strictly_disjoint_bytes` | Permit; distinct insertion anchors 3411 and 6454 |
| `be28b6c6…` | `CHANGES.rst`, `click/core.py` | 0 | 0 | `strictly_disjoint_bytes` | Permit; independent consumed intervals/anchors |
| `8df9a6b2…` | `CHANGES.rst`, `click/core.py` | 0 | 0 | `strictly_disjoint_bytes` | Permit; independent consumed intervals/anchors |
| `c70d4636…` | `tests/test_options.py` | 0 | 0 | `strictly_disjoint_bytes` | Permit; independent interval/anchors |
| `c63c70da…` | `.pre-commit-config.yaml`, `CHANGES.rst`, `src/click/core.py`, `tests/test_options.py` | 0 | 0 | `strictly_disjoint_bytes` | Permit; all four paths have disjoint edit scripts. Git's clean integration is nevertheless focal-incorrect |

There were no validated `boundary_contact_only` or `partially_overlapping` sites. Overlapping CLEAN candidates were not discarded: Hugo `62119022…` and Java `e0cfe47…` were attempted and recorded below, but both failed their language gates.

### Exact range record

The authoritative complete arrays are embedded per site at `/sites/<index>/byte_overlap/paths` in `sites-permissive.json`; they are not reconstructed from this prose. In the compact notation below, `I` means consumed intervals and `A` means insertion anchors. This index records every small array directly and gives the exact JSON pointer and counts for the two large arrays.

- `3bb230dc…`, `CHANGES.rst`, base 65,752 bytes: P1 `I=[] A=[2958]`; P2 `I=[] A=[3139]`.
- `1c3dfacd…`, `tests/test_options.py`, base 6,454 bytes: P1 `I=[] A=[3411]`; P2 `I=[] A=[6454]`.
- `be28b6c6…`, `CHANGES.rst`, base 24,062 bytes: P1 anchors `[1315,6581,6633,10805]`, intervals `[[6362,6365],[6366,6368],[6416,6417],[6418,6420],[6624,6625],[6626,6628],[6994,6995],[6996,6998],[7522,7525],[7526,7528],[7576,7579],[7580,7582],[7738,7739],[7740,7742],[7946,7947],[7948,7950],[8470,8471],[8472,8474],[8994,8997],[8998,9000],[9520,9523],[9524,9526]]`; P2 anchors `[2919,4222,4361,9213,9265,9371]`, intervals `[[2673,2725],[2744,2762],[2971,2972],[2973,2984],[2985,2989],[3004,3041]]`.
- `be28b6c6…`, `click/core.py`, base 75,028 bytes: P1 anchors `[]`, intervals `[[55667,55668],[56580,56581],[74394,74454],[74524,74531],[74583,74584]]`; P2 anchors `[7221,13541,48721,58664]`, intervals `[]`.
- `8df9a6b2…`, `CHANGES.rst`, base 23,851 bytes: P1 has 199 exact anchors and intervals `[[13366,13367],[20634,20635]]`; P2 `I=[] A=[189]`. The complete ordered 199-anchor array is at `/sites/3/byte_overlap/paths/0/parent1/anchors`; its source record SHA-256 is `108748afcecbb37a953092f41a64d574a148073163b002a5f85fd0ea5008600f`.
- `8df9a6b2…`, `click/core.py`, base 73,650 bytes: P1 `I=[] A=[32251]`; P2 `I=[[19867,19870]] A=[370,19866,19871,29401,29875]`.
- `c70d4636…`, `tests/test_options.py`, base 12,680 bytes: P1 `I=[[8918,8919]] A=[5278]`; P2 `I=[] A=[8491]`.
- `c63c70da…`, `.pre-commit-config.yaml`, base 795 bytes: P1 `I=[[161,162]] A=[]`; P2 `I=[[102,104],[105,106]] A=[]`.
- `c63c70da…`, `CHANGES.rst`, base 40,202 bytes: P1 `I=[] A=[26]`; P2 `I=[] A=[182]`.
- `c63c70da…`, `src/click/core.py`, base 111,407 bytes: P1 has 24 intervals and 19 anchors, all recorded at `/sites/5/byte_overlap/paths/2/parent1`; P2 `I=[[103512,103519]] A=[25,103511]`. The complete source record SHA-256 is `7ef8a5a35bf01a7fba7b9dbe3545c24ef71ef0ee0e6108ac07a8b4bd3dd9ce00`.
- `c63c70da…`, `tests/test_options.py`, base 26,109 bytes: P1 `I=[[21913,21917],[22172,22178]] A=[21807,21887,21888,21996,22010,22029,22054,22055,22179]`; P2 `I=[] A=[8833]`.

No listed path has strict contact or boundary contact. Thus a claimant implementing these exact half-open intervals and insertion anchors would allow both writers at every validated site.

## Rejections (exact failing checks)

Rejections are gate results, not evidence that an integrated merge is safe or unsafe.

### Python

`099b54955fccb6a3dd9eddf0ff706f92aaf6a1a5`, raw result SHA-256 `061d42aece3421ca1e0d3451e7e7cdc8a145e2d89a271246523e53f0c91b4f99`:

```text
parent1: red test-patch-only check: pytest exit was 0, expected test-failure exit 1; no test-level failing leaf identity was recorded; JUnit recorded no test-level failure or error
```

`bc53a964691844d33675beb60d1d9921312258ef`, raw result SHA-256 `786a81c1e34299c8451bef26fc4166efd9873c4c61499ac7996d9cfb8f0c5a89`:

```text
parent1: base determinism: untouched B was not pytest-green in attempts [1, 2, 3, 4, 5]; base attempt 1: pytest exit was 1, expected 0; JUnit green predicate failed: failure=3, error=0; base attempt 2: pytest exit was 1, expected 0; JUnit green predicate failed: failure=3, error=0; base attempt 3: pytest exit was 1, expected 0; JUnit green predicate failed: failure=3, error=0; base attempt 4: pytest exit was 1, expected 0; JUnit green predicate failed: failure=3, error=0; base attempt 5: pytest exit was 1, expected 0; JUnit green predicate failed: failure=3, error=0
```

`c55d7d24ed0a64fbb646f1f01c41eb35a89d76eb`, raw result SHA-256 `9b98042a5b845f25052b0b715973755211a21b0c16c1de0ec42d5f0631967c20`:

```text
parent1: base determinism: untouched B was not pytest-green in attempts [1, 2, 3, 4, 5]; base attempt 1: pytest exit was 1, expected 0; JUnit green predicate failed: failure=10, error=0; base attempt 2: pytest exit was 1, expected 0; JUnit green predicate failed: failure=10, error=0; base attempt 3: pytest exit was 1, expected 0; JUnit green predicate failed: failure=10, error=0; base attempt 4: pytest exit was 1, expected 0; JUnit green predicate failed: failure=10, error=0; base attempt 5: pytest exit was 1, expected 0; JUnit green predicate failed: failure=10, error=0
parent2: base determinism: untouched B was not pytest-green in attempts [1, 2, 3, 4, 5]; base attempt 1: pytest exit was 1, expected 0; JUnit green predicate failed: failure=2, error=0; base attempt 2: pytest exit was 1, expected 0; JUnit green predicate failed: failure=2, error=0; base attempt 3: pytest exit was 1, expected 0; JUnit green predicate failed: failure=2, error=0; base attempt 4: pytest exit was 1, expected 0; JUnit green predicate failed: failure=2, error=0; base attempt 5: pytest exit was 1, expected 0; JUnit green predicate failed: failure=2, error=0
```

`6a141c3681027e8124ce5a3c70e608dbbebffafb`, raw result SHA-256 `b6a298bc3c5639d0f379bd79c00adfe0d6d35e955bd446c74e2e8f913b030e2f`:

```text
parent2: red test-patch-only check: pytest exit was 0, expected test-failure exit 1; no test-level failing leaf identity was recorded; JUnit recorded no test-level failure or error
```

`c3687bf1d2d5c175ee50dc083fce5b19de152de0`, raw result SHA-256 `6590fa02a1e5c41e50725b554da5ed51eceadd4fbbef464a70f15125f9db68da`:

```text
parent2: red test-patch-only check: pytest leaf recorder artifact is missing or invalid; pytest collected zero focal leaf items; fixed collector targets with zero mapped leaves: ['tests/test_shell_completion.py', 'tests/test_types.py']; normalized leaf nodeid/outcome evidence is missing or hash-invalid; normalized JUnit evidence is missing or invalid; collected leaf ids differ from the frozen test-overlay leaves; pytest exit was 0, expected test-failure exit 1; no test-level failing leaf identity was recorded; JUnit recorded no test-level failure or error
```

### Go

`62119022d1be41e423ef3bcf467a671ce6c4f7dd`, authoritative result SHA-256 `02c06eb849eb55291af421636d6f83cff2c0e8bc72f184d58c0712f7a605ac0e`:

```text
failed check: parent1.red_test_patch_only
reason: ./output: missing focal terminal events ['TestLayout']
raw stderr:
go: github.com/gohugoio/hugo/output: package github.com/kylelemons/godebug/diff imported from implicitly required module; to add missing requirements, run:
	go get github.com/kylelemons/godebug/diff@v1.1.0
```

This CLEAN candidate is `partially_overlapping`: on all six common paths P1 and P2 made byte-identical edits, so Git coalesced them. A byte-exclusive claimant would block rather than permit it. The complete overlap artifact SHA-256 is `e6534c105bf9fa67a6328aae2e1a820c4e90ebc7cd2c9ab394e6068eb0c83f6b`.

The frozen Go executables used for this rejection were focalmap SHA-256 `cf2857b53ae7cf269054a1bcfd311eeceb4beb39ce1478fa8d60adb2d81733d2`, focalgate SHA-256 `44c08c3d334e1113b93c32f659d328140ba26287ea2bedf56584e066c3494067`, and Go SHA-256 `0fea51e5fd529ec7d7cab943b93c12fd74664a7edc3954125777ef1cf66ef50e`.

Its identical P1/P2 ranges are: `common/hugo/version_current.go` intervals `[[757,758],[773,774]]`, anchor `[790]`; `hugolib/content_map.go` anchors `[19630,20364]`; `hugolib/disableKinds_test.go` interval `[[10090,10091]]`, anchors `[1434,10240]`; `hugolib/page__paths.go` intervals `[[2141,2143],[2144,2147]]`; `hugolib/site_render.go` intervals `[[7068,7070],[7071,7074],[7250,7252],[7254,7261],[7269,7296]]`, anchors `[7179,7253,7315]`; and `snap/snapcraft.yaml` intervals `[[24,25],[26,27],[339,343],[344,345]]`.

### Java

`c8cc65165143ead3df00f05c9eedaf23ad095bf9`:

```text
at least one offline focal run was not green; focal baseline contained failure/error/rerun-flaky outcomes
```

All five base runs returned 1 with 112 passes and the same two failures: `FastDateParserTest.testParseZone` and `FastDateParserTest.testTzParses`. It is `strictly_disjoint_bytes` on `FastDateParser.java` (P1 anchors 21516 and 33338; P2 anchor 29880), so byte claims would permit it, but the untouched-base prerequisite prevents a correctness verdict.

`e0cfe47b77d9a36c6b579b9b3fa3eb95c5c3c265`, GRG result SHA-256 `5d65002d99a31b739c6ef0726ac6b09fc0d0608848ee6ddfb3ac990c0f7988a4`:

```text
parent1: red run produced zero testcase records
parent1: red missing focal classes: ['org.apache.commons.lang3.CharSequenceUtilsTest', 'org.apache.commons.lang3.concurrent.LocksTest']
parent1: red run recorded no test-level failure or error
parent1: red/green testcase identity sets differ: red_only=[], green_only=[('org.apache.commons.lang3.CharSequenceUtilsTest', 'testConstructor'), ('org.apache.commons.lang3.CharSequenceUtilsTest', 'testNewLastIndexOf'), ('org.apache.commons.lang3.CharSequenceUtilsTest', 'testRegionMatches'), ('org.apache.commons.lang3.CharSequenceUtilsTest', 'testSubSequence'), ('org.apache.commons.lang3.CharSequenceUtilsTest', 'testSubSequenceNegativeStart'), ('org.apache.commons.lang3.CharSequenceUtilsTest', 'testSubSequenceTooLong'), ('org.apache.commons.lang3.CharSequenceUtilsTest', 'testToCharArray'), ('org.apache.commons.lang3.concurrent.LocksTest', 'testReadLock'), ('org.apache.commons.lang3.concurrent.LocksTest', 'testResultValidation'), ('org.apache.commons.lang3.concurrent.LocksTest', 'testWriteLock')]
```

P2's GRG passed, but both sides are mandatory. This candidate is `partially_overlapping`: both parents made identical changes on `Locks.java` and `LocksTest.java`, and the merge result equals both parent blobs. Git cleanly coalesced identical edits; a byte-exclusive claimant would block them. These overlapping CLEAN candidates were recorded rather than silently discarded.

On `Locks.java` (base 5,041 bytes), both sides have intervals `[[3215,3216],[4705,4707],[4708,4711]]` and anchors `[3166,3315,3457,3609,3763,3924,4087,4495,4742,4918]`. On `LocksTest.java` (base 3,655 bytes), both sides have anchors `[849,908,1492,1800]` and no intervals. The strict overlap is therefore exact and caused by equal edits, not an ambiguous diff alignment.

The exact Java gate report SHA-256 is `080ec555c5a0ea033c6c806914ba15aca5fc5231d08f8b40f51b4e8a72270331`; the complete eight-candidate byte artifact SHA-256 is `66e4080d49fd95d074e1972ca8a2ee9fa672d239d101fcf7c88216a7d5b3c399`.

## Claims that could NOT be verified

- Full-suite, whole-program, production, or historical-intent correctness was not verified. The positive and negative integrated verdicts are exact but focal-scoped.
- The merge commit's own integrated test tree was not run as a combined suite. In the counterexample it does not contain the old P2 node ID; the negative result comes from testing canonical joint sources against each parent's preserved focal contract separately.
- The five jointly satisfiable sites are not proved free of other semantic interactions; they only pass both frozen side contracts under the exact joint sources.
- The `c63c70da…` counterexample proves one focal incompatibility under the frozen environment, not every possible behavior or environment.
- No Pygments candidate and no additional Hugo or Java candidate was validated before the global stop. The six validated sites are all Click, so repository-wide or language-wide prevalence is unsupported.
- The 63 unattempted members of the 77-candidate both-tests pool were not evaluated after the required stop.
- No actual arms scheduler or production byte-claim implementation was run. Permission is inferred from the pinned half-open interval/anchor rule; another implementation may reserve insertion boundaries differently.
- The reported rename-aware census is 220; the miner-coordinate no-renames sensitivity is 254 because it additionally treats 34 Hugo rename/delete-versus-edit old-path endpoints as common. The six validated rows are ordinary same-path byte edits and are unaffected by this naming sensitivity.
- There is no validated boundary-contact or partially-overlapping site. The encountered partial-overlap candidates were rejected by their gates and, in any event, would be serialized by a byte-exclusive claimant.
- Five identical base runs reduce observed flakiness but do not prove universal determinism.
- The corpus mirrors establish Git objects and histories, not an independently archived historical dependency universe.

## What would change this verdict

- The under-blocking verdict would be withdrawn or narrowed if the frozen `c63c70da…` raw result, prepared patch, canonical result tree, or byte ranges failed hash verification; if an exact rerun did not reproduce the same parent-2 joint failure twice; or if the failing test were shown not to belong to the frozen focal contract.
- A claim policy that reserves semantic units, whole files, or a larger neighborhood around edits would not permit the counterexample's concurrent writes. That would change the policy conclusion, not the observed Git merge or focal failure.
- A corrected corpus identity, merge base, parent identity, or recovered path intersection would change the census and could remove a row from this stratum.
- Running full suites or additional environments could add failures or narrow the five positive joint verdicts. It cannot erase the recorded focal counterexample unless it exposes an apparatus/provenance defect in this run.
- Evaluating more of the 77 candidates could change frequency and language-diversity claims. It would not by itself negate the existence of the validated counterexample.
- Changing the pinned byte refinement/alignment or insertion-anchor ownership rule could reclassify a site; any such result would be a different stratum definition and must be reported separately.

## Per-claim confidence

| Claim | Confidence | Reason |
|---|---|---|
| Census is 1,270 CLEAN / 220 same-file / 77 both-tests | **High** | Exhaustive read-only path recovery plus prompt-literal rename-aware checks; the Click/Pygments superset audit changed zero rows, and the Hugo/Java census artifacts and no-renames sensitivity hash are recorded |
| Six rows satisfy per-side GRG | **High, environment-conditional** | Frozen validator/protocol hashes, five base runs, test-level red identities, greens, manifests, batches, and runtime pre/postflight evidence are embedded and hash-linked |
| All six validated rows are strictly byte-disjoint | **High under the pinned rule** | Exact base-coordinate intervals/anchors were computed with the unchanged pinned miner; no common path has strict or boundary contact |
| A byte-exclusive claimant using these ranges would permit all six | **High under the stated claim semantics** | Every P1/P2 range set is disjoint, including insertion-anchor contact; implementations with different anchor ownership are outside this claim |
| `c63c70da…` is an integrated focal-contract counterexample | **High, focal-scoped** | Both sides pass GRG independently; exact canonical joint sources are constructible; the same P2 leaf fails in both joint reruns while P1 is green |
| The other five integrated sources satisfy both focal contracts | **High, focal-scoped** | Each is `JOINTLY_SATISFIABLE` under the exact canonical joint source patch; no full-suite conclusion follows |
| The Hugo attempt was correctly rejected and is partially overlapping | **High under the frozen gate and pinned byte rule** | The focalmap, focalgate, Go executable, result, and complete overlap-artifact hashes are recorded; all six common-path edits are byte-identical across parents |
| The population-wide failure rate is 1/6 | **Unsupported as an estimate** | `1/6` describes only this stopped, outcome-observed validated set; selection was not a probability sample and all validations are Click |
| Corpus mirrors were not mutated | **High** | Census and overlap used read-only plumbing with no working tree; all build/test activity used separately owned physical scratch copies |
