# Posture experiment repository selection

## Gate result

**Accepted: `pallets/click` at `2c8cd3ac958a7eb316d67f2d316c27086c4c0369`.**

Click's complete repository-configured pytest run collected 33,016 cases,
selected 2,016 ordinary cases under the repository's `not stress` marker, and
produced the same 2,016 normalized test identities and outcomes in five
consecutive runs. Every run had normalized-result SHA-256
`7de20e03b60b4de313284e0c0e779dff8d4ff28de86e6040c3e68f4206d1381b`.
HEAD, HEAD tree, index tree, tracked status, and tracked diff hash were unchanged
before and after every run.

| Run | Harness wall seconds | Exit | Normalized result |
|---:|---:|---:|---|
| 1 | 13.272 | 0 | 1,907 passed, 108 skipped, 1 xfailed; 31,000 stress cases deselected |
| 2 | 14.917 | 0 | identical |
| 3 | 14.082 | 0 | identical |
| 4 | 13.976 | 0 | identical |
| 5 | 14.519 | 0 | identical |

Before final task construction, the managed corpus copy lost its Git object
store while a retained preparation pass was running. That invalidated the
copy, not Click's test result, and the pass was rejected before `TASKS.json`.
A new full-history clone was made at
`exploratory/posture/repositories/pallets-click`, detached at the exact same
accepted commit and tree. It is non-shallow and non-partial, has no alternates,
reports zero missing objects, and passes `git fsck --full` with no output.

The replacement was gated from scratch rather than inheriting the original
result. Its five runs had the same 2,016 identities and exact normalized
SHA-256 as above, all with exit 0; harness walls were 23.380, 16.134, 17.524,
18.501, and 17.243 seconds. The replacement clone is therefore the accepted
experiment target. The damaged managed copy is retained as a rejected
infrastructure result and is never used by the pilot.

The original machine-readable gate record, JUnit XML, and captured output are
under `exploratory/posture/repository-gates/pallets__click/`; the authoritative
replacement gate and clone-integrity record are under
`exploratory/posture/repository-gates/recovery-20260823T183731Z/pallets__click/`.
The exact command was
the repository-configured `python -m pytest`, with `PYTHONPATH` pinned to the
candidate's own `src` directory and `PYTHONDONTWRITEBYTECODE=1`. Dependencies
were installed only in `exploratory/posture/envs/click`; no global Python
environment was changed.

The accepted source identities are:

- commit: `2c8cd3ac958a7eb316d67f2d316c27086c4c0369`
- commit tree: `24f8d0c330254e2fbabaa49c6c64a779b0767ea3`
- empty tracked diff SHA-256:
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- first-parent commits: 1,378

Task construction subsequently used historical anchor commits from this same
first-parent history. Each synthetic all-reverted task base and each focal
evaluation oracle is required to pass the full suite before a pilot may start;
those are construction checks, not substitutes for this unmodified-repository
five-run gate.

## Candidates tried

| Candidate | Suite command considered | Measured runtime | Determinism | Decision |
|---|---|---:|---|---|
| `apache/commons-lang` | `mvn test` | Not measurable | Not run | Rejected: neither Java nor Maven nor a usable local cache was present. Its history did contain a six-task `StringUtils.java` clique, so history was not the blocker. |
| `BurntSushi/ripgrep` | `cargo test --all` | Not measurable | Not run | Rejected: Cargo, rustc, and rustup were absent; the checkout requires Rust 1.96. Its history contained a six-task `crates/ignore/src/walk.rs` clique. |
| `ansible/ansible` | `ansible-test units -v --docker` | Not measurable | Not run | Rejected: the Windows checkout could not execute the Unix launcher, required packages and a cached test image were absent, and the promising tasks required a 401-target integration suite. Its history contained a six-task `lib/ansible/modules/user.py` clique. |
| `psf/requests` | `python -m pytest tests` | 178.98 s pytest / more than 181 s harness wall | Not repeated after valid timing | Rejected: after isolated dependency provisioning and Windows symlink normalization, the local-source suite passed 631 tests (3 skipped, 1 xfailed) but exceeded the approximately two-minute ceiling. An earlier 101.674 s diagnostic loaded site-packages Requests and lacked fixtures, so it was retained as invalid diagnostic evidence rather than counted. |
| `pallets/itsdangerous` | `python -m pytest` | 2.044–5.895 s | Five identical green runs, 297 passed | Rejected after the initial gate: fast and deterministic, but its small eight-module history could not supply both the required real-overlap treatment bundle and a credible four-agent behavior-bearing pairwise-independent control. This is the specification's task-construction gate, not a test failure. |
| `pallets/click` | `python -m pytest` under repository `not stress` marker | 13.272–14.917 s | Five identical green normalized runs | Accepted: fast, deterministic, clean, and sufficiently broad first-parent history for behavior-bearing overlapping and independent reverted-commit bundles. |

The table's original Click row records the first accepted copy. The final
experiment target is the fresh replacement clone described above: 16.134 to
23.380 seconds, five identical green normalized runs, and a clean full-object
integrity check. The original copy is rejected for infrastructure loss.

## Interpretation choices made during the gate

1. The original corpus clones intentionally had empty primary worktrees with
   tracked files staged as deleted. That storage convention was not called
   corruption. Read-only probes used detached worktrees and preserved each
   canonical clone's branch and index.
2. "Deterministic" means identical normalized JUnit test identities and
   statuses, not identical durations or byte-for-byte progress output. Five
   zero exit codes alone would have been too weak.
3. The runtime ceiling was applied to harness wall time, not pytest's shorter
   self-reported time. That rejects Requests despite its incomplete diagnostic
   finishing near 100 seconds.
4. Installing declared test dependencies in an experiment-local virtual
   environment was treated as environment provisioning, not a source-tree
   modification. The interpreter and environment are frozen across arms.
5. Requests tracks a Unix directory symlink. On this Windows host it was
   normalized to a directory junction for the valid diagnostic without
   changing the Git index tree. It still failed the runtime gate.
6. For Click, "full test suite" means every ordinary test selected by the
   repository's own `not stress` default. The 31,000 deliberately stress-marked
   parameter cases are a separate stress job and were consistently deselected
   in all five gate runs and all experiment arms.
7. The gate used the accepted HEAD, while reverted tasks use preregistered
   historical anchors. Historical runs use a separate experiment-local
   environment pinned to `pytest==8.4.2` and `colorama==0.4.6`; no warning is
   suppressed. This avoids pytest 9 turning a pre-existing collection warning
   into an error, without changing Click's tests or source.
8. ItsDangerous was not retained merely because it passed the timing gate. The
   experiment requires a trustworthy independent control as well as overlap;
   failing that construction requirement is a valid rejection result.
9. A repository gate binds both source identity and a usable clone. Losing the
   managed Click object's store revoked that copy even though its earlier tests
   were deterministic. Re-cloning the exact commit, checking the full object
   graph, and repeating all five runs was required before proceeding.

No posture-arm outcome was inspected before the repository and task-design
gates were fixed.
