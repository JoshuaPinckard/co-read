0 validated disjoint-stratum sites / 3 candidates attempted / 9 census total.

# Same-file-disjoint arms-site census and validation

The first line uses the preregistered, exact mined class `same_file_disjoint`. The separate `boundary_only` sensitivity class produced **2 validated / 3 attempted / 11 runnable-census** sites. Across both requested classes, the runnable-four result is **2 validated / 6 attempted / 20 census**. Boundary-only sites are not relabeled as same-file-disjoint.

The decisive result is therefore zero strict disjoint-stratum sites. Amendment 2's requirement is still unmet: this job found no validated `same_file_disjoint` site that can be added before phase 2. Because zero is below four, the census was extended to all 16 mined repositories as required.

## Census first

Selection was exact: `evaluation_status == conflicted` and `overlap.classification` equal to `same_file_disjoint` or `boundary_only`. “Both tests” is the recorded Boolean `both_sides_touched_tests`; it was also checked against `diffs.parent1.touches_tests && diffs.parent2.touches_tests`. A row without both-sides tests remains in the census and was not sent to a runner.

| Repository | Runnable gate | `same_file_disjoint` | Both tests | `boundary_only` | Both tests |
|---|:---:|---:|---:|---:|---:|
| `ansible/ansible` | no | 19 | 1 | 5 | 0 |
| `apache/commons-lang` | Java | 3 | 3 | 0 | 0 |
| `BurntSushi/ripgrep` | no | 0 | 0 | 0 | 0 |
| `chinabugotech/hutool` | no | 0 | 0 | 0 | 0 |
| `gohugoio/hugo` | Go | 1 | 0 | 0 | 0 |
| `hashicorp/terraform` | no | 7 | 4 | 1 | 1 |
| `hashicorp/terraform-provider-random` | no | 0 | 0 | 0 | 0 |
| `jupyter/notebook` | no | 1 | 1 | 0 | 0 |
| `pallets/click` | Python | 5 | 0 | 11 | 3 |
| `pallets/itsdangerous` | no | 1 | 0 | 3 | 0 |
| `prometheus/prometheus` | no | 0 | 0 | 0 | 0 |
| `psf/requests` | no | 2 | 0 | 1 | 0 |
| `pygments/pygments` | Python | 0 | 0 | 0 | 0 |
| `redis/redis` | no | 7 | 1 | 1 | 0 |
| `superfluid-org/protocol-monorepo` | no | 0 | 0 | 0 | 0 |
| `ydb-platform/ydb` | no | 0 | 0 | 0 | 0 |
| **Runnable four subtotal** |  | **9** | **3** | **11** | **3** |
| **Remaining 12 subtotal** |  | **37** | **7** | **11** | **1** |
| **All 16 repositories** |  | **46** | **10** | **22** | **4** |

The global totals reconcile exactly to the mined population: 46 `same_file_disjoint` plus 22 `boundary_only` equals 68. All 68 rows use schema 1 and overlap rule `conflict-byte-overlap-v4-pinned-bounded-linear`, have one merge base, and have a unique `(repo, full merge)` identity.

Among repositories lacking one of the four approved runners, `hashicorp/terraform` has the largest strict opportunity: **4 `same_file_disjoint` both-tests candidates**. It also has one boundary-only both-tests row, so a Terraform runner would buy five attempts across the two requested classes. Ansible, Jupyter, and Redis are next for the strict class with one each.

### Complete 68-row enumeration

Line numbers are one-based positions in `corpus/conflicts/<slug>.jsonl`. “Yes” marks the 14 rows eligible for validation if a gated runner exists; only the six in the runnable four were attempted.

