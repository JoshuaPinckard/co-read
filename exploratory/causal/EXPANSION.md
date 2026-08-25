# Click causal-task expansion

## Result

Exactly 40 additional, byte-distinct Click causal tasks were produced as 40
`pr-N.patch` / `pr-N.tests.patch` pairs. Every retained pair applies normally
to its declared base and passed a full-suite green-red-green check. Four exact
historical anchors are used: `base-expansion-0` through `base-expansion-3`.

No perturbation sweep was run. This job only selected, regenerated, and
validated task definitions and patch pairs.

## Selection rule fixed in advance

The acceptance rule was fixed before inspecting any candidate's test outcome:

1. Inside fixed, non-overlapping history windows, enumerate first-parent
   commits oldest-first, then enumerate the remaining reachable commits in
   reverse topological order while excluding every PR already attempted. Use
   the commit's first-parent diff, including for merge commits, and require an
   unambiguous PR number for the `pr-N` identity.
2. Require at least one changed path under `src/click/` and at least one added
   or modified Python file under `tests/`. Exclude the 13 PR identities already
   present in the fixture.
3. Restrict the historical diff to `src/click/` and `tests/`. Apply it to the
   window's exact lower anchor using Git's automatic three-way machinery.
   Reject every conflict; never resolve a hunk or hand-edit a patch.
4. Re-diff the successfully populated temporary index against the lower
   anchor. Require the resulting full and test-only patches to pass ordinary
   `git apply --check` on the exported base.
5. Require the base full suite to be green, the full suite with only the test
   patch to exit 1 with at least one test failure or error, and the full suite
   with both source and tests to be green.
6. Deduplicate exact full-and-test patch payloads. Keep the first identity in
   the fixed traversal and stop after 40 distinct accepted payloads.

The three primary windows were frozen before their green-red-green gates. The
older 8.1.4 window and the merge-side completion passes were frozen before
their own test outcomes, after the aggregate primary-window yield was known.
Thus the acceptance rule was never adapted to an individual result, but this
report does not claim that the entire four-window sampling frame was
preregistered before the first validation run.

## Sampling frame and bases

The accepted clone HEAD is
`2c8cd3ac958a7eb316d67f2d316c27086c4c0369`, with 3,329 commits reachable
from HEAD. The four windows do not overlap.

| Base | Lower anchor | Source tree | Window upper bound | Baseline |
|---|---|---|---|---|
| `base-expansion-0` | `3d873a3f567d1bbcb6fb25f0fbe3c3128488d99d` | `3089fd688f3d21a7abbb1fcb6c02e44dce9349d0` | `02046e7a19480f85fff7e4577486518abe47e401` | 582 passed, 28 skipped, 1 xfailed |
| `base-expansion-1` | `02046e7a19480f85fff7e4577486518abe47e401` | `c764e16f14d1e7c066789ced2876f39fcdf5b647` | `219206a18666624072fdbb803901e5eb7ce575a1` | 590 passed, 28 skipped, 1 xfailed |
| `base-expansion-2` | `219206a18666624072fdbb803901e5eb7ce575a1` | `b6dfb51bb2738878bfa4ff12b85b7cb022ababf9` | `052c006033729bbb422cbdad0c4fee988ecb5aa5` | 668 passed, 73 skipped, 1 xfailed |
| `base-expansion-3` | `052c006033729bbb422cbdad0c4fee988ecb5aa5` | `e3b758c4e87e6e95e5863849412c9e13e92a2b28` | `2c8cd3ac958a7eb316d67f2d316c27086c4c0369` | 1,333 passed, 73 skipped, 30,000 deselected, 1 xfailed |

The Windows exports of bases 1--3 are content-identical to their source trees
but normalize `.devcontainer/on-create-command.sh` from mode 100755 to 100644.
Their normalized tree identities are respectively
`05a90697ebb3ce95a41530f7b3d9da69b10e90f5`,
`7fd6819ff2b227766ff05278c01a91e97211c2e9`, and
`afe2fa27139f8efd2647d468128a579145a19be3`. Base 0 matches its source tree
exactly. No task touches the normalized file.

## Candidate accounting and yield

