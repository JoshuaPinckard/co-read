Gate verdict: 2/15 Go sites pass focal mapping and five-run base determinism -- hashicorp/terraform 0/12; gohugoio/hugo 2/3.

# Go runner: Phase 0 instrument gate

This is the Go runner gate required by `HYPOTHESES.md` section C, not the separate Phase 0 source/test red-green discrimination result. `ELIGIBLE` below means that this runner can map the site's two test-side diffs to base-present focal tests, reproduce its dependencies offline, and obtain five identical green base runs. Rejections are results. No full repository suite was run.

The exact machine-readable record is `exploratory/arms/sites-go.json` (SHA-256 `03061b5ae13aa51d66a63456c2864850cdf5c7a8fdaf110309c292c25ce4f741`). It contains every package-specific mapped name and anchored regexp, all 15 identities, the dependency inventory, normalized run signatures/events, and all 40 successful round-trip sample records. Its rows retain the core `sites.json` identity, side, verdict, failure, and evidence fields. Because this artifact gates only the runner and does not perform the later source/test red-green discrimination, `validated` is false on every row; the two passes are represented by `runner_eligible: true` and `verdict: "ELIGIBLE"`. The governing `HYPOTHESES.md` SHA-256 is `5ced2a18fdda351ca93c8344bcca2d5d8120f2d2020b28c1ec76df78736dfc1d`.

## Runner implementation

The source is under `instruments/arms/go/` and has no third-party Go dependency. The stable code-only source-manifest SHA-256 is `b5738fd185da201b841fa2f46c3f2d819d01161bac63a38c9165f594ed061a9b`; the manifest including `README.md` and `TOOLCHAIN.json` is `c3362f9ea2684ae57a4318c6a9f7e04c37219266094d680bdfacd25b50d6d80a`. Canonicalization is stored order, with each line `lowercase-sha256`, two ASCII spaces, path relative to `instruments/arms/go/`, then LF, including the final LF; code-only uses the first eight entries and full uses all ten.

Final binaries used for the production evidence were built with `go build -trimpath`; a final rebuild reproduced all three hashes exactly:

- `perturb`: `5e622dc9f48c8136cc8c363690c594c406772f2cb1ad2cc323b1c68c541dbe7c`
- `focalmap`: `cf2857b53ae7cf269054a1bcfd311eeceb4beb39ce1478fa8d60adb2d81733d2`
- `focalgate`: `44c08c3d334e1113b93c32f659d328140ba26287ea2bedf56584e066c3494067`

`go test -count=1 ./...` and `go vet ./...` both pass. The final gate binary accepted the internal package partition, two-parent structure, base-presence partition, and exact regexp derivation of all 15 production maps. A second production regeneration of all 12 Terraform maps from the read-only mirror was byte-identical; the ordered map-manifest SHA-256 is `6358d8685180151ee73844fc9b6efe20528c88f850b8976f3583e4554b144e93`.

## Perturbation operator

`perturb` parses one file with `go/parser` and inserts a newline-delimited `panic("perturbed")` immediately after the opening brace of every top-level `*ast.FuncDecl` body. The rule includes free functions, methods, and `init`. It does not traverse `*ast.FuncLit`: closures, including anonymous functions assigned by a package-level variable initializer, are not independently perturbed.

The operator refuses a case-insensitive `*.pb.go` basename, a case-insensitive `zz_generated*` basename, a leading `//go:build` or `// +build` expression containing the `ignore` tag, the standard leading `// Code generated ... DO NOT EDIT.` marker, and files with no named function or method body.

Before parsing, it captures the original bytes, permissions, and SHA-256. Cleanup is installed before the first write. Restoration writes the captured bytes, never AST/printer output, then rereads and requires both byte equality and SHA equality. A catchable interrupt kills the child before cleanup. An uncatchable process kill or power loss remains outside an in-process cleanup guarantee, so every use is confined to a disposable scratch checkout and followed by an exact-base Git check.

Round-trip gate:

