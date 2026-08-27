**Repository: `pygments/pygments`. Gate: PASSED.**

# Second-repository causal fixture

Pygments at `38f426a6b1cd4ffc6429f5808031b7c62ea57b1f` passed the
strict five-run repository gate and yielded 30 historical tasks with complete
green-red-green verification on one common base. The fixture is under
`fixture/pygments/`. The perturbation sweep was not run.

## Gate table

For executed gates, determinism means one normalized hash over sorted JUnit
test identities, outcomes, and failure/error type or message. Every executed
run preserved HEAD, HEAD tree, index tree, tracked status, and tracked-diff
hash. Passing exits without one normalized hash were not accepted.

| Candidate/configuration | Runtime seconds | Five-run result | Accepted or rejected, and why |
|---|---:|---|---|
| `psf/requests` (reused prior valid evidence) | 178.98 pytest; >181 harness | One valid green run; not repeated | Rejected: over the approximately two-minute ceiling. |
| `pallets/itsdangerous` (reused prior evidence) | 2.044-5.895 | Five identical green runs; 297 passed | Repository gate passed; rejected for this fixture because prior construction found insufficient task structure. |
| `encode/httpx` | 109.244-126.003 | Two hashes; 1,409 pass/3 fail/1 skip on four runs and 1,408 pass/4 fail/1 skip on one | Rejected: nondeterministic, not green, and one run exceeded 120 seconds. |
| `pallets/flask` | 4.754-9.994 | One green hash; 494 passed | Gate passed; fixture rejected after 2 of 60 frozen historical candidates passed. The other 58 bases were not green under the fixed environment. |
| `python-attrs/attrs`, inherited PATH | 24.958-32.090 | One failing hash; 1,401 pass/3 fail/8 skip | Invalid diagnostic environment: a broken global `pyright` shim caused the failures. |
| `python-attrs/attrs`, clean PATH | 26.615-39.222 | One green hash; 1,401 pass/11 skip | Gate passed; fixture rejected after the first 4 frozen bases all failed because raw exports lacked distribution metadata. No packaging retrofit was introduced after that outcome. |
| Marshmallow HEAD, missing `tzdata` | 0.976-2.739 | Five collection failures; no cases | Invalid diagnostic environment; rerun after provisioning the missing Windows timezone data. |
| Marshmallow HEAD, with `tzdata` | 5.103-6.974 | Five hashes; 1,189 pass/1 fail | Rejected: Windows timestamp-overflow failure details varied. |
| Marshmallow 3.26.2 | 4.101-5.995 | Five hashes; 1,238 pass/2 fail | Rejected: missing distribution metadata and Windows timestamp-overflow behavior. |
| Marshmallow 3.21.3, missing `pytz` | 0.402-0.984 | Five collection failures; no cases | Invalid diagnostic environment; rerun with declared dependencies. |
| Marshmallow 3.21.3, declared dependencies | 3.363-4.764 | Five distinct hashes; every run 1,229 passed | Rejected: two parametrized testcase identities embedded the current seconds. Green exits alone were insufficient. |
| `pallets/jinja` | 5.642-7.420 | One green hash; 911 passed | Gate passed; fixture rejected after 7 of 88 candidates passed. The other 81 historical bases failed on fixed Python traceback behavior. |
| `pytest-dev/pluggy` | 1.952-2.411 | One green hash; 144 passed | Gate passed; fixture rejected at 19 of 38 candidates: 17 test overlays were not red and 2 bases were not green. |
| `encode/starlette` | 13.673-16.293 | Five hashes; each 1,096 pass/14 fail/4 skip | Rejected: Windows symlink-privilege and CRLF failures included run-specific temporary paths. |
| `tox-dev/tox` | Not run | Not run | Preflight rejection: its canonical integration suite installs from PyPI/devpi and conflicts with no-egress consumption; its Windows integration configuration also contains multi-minute tests. |
| `pydantic/pydantic` | Not run | Not run | Preflight rejection: exact native `pydantic-core==2.48.0`, a large dependency surface, and only 12 conservative current-core historical candidates. |
| `more-itertools/more-itertools` | Not run | Not run | Preflight rejection: canonical `unittest`/`load_tests` doctests are not the plain pytest suite available in cloud, and its small dependency-light utility structure is too close to Click on the requested axis. |
| `pyparsing/pyparsing` | Not run | Not run | Preflight rejection: only 30 plausible source/test PRs existed, with no attrition margin, and the canonical suite adds Jinja/railroad dependencies. |
| `pygments/pygments` | 25.526-33.686 | One green hash; 5,330 pass/16 skip | **Accepted:** all strict gate conditions passed and 47 path-disjoint candidates existed before outcomes. |