| Repository | Line | Merge | Class | Both tests |
|---|---:|---|---|:---:|
| `ansible/ansible` | 4 | `2a8d1f07d4f979c7a7bf1ba6855040d6c63a8fb5` | `same_file_disjoint` | no |
| `ansible/ansible` | 7 | `d0994cd169aef0d70f742c418eb8764b1bbe8a84` | `same_file_disjoint` | no |
| `ansible/ansible` | 16 | `1366c663eb3f80bac54c2f4c255b6c70ef63346f` | `same_file_disjoint` | no |
| `ansible/ansible` | 23 | `05a4513a03aed0d0957f46f1c9c1ad71b2a8e920` | `boundary_only` | no |
| `ansible/ansible` | 24 | `78fdedd4906da6b4a3747f213984b75552e37ed3` | `same_file_disjoint` | no |
| `ansible/ansible` | 30 | `727cee509cf3a7c568382792793ec0cb2acb80e6` | `boundary_only` | no |
| `ansible/ansible` | 31 | `8f5c9aec47a61a2a44fcf020244aa12bc46197a0` | `same_file_disjoint` | no |
| `ansible/ansible` | 32 | `c2988dfdb0396cab4f2863bbabbdb971d999932b` | `same_file_disjoint` | no |
| `ansible/ansible` | 33 | `bc02e20503b1781ce232b9e4ada782bd52a5f3d7` | `same_file_disjoint` | no |
| `ansible/ansible` | 37 | `47b9cc63119c0a9b73d364150251364b45fd4b17` | `same_file_disjoint` | no |
| `ansible/ansible` | 38 | `912e3a7b0bc9722d510336a588a3dabb7de504af` | `same_file_disjoint` | no |
| `ansible/ansible` | 39 | `65178290e717d3f04979710a7c18164bcf969a35` | `same_file_disjoint` | no |
| `ansible/ansible` | 40 | `6694b28d5189ffabe64730e4365ea4c09434c1d8` | `boundary_only` | no |
| `ansible/ansible` | 45 | `288c33e2861684f7ce77ced740ec809ec28c537c` | `same_file_disjoint` | no |
| `ansible/ansible` | 46 | `f820e8e719350be90a8bcce9ab1869ea3efd9b52` | `same_file_disjoint` | no |
| `ansible/ansible` | 48 | `a5c29b886eff0f20a01ddb3dae8eaf49ff3c51b9` | `same_file_disjoint` | no |
| `ansible/ansible` | 49 | `7eadf7800903d773f821831d1d7735ff93bbe67c` | `boundary_only` | no |
| `ansible/ansible` | 50 | `33242cacf3c4b12381bf0f79f58c8d3227bd340d` | `same_file_disjoint` | **yes** |
| `ansible/ansible` | 55 | `87bf16930e51e08b0b0b1d14f727f47dc9a7d048` | `same_file_disjoint` | no |
| `ansible/ansible` | 56 | `6fe369fca0c92c11a2cc8f1883f7e13e1aed1e8d` | `boundary_only` | no |
| `ansible/ansible` | 63 | `19c92b3a421928c9fae6e7bd4b536fadf7850a43` | `same_file_disjoint` | no |
| `ansible/ansible` | 65 | `8285ac5b315cfdc27e6b68e610e5c9bdb1411cb3` | `same_file_disjoint` | no |
| `ansible/ansible` | 67 | `e79171cbfb6f4b735416821225f68cd45b05bfec` | `same_file_disjoint` | no |
| `ansible/ansible` | 71 | `c751168895e6678a7b92c9c2cef36cb0065f8d9a` | `same_file_disjoint` | no |
| `apache/commons-lang` | 9 | `cfe63beeacf30e78c0fccb89132d6e50f650fa2f` | `same_file_disjoint` | **yes** |
| `apache/commons-lang` | 19 | `a3b74d9c230a85df8ebeef3169fc1cd0439c9a87` | `same_file_disjoint` | **yes** |
| `apache/commons-lang` | 22 | `ee87df847299c55c97347e6c11e00a283feb189d` | `same_file_disjoint` | **yes** |
| `gohugoio/hugo` | 97 | `a018259bcf13eaa69c539e745aa1e1c6936d10ad` | `same_file_disjoint` | no |
| `hashicorp/terraform` | 1 | `166847d5dcd782a7eb5b2fde9223ca306cc33c10` | `boundary_only` | **yes** |
| `hashicorp/terraform` | 2 | `0d1867c0b3c1822ef18808bc3a04250a871aff51` | `same_file_disjoint` | **yes** |
| `hashicorp/terraform` | 6 | `5113761f41b247451701462123aaee4d6f6dddb9` | `same_file_disjoint` | **yes** |
| `hashicorp/terraform` | 7 | `fafc32b18338a6fa4f7eef6d58760979b32a38e7` | `same_file_disjoint` | **yes** |
| `hashicorp/terraform` | 9 | `e317d6ec7b1ec72087d8d79964061fa90493c7a9` | `same_file_disjoint` | no |
| `hashicorp/terraform` | 15 | `81dc5ee328bd4d0f49c297dd133050e87a8299e3` | `same_file_disjoint` | no |
| `hashicorp/terraform` | 16 | `be6ae20ac1858e0df0bac6b66160d4a87f79f642` | `same_file_disjoint` | **yes** |
| `hashicorp/terraform` | 17 | `41a2376915eeda3b3ba23faba14e36d50311acf0` | `same_file_disjoint` | no |
| `jupyter/notebook` | 1 | `45faf9a949f418668dbafa7c3629eeac1deb82a6` | `same_file_disjoint` | **yes** |
| `pallets/click` | 3 | `61e5a163179362679fc5f6f04a1fd98cd2895f10` | `same_file_disjoint` | no |
| `pallets/click` | 4 | `8bd9f7a710fd74d141f5ecc25b212cf42b47ffc8` | `same_file_disjoint` | no |
| `pallets/click` | 5 | `9740a7479b532458bd28b83ab10954bc9b5e4b97` | `boundary_only` | no |
| `pallets/click` | 6 | `99e3d8d3fe0db51ba9cb6838afcf22cb54bc5c42` | `boundary_only` | no |
| `pallets/click` | 9 | `a85c5b7e3275d49108701397aa93c7fd26b9e42b` | `same_file_disjoint` | no |
| `pallets/click` | 10 | `81b2d3e3b674bb9fed91e48a5bdddd2e8649d25b` | `boundary_only` | no |
| `pallets/click` | 15 | `b79b6f0acbbf7f0096a2435a0ebd8212de7181d5` | `boundary_only` | no |
| `pallets/click` | 19 | `b3f0a13d260d953ff24ddc4be604daa8b8089607` | `boundary_only` | no |
| `pallets/click` | 23 | `41cb9c9f1d9c6d0d1aa5f30baedfc84c4fd3b43d` | `boundary_only` | no |
| `pallets/click` | 25 | `6fcda3e5fcddd9001b314e83a1e0546bd22c78c2` | `boundary_only` | no |
| `pallets/click` | 34 | `a50befdc3ac475826af75e48fd48b21530851176` | `boundary_only` | no |
| `pallets/click` | 39 | `11abf2bff0f48b7f7b04b38b6a70fb102ef17662` | `boundary_only` | **yes** |
| `pallets/click` | 50 | `0a81393fdf41edb0ab9d2f527eccdc8ce38d7d42` | `boundary_only` | **yes** |
| `pallets/click` | 53 | `9e9fe41a53d885d96e43dec7cd9eb69e352f801a` | `same_file_disjoint` | no |
| `pallets/click` | 68 | `9f63c3b477d444245b0cc21ac23c28f6e8f4c385` | `same_file_disjoint` | no |
| `pallets/click` | 73 | `240603f240a9ff179d834fede836060d897c6980` | `boundary_only` | **yes** |
| `pallets/itsdangerous` | 4 | `8abfce3e0e507f2151ad1932f06f68f9bc102bef` | `same_file_disjoint` | no |
| `pallets/itsdangerous` | 6 | `fca4d63c1c3a443f824667405b8b7ff27c0c69d6` | `boundary_only` | no |
| `pallets/itsdangerous` | 7 | `78cf8f0fcb693e961fd6b31b40f7b1e3c176a745` | `boundary_only` | no |
| `pallets/itsdangerous` | 8 | `4d342cb6c6efb817250d6169bc9dfeb65267780d` | `boundary_only` | no |
| `psf/requests` | 2 | `3347146a441d47defaa4a1fe40337b68496c6bb7` | `same_file_disjoint` | no |
| `psf/requests` | 9 | `098865122c040ed87e57c4f2f1b0b146f51448a1` | `boundary_only` | no |
| `psf/requests` | 11 | `014ec0eb2622369c2053d65435d002014617f4d7` | `same_file_disjoint` | no |
| `redis/redis` | 1 | `0c9ca0e11ca290392e2747596b89d18db175af7e` | `same_file_disjoint` | no |
| `redis/redis` | 2 | `e3f46030fcfd0ffd916d473b16d0ed07e138fefb` | `same_file_disjoint` | no |
| `redis/redis` | 5 | `4583c4f0ea0ef3cdb3cd1b0ede77e5b95be18327` | `same_file_disjoint` | no |
| `redis/redis` | 11 | `21dbc6499a538af07f52a41742cf1683f3fc9c23` | `same_file_disjoint` | **yes** |
| `redis/redis` | 12 | `ce260f736e483d40967d3e551f04534154c12aba` | `same_file_disjoint` | no |
| `redis/redis` | 20 | `0420c3276f96be8d2261187326cdafca94942577` | `boundary_only` | no |
| `redis/redis` | 22 | `2303ba1441989d9501f4a97f07cdc3efd9536117` | `same_file_disjoint` | no |
| `redis/redis` | 24 | `545a5046f5bd2156e29be513d127d911ad2a5a85` | `same_file_disjoint` | no |