| Repository | Sample base | Deterministic selection | Result | Bodies changed | Final state | Evidence SHA-256 |
|---|---|---|---:|---:|---|---|
| hashicorp/terraform | `aa1c4cafa4028663f6184cb5b7e19a98e0399906` | 2,478 tracked non-vendor `.go` candidates, sorted by SHA-256 of normalized relative path | 20/20 files; 20 attempts | 77 | all perturbed hashes differed; all restored bytes/hashes exact; clean | `eb9739869ac5b2b142dd1009c243c2ee62395d1807fe6aeaa576901898e423f9` |
| gohugoio/hugo | `821adf3ae877fdddce67afcccd751d47f4589538` | 402 tracked candidates under the same path-hash rule | 20/20 eligible files; 21 attempts | 225 | one expected refusal (`hugolib/permalinker.go`, no named body); all attempts restored exact; clean | `deaa13dcfee5dca659dd18f4b34fba9e2239e6375c2de2c7ca718848048dcb0d` |

## Focal mapping rule

For each parent `Pi`, `focalmap` reads the task-owned mirror without checkout and computes `git diff --unified=0 --find-renames B Pi` for `_test.go` paths. It parses the old and new blobs. A non-empty hunk side maps only when its line range overlaps a top-level, receiver-free function whose name satisfies Go's lexical `TestXxx` convention. `focalmap` does not independently type-check the function signature; the fail-closed gate subsequently requires `go test` to discover and emit a terminal event for every expected parent. `TestMain`, methods, benchmarks, fuzz targets, examples, helpers, and anonymous functions are not test identities.

Package, import, package-scope fixture, and helper-only hunks are counted as unmapped rather than guessed. A table-driven loop or `t.Run` closure is inside its parent declaration, so it maps to the enclosing `TestXxx`; the dynamic subtest name is not frozen separately. Added or renamed tests absent at `B` are recorded but are not runnable evidence at `B`.

The strict Phase 0 rule is per side: each of the two parent test-side diffs must contribute at least one mapped `TestXxx` already present at `B`. This is the Go analogue of the frozen Python method in `exploratory/arms/protocol.json`, where every side's direct focal collector target must exist and collect at untouched `B`; it was fixed from the cross-language method, not adapted to these outcomes. Only then are the two sides unioned by package. Base-present names are sorted, regexp-escaped, and emitted exactly as `^(?:TestA|TestB)$`. The expression is package-specific because the same test name can exist in unrelated packages.

Three worked examples:

1. Terraform site 12, parent `672f4963...`, changes the table loop in `terraform/resource_address_test.go` to use `t.Run`. All 17 hunks remain inside `TestParseResourceAddress`, so the identity is the parent test. At `B`, the `./terraform` regexp is `^(?:TestApplyGraphBuilder_depCbd|TestCloseProviderTransformer_withTargets|TestParseResourceAddress)$`; no generated subtest label becomes a separate focal identity.
2. Hugo site 2 adds `TestAppend` under `./common/collections`, where that name is absent at `B`, while `TestAppend` already exists under `./tpl/collections`. The former is recorded as base-absent and has no run expression; the latter is included in `^(?:TestAppend|TestIsSet|TestSlice)$`. Package-specific inventory prevents the new test from being falsely credited to the other package.
3. Terraform site 4, parent `d4e7c2de...`, changes imports/package-level helpers in `resource_aws_s3_bucket_test.go` and `s3_tags_test.go`. None of its five hunks overlaps a named test body. The mapper returns zero, does not guess consumers, and the site is rejected before execution.

## Focal oracle and determinism gate

After dependency warm-up, each active package is invoked only as:

```text
go test <package> -run '<exact anchored package regexp>' -count=1 -json
```

There is no `./...` test run and no full-suite fallback. The gate requires a clean exact `B`: HEAD, index tree, and HEAD tree must agree; tracked, untracked, and ignored additions must be absent. It repeats the complete focal subset five times. Every package command must exit zero, every expected parent test must emit a terminal event, at least one focal parent must pass, and the exact-base state must remain unchanged.

Normalization sorts `(requested package, full parent/subtest name, pass|fail|skip, TF_ACC guard)`. It ignores timing, elapsed fields, output text, and event order. Five non-empty signatures must be identical. `TF_ACC` is removed from the environment. Guarded acceptance tests can therefore skip, and every all-skipped focal set rejects. The specific `TF_ACC` label is applied only when emitted skip output contains that string; neither eligible Hugo site had a skip.