### Accepted gate detail

The five Pygments walls were 31.373, 28.420, 27.940, 33.686, and
25.526 seconds. Every run exited zero with 5,330 passed and 16 skipped. Every
normalized result had SHA-256
`c0cfda900015648c012c77e5e0b9b47659d0b278f38f5e78e9b11da68f54df06`.

The immutable identities were:

- HEAD `38f426a6b1cd4ffc6429f5808031b7c62ea57b1f`
- HEAD/index tree `ef5ef11d79315fe64ed6663277d7466c4d065b16`
- empty tracked-diff SHA-256
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- CPython 3.11.9

The complete clone was non-shallow and passed `git fsck --full`. Candidate
checkouts totaled about 260 MiB at peak; the Pygments clone itself was about
74.9 MiB. Isolated environments were outside the emitted fixture. The final
fixture is 43.5 MiB, of which its single plain base is 43.1 MiB.
The task-created clones and virtual environments were removed after final
validation; the compact gate, ledger, result, and arm evidence was retained.

Machine evidence:

- gate: `exploratory/causal/gates/pygments__pygments/gate.json`
- frozen rule: `exploratory/causal/PYGMENTS-SELECTION-RULE.md`
- ledger: `exploratory/causal/inventory/pygments-candidates.json`
- verification: `exploratory/causal/verification/pygments-results.json`
- constructor: `instruments/causal/build_pygments_fixture.py`

## Why Pygments is structurally unlike Click

This choice is different on architecture and test style, not merely project
name or popularity.

The accepted Pygments tree has 343 production Python files and about 118,455
physical lines, including 263 lexer modules. Its core behavior is a registry
of language recognizers implemented largely as regex/state-machine tables,
with shared lexer machinery, formatters, filename/alias guessing, and generated
mapping data. A change in one language module can therefore be exercised by
both its own sample and repository-wide registry/guessing tests.

Its tests also use a native custom pytest collector over 672 example inputs
and 836 inline snippet inputs: 1,508 golden cases backed by about 39.3 MiB of
test artifacts. The accepted Click fixture base, by comparison, has 17
production Python modules and about 9,096 physical lines, centered on CLI
command/context behavior and conventional Python test modules. Pygments thus
changes both the production topology (hundreds of data-rich recognizers behind
shared registries) and the observation style (file/golden collectors plus
global registry tests). That is the axis the second fixture was chosen to
stress.

The final red results exhibit this coupling rather than merely asserting it:
PR 3225 produced 12 full-suite failures from a lexer/registry addition, while
other tasks produced one to six failures across Python and golden artifacts.

## Selection rule and yield

The rule was finalized after the current-tree gate and outcome-blind static
audit, before the ledger and before any historical pytest run. The only
clarification made during static audit was that a golden `.output` file remains
in the test patch but is not itself collectable and cannot make an output-only
change qualify. No historical outcome existed at that point.

The fixed rule:

1. Scan first-parent commits since 2024-01-01 from the gated anchor, newest
   first. Recognize only unambiguous non-revert `(#N)` or
   `Merge pull request #N` landings.
2. Reduce each first-parent diff to A/M paths under `pygments/` and `tests/`.
   Require 2-30 paths, a production Python file, a changed collectable test
   outside `tests/contrast`, at most 10 test paths, at most 500 KiB, regular
   blobs, and reverse applicability from the anchor.
3. Keep candidates greedily only when their complete reduced path set is
   disjoint from every earlier kept candidate.
4. Freeze and hash the complete ledger before pytest. Require all 47 patches
   to reverse cumulatively to
   `fdba588edb787a757b5f332715bb119d1c11397a` and replay exactly to the anchor
   tree.
5. Screen every kept candidate in order on a singleton reversion: changed
   tests red and mapped without source; the complete patch restores the anchor
   and those targets green.