The seven zero-row repositories are `BurntSushi/ripgrep`, `chinabugotech/hutool`, `hashicorp/terraform-provider-random`, `prometheus/prometheus`, `pygments/pygments`, `superfluid-org/protocol-monorepo`, and `ydb-platform/ydb`.

## Validation scope and frozen gates

The six runnable both-tests identities had already been executed inside the gates' frozen populations. This job joined them by repository plus full merge, base, parents, and corpus line; checked that current corpus and implementation hashes still equal the recorded hashes; and reused the immutable gate evidence. It did not rerun or overwrite the fixed-output result trees, in accordance with the instruction not to modify existing results. “Attempted” below therefore means an exact recorded gate attempt, not a newly launched duplicate process.

- Python used protocol SHA-256 `fb1c0a9f9c7b48c30178d8a7e737250e18535ada63959c49c73c180505c69828` and unmodified validator SHA-256 `e04bc99409e584636ce8cb81cc22f6a7ca9c917f9ec9e46ab68a4c2328b6b6c6`. Both match `SITES.md`. The three Click boundary-only rows are indices 5, 14, and 23 in its frozen both-tests population.
- Java used the existing compile, focal-map, and five-run gate. Current `instruments/arms/java/gate.py` SHA-256 `8222d45c4021fb341c5fe0c24f4b636f318131911be41556da3dece07702dbbc` matches the gate report's before/after binding; the Commons corpus SHA-256 remains `aad01a6946c91cada8e6f47097b77f49f692339cd4cc9c343d665d8328db391c`.
- Hugo's sole target-class row, `a018259bcf13eaa69c539e745aa1e1c6936d10ad`, has no test files on either side. The protocol therefore records it in the census but does not invoke `focalmap` or `focalgate`.
- Pygments has no row in either requested class.

