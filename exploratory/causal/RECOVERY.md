# Click causal-task recovery

## Result

Three of the five failed tasks were recovered with their existing,
history-derived patch pairs unchanged. PRs 787 and 1061 use exact first-parent
base trees. PR 3013 uses the expansion anchor. The unchanged PR 2991 and PR
3330 pairs apply normally to their exact first-parent trees, but both produce
green-green-green rather than green-red-green, so they cannot be recovered as
causal probes.

| Task | Integrated commit | Declared lineage | Diagnosis | Outcome |
|---|---|---|---|---|
| `pr-787` | `56314dbdc6075307d2104b2ae640186acb81a8ef` | historical control | the shipped control base already contains the change; its cumulative-revert construction never removed this rejected replacement | recovered on `base-pr-787` |
| `pr-1061` | `752c5e4b6623d97369076b6c03015af1041ea931` | historical control | the shipped control anchor predates context required by PR 1061 | recovered on `base-pr-1061` |
| `pr-2991` | `737bfbd3122d96d41924323a3f45a8021c30d4c8` | rejected modern control | the modern shipped base was built by reverting a different set; even the exact first parent has no red phase | unrecoverable as a probe |
| `pr-3013` | `701b313160be12752e49b3f7cea32402c0969a69` | rejected modern control | the modern shipped base already contains the change | recovered on `base-expansion-1` |
| `pr-3330` | `452935115a4073a074069d5428db998766886b8b` | rejected modern control | the modern shipped base already contains the change; the source delta is behavior-neutral and the exact first parent has no red phase | unrecoverable as a probe |

`fixture/click/TASKS.md` records PRs 787 and 1061 in rejected control
constructions and PRs 2991, 3013, and 3330 in the rejected first independent
control. They are not members of the eight-task frozen bundles. That text is
the authoritative lineage evidence used here.

## Apply-check method and an important false-success trap

The fixture bases are directories inside the outer Blast-Radius Git worktree.
Without a discovery ceiling, `git apply` can find the outer repository, skip
every unprefixed patch path, and exit 0. For example, an unisolated verbose
check printed:

```text
Skipped patch 'src/click/shell_completion.py'.
Skipped patch 'tests/test_shell_completion.py'.
```

All results below instead set:

```powershell
$env:GIT_CEILING_DIRECTORIES = 'C:\Users\joshp\Desktop\Blast-Radius\fixture\click'
```

Thus the earlier observation that a patch passed `git apply --check` against
both bases was a vacuous zero-path check, not evidence of dual applicability.
As an independent control, a real source-only check for the instructive PR 973
succeeds on `base-control` and fails on `base-overlap` with
`click/types.py: No such file or directory`.

## Exact source-only errors against both shipped bases

Each command was run from the named base with the absolute patch path. Output
below is Git's stderr, excluding the shell's exit-code annotation; every check
exited 1.

### `pr-787`

```text
git apply --check --exclude=tests/test_context.py pr-787.patch

base-control:
error: patch failed: click/decorators.py:61
error: click/decorators.py: patch does not apply

base-overlap:
error: click/decorators.py: No such file or directory
```

### `pr-1061`

```text
git apply --check --exclude=tests/test_bashcomplete.py pr-1061.patch

base-control:
error: patch failed: CHANGES.rst:84
error: CHANGES.rst: patch does not apply
error: patch failed: click/_bashcomplete.py:23
error: click/_bashcomplete.py: patch does not apply

base-overlap:
error: patch failed: CHANGES.rst:84
error: CHANGES.rst: patch does not apply
error: click/_bashcomplete.py: No such file or directory
```

### `pr-2991`

```text
git apply --check --exclude=tests/test_testing.py pr-2991.patch

base-overlap:
error: patch failed: src/click/testing.py:99
error: src/click/testing.py: patch does not apply

base-control:
error: src/click/testing.py: No such file or directory
```

### `pr-3013`

```text
git apply --check --exclude=tests/test_shell_completion.py pr-3013.patch

base-overlap:
error: patch failed: src/click/shell_completion.py:405
error: src/click/shell_completion.py: patch does not apply

base-control:
error: src/click/shell_completion.py: No such file or directory
```

### `pr-3330`

```text
git apply --check --exclude=tests/test_termui.py pr-3330.patch

base-overlap:
error: patch failed: src/click/_termui_impl.py:367
error: src/click/_termui_impl.py: patch does not apply

base-control:
error: src/click/_termui_impl.py: No such file or directory
```

## Patch provenance