6. Take the first 30 provisional candidates, reverse exactly those into one
   common base, then use the complete fixed suite for base green, each
   tests-only red, and each complete-patch green. Replacement would be one at a
   time in ledger order with the whole common-base proof restarted.

The task environment was CPython 3.11.9 and pytest 8.4.2 with plugin autoload
disabled. The fixed task command was
`python -m pytest --ignore=tests/contrast`. This single exclusion was fixed
before outcomes because `tests/contrast` imports the unavailable
`wcag-contrast-ratio`; it is identical in every arm.

Accounting:

- 385 first-parent commits scanned
- 231 unambiguous unique PR landings
- 71 structurally eligible before disjointness
- 47 path-disjoint candidates frozen and examined
- 46 candidates passed targeted screening
- PR 3057 rejected because its changed test remained green without source
- first 30 provisional candidates passed final verification on attempt 1
- 30 / 47 examined yielded accepted tasks: **63.8%**
- no final task replacements

The ledger SHA-256 is
`3746dde7222bcd854ed9d6af816ffaae9ed588345bdc13997928aadded4a51bf`.
The final 30-task shared base is tree
`ad20930041fbd242b17a4ce3e84770b63743ef7e`, and replaying all complete patches
reconstructs the gated anchor exactly.

## Green-red-green verification for every accepted task

The common base green was 5,274 passed, 0 failed, 0 errors, and 16 skipped
(5,290 cases). Counts below are `passed / failed / errors / skipped`. `Mapped`
is the number of red failures mapped exactly to a changed test artifact. All
red arms exited 1; all green arms exited 0; every arm preserved the bytes of
all files present at its start.

A post-emission smoke run from a fresh copy of the actual vendored base
repeated 5,274 passed and 16 skipped in 22.14 seconds with the documented
pytest-only command.

| # | PR | Base green | Tests-only red | Mapped | Complete-patch green |
|---:|---:|---|---|---:|---|
| 1 | 3225 | 5274 / 0 / 0 / 16 | 5272 / 12 / 0 / 16 | 9 | 5288 / 0 / 0 / 16 |
| 2 | 3217 | 5274 / 0 / 0 / 16 | 5273 / 1 / 0 / 16 | 1 | 5274 / 0 / 0 / 16 |
| 3 | 3209 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 4 | 2969 | 5274 / 0 / 0 / 16 | 5275 / 1 / 0 / 16 | 1 | 5276 / 0 / 0 / 16 |
| 5 | 3215 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 6 | 3216 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 7 | 3214 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 8 | 3213 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 9 | 3211 | 5274 / 0 / 0 / 16 | 5273 / 1 / 0 / 16 | 1 | 5274 / 0 / 0 / 16 |
| 10 | 3206 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 11 | 3210 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 12 | 3204 | 5274 / 0 / 0 / 16 | 5274 / 6 / 0 / 16 | 6 | 5280 / 0 / 0 / 16 |
| 13 | 3201 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 14 | 3199 | 5274 / 0 / 0 / 16 | 5274 / 2 / 0 / 16 | 1 | 5276 / 0 / 0 / 16 |
| 15 | 3197 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 16 | 3195 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 17 | 3190 | 5274 / 0 / 0 / 16 | 5274 / 2 / 0 / 16 | 2 | 5276 / 0 / 0 / 16 |
| 18 | 3185 | 5274 / 0 / 0 / 16 | 5273 / 2 / 0 / 16 | 2 | 5275 / 0 / 0 / 16 |
| 19 | 3177 | 5274 / 0 / 0 / 16 | 5273 / 2 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 20 | 3163 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 21 | 3164 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 22 | 3140 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 23 | 3143 | 5274 / 0 / 0 / 16 | 5275 / 2 / 0 / 16 | 2 | 5277 / 0 / 0 / 16 |
| 24 | 3160 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 25 | 3159 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 26 | 3165 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 27 | 3167 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 28 | 3176 | 5274 / 0 / 0 / 16 | 5273 / 2 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 29 | 3172 | 5274 / 0 / 0 / 16 | 5274 / 1 / 0 / 16 | 1 | 5275 / 0 / 0 / 16 |
| 30 | 3168 | 5274 / 0 / 0 / 16 | 5274 / 5 / 0 / 16 | 5 | 5279 / 0 / 0 / 16 |