| Repository / class | Census | Both-tests attempts | Passed | Rejected | Not validated (no both-tests) |
|---|---:|---:|---:|---:|---:|
| `pallets/click` / `same_file_disjoint` | 5 | 0 | 0 | 0 | 5 |
| `pallets/click` / `boundary_only` | 11 | 3 | 2 | 1 | 8 |
| `pygments/pygments` / both classes | 0 | 0 | 0 | 0 | 0 |
| `gohugoio/hugo` / `same_file_disjoint` | 1 | 0 | 0 | 0 | 1 |
| `apache/commons-lang` / `same_file_disjoint` | 3 | 3 | 0 | 3 | 0 |
| **Runnable four / both classes** | **20** | **6** | **2** | **4** | **14** |

## Per-site evidence: strict `same_file_disjoint`

These are Java runner-gate results. As `JAVA-RUNNER.md` specifies, a pass would mean base compilation, complete focal mapping/execution, and five identical green offline Surefire signatures; it would not itself be the later two-sided source/test discrimination check. No row passed.

| Merge | Parents / base | Compile | Focal map and five-run check | Verdict |
|---|---|---|---|---|
| `cfe63beeacf30e78c0fccb89132d6e50f650fa2f` | P1 `9604c853069562e8c4219ae27eaa4b2bad59eabc`<br>P2 `9efa153e441eb67526bf33200445095b30956ac4`<br>B `b37837ce638048384756850a3a6892519ddbc743` | PASS on JDK 11 after JDK 25 exit 1; JDK 8 not needed | **FAIL before five-run.** Exact check: mapped focal class absent at base: `src/test/java/org/apache/commons/lang3/ThreadUtilsTest.java`, `src/test/java/org/apache/commons/lang3/builder/ReflectionToStringBuilderExcludeWithAnnotationTest.java`, `src/test/java/org/apache/commons/lang3/builder/ReflectionToStringBuilderTest.java`, `src/test/java/org/apache/commons/lang3/test/SystemDefaultsSwitchTest.java`. Five-run: not run. | `REJECTED` |
| `a3b74d9c230a85df8ebeef3169fc1cd0439c9a87` | P1 `cac7a60abf0a4451a5c80ef57343a14ea1ba443f`<br>P2 `2b36e25f6699d3bcf5fd2762c824aefb40c424c5`<br>B `bd9adbb637a8a4aa5eb61c6fde2c576d0ab3c4fa` | PASS on JDK 11 after JDK 25 exit 1; JDK 8 not needed | **FAIL before five-run.** Exact check: mapped focal class absent at base: `src/test/java/org/apache/commons/lang3/StringUtilsContainsTest.java`, `src/test/java/org/apache/commons/lang3/StringUtilsEmptyBlankTest.java`, `src/test/java/org/apache/commons/lang3/StringUtilsTrimStripTest.java`. Five-run: not run. | `REJECTED` |
| `ee87df847299c55c97347e6c11e00a283feb189d` | P1 `83dd32b901dea25a571adcd6a976464c8a36601c`<br>P2 `b19b2d37dafb1f26360ee5e8ac6fa297e9ae86ab`<br>B `3ce3b27dbd579a918e97e1fb09e9b0153cc71a60` | PASS on JDK 11 after JDK 25 exit 1; JDK 8 not needed | 30 mapped focal classes existed at B. Runs 1–5 each exited 0 with 2,829 passes and identical normalized signature `918d1d106266b377871b027845fae0a4389b2ecf4b94072c888189833f15f4b8`. **FAIL:** `org.apache.commons.lang3.ValidateTest` produced no testcase record in every run; exact gate reason: “at least one mapped focal class produced no testcase record.” | `REJECTED` |