| Window base | Structural commits inspected | Automatic clean transplants | Existing fixture identities | Expensive GRG attempts | Accepted PR identities | Retained distinct tasks |
|---|---:|---:|---:|---:|---:|---:|
| `base-expansion-0` | 11 | 7 | 0 | 7 | 4 | 4 |
| `base-expansion-1` | 27 | 19 | 0 | 19 | 14 | 14 |
| `base-expansion-2` | 33 | 14 | 2 | 12 | 8 | 7 |
| `base-expansion-3` | 61 | 25 | 2 | 22 | 16 | 15 |
| **Total** | **132** | **65** | **4** | **60** | **42** | **40** |

The four existing clean identities were PRs 2972, 3013, 3239, and 3299. Of
128 novel structural candidates, 67 failed the automatic no-conflict
transplant gate and 61 were clean. The target was reached after expensive
testing of 60 clean identities; one remaining clean identity was deliberately
left untested. Those 60 identities represented 58 distinct payloads because
PR 2816 equals PR 3031 byte-for-byte and PR 3277 equals PR 3466 byte-for-byte.
The earlier identity in each traversal was retained.

The 60 expensive checks produced 42 green-red-green PR identities. Thirteen
had a green tests-only state, four had a timeout or collection error rather
than a qualifying red test run, and PR 3769 was red again after its source was
applied. After exact-payload deduplication, the result is 40 tasks.

The end-to-end yield is **40 / 128 = 31.25%** of novel structural candidates.
The expensive-gate yield is **40 / 58 = 68.97%** of distinct payloads actually
subjected to green-red-green testing. Both denominators are reported so that a
repeat does not silently ignore conflict and deduplication attrition.

## Mechanical regeneration and integrity

For each commit, the exact first-parent diff was restricted to `src/click/`
and `tests/`. A temporary Git index rooted at the declared anchor received
`git apply --3way --cached`; conflicts were rejected. A new binary/full-index
diff was then emitted from that index. This refreshes context against the base
without choosing or editing a hunk.

Each `pr-N.patch` contains source and test hunks, matching the existing fixture
convention. Each `pr-N.tests.patch` contains the test hunks alone. Source-only
application uses `--exclude=tests/**`. Final checks set
`GIT_CEILING_DIRECTORIES` to `fixture/click` so nested bases cannot falsely
fall through to the outer repository. All 80 delivered files match their
recorded SHA-256 values, all 40 source/test checks exit 0 on their declared
bases, and no two delivered full patches have the same SHA-256.

Validation used Python 3.11.9 and pytest 8.4.2. The base digest was checked
before and after every candidate. JUnit counts below fold xfails into the
skipped column; `P/F/E/S` means passed/failures/errors/skipped.

## Green-red-green result for every accepted task