Full commits, subjects, dates, paths, per-arm runtimes, normalized hashes, and
mapped failing testcase identities are in `fixture/pygments/TASKS.json`. Its
SHA-256 is
`ede0b9c52358fbb7d7239c0dea8ff2ba24266debf6ce85ee9e6f7dd6b2374dea`.

## Claims that could NOT be verified

Confidence below means confidence that the stated unverified claim would hold,
not confidence in the fact that it remains unverified.

| Unverified claim | Confidence | Reason |
|---|---|---|
| The task fixture gives identical normalized results on Codex Cloud/Linux. | Medium | Construction and all gates ran on Windows 10 with CPython 3.11.9. The fixture is pytest-only and uses regular files, but no cloud execution was performed. |
| The task command never attempts network access under a hard egress block. | Medium-high | Static imports and successful isolated runs showed no task dependency beyond pytest/local Pygments, but the local runs were not executed inside a network namespace that forcibly denied sockets. |
| Every recorded PR number and landing description matches the remote GitHub PR page. | Medium-high | Numbers came from unambiguous local first-parent commit subjects; remote pages were deliberately not queried during selection. |
| Every individual production hunk, rather than the reduced patch as a whole, is necessary for its tests. | Low | Green-red-green establishes patch-level discrimination. It does not perform hunk-level ablation or prove a unique minimal fix. |
| Task arms cover literally the complete upstream-declared suite. | None; known false | `tests/contrast` is consistently excluded because it requires `wcag-contrast-ratio`. The current-tree gate, but not task arms, included it. |
| Thirty Pygments tasks plus the existing Click fixture establish population-level generality. | Low | This removes the one-repository design defect but still covers only two repositories. The requested perturbation sweep was not run. |
| The frozen modern pytest/Python environment exactly reproduces each PR's original CI environment. | Low | Historical tasks were intentionally normalized onto CPython 3.11.9 and pytest 8.4.2; original CI matrices were not reconstructed. |

## What would change this verdict

| New evidence | Effect on verdict | Confidence and reason |
|---|---|---|
| Any repeat of the unmodified Pygments gate changes the normalized hash, tracked identities, or exceeds 120 seconds. | Revoke the repository gate and stop using the fixture until explained and re-gated. | High: these are mandatory preregistered conditions. |
| The shared base is not green, any tests-only arm is not red with a mapped changed-test failure, or any complete-patch arm is not green on replay. | Reject that cohort/task and rebuild using the frozen next-candidate rule; if supply is exhausted, reject Pygments. | High: this is the causal definition, not a quality preference. |
| A no-egress Codex Cloud run cannot collect or complete the fixed task suite with pytest alone. | Mark the fixture non-consumable in its target environment; either vendor a permitted dependency under a newly fixed rule or reject it. | High: self-contained cloud execution is an explicit requirement. |
| Linux/cloud repeats are green but produce different normalized identities. | Restrict the fixture's validity to the measured platform or re-gate on the target platform before a sweep. | High: cross-platform equivalence is currently unverified. |
| Remote PR provenance contradicts a commit-subject PR number or the BSD-2-Clause licence record. | Correct provenance and regenerate affected manifests/patches; causal test evidence may remain, but the historical-PR claim would be downgraded meanwhile. | Medium-high: local Git/licence evidence is strong but not the omitted remote check. |
| The selection rule, ledger bytes, accepted anchor, or patch hashes change. | Invalidate all current outcome evidence and rerun construction from the static ledger stage. | High: the hashes bind outcomes to the preregistered inputs. |
| A sweep across these tasks and additional structurally distinct repositories shows stable effects. | Strengthen external-validity claims beyond the present two-repository probe. | Medium: it would address breadth, but representativeness would still depend on repository/task sampling. |

## Verdict

The second fixture is usable for the intended causal probe. Pygments passed the
strict deterministic gate, the selection rule was fixed before historical
outcomes, 47 candidates were examined, and 30 tasks passed complete
green-red-green verification on a single exactly replayable base. This verdict
does not include, imply, or rely on a perturbation sweep.