Raw Java evidence is retained under `exploratory/arms/java-gates/br-java-v4/`; the full authoritative report is `exploratory/arms/java-gate-report.json`, SHA-256 `72f3d30b0dcd2248111e56e4e397c8df360ccbb42b8514ad524df54deab7ed3e`.

## Per-site evidence: `boundary_only` sensitivity

Count notation is `pytest / normalized JUnit / recorder leaf`. Every base entry below is five fresh untouched-B runs with identical normalized JUnit and rich leaf outcome signatures.

| Merge | Parents / base | Parent 1 | Parent 2 | Verdict |
|---|---|---|---|---|
| `11abf2bff0f48b7f7b04b38b6a70fb102ef17662` | P1 `9c5769e2c372e506c6efa16d4da0f08150ea1a34`<br>P2 `02756dd41714939ae877cba89b04c1fdc6e0ff21`<br>B `68c93287a8195af539c85656927f5c67ba5631d5` | targets `tests/test_basic.py`, `tests/test_compat.py`, `tests/test_context.py`, `tests/test_termui.py`; overlay leaves 104.<br>Base x5 PASS: each exit 0; `{passed=93,xfailed=1} / {passed=93,skipped=1} / {passed=93,xfailed=1}`.<br>Red PASS: exit 1; `{failed=7,passed=96,xfailed=1} / {failure=7,passed=96,skipped=1} / {failure=7,passed=96,xfailed=1}`.<br>Green PASS: exit 0; `{passed=103,xfailed=1} / {passed=103,skipped=1} / {passed=103,xfailed=1}`; all 7 red leaves passed. | targets `tests/test_normalization.py`, `tests/test_options.py`; overlay leaves 41.<br>Base x5 PASS: each exit 0; `{passed=40} / {passed=40} / {passed=40}`.<br>Red PASS: exit 1; `{failed=3,passed=38} / {failure=3,passed=38} / {failure=3,passed=38}`.<br>Green PASS: exit 0; `{passed=41} / {passed=41} / {passed=41}`; all 3 red leaves passed. | `VALIDATED` |
| `0a81393fdf41edb0ab9d2f527eccdc8ce38d7d42` | P1 `655918a61e22cade722dacb9bf798e86b13093af`<br>P2 `96146c9d0b25d700d00b65c916739bd491dd15e0`<br>B `3e88d3dcadbe357c90059bbaeb691f4a2d20d9e6` | targets `tests/test_formatting.py`, `tests/test_options.py`; overlay leaves 110.<br>Base x5 PASS: each exit 0; `{passed=108} / {passed=108} / {passed=108}`.<br>Red PASS: exit 1; `{failed=2,passed=108} / {failure=2,passed=108} / {failure=2,passed=108}`.<br>Green PASS: exit 0; `{passed=110} / {passed=110} / {passed=110}`; both red leaves passed. | target `tests/test_options.py`; overlay leaves 95.<br>Base x5 PASS: each exit 0; `{passed=92} / {passed=92} / {passed=92}`.<br>Red **FAIL**: exit 3; `{failed=3,passed=92} / {failure=3,passed=92} / {failure=3,passed=92}`. Exact check: `pytest internal/plugin error; pytest exit was 3, expected test-failure exit 1`.<br>Green PASS: exit 0; `{passed=95} / {passed=95} / {passed=95}`. | `REJECTED` |
| `240603f240a9ff179d834fede836060d897c6980` | P1 `679a7a0eccbdded7a6e85680bdaaf08003765e01`<br>P2 `df2e5ed8c4e89f51ff4eddb9600d913083613e62`<br>B `8929d392781c8113bc569f388c15c47b94f86581` | targets `tests/test_arguments.py`, `tests/test_info_dict.py`, `tests/test_utils.py`; overlay leaves 285.<br>Base x5 PASS: each exit 0; `{passed=197,skipped=72,deselected=1000} / {passed=197,skipped=72} / {passed=197,skipped=72}`.<br>Red PASS: exit 1; `{failed=17,passed=196,skipped=72,deselected=1000} / {failure=17,passed=196,skipped=72} / {failure=17,passed=196,skipped=72}`.<br>Green PASS: exit 0; `{passed=213,skipped=72,deselected=1000} / {passed=213,skipped=72} / {passed=213,skipped=72}`; all 17 red leaves passed. | targets `tests/test_basic.py`, `tests/test_options.py`; overlay leaves 737.<br>Base x5 PASS: each exit 0; `{passed=732} / {passed=732} / {passed=732}`.<br>Red PASS: exit 1; `{failed=4,passed=733} / {failure=4,passed=733} / {failure=4,passed=733}`.<br>Green PASS: exit 0; `{passed=737} / {passed=737} / {passed=737}`; all 4 red leaves passed. | `VALIDATED` |