| Task | Base | Integrated commit | Tests-only P/F/E/S | Source+tests P/F/E/S |
|---|---|---|---:|---:|
| `pr-2604` | `base-expansion-0` | `9c5e989d52509498ce78a4015ebc7d9408f70c56` | 582/5/0/29 | 587/0/0/29 |
| `pr-2724` | `base-expansion-0` | `d3e3852eba45a6f7ce0ae5e02ca49106e228add6` | 582/1/0/29 | 583/0/0/29 |
| `pr-2729` | `base-expansion-0` | `5b1624bf09947d6f10eaffd63d6fb737dfe7656a` | 582/1/0/28 | 583/0/0/28 |
| `pr-2730` | `base-expansion-0` | `bc16dbf788861266177661b576291a1b264fb5de` | 582/1/0/29 | 583/0/0/29 |
| `pr-2365` | `base-expansion-1` | `1a4d8c1bb1e8f8e214ede7223bd2c05dc2ce006a` | 590/5/0/29 | 595/0/0/29 |
| `pr-2509` | `base-expansion-1` | `a6e0d2936891030f5092c9869c7391104f6721d9` | 590/1/0/29 | 591/0/0/29 |
| `pr-2517` | `base-expansion-1` | `948d7a707a0abda8ce0a1b39ba87390f11ffcd5e` | 565/25/0/29 | 590/0/0/29 |
| `pr-2523` | `base-expansion-1` | `0e0c00324a5af35f8995c7ff88df67b0f5594a58` | 588/2/0/29 | 590/0/0/29 |
| `pr-2696` | `base-expansion-1` | `1b524bb30d0bd39528369c7029b9296e7663cf19` | 590/1/0/29 | 591/0/0/29 |
| `pr-2680` | `base-expansion-1` | `c326df95e9e3da0425360e030413f6a3ee25fdee` | 591/3/0/29 | 594/0/0/29 |
| `pr-2727` | `base-expansion-1` | `fcd85032cff78aa536a6d2b455fb83bfcc02b228` | 590/1/0/29 | 591/0/0/29 |
| `pr-2788` | `base-expansion-1` | `1787497713fa389435ed732c9b26274c3cdc458d` | 590/1/0/29 | 591/0/0/29 |
| `pr-2622` | `base-expansion-1` | `4271fe283dc9365563aebb369ada8d20eee015a8` | 590/1/0/29 | 591/0/0/29 |
| `pr-1489` | `base-expansion-1` | `d8763b93021c416549b5f8b4b5497234619410db` | 588/2/0/29 | 590/0/0/29 |
| `pr-2799` | `base-expansion-1` | `d791537317947448a3960662d00acf160eff2602` | 591/1/0/28 | 592/0/0/28 |
| `pr-2271` | `base-expansion-1` | `5961d31fb566f089ad468a5b26a32f1ebfa7f63e` | 588/15/0/29 | 603/0/0/29 |
| `pr-2829` | `base-expansion-1` | `b5464b7e065bdfd952b1532880941f1be07e4e2b` | 585/5/0/29 | 590/0/0/29 |
| `pr-2607` | `base-expansion-1` | `afc86c748c214939e2293ec00879d85293bcd9fa` | 590/1/0/28 | 591/0/0/28 |
| `pr-2930` | `base-expansion-2` | `884af5c20fdc95c9c7352df35c37273391464fb9` | 674/5/0/74 | 679/0/0/74 |
| `pr-2935` | `base-expansion-2` | `b7cf06970e40a3144eb963ff34ed7c38934afb40` | 670/4/0/74 | 674/0/0/74 |
| `pr-2933` | `base-expansion-2` | `cfa6f4ad3e0078db43f866246b483544afa33ed3` | 668/1/0/74 | 669/0/0/74 |
| `pr-2846` | `base-expansion-2` | `a1235aacb1be55dc66ddcfefbf64dec44b6ab54d` | 668/3/0/74 | 671/0/0/74 |
| `pr-2816` | `base-expansion-2` | `4f936ac1981645488f396953bc59e50445de00b6` | 668/1/0/74 | 669/0/0/74 |
| `pr-3029` | `base-expansion-2` | `5926f83c3d84675b30905e8d37ef2ba6a0142f73` | 670/1/0/74 | 671/0/0/74 |
| `pr-3058` | `base-expansion-2` | `36deba8a95a2585de1a2aa4475b7f054f52830ac` | 668/2/0/74 | 670/0/0/74 |
| `pr-3245` | `base-expansion-3` | `63ea71f9b0544b7c4ba21385a1164a7b29b17e42` | 1336/9/0/74 | 1345/0/0/74 |
| `pr-3235` | `base-expansion-3` | `ac2dd7aadf08aa0ce7f75bf0e96b95ad5e62d1b9` | 1335/1/0/74 | 1336/0/0/74 |
| `pr-3240` | `base-expansion-3` | `4a352253c9ff013e36d11e4a6820d36d00ff2cd4` | 1333/1/0/74 | 1334/0/0/74 |
| `pr-3363` | `base-expansion-3` | `b03f211e602d915f413068eba87ff08469b38477` | 1323/11/0/74 | 1334/0/0/74 |
| `pr-3208` | `base-expansion-3` | `0cd28d6548f92e46396e0ffdd19ec5ea75b4b5e4` | 1336/3/0/74 | 1339/0/0/74 |
| `pr-3126` | `base-expansion-3` | `19fd4d6e18bc9fce451f92f422696b11169faa57` | 1330/4/0/74 | 1334/0/0/74 |
| `pr-3228` | `base-expansion-3` | `831c8f0948af519e45b90801d7430ff25451f972` | 1329/7/0/74 | 1336/0/0/74 |
| `pr-3211` | `base-expansion-3` | `fc6c7c47edd6110b6bd5a1a5297b2035214b0cd1` | 1334/1/0/74 | 1335/0/0/74 |
| `pr-3256` | `base-expansion-3` | `c943271a269e6941fcc51e3506ead074b9dda6be` | 1335/14/0/74 | 1349/0/0/74 |
| `pr-3642` | `base-expansion-3` | `16fc00e2f4a2717a521084f193709a6058afc693` | 1333/1/0/74 | 1334/0/0/74 |
| `pr-3728` | `base-expansion-3` | `9c4dfdaebe0e6b2aabc566eb81f6f10eb5cd6ea1` | 1342/4/0/74 | 1346/0/0/74 |
| `pr-3277` | `base-expansion-3` | `a3321c9ca14ddb951f04ac981f275b0de2f46208` | 1333/1/0/74 | 1334/0/0/74 |
| `pr-3471` | `base-expansion-3` | `3c12ce7050bf63c9fa522e5d7b95543037da0f63` | 1332/1/0/74 | 1333/0/0/74 |
| `pr-3493` | `base-expansion-3` | `a5f5aa6d4012d256ccca24638f2642fc371e9f77` | 1332/1/0/74 | 1333/0/0/74 |
| `pr-3507` | `base-expansion-3` | `97210f24eb22399eb6db87d01bfef14f78862d01` | 1343/2/0/74 | 1345/0/0/74 |