Runs pin `GOFLAGS`; `GO111MODULE`; `GOPROXY=off`; `GOSUMDB=off`; `GOPRIVATE=`; `GONOPROXY=none`; `GONOSUMDB=none`; `GOINSECURE=`; `GOVCS=*:off`; `GOTOOLCHAIN=local`; `CGO_ENABLED=0`; and unset `TF_ACC`. Reserved values cannot be reintroduced through extra environment arguments. Module sites use `GOFLAGS=-mod=readonly -vet=off`; GOPATH sites use `GOFLAGS=-vet=off`. Disabling vet keeps evolving newer-toolchain diagnostics outside the runtime oracle.

## Toolchain and dependency warm-up

No Go executable was present on `PATH`. The installed toolchain is project-local `tools/go/`, not system-wide:

- `go version go1.16.15 windows/amd64`
- official `go1.16.15.windows-amd64.zip`, 144,126,817 bytes
- archive SHA-256 `0d6e551206b6d744d1286e62abf46aa2f17fed90f07ec4624a0448d71380407d`
- extracted inventory 9,807 files / 406,176,951 bytes, matching the archive inventory
- `go.exe` SHA-256 `0fea51e5fd529ec7d7cab943b93c12fd74664a7edc3954125777ef1cf66ef50e`
- `gofmt.exe` SHA-256 `6ab2c6c8f4794a1c28fdcf67c523c7d41cc229dcf58dd8ef20472862d701d2e7`
- Git `2.46.0.windows.1`; Windows `10.0.26200.8655`

Go 1.16.15 was pinned because the candidate bases span 2013-2018 GOPATH and early-module layouts, and it does not auto-switch toolchains. This parser does not support later generics syntax; that limitation does not affect these candidate snapshots.

Hugo sites 2 and 3 have the same committed module graph. `go mod download all` succeeded with the proxy enabled; the exact 71-entry `module@version` list is embedded in `sites-go.json`. Its newline-list SHA-256 is `6f9c19c33b771943e567e390e553065661c117a9fe74fd76de4339775a311e11`; the structured fetch artifact SHA-256 is `bc2bb105c8c526058bf70097eba7ccc6e132b1a0da7a23047010d87462874ff8`; `go.mod` is `fc557c67...8462a` and `go.sum` is `1a4c26f8...07394`.

Hugo site 1 is legacy GOPATH with no lock/vendor snapshot. A 300-second `go get -d ./hugolib` warm attempt timed out. Its partial, untrusted diagnostic inventory was: BurntSushi/toml `d733fc535e4a9f3c454e421a87b90ade5d49bf31`; kr/pretty `3cd153a126da607b78d1762779b1e1054f9889fc`; kr/text `8ec235ee9246453d64081cb5f2c8af2d5f8dccd8`; rogpeppe/go-internal `b848aba6082101b7cbc37c5e55b681dd13096926`; spf13/nitro `24d7ef30a12da0bdc5e2eb370a79c659ddccf0e8`; and theplant/blackfriday `979429e1c46cdf38ac4556e1f28ee60720e80c33`. These heads were not treated as a reproducible dependency lock. Offline runs still lacked `bitbucket.org/pkg/inflect` and `launchpad.net/goyaml`.

Terraform sites 1-8 have neither modules, vendor files, nor a recognized lock. A site-3 diagnostic warm attempt fetched six unpinned heads -- errwrap `f3725be46c9a3ed05afef6b71848942e3e017ca1`, go-multierror `6d4d48630db25c3c83fa83ecd41dd8438b82963c`, hcl `6bf1a67a9381988ec9d660b405a83dfc8b5fcf2a`, copystructure `d4ce1f938f7a7ea2a40bff4544b56be9c00b5e84`, mapstructure `8508981c8b6c964e6986dd8aa85490e70ce3c2e2`, and reflectwalk `e0c24fdb021963cd2c4013097a0b993a7c4e344f` -- then failed with an incompatible directory layout and missing closure. They were diagnostic only and never used for eligibility.

Terraform sites 9-12 use checked-in dependency snapshots and fetched nothing: respectively 2,104 files plus `Godeps/Godeps.json`, then 2,498, 2,532, and 3,804 files plus `vendor/vendor.json`. Site 9 nevertheless fails the focal-package compile on Windows in vendored `xanzy/ssh-agent/pageant_windows.go` (`Pointer` redeclaration and `syscall.Pointer`/`unsafe.Pointer` incompatibility).

`GOPROXY=off` plus the private-module/direct-VCS pins disables Go dependency retrieval during measured runs. This is not an operating-system firewall against arbitrary network calls made by repository tests.

