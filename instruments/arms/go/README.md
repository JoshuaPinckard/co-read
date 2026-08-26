# Go arms runner

This directory contains the Go-specific instruments for the arms ladder. The
commands share a fail-closed contract: a successful process exit without an
observed focal test is not evidence.

## Pinned build

The Phase 0 gate was built and exercised with the project-local Go 1.16.15
Windows/amd64 archive. Build all commands and run their tests from this
directory:

```text
../../../tools/go/bin/go test ./...
../../../tools/go/bin/go build ./cmd/perturb
../../../tools/go/bin/go build ./cmd/focalmap
../../../tools/go/bin/go build ./cmd/focalgate
```

There are no third-party dependencies in this module.

## Perturbation

`perturb` uses `go/parser` positions to insert `panic("perturbed")` immediately
after the opening brace of every top-level `*ast.FuncDecl` body in one target
file. This includes free functions, methods, and `init`. It does not traverse
`*ast.FuncLit`, so closures are not independently perturbed. It refuses:

- a basename ending in `.pb.go`;
- a basename beginning `zz_generated`;
- a leading `//go:build` or `// +build` expression containing the `ignore` tag;
- a leading standard `// Code generated ... DO NOT EDIT.` marker; and
- a file with no named function or method body.

The command captures the original bytes and SHA-256 before parsing. It never
uses formatted/parser output for restoration. Cleanup is installed before the
first write, including the partial-write failure path. `-roundtrip` writes the
perturbation, restores the captured bytes in deferred cleanup, rereads them,
and requires both byte equality and SHA equality:

```text
perturb -file path/to/file.go -roundtrip
```

To run an oracle while the file is perturbed, place the command after `--`.
The child exit code is preserved after successful restoration:

```text
perturb -file path/to/file.go -- go test ./pkg -run ^TestName$ -count=1
```

Interrupt notification is installed before the first mutation and remains
active through deferred restoration, including round-trip and pre-child gaps.
While a child is running, an interrupt kills it before restoration. As with any
in-process cleanup, an uncatchable process kill or power loss can prevent the
deferred restore; arms orchestration must therefore use disposable scratch
clones and verify tracked state after every invocation.

## Focal mapping

`focalmap` reads Git objects without checking out or executing repository code:

```text
focalmap -repo path/to/bare-mirror -base B -parent P1 -parent P2 -out map.json
```

For each zero-context `B..Pi` hunk in `_test.go`, it parses both images and
selects an overlapping top-level named `TestXxx` declaration/body. A hunk in a
`t.Run` closure or table-driven loop is inside that declaration and therefore
maps to the parent `TestXxx`. Import, package-scope fixture, and helper-only
hunks map to no name and are counted, not guessed. Methods, `TestMain`,
benchmarks, fuzz targets, examples, helpers, and anonymous functions are not
focal test identities.

Names are unioned by package, sorted, regexp-escaped, and emitted as
`^(?:TestA|TestB)$`. Expressions are package-specific: sharing one expression
across packages can accidentally select an unrelated test with the same name.
The output distinguishes names present at B from added/renamed names absent at
B. The latter are recorded but cannot silently count as executed in the base
gate. Mapping is fail-closed per side: each of the two parent test-side diffs
must map at least one named test that is present at B. A helper/import-only side
or a side containing only newly added/renamed tests rejects the site, even when
the other side yields a runnable union.

## Five-run base gate

Materialize B using an independent local clone. Do not attach a worktree to a
task-owned bare mirror because that writes mirror metadata. Warm dependencies
before the gate, then invoke:

```text
focalgate -repo path/to/scratch \
  -mapping map.json \
  -go path/to/tools/go/bin/go.exe \
  -goflags "-vet=off" \
  -go111module off \
  -env GOPATH=path/to/local/gopath \
  -env GOCACHE=path/to/local/gocache \
  -out gate.json
```

For a module repository use `-goflags "-mod=readonly -vet=off"` and provide a
project-local `GOMODCACHE`. Warm in a disposable copy with the proxy enabled;
discard any `go.mod`/`go.sum` changes before gating the exact base. Legacy
GOPATH repositories without a committed dependency lock/vendor snapshot are
rejected when their dependency graph cannot be reconstructed exactly.

The gate always sets `GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, the
chosen `GOFLAGS`, and the chosen `GO111MODULE`; it removes ambient `TF_ACC`.
It also pins `GOPRIVATE` empty, `GONOPROXY=none`, `GONOSUMDB=none`,
`GOINSECURE` empty, and `GOVCS=*:off`, preventing private-module rules from
bypassing the disabled proxy. Reserved values cannot be overridden with
`-env`; that option is only for paths such as `GOPATH`, `GOMODCACHE`, and
`GOCACHE`. The mapping is rejected unless every supplied regex is exactly the
anchored, escaped expression derived from its sorted base-present names.
`-vet=off` keeps a newer toolchain's evolving static diagnostics out of the
runtime oracle. It runs one command per package in the form:

```text
go test <package> -run '<anchored package regex>' -count=1 -json
```

Five consecutive runs are eligible only if the starting checkout is the clean
exact base (HEAD/index/tree agree and there are no tracked, untracked, or
ignored additions), every command exits zero, every base-resolvable mapped
parent reaches a terminal event, at least one focal parent passes, exact-base
state remains unchanged, and the sorted normalized tuples `(requested
package, full Test/subtest name, pass|fail|skip, TF_ACC guard)` are identical.
Timing, elapsed fields, output text, and event order are ignored. Repeatable
build failures, timeouts, missing tests, and empty result sets are rejections;
an all-skipped focal set is rejected. A skip is attributed specifically to
`TF_ACC` only when its emitted skip output contains that string; a generic
guard message receives a generic rejection reason.
Each output uses a dedicated `<out>.artifacts/` directory for JSONL/stdout and
stderr evidence, so package logs from different site gates cannot overwrite one
another; an artifact-write failure rejects the run.