## Delivered patch hashes

| Task | Full patch SHA-256 | Test patch SHA-256 |
|---|---|---|
| `pr-2604` | `4161ffb58390374df86441ff82311944719c2d6eec98d2153dc42c11d8e6ad6a` | `33262ddf265e27aab5b219dcdfb69b55d3cfe60e440cc49206b69cb616b6eebf` |
| `pr-2724` | `36dc6b2b6b3a1507a44f8ca95f02259877a3216b678736e01a7bbd2a87d50127` | `bc251baa2b5a53c8c69b2c2c9c024196c04250064a21aa35518b6e19492059de` |
| `pr-2729` | `9ddc7cb06106e38c168ebd4c3cbc914d8fa9edf273e0aeb59f878fb9d4886d07` | `385e3cf458793441a5479ac63e2c61c7445c5761db960e985afcb9008c949791` |
| `pr-2730` | `160ada763b77a112bfe31da991d19ae84d1113d98d1fb7fa764ba7792062a0ca` | `0c48044024b42a45edc51484ac60b567dd29eb1e990a1d0135a42da9e445d84d` |
| `pr-2365` | `8feeee6b54b7f53dbcaefa98730acddb0657d5c5b5951b2b57cf03c02ef770b3` | `cd0bafe25c6b478857ffded6a8c7971929611055e7dfd98e9d2b3a6857c5f5ab` |
| `pr-2509` | `287f93232543b686a104bc241086337668fd3f4fd3bd6c2020e3dba34f953ee6` | `4fc096778812ff8c904112e6f2ed6551a97f82345fe55aedbe63ea1ae121ebf3` |
| `pr-2517` | `e286e6cc9109b1c8f020992009d05c6ada53abc9168cf480fbd7e20957089d8a` | `849db0b4ca8a2f170d302d4fdb9eca26b9153411d570963538476114d94d019a` |
| `pr-2523` | `c0d0b3ebfea7a9d1ce294ad09e3f8193051c6043a0094bd12ceaa73e7407e1c6` | `fa05baeab550277605957c631eb09e2b2fd90c02d5c8fd7d0ebf8bf83e44a267` |
| `pr-2696` | `15993c06b79fd2f77e9e80fe886fc61dfc3e9f3dfa41855e77e84bb139acfdf2` | `3fe4fda7de929f2b3a2eda6e2569c4dd1e4d880fb193e6b647def93ed114f69b` |
| `pr-2680` | `fb6c4d0e9380ecc38ed521245bcbcf8daaebc2f4277094a397e3ff50029ce5f0` | `0579821aabf025eccd2789312eda43ff5e8b9a1c46f3c49ff975db3178704431` |
| `pr-2727` | `36fc81d5700866225a44664c049c59ab1e02c51480c981dd970f315e13e8c9b0` | `830ed05f55c543e717d57895da523d21d1c67a69c14170d134934845d0e443ff` |
| `pr-2788` | `4321d6ab4c13e3a7ca76d1cfcbc15225b9d901678bcc9827ead2179817e201e5` | `53ed8d316abecb28527e55e82cd2b442395357663b465dce8e132957c19e65c8` |
| `pr-2622` | `e8461f0a27fcd814741cce0005d2e9037c000298fa82aae802bf82da63b54ed6` | `c144418983dc773d27542e22982771413f2ad1873510a04973aa01de9a519063` |
| `pr-1489` | `bb95ea624674e30a328126311e182a55af421cbb1b9e64ec46eaae596c5abf9e` | `09788eb484d90e97f508d0d147f5617c103fdcd60fcb7f3209c17c235098376b` |
| `pr-2799` | `1406f745519108c17b9c4e953c53001bf9f51cd43d92f81c2aa1d3ea6bbe63ae` | `2e3e7cce18e80d0f031b8ecacd1fb548983cfca1ed3088ac83fa83d1ddf48685` |
| `pr-2271` | `83f1a702a768c2c3849ed8eb91159926491f180bf5b9cdde545a059544575cdf` | `5ef5124940eb39c16f28cca0910786809196a2f41da6902584a65405504a25ce` |
| `pr-2829` | `b838118a026e361ac37740ec89119f8c293dd78dedb7c25e8708d6b7f90effb9` | `835717b5c4e208104c22f21ffef43eaeae8b3951ddafd6e856cf20be1346c87a` |
| `pr-2607` | `ab4fa1753c398572328adb2e6164fc93f91f65683dff55f982826afd535263f5` | `8cb122d3ca653922846a1704ed7d04bd676f328ed7564cfec30b8a7eb6d21f40` |
| `pr-2930` | `7e6dae6aff6c5cd9ef89db9484c1e80dfaea3fe423442a76291045ff9f49f829` | `13e1a8bf90a268ff0a258961e1d707ffc9dc9de054929a4b795fcc510d121a46` |
| `pr-2935` | `effdff5e0e21695a34bdc6f9a0d354bf0c93e195e5728c971cbdedae838bd7d5` | `5dac2a566322482e6b93de66cfd0a8f531e3a21e73435f4149c0748b0c75c13a` |
| `pr-2933` | `5180f2e8baa890882ab8760626a8238a878f8c5c53abc723c8bdc20542098203` | `981961b8799780537a5dbca913c5cee8b5a2aa75f39aa3c22a54e1bb518eafcd` |
| `pr-2846` | `497e0cd32fa64fc386b38a9bcef60e4f5a0908b99a9b96c5e2dd7947b1d29fca` | `0842dfcebe2f84d3d801c3a6ab8de5d2a06d0dc5e8d0f83fca37d8bad42c5eb4` |
| `pr-2816` | `f0573a954f011ccb6183eeaf655eb129db27d0c9ee1ba61fae60967ead025549` | `6131adc04784b94d53d91f4d7c9c107ffaaef41594b4201e5694b20de286486b` |
| `pr-3029` | `ebba908a086fb32751c8ffa73f5c54fad54c518ef8402af1bc3a8ba6ca2729fd` | `52a1da4d6a29b811a4c38ed36ab1f8b3f7fb5ff6a406adebd51f36c200c5f915` |
| `pr-3058` | `492a86fc1ccd5bdfd9d579f712e606500ff740269242c6b4ca606ab15f512a89` | `926344bcf62cb73456ceb7a93eb2dfae6850b2ebd637b5517d718a4f4b059abf` |
| `pr-3245` | `0f88b76ab6c830e57a43959f3c6e1bd9c052e2163384386a9043afe370581a2f` | `ff7d63093d559d9979927adea599db1fc8f1bf68e3b8b224d0df6b3dd8869edd` |
| `pr-3235` | `a56d137c72caf0552c3d311e22f70d520fb8d9c7780de117135d92bcbd96f40f` | `ba72e6e16fc8443295ce5d6ba52e12608d23e62f67548e6dcb3dc32983fba21b` |
| `pr-3240` | `eebf0dcf9510652465c5ca52b728c04996439e295924fc0b6417fa698402027a` | `98d477ed5aaa8b007bf7285deb55631c1b38ceab31eab36490ba8916fd68301e` |
| `pr-3363` | `81b1ea360fc1b4f5e040e06571f2cfcdf7e0d8140ff9358d36fcc8c62ba62b5d` | `307ddba8f8bfbe27699e852f50425a8e5ca436f76e3049ce3ae77bd755f416c0` |
| `pr-3208` | `5fffe7f2e52e7dcefd71ba63e594e74dff3b54b3b80ee6a2fbcdb818f3c51744` | `564a8fe908a6a3e1689023f4d13df14a1e7528e2609d25ecbdeb34cbd6e51a06` |
| `pr-3126` | `e1c8d7f4e74f42b3081532c917ec216dfe54ec91e91cedb0ca668e04633a9808` | `6ea2f97520cdb01758f65cf660512bc621042cecebdf167959280939c2bbe120` |
| `pr-3228` | `6ee93e8e02cb35e7b42ae2de83d762568959980e953b11ce0c3a19d91fea83ff` | `9bb6534934cab132632a5de1182cf0ee171478d246eab07306cafaa67968d101` |
| `pr-3211` | `d2cbf699f029395e1d4086487fed6f11f8ac33596f193cfb7e030157907903ef` | `3a1f9dbe9fcb95502872e155c2615bb2196338b493f22416a307f8a112f6e431` |
| `pr-3256` | `75e567bac1cb46baceee3d95b77a1904848c8e6a33b06815d153e68e609760ad` | `21ebd6d5d0ee3d432593543f98912981766c71332e1b6e70ebbd5747fb84c486` |
| `pr-3642` | `8eb7c5026668942523faac66f09f75e3b69d2c7c4582e9e10f06a7ac6d0c0c68` | `f0d29ef599c2fbc89a51ccc02027ef73e7a3c70c9cc161f1f6febec610a29496` |
| `pr-3728` | `703f9e8470ca6dc3ab51a8d9ae24ddf285c143908412316e1f950410e7f8e093` | `39afc91450ade87f6137dfe07040eb7ce6312fef76c6eafd50d815238b81cf2b` |
| `pr-3277` | `7dd2bd03a79038fc05702af41354b899148a1e7e2cff77a75d8cb34ed27b960f` | `87e39ac3a5954c3f86192a2d05e81e70fbacf8e19141d48bbe52c29aeaf43f58` |
| `pr-3471` | `b6182dbb9688ad00e2fc5b2865a8113276817d06954ee03ed3e1c65bf1ca3f97` | `ceda16dcd7a7d09267136be73aba202e485e0cdb07a5262e2d13554faecbbd7e` |
| `pr-3493` | `42e1417ebfc452ab3c6a0ac5bf944511f124471e38984598c880bbb33ab2d69a` | `c42ad20bb126bea77202fea45e4ad69494cc84b70b7972f1a1bf95db42c873da` |
| `pr-3507` | `9984ea74bfca2d9b7b81b93f7384109516b9616db3bcbdbfa13a54d7b4dfcef5` | `e8e66786353202def5d9b4500baa7aab27e6b4b517602ffb86e6c9d37fd5b35c` |