The recorded red failing leaves were:

- `11abf2...` parent 1: `tests/test_basic.py::test_repr`, `tests/test_compat.py::test_zsh_func_name`, `tests/test_context.py::test_parameter_source_commandline`, `tests/test_context.py::test_parameter_source_default`, `tests/test_context.py::test_parameter_source_default_map`, `tests/test_context.py::test_validate_parameter_source`, `tests/test_termui.py::test_progressbar_update_with_item_show_func`. Parent 2: `tests/test_normalization.py::test_choice_normalization`, `tests/test_options.py::test_case_insensitive_choice`, `tests/test_options.py::test_case_insensitive_choice_returned_exactly`.
- `0a8139...` parent 1: `tests/test_formatting.py::test_global_show_default`, `tests/test_options.py::test_hide_false_default_boolean_flag_value[False]`. Parent 2 recorded `tests/test_options.py::test_count_default_type_help`, `tests/test_options.py::test_file_type_help_default`, and `tests/test_options.py::test_multiple_option_with_optional_value`, but the run's exit 3/internal-plugin condition rejects it even though test-level failures were present.
- `240603...` parent 1: `tests/test_arguments.py::test_argument_help`, `tests/test_arguments.py::test_argument_help_optional_metavar`, four `test_deprecated_empty_help_no_leading_space` parameterizations, two `test_deprecated_usage_help_record` parameterizations, `tests/test_arguments.py::test_deprecated_usage_help_record_without_help`, four `tests/test_info_dict.py::test_argument_to_info_dict_help` parameterizations, `tests/test_info_dict.py::test_argument_to_info_dict_nargs`, `tests/test_info_dict.py::test_command[Nested Group]`, `tests/test_info_dict.py::test_command_to_info_dict_multiple_arguments`, and `tests/test_info_dict.py::test_parameter[Argument]`. Parent 2: `tests/test_basic.py::test_choice_argument_optional_metavar`, `tests/test_basic.py::test_datetime_argument_optional_metavar`, `tests/test_basic.py::test_version_option_ambiguous_import_name_errors`, `tests/test_basic.py::test_version_option_resolves_import_name_to_distribution`.