Fresh `git diff --binary --full-index <commit>^1 <commit>` output and its
test-only `-- tests/` subset have the same stable patch identities as all five
existing pairs. Regeneration therefore did not justify overwriting any file
under `fixture/click/patches/`, which the task expressly forbids.

| Task | First parent | Full-patch SHA-256 | Test-patch SHA-256 |
|---|---|---|---|
| `pr-787` | `0e088e1d57d1168b09f1c23e674f93c1464fa505` | `03d9fd2b2727116dcb58063fc56f2279c0980e44f7b5d638d22205c6fc6ddec7` | `81822b8f2aa7edef31993264a89486affe0b1f61c59e0ed4599bb8f08de5cd48` |
| `pr-1061` | `df0f81d2401eaaf6634969e82d8b0e32b84f8dce` | `355558ec7e635c13f2205da7a19f5a6c0a5677afd3019c1584fc0b60e02ccb78` | `7f2fd00a87a328a9b4feeccc6c32cd08021df7c2acd5a982707f4763b07f8236` |
| `pr-2991` | `7b72bfb8cd3a0cec38197f6786ed583b5b3737e7` | `3c86cbe03abf253897c660a09bc9a9dd7726df0b8e008b60b7dcbdc89db87246` | `d4fdd75abc36d850838690634d9e204fe15043d784f8e6d66b589abcb408100b` |
| `pr-3013` | `3be1f33c8e0b13295853f6aa64d1f7412b5312df` | `12cc262a7be2c29f614324bb199ad68443ca5d64193afc3fd47815ccf4399886` | `28048709c025e6252c471f2b10cc9e147af2956c5a0f833c80bc9ed9571620e0` |
| `pr-3330` | `ac2dd7aadf08aa0ce7f75bf0e96b95ad5e62d1b9` | `2a70e248fbb38bbc01730e72be5c9acc1d02499889a8b61a4d1e057373dc02c3` | `87e9de88847da4805e9f1bb6243b7ba33d194c69af8604d84f62509997592120` |

## Per-task decisions and validation

### `pr-787`: recovered

The control base already has both `ctx.invoke(f, obj, *args, **kwargs)` and
`test_make_pass_decorator_args`; reverse checks confirm that PR 787 is already
integrated. This is context drift caused by the base's different revert set,
not patch corruption. The modern base also has the wrong `src/click/` layout.

The exact first parent was exported as `fixture/click/base-pr-787` (Git tree
`2415a2d2197a0fac8780b64a55b4448466d491c1`). With Python 3.11.9,
pytest 8.4.2, and the historical `collections.Iterable` compatibility binding:

```text
base:              165 passed, 19 skipped, 3 xfailed
tests only:        1 failed
failure:           TypeError: test1() missing 1 required positional argument: 'ctx'
source and tests:  166 passed, 19 skipped, 3 xfailed
```

The unchanged pair applies normally to this base.

### `pr-1061`: recovered

The source preimage depends on intervening PR 1059, which is later than the
shipped control anchor. Its `_bashcomplete.py`, changelog, and historical-test
contexts are consequently absent or drifted. The modern base has the wrong
path layout and no `_bashcomplete.py`.

The exact first parent was exported as `fixture/click/base-pr-1061` (Git tree
`749080e48edb1ecc6eb1239b7cd02628f7a01d99`). Validation produced:

```text
base:              171 passed, 19 skipped, 3 xfailed
tests only:        1 failed
failure:           AssertionError: assert ['--name'] == []
source and tests:  172 passed, 19 skipped, 3 xfailed
```

A single old common base was considered and rejected. Ordinary mechanical
reversion conflicts in `tests/test_context.py`; `git revert -X theirs` makes
both pairs work but also deletes the unrelated passing
`test_exit_not_standalone`. Two exact-parent trees preserve provenance and do
not introduce that collateral deletion.

### `pr-2991`: unrecoverable as a causal task

The unchanged pair applies normally to exact first parent
`7b72bfb8cd3a0cec38197f6786ed583b5b3737e7` (tree
`15ac8f4deefd4a58c5e25bc678cfa809745f736f`). The final overlaid tree also
matches merge tree `8e34949cb7664f88429bd73a3071545a493eda05` exactly. Its full-suite sequence
is nevertheless:

```text
base:              845 passed, 73 skipped, 1 xfailed
tests only:        845 passed, 73 skipped, 1 xfailed
source and tests:  845 passed, 73 skipped, 1 xfailed
```

The test delta moves assertions inside `runner.isolation()`; it does not fail
when the new `StreamMixer.__del__` method is absent. Thus the strongest
historical base is green-green-green.

