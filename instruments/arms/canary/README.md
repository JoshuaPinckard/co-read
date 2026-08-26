# ARMS clean-room canary

This directory contains the environment-manifest and two-sided planted-marker
instrument required by `HYPOTHESES.md` section C, Amendment 2, precondition 2.
It is standard-library-only and works with Windows paths and CLI wrappers.

The immutable `certificates/` evidence records the 2026-08-25 calibration:
Claude's independently valid legs remain in the original joint-run certificate,
and Codex's valid legs are in the targeted rerun certificate. `check-set`
revalidates them together and accounts for all six probe invocations. A
certificate is evidence of calls that actually ran; generating one without the
calls would defeat the gate. The `calibrate` command is the only code path that
launches subject CLIs, and it requires `--confirm-model-calls`.

## What the canary proves

For each explicitly selected CLI, one planted probe runs in a newly created
room containing two different marker tokens: one in the redirected global
instruction file and one in the isolated workspace instruction file. The
response must contain both tokens. A second probe runs in a separate clean room
and must return a run-specific clean acknowledgement without either token.

The calibration therefore uses exactly two model-probe CLI invocations per
selected surface: four for one joint Codex-plus-Claude calibration. The code
retains an eight-invocation hard ceiling and never retries a probe. If a failed
joint run leaves one independently certified surface, a targeted two-probe
rerun of only the other surface can be aggregated without altering either
certificate.
`--version` metadata queries do not contact a model and are recorded separately.

A crash, empty output, timeout, unparsable structured response, missing marker,
or missing clean acknowledgement fails. Diagnostic stdout/stderr is never
treated as model output. The planted/clean manifests, raw outputs, extracted
responses, hashes, commands, timestamps, room contents, and failure evidence
remain under the certificate output directory.

## Instruction locations recorded

The manifest is intentionally conservative and records absent candidates as
well as existing files:

| CLI | Global and managed locations | Workspace locations |
| --- | --- | --- |
| Codex | `CODEX_HOME/AGENTS.override.md`, then `AGENTS.md`; `$HOME/.agents/skills`; admin skills; user/system/managed configuration and system `requirements.toml` | From Git root to cwd, one instruction candidate and every `.codex/config.toml`/`.agents/skills` layer per directory |
| Claude | Managed `CLAUDE.md`; `CLAUDE_CONFIG_DIR/CLAUDE.md`; user rules/settings and auto-memory search root | `CLAUDE.md`, `CLAUDE.local.md`, `.claude/CLAUDE.md`, rules and settings along the cwd ancestry |
| Gemini | System settings; `GEMINI_CLI_HOME/.gemini/GEMINI.md`; user settings, env, and extension context | Effective `context.fileName` candidates and env/settings along the cwd ancestry |

Each existing regular file record includes SHA-256, byte length, nanosecond
mtime, symlink status, scope, and discovery semantics. Environment manifests
record redirect paths and which authentication variable names were present,
but never authentication values.

The path model follows the current vendor documentation:

- [Codex AGENTS.md discovery](https://developers.openai.com/codex/guides/agents-md)
- [Claude Code memory and CLAUDE.md](https://code.claude.com/docs/en/memory)
- [Claude Code configuration relocation](https://code.claude.com/docs/en/claude-directory)
- [Gemini CLI GEMINI.md hierarchy](https://geminicli.com/docs/cli/gemini-md/)
- [Gemini CLI configuration and GEMINI_CLI_HOME](https://geminicli.com/docs/get-started/configuration/)

## Read-only operations

Generate a manifest without launching any CLI:

```powershell
python instruments/arms/canary/canary.py manifest `
  --cli codex `
  --cwd C:\path\to\agent-worktree `
  --draw-id arms-draw-id `
  --model gpt-explicit-id `
  --output C:\path\to\draw\codex-environment.json
```

Inspect exact command construction without launching a CLI:

```powershell
python instruments/arms/canary/canary.py plan `
  --surface codex --model codex=gpt-explicit-id `
  --surface claude --model claude=claude-explicit-id
```

`plan` resolves executable paths, but does not execute them.

## Calibration (real short model calls)

This job's approved calibration surfaces are Codex and Claude; do not add
`--surface gemini`: the calibration API and CLI reject it. Gemini's manifest
and command adapter remain available for later protocol use. Model identifiers
are mandatory so certificates never silently inherit a mutable CLI default.

If authentication is already supplied through the provider API-key environment
variable, run:

```powershell
python instruments/arms/canary/canary.py calibrate `
  --surface codex --model codex=gpt-explicit-id `
  --surface claude --model claude=claude-explicit-id `
  --confirm-model-calls
```

For an existing CLI login, copy only the named credential file into each clean
leg. Nothing else from the real CLI home is copied:

```powershell
python instruments/arms/canary/canary.py calibrate `
  --surface codex --model codex=gpt-explicit-id `
  --credential codex=C:\Users\name\.codex\auth.json `
  --surface claude --model claude=claude-explicit-id `
  --credential claude=C:\Users\name\.claude\.credentials.json `
  --confirm-model-calls
```

Copied live credential files are removed after the probes (or after a
preflight/version failure). The certificate retains their source/destination,
size, SHA-256, removal timestamp, and absence-after-cleanup result, but not the
credential bytes.
When a credential file is supplied, inherited provider token/key variables are
removed from both legs so CLI precedence cannot silently select another account.

The default certificate/evidence destination is `certificates/`. On Windows,
the actual rooms default to `C:\arms-canary-rooms`; placing them at the volume
root keeps a real user `.claude`, `.agents`, or similar directory out of the cwd
ancestry. Use `--room-root` to select another already-understood clean anchor.
Rooms are persistent and their absolute location is recorded. Every immutable
JSON certificate has a `.json.sha256` sidecar and an evidence directory. A
failed or partial run also writes a `FAIL` certificate; absence of a marker is
never inferred from a missing or broken executable.

## Pilot gate

Require a same-day, integrity-checked pass for every surface that the pilot will
use:

```powershell
python instruments/arms/canary/canary.py check `
  --certificate instruments/arms/canary/certificates/CANARY-YYYY-MM-DD-ID.json `
  --require codex --require claude
```

If a multi-surface calibration fails only one surface, preserve that immutable
certificate and rerun only the failed surface within the eight-call job budget.
Gate the independently valid surface evidence as a set:

```powershell
python instruments/arms/canary/canary.py check-set `
  --surface-certificate claude=C:\path\to\original-fail.json `
  --surface-certificate codex=C:\path\to\codex-rerun-pass.json
```

`check-set` revalidates each surface's raw evidence and sidecar, requires the
same calibration day/UTC offset, leaves every source certificate immutable, and
counts every distinct source run's calls once against the eight-call ceiling.

"Same day" is evaluated using the calibration host's recorded UTC offset. The
single-certificate checker also requires a `PASS` verdict. Both checkers require
SHA-256 sidecars, recompute each selected surface's response semantics, and
enforce the eight-call ceiling. The arms runner should call
`instrument.check_certificate_set` immediately before its first draw and fail
closed on any error.

## Clean-room controls

Every planted and clean leg gets separate `HOME`, `USERPROFILE`, `APPDATA`,
`LOCALAPPDATA`, XDG directories, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and
`GEMINI_CLI_HOME`. Gemini system settings are redirected to nonexistent files,
`GEMINI_SYSTEM_MD` is disabled, and Claude auto-memory is disabled. Only
platform/network essentials and the selected provider's authentication
variables survive. Provider base-URL overrides are deliberately discarded so
an inherited endpoint cannot counterfeit a passing calibration. A manifest
warning, unreadable candidate, or unexpected
instruction/configuration source prevents model launch.

The workspace ancestry itself cannot be redirected by a HOME variable. The
preflight therefore enumerates it to the filesystem root and refuses to launch
if it finds an unexpected instruction source. Claude's documented Windows
managed-policy path and Codex's system requirements/managed-default locations
are checked as well.

## Version-sensitive limits

CLI instruction discovery changes over time. The manifest covers the documented
static channels and rejects populated rules, auto-memory, extensions, settings,
and env loaders in calibration rooms. It does not claim to reverse-engineer an
unknown future CLI or an enterprise binary patched to read another location.
Imported files referenced from a contaminated instruction file are not expanded
because the presence of that parent file already fails the clean-room preflight.
Cloud-delivered enterprise policy has no stable local file location to hash;
the manifest records local managed layers but the certificate does not claim to
inventory an opaque provider-side policy bundle.

The eight-call ceiling counts CLI model-probe invocations. A vendor CLI may
perform opaque transport retries inside one invocation; the harness cannot
measure or certify a provider-internal HTTP request count.

The certificate records the exact requested model identifier and the literal
`--version` output. It cannot prove which provider-side snapshot a mutable model
alias resolved to unless the provider/CLI exposes that identifier in a stable
machine-readable field. Pin immutable identifiers where the CLI supports them.

Run the tests without any subject CLI calls:

```powershell
python -m unittest discover -s instruments/arms/canary/tests -v
```

The end-to-end test injects an in-process fake runner and never invokes
`codex`, `claude`, or `gemini`.