Python raw result hashes are `0ad97dd17144e0f292bf4df2a2c6e3c5466788d128dae7e4a4d5cfabe294a85d` (`11abf2...`), `513f8aa1de3e72d5a9874b88d4aefd4d04ad96217569a4a3be71ff6a53e05ff5` (`0a8139...`), and `b44c8572869a59958938845a2d81b0a7753f7ffb49eefcd955b388f615a60232` (`240603...`). Their manifests, batch records, and full attempt artifacts remain at the paths embedded in `sites.json`.

## Validated-row manifest

`exploratory/arms/sites-disjoint.json` follows the `sites.json` top-level and site-row schema. It contains only validated rows, so it has two Click rows, each mechanically copied from `sites.json` and augmented with `stratum: "boundary_only"`. It contains no `same_file_disjoint` row because none validated, and no rejected row because the requested file is a validated-site manifest.

- `sites-disjoint.json` SHA-256: `eaf816384674e1a81caa8a55407b3d9536f814966571ec80c2aef2f23a300a29`.
- Source `sites.json` SHA-256: `2a7e87029f40428859339cf4c91654eaec84c929485098111c4fd07fa4a84bc0`.
- Ordered 16-JSONL census manifest SHA-256: `f002d545a55fd5e92331c72ae9987d38f7b4207840d4060af8405ff1ba9f93d9`.

## Claims that could NOT be verified

- The fine-claiming safety question for strict same-file-disjoint work could not be tested: zero strict site survived the approved runnable gates.
- The two validated boundary-only sites do not verify the strict `same_file_disjoint` stratum and are not substituted for it.
- The seven strict both-tests candidates in the remaining 12 repositories were census-only. Without a frozen gated runner for those repositories, this report makes no claim that they would pass.
- Rows without both-sides tests were not validated. Their census presence does not show that a two-sided task oracle can be built.
- The Java gate is a compile/focal/determinism runner gate, not Python-style two-sided source/test red-green discrimination. Even a Java pass would need that semantic distinction before being called equivalent to a Python validated task.
- The existing exact gate executions were hash-verified and reused, but were not independently replayed in this job. A literal Java replay requires a disposable full project-layout copy because the approved runner fixes its output paths; a main-checkout replay would modify protected existing results.
- Five agreeing runs do not prove indefinite absence of flakiness, and focal greens do not establish full-suite behavioral equivalence.
- Historical dependency/platform identity, developer intent, behavior outside focal tests, and alternative conflict resolutions were not reconstructed.
- No agent arms or phase-2 draws ran, so no collision, throughput, wasted-compute, livelock, attribution, fairness, or fine-claiming safety outcome was measured.