## Per-site gate

In the focal column, `P1/P2` is each side's count of base-present mapped names; `U=P/A` is the package-union count of base-present/base-absent names. Union counts can be smaller than the side sum because the same test can be touched by both sides. Exact package/name arrays and regexps are in the corresponding `sites-go.json` row.

| Repo/site | Merge | Base B | Focal mapping | Five-run result | Verdict | Reason |
|---|---|---|---|---|---|---|
| Terraform 1 | `166847d5dcd782a7eb5b2fde9223ca306cc33c10` | `7d1d9bab79d5bbd839737fe48c4f1d9f17aafcc3` | P1/P2 9/122; U=131/59; 11 pkgs | 0/5, not run | REJECTED | No module/vendor/lock; exact GOPATH graph unavailable. |
| Terraform 2 | `0d1867c0b3c1822ef18808bc3a04250a871aff51` | `bea81d7710fecfa7610ac6ea7017645a09729582` | 0/95; U=95/44; 4 pkgs | 0/5, pre-run | REJECTED | Parent 1 has no base-present mapped test. |
| Terraform 3 | `a8c80a447ed41f5e99dd0de64022ab9cd25ce636` | `dc4abb48fad02c4b93d1185599812af32b0e31fb` | 5/0; U=5/11; 2 pkgs | 0/5, pre-run | REJECTED | Parent 2 has no base-present mapped test. |
| Terraform 4 | `c59bfd0ca56f77700638aa52be9787adce4ec4d7` | `e7e2aeadab2b5b31e86340727173d53e2174372e` | 19/0; U=19/12; 5 pkgs | 0/5, pre-run | REJECTED | Parent 2 has five helper/import hunks and zero named test-body mappings. |
| Terraform 5 | `5113761f41b247451701462123aaee4d6f6dddb9` | `931d05198c19f292b3b7a599c72d3f9fb5e35589` | 17/0; U=17/25; 7 pkgs | 0/5, pre-run | REJECTED | Parent 2 maps only a test absent at B. |
| Terraform 6 | `fafc32b18338a6fa4f7eef6d58760979b32a38e7` | `66c51d44f6b7cd50d620e7327f2a8d729ee7b12a` | 66/0; U=66/95; 15 pkgs | 0/5, pre-run | REJECTED | Parent 2 maps only a test absent at B. |
| Terraform 7 | `c79a661ce156d92f362149bfebf5b722f1c8e373` | `4bf0d5e24394f8eeb961b3d09c2708fe03b0ef86` | 7/2; U=9/9; 4 pkgs | 0/5, not run | REJECTED | Mapping passes; no module/vendor/lock, so exact GOPATH graph is unavailable. |
| Terraform 8 | `ee7553f076358e48510e1a950a80d4ce2818033a` | `c7573de75b196c474ba70de7f908afa8b83f8b41` | 64/0; U=64/54; 10 pkgs | 0/5, pre-run | REJECTED | Parent 2 maps only a test absent at B. |
| Terraform 9 | `c4fa91b176c63a71406163a1eb4526cc4c6efd98` | `980f165bf77d30e9d626b940f05d4be8e602137f` | 394/1; U=394/527; 38 pkgs | 0/5, compile preflight | REJECTED | Vendored `./communicator/ssh` does not compile with the pinned Windows toolchain. |
| Terraform 10 | `21d1ac41fab1820dc7ea80e198717892a98d92b6` | `66e5b1cc4c6d37058b32d17981e12480332f8a7c` | 11/0; U=11/25; 7 pkgs | 0/5, pre-run | REJECTED | Parent 2 has no named test-body mapping. |
| Terraform 11 | `4f256a54dbfd545668ca3976c393cd34b456faab` | `8b0983089597a79cb40c970a5039b2ea5eab2c69` | 367/0; U=367/566; 44 pkgs | 0/5, pre-run | REJECTED | Parent 2 maps only a test absent at B. |
| Terraform 12 | `be6ae20ac1858e0df0bac6b66160d4a87f79f642` | `aa1c4cafa4028663f6184cb5b7e19a98e0399906` | 71/0; U=71/75; 24 pkgs | 0/5, pre-run | REJECTED | Parent 2 maps only tests absent at B. |
| Hugo 1 | `a82efe5bb131f1d4a811d3220c2ce40d56aa9eaf` | `1de19926640ffc2baa43a9691b5d277a7f22cf49` | 8/3; U=10/21; 5 pkgs | 0/5 green; five build failures with identical empty outcome signatures | REJECTED | Missing `bitbucket.org/pkg/inflect` and `launchpad.net/goyaml`; no focal event observed. |
| Hugo 2 | `3583dd6d713c243808b5e8724b32565ceaf66104` | `398996e8b05e517ea3ffa1097c24799b945123d3` | 25/9; U=25/4; 6 pkgs | 5/5; 25 pass, 0 fail/skip each | ELIGIBLE | Signature `7e84b0a69f6e4b808f3b5266d67f7e38af0141f8b1f06b2d92e9b467a86dddd5`; clean every run. |
| Hugo 3 | `604ddb90c5d6f1ca5583be1ec0ea8e48f014741a` | `821adf3ae877fdddce67afcccd751d47f4589538` | 17/1; U=17/1; 5 pkgs | 5/5; 17 pass, 0 fail/skip each | ELIGIBLE | Signature `61e4b15a92ac636dbbb7d69a3dc5b9f8be73a6c1aefda50484537091897037f6`; clean every run. |

