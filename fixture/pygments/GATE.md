# Second-repository gate

## Gate result

**Accepted: `pygments/pygments` at
`38f426a6b1cd4ffc6429f5808031b7c62ea57b1f`.**

The complete repository-declared pytest suite ran five times in an isolated
CPython 3.11.9 environment. All runs exited zero with 5,330 passed and 16
skipped. The normalized result was the sorted JUnit tuple `(classname, name,
outcome, failure/error type or message)` and had SHA-256
`c0cfda900015648c012c77e5e0b9b47659d0b278f38f5e78e9b11da68f54df06`
on every run.

Before and after every run, all of the following were identical:

- HEAD `38f426a6b1cd4ffc6429f5808031b7c62ea57b1f`
- HEAD and index tree `ef5ef11d79315fe64ed6663277d7466c4d065b16`
- empty tracked status
- empty tracked-diff SHA-256
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`

| Run | Harness wall seconds | Exit | Normalized result |
|---:|---:|---:|---|
| 1 | 31.373 | 0 | 5,330 passed, 16 skipped; fixed hash |
| 2 | 28.420 | 0 | identical |
| 3 | 27.940 | 0 | identical |
| 4 | 33.686 | 0 | identical |
| 5 | 25.526 | 0 | identical |

The longest run was 33.686 seconds, well below the approximately two-minute
ceiling. The clone was complete and non-shallow, `git fsck --full` reported no
missing objects, and its Git pack plus checkout occupied about 74.9 MiB.

The gate used the repository's declared pytest dependencies, including
`wcag-contrast-ratio` for `tests/contrast` and `pytest-randomly`. The later
task suite uses pytest only and excludes `tests/contrast` consistently across
all arms; this preregistered cloud-compatibility difference is not part of the
five-run gate claim.

## Candidates tried

An executed row is accepted by the strict repository gate only if all five
runs are green, under 120 seconds, have one normalized hash, and preserve all
tracked repository identities. Every executed row below preserved tracked
state; failures are test/runtime/determinism failures rather than source-tree
mutation. `Not run` means an outcome-blind preflight already established a
mandatory incompatibility, so five expensive runs were not started.

| Candidate/configuration | Runtime seconds | Five-run normalized result | Decision |
|---|---:|---|---|
| `psf/requests` (pre-existing valid gate evidence) | 178.98 pytest; >181 harness | One valid green run; not repeated | Rejected: exceeded the runtime ceiling. |
| `pallets/itsdangerous` (pre-existing gate evidence) | 2.044-5.895 | Five identical green runs; 297 passed | Gate passed, fixture rejected: prior construction showed insufficient task structure. |
| `encode/httpx` | 109.244-126.003 | Two hashes; four runs had 1,409 pass/3 fail/1 skip, one had 1,408 pass/4 fail/1 skip | Rejected: nondeterministic and one run exceeded 120 seconds. Network-marked tests alone were excluded consistently. |
| `pallets/flask` | 4.754-9.994 | One hash; 494 passed | Gate passed, fixture rejected: only 2 of 60 frozen historical candidates achieved green-red-green; 58 historical bases were not green under the fixed environment. |
| `python-attrs/attrs`, initial PATH | 24.958-32.090 | One failing hash; 1,401 pass/3 fail/8 skip | Invalid environment: a broken global `pyright` shim caused all three failures. |
| `python-attrs/attrs`, clean PATH | 26.615-39.222 | One green hash; 1,401 pass/11 skip | Gate passed, fixture rejected after 0 of 4 early historical bases were green because raw exports lacked distribution metadata; the rule was not retrofitted after seeing that. |
| `marshmallow-code/marshmallow`, missing `tzdata` diagnostic | 0.976-2.739 | Five collection failures; no JUnit cases | Rejected invalid environment; rerun with the missing platform data below. |
| `marshmallow-code/marshmallow`, with `tzdata` | 5.103-6.974 | Five hashes; 1,189 pass/1 fail | Rejected: Windows timestamp-overflow failure details varied. |
| `marshmallow-code/marshmallow` 3.26.2 | 4.101-5.995 | Five hashes; 1,238 pass/2 fail | Rejected: missing distribution metadata plus Windows overflow behavior. |
| `marshmallow-code/marshmallow` 3.21.3, missing `pytz` diagnostic | 0.402-0.984 | Five collection failures; no JUnit cases | Rejected invalid environment; rerun with declared dependencies below. |
| `marshmallow-code/marshmallow` 3.21.3, declared dependencies | 3.363-4.764 | Five distinct hashes; all runs 1,229 passed | Rejected: two parametrized testcase identities embedded the current seconds, so green exits were not deterministic. |
| `pallets/jinja` | 5.642-7.420 | One hash; 911 passed | Gate passed, fixture rejected: 7 of 88 frozen candidates passed construction; 81 historical bases failed on the fixed Python traceback behavior. |
| `pytest-dev/pluggy` | 1.952-2.411 | One hash; 144 passed | Gate passed, fixture rejected: 19 of 38 candidates passed, short of 30; 17 test overlays were not red and 2 bases were not green. |
| `encode/starlette` | 13.673-16.293 | Five hashes; each run 1,096 pass/14 fail/4 skip | Rejected: Windows symlink privilege and CRLF failures included run-specific temporary paths. |
| `tox-dev/tox` | Not run | Not run | Rejected at preflight: its canonical integration suite installs from PyPI/devpi, conflicted with no-egress consumption, and exceeded a 240-second diagnostic window. |
| `pydantic/pydantic` | Not run | Not run | Rejected at preflight: the checkout required exact native `pydantic-core==2.48.0`, had a large dependency surface, and only 12 conservative current-core candidates survived static compatibility checks. |
| `more-itertools/more-itertools` | Not run | Not run | Rejected at preflight: the canonical suite is `unittest` with `load_tests` doctests that plain pytest omits, and its small dependency-light utility structure was too close to Click on the requested axis. |
| `pyparsing/pyparsing` | Not run | Not run | Rejected at preflight: only 30 plausible source/test PRs existed, leaving no attrition margin for a 30-task causal yield; the canonical suite also added Jinja/railroad dependencies. |
| `pygments/pygments` | 25.526-33.686 | One green hash; 5,330 pass/16 skip | **Accepted:** fast, deterministic, clean, structurally unlike Click, and 47 path-disjoint historical candidates remained before outcomes. |

## Why passing the gate did not guarantee selection

Flask, attrs, Jinja, and Pluggy all passed the strict unmodified-tree gate.
Each then received its own outcome-blind selection rule before historical test
execution. Their construction attrition is a result, not a relaxed threshold:
none was promoted with fewer than the requested 30 tasks. Pygments was the
first gated repository whose frozen candidate pool yielded 30 final tasks.

The current causal workspace used complete or tag-specific clones totaling
about 260 MiB at peak. Isolated environments and raw run evidence were kept
outside the fixture. The emitted fixture itself is 43.5 MiB: one 43.1 MiB base,
60 small patches, documentation, a manifest, and a compressed commit stream.
The task-created clones and virtual environments were removed after final
fixture validation; compact gate/verification evidence remains under
`exploratory/causal/`.