## Claims that could NOT be verified

| Claim | Confidence | Reason |
|---|---|---|
| Every accepted task is deterministic across repeated executions. | Medium | One complete green-red-green sequence was observed per task, with base restoration checks; the five-run normalized determinism gate was not repeated 40 times. |
| The local result exactly predicts the future network-isolated cloud runner. | Medium-high | Bases and patches are self-contained and all local checks used Python 3.11.9 / pytest 8.4.2, but this job did not run the cloud image. |
| The complete four-window sampling frame was preregistered before the first outcome. | High confidence that the claim is false | The acceptance rule and each pass were frozen before that pass's outcomes, but the older and merge-side completion passes were added after aggregate yield from primary passes was known. |
| The 40 tasks are statistically independent samples. | Low | Exact duplicate payloads were removed, but tasks still share one repository, history, and sometimes source paths. |
| Every one of the 59 distinct clean candidate payloads satisfies or fails GRG. | High confidence that this was not verified | The target was reached after testing 58 distinct payloads; one clean payload remained deliberately untested. |

## What would change this verdict

| Verdict | Confidence and reason | Evidence that would change it |
|---|---|---|
| Exactly 40 distinct additional tasks are ready. | High: 80 hashes are unique by pair, ordinary application checks pass, and every retained row has a recorded green-red-green sequence. | A hash mismatch, a failed isolated apply check, a duplicate full/test pair, or a reproducible non-green base/final suite. |
| The end-to-end yield is 31.25%. | High for this stated frame: 40 retained tasks divided by 128 novel structural candidates. | A different history boundary, PR identity rule, package/test path rule, runtime, or conflict policy; those define a different sampling frame. |
| The expensive-gate yield is 68.97%. | High for observed distinct payloads: 40 of 58 tested payloads passed all gates. | Discovery of a mistaken duplicate classification or a recorded GRG result that cannot be reproduced. |
| Conflict-rejected commits are not tasks in this fixture. | High under the fixed rule: automatic application conflicted and no manual resolution was allowed. | A separately preregistered base construction on which the exact historical diff applies automatically and then passes GRG. |
| No perturbation result was produced. | High: only selection, patch application, hashing, and pytest validation were run. | Evidence of a perturbation command or perturbation output from this job. |