The exact Hugo focal sets are compact enough to state here:

- Hugo 1, `./hugolib`: `TestDegenerateEmptyPage`, `TestDegenerateInvalidFrontMatterShortDelim`, `TestDegenerateMissingFolderInPageFilename`, `TestLayoutOverride`, `TestNewPageWithFilePath`, `TestPageWithDelimiter`, `TestParseIndexes`, `TestPrimeTempaltes`, `TestRenderThing`, `TestTemplatePathSeperator`.
- Hugo 2: `./common/maps` has `TestScratchAdd`, `TestScratchAddSlice`, `TestScratchDelete`, `TestScratchInParallel`, `TestScratchSet`, `TestScratchSetInMap`; `./helpers` has `TestMakeSegment`; `./hugolib` has `TestEmbeddedSC`, `TestFigureImgHeight`, `TestFigureImgWidth`, `TestFigureImgWidthAndHeight`, `TestFigureLinkNoTarget`, `TestFigureLinkWithTarget`, `TestFigureLinkWithTargetAndRel`, `TestFigureOnlySrc`, `TestNestedSections`, `TestPageWithShortCodeInSummary`, `TestPermalinkExpansion`, `TestResourceChain`, `TestShortcodeFigure`, `TestWordCount`; `./tpl/collections` has `TestAppend`, `TestIsSet`, `TestSlice`; `./tpl/lang` has `TestNumFormat`.
- Hugo 3: `./common/collections` has `TestAppend`; `./helpers` has `TestMakeSegment`; `./hugolib` has `TestEmbeddedSC`, `TestFigureImgHeight`, `TestFigureImgWidth`, `TestFigureImgWidthAndHeight`, `TestFigureLinkNoTarget`, `TestFigureLinkWithTarget`, `TestFigureLinkWithTargetAndRel`, `TestFigureOnlySrc`, `TestNestedSections`, `TestPageWithShortCodeInSummary`, `TestPermalinkExpansion`, `TestShortcodeFigure`, `TestWordCount`; `./tpl/collections` has `TestIsSet`; `./tpl/lang` has `TestNumFormat`.

Terraform site 10 has supplemental union-only evidence: five manual runs were identical with 10 passes and one `TF_ACC` skip, signature `794b2fb63c3e2641a4493b052dd8bf96eae645e6e5c718b0a3fef23800681f91`. It does not change eligibility because its second parent contributes no base-present focal identity under the finalized per-parent rule.

## Claims that could NOT be verified