The shipped modern base contains the historical test change, while later
commit `d8e987eae723fbb0ccc355d125d0e179a1bf5fd8` removed the added destructor.
On that retained rejected modern-control base, the same pair gives the
secondary sequence:

```text
base:              1347 passed, 73 skipped, 30000 deselected, 1 xfailed
tests only:        1347 passed, 73 skipped, 30000 deselected, 1 xfailed
source and tests:  1 failed, 1346 passed, 73 skipped, 30000 deselected, 1 xfailed
final failure:     tests/test_stream_lifecycle.py::test_no_streammixer_del
```

The historical assertions pass without the source on both bases; on the later
base, reintroducing the removed destructor additionally breaks a current
invariant. Neither provenance-preserving check has the required red middle
state, so the task is rejected rather than forced.

### `pr-3013`: recovered

The shipped modern base already contains the Fish-completion fix and its test;
the control base has the wrong path layout. The exact existing pair applies to
the expansion anchor `02046e7a19480f85fff7e4577486518abe47e401`, exported as
`fixture/click/base-expansion-1` (source tree
`c764e16f14d1e7c066789ced2876f39fcdf5b647`). Full-suite validation was:

```text
base:              590 passed, 28 skipped, 1 xfailed
tests only:        1 failed, 590 passed, 28 skipped, 1 xfailed
failure:           tests/test_shell_completion.py::test_full_complete[fish-env6-plain,b\tbee\n]
actual / expected: '\n' / 'plain,b\tbee\n'
source and tests:  591 passed, 28 skipped, 1 xfailed
```

### `pr-3330`: unrecoverable as a causal task

The unchanged pair applies normally to exact first parent
`ac2dd7aadf08aa0ce7f75bf0e96b95ad5e62d1b9` (tree
`7eb8d5c85032ef777c081cc2302c388dec13adff`), and its final overlay exactly
matches merge tree `f7ac9fff93bc593320cc12042d3eadbe54b73bfc`.

The source diff moves text between comments and docstrings; parent and
integrated executable ASTs are identical after docstrings are removed. The
exact-parent full suites were:

```text
base:              1349 passed, 73 skipped, 30000 deselected, 1 xfailed
tests only:        1370 passed, 73 skipped, 30000 deselected, 1 xfailed
source and tests:  1370 passed, 73 skipped, 30000 deselected, 1 xfailed
```

The exact changed test functions were also green in both overlay states (14
passed, 1 skipped) on the retained rejected base. This is green-green-green on
the exact historical base, so the commit has no causal red phase.

## Claims that could NOT be verified

| Claim | Confidence | Reason |
|---|---|---|
| The three recovered tasks are deterministic across repeated runs on their new bases. | Medium | Each complete three-state sequence was observed, but five repeated normalized runs were not performed; that would be a separate determinism gate. |
| The local Python/pytest result exactly predicts a future cloud runner. | Medium-high | The bases and patches are self-contained and use the pinned local pytest 8.4.2 environment, but this job did not execute in the network-isolated cloud image. |
| PRs 2991, 3013, and 3330 had a frozen assignment to either shipped fixture base. | High confidence that the claim is false | `TASKS.md` records their bundle as rejected and omits them from both frozen bundles; no newer machine-readable preregistration was found. |
| PR 2991 or PR 3330 could discriminate on some unrelated synthetic base. | Low | Synthetic bases were not exhaustively searched. The exact first-parent bases are the strongest provenance checks, and both are green in the tests-only state. |

## What would change this verdict

| Verdict | Confidence and reason | Evidence that would change it |
|---|---|---|
| PR 787 is recovered on its exact parent. | High: exact patch provenance, ordinary application, and full-suite green-red-green were observed. | A repeatable base or final-suite failure under the declared compatibility environment. |
| PR 1061 is recovered on its exact parent. | High: exact patch provenance, ordinary application, and full-suite green-red-green were observed. | A repeatable base or final-suite failure, or a clean common base that preserves every unrelated test while accepting both unchanged pairs. |
| PR 2991 is not a causal probe. | High: ordinary application reconstructs the merge tree, but every exact-parent state is green; its source also makes the later retained suite red. | A provenance-valid test diff from the same task commit that fails only when the destructor change is absent and passes with it. |
| PR 3013 is recovered on the expansion anchor. | High: the exact existing pair produced a sole tests-only failure and a green complete suite with source restored. | Failure to reproduce that sequence from the committed `base-expansion-1` tree in the execution environment. |
| PR 3330 is not a causal probe. | High: the exact-parent sequence is green-green-green, reconstructs the merge tree, and the source delta has no executable AST change. | Evidence of executable source behavior changed by this exact commit and an exact historical test that distinguishes it. |