## What would change this verdict

- Build and freeze a Terraform gated runner first. It exposes four strict both-tests candidates—the largest remaining strict yield—and one boundary-only sensitivity candidate.
- Then gate the one strict both-tests row in each of Ansible, Jupyter, and Redis. Passing rows would be appended with `stratum: "same_file_disjoint"` under the same identity and evidence rules.
- A faithful independent replay of the current candidates could strengthen reproducibility evidence, but it must use an external disposable project-layout copy and the unmodified fixed-output runner; it cannot overwrite the authoritative results.
- A preregistered historical-environment amendment could change a missing-at-base, collection, compilation, or focal-execution rejection. That would be a new protocol result, not a reinterpretation of this one.
- Reclassifying boundary-only as strict same-file-disjoint would require an explicit amendment to the pre-specified strata. Endpoint contact is not silently collapsed here.
- Phase 2 remains gated until enough exact `same_file_disjoint` sites pass an approved validation protocol and the other non-negotiable arms preconditions remain satisfied.

## Per-claim confidence

| Claim | Confidence | Reason |
|---|---|---|
| The all-repository census is 46 `same_file_disjoint` and 22 `boundary_only` | High | Every canonical JSONL row was streamed; all 68 selected rows have unique full identities, the same frozen overlap revision, one merge base, and internally consistent both-tests metadata. The totals reproduce the published mining totals exactly. |
| The runnable-four census is 9 strict and 11 boundary-only, with 3 both-tests candidates in each class | High | Exact repository/full-hash joins were independently reconciled against all four JSONLs; no candidate predicate was inferred from filenames. |
| Zero of three strict candidates passed | High for the recorded gate and current hashes; environment-conditional | All three Commons identities occur in the exact 19-row Java execution. Current gate and corpus hashes match its before/after bindings, and every rejection names a fail-closed mapping/execution check. A different historical environment or protocol may differ. |
| Two of three boundary-only candidates passed | High for the frozen Python execution; environment-conditional | The protocol and validator hashes match `SITES.md`; each verdict is backed by full focal selection, five base attempts per side, red, green, raw counts, and publication records. |
| Terraform offers the largest new-runner yield | High as a census claim | It has four strict both-tests rows, versus one each in Ansible, Jupyter, and Redis; it also has the only remaining boundary-only both-tests row. Yield means attempts available, not expected passes. |
| The strict disjoint fine-claiming safety question is answered | Not evaluated | There is no validated strict site and no phase-2 arm outcome. |

## Evidence and provenance

- Census inputs: all 16 `corpus/conflicts/*.jsonl` files, 19,014,714 bytes total; ordered manifest rule and hash are embedded in `sites-disjoint.json`.
- Frozen classification source: `overlap.classification` from conflict-byte overlap revision `conflict-byte-overlap-v4-pinned-bounded-linear`; no class was recomputed or guessed.
- Python evidence: `exploratory/arms/SITES.md`, `exploratory/arms/sites.json`, per-site manifests under `exploratory/arms/patches/pallets__click/`, and raw attempts under `exploratory/arms/raw/pallets__click/`.
- Java evidence: `exploratory/arms/JAVA-RUNNER.md`, `exploratory/arms/java-gate-report.json`, `exploratory/arms/sites-java.json`, and `exploratory/arms/java-gates/br-java-v4/`.
- Go census-only decision: corpus line 97 has `both_sides_touched_tests=false`, with both `test_files` arrays empty; no Go validation artifact was created for it.
- Corpus mirrors, fixtures, prompts, existing results, and `instruments/arms/shim/` were not modified.