- No claim is made that either eligible site passes full Hugo tests, that any Terraform full suite passes, or that acceptance services work. Full suites were intentionally never run.
- Runner eligibility is not proof that each side's test patch discriminates its source patch from `B`; that separate Phase 0 red/green validation still remains.
- The Go environment blocked module and VCS dependency retrieval, not arbitrary sockets at the operating-system level.
- `TF_ACC` attribution is evidence-based but heuristic: a skipped test is labeled guarded only when its emitted skip output contains `TF_ACC`. An all-skipped set still rejects, but a guard with a generic message would not receive the specific `TF_ACC` label; mixed non-`TF_ACC` skips were not exercised by the eligible sites (both had zero skips).
- Restoration cannot be guaranteed after an uncatchable kill or power failure; the verified claim covers normal completion and handled interrupts, with disposable scratch containment.
- The runner was gated on Windows/amd64 with Go 1.16.15. Portability to another OS/toolchain and parsing of post-generics repositories were not tested.
- Hugo mapping provenance was bound to the exact base/parents and passed final structural validation, but unlike Terraform it was not independently regenerated a second time byte-for-byte.
- A literal pre-task Terraform mirror object count was not captured. Current object-file timestamps predate this gate work and scratch clones remained clean, so mirror non-mutation is strongly supported but not claimed from a direct before/after count. Hugo's before/after object counts were unchanged.
- Timing and test output text are deliberately outside the determinism claim; only normalized terminal test outcomes and exact repository state were compared.
- The production gate serialized an empty normalized-event slice as JSON `null`; the Hugo-1 ledger preserves that exact canonical payload, whose SHA-256 is `74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b`. This proves identical empty outcome signatures, not identical stderr text.
- The ledger embeds the reduced maps, regexps, module inventory, 40 round trips, and normalized run summaries needed for the stated verdicts. The larger raw mapping/gate transcripts remained in task-local scratch storage, so their recorded hashes are not durable path-addressable evidence in this checkout. Production `focalmap` and `focalgate` artifacts also do not embed the executable SHA; their association with the globally recorded final binary hashes is procedural rather than self-authenticating.

## What would change this verdict

- Terraform sites 1 and 7 need an archival, byte-pinned dependency closure (or a preregistered historical environment with equivalent hashes), followed by five green focal runs.
- Terraform site 9 needs a preregistered compatible toolchain/OS or dependency snapshot that compiles its mapped packages, followed by a new five-run gate. That would be an amended environment, not reinterpretation of this result.
- The nine per-parent Terraform mapping rejections need a justified, frozen mapping amendment or a base-present focal identity from the currently unrunnable side. A union-only rule would be a new protocol; the stable site-10 diagnostic shows why it cannot silently replace the approved per-side rule.
- Hugo site 1 needs a complete byte-pinned legacy GOPATH closure containing the two missing packages, then five green offline focal runs.
- Any future variation in normalized outcome, missing expected parent, entirely `TF_ACC`-guarded set, timeout, nonzero package exit, or checkout mutation rejects the affected site.
- Even after a runner rejection is cured, the separate source/test red-green discrimination gate must pass before an arms draw.

## Claim confidence

| Claim | Confidence | Reason |
|---|---|---|
| The delivered perturb/map/gate source implements the stated rules | High | Direct source audit, unit tests, vet, final binary hashes, and 15/15 internal map validation agree. |
| Perturbation restores 20 sampled files per repository byte-exactly | High | 40/40 successful samples include original/perturbed/restored hashes and clean post-Git state. |
| Terraform focal maps are exact under the declared syntactic rule | High | All 12 were independently regenerated from exact Git objects and were byte-identical. |
| Hugo focal maps are exact under the declared syntactic rule | High, slightly narrower | Exact base/parent binding plus final gate structural checks; no second independent byte-for-byte regeneration. |
| Hugo sites 2 and 3 are five-run deterministic under this environment | High | Canonical final-binary artifacts show five identical non-empty signatures, all package exits zero, 25/17 passes, and unchanged exact-base state. |
| Terraform 0/12 and Hugo 1/3 are runner rejections for the stated reasons | High | Fail-closed mapping, dependency, and compile evidence; rejections are not inferred as test failures. |
| Measured runs could not retrieve Go dependencies | High for Go tooling routes | Proxy, private-module bypasses, checksum, and direct VCS routes were pinned off; arbitrary test sockets were not firewalled. |
| Task-owned mirrors were not modified | High for Hugo; medium-high for Terraform | Hugo has matching before/after counts; Terraform has clean separate scratch state and pre-task object mtimes but lacks a literal pre-task count. |
| Results generalize to other Go versions, operating systems, repositories, or full suites | Low / unsupported | One pinned Windows toolchain, two repositories, focal subsets only. |

Corpus inputs: Terraform `ad33caada4c91461d03cbdd8d2fdabc5ad484b16a414625fceba4191d2dca949`; Hugo `ef19ab06e7a08604525bfbe1db08a45ae0bb033e44df1b607ea01ce037f10d66`.
