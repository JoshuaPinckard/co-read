// Command focalgate runs a package-specific focal Go-test mapping five times
// with the network proxy disabled and compares normalized per-test outcomes.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

type stringList []string

func (values *stringList) String() string { return strings.Join(*values, ",") }
func (values *stringList) Set(value string) error {
	*values = append(*values, value)
	return nil
}

type mappingPackage struct {
	Package          string   `json:"package"`
	MappedNames      []string `json:"mapped_names"`
	BasePresentNames []string `json:"base_present_names"`
	BaseAbsentNames  []string `json:"base_absent_names"`
	RunRegexp        string   `json:"run_regexp"`
}

type mapping struct {
	SchemaVersion int              `json:"schema_version"`
	Base          string           `json:"base"`
	Parents       []mappingParent  `json:"parents"`
	Packages      []mappingPackage `json:"packages"`
}

type mappingParent struct {
	Parent   string           `json:"parent"`
	Packages []mappingPackage `json:"packages"`
}

type gitState struct {
	Head            string `json:"head"`
	HeadTree        string `json:"head_tree"`
	IndexTree       string `json:"index_tree"`
	PorcelainStatus string `json:"porcelain_status"`
	HeadDiffSHA256  string `json:"head_diff_sha256"`
	CleanExactBase  bool   `json:"clean_exact_base"`
}

func commandOutput(directory string, environment []string, name string, arguments ...string) ([]byte, []byte, int, error) {
	command := exec.Command(name, arguments...)
	command.Dir = directory
	command.Env = environment
	var stdout, stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	err := command.Run()
	if err == nil {
		return stdout.Bytes(), stderr.Bytes(), 0, nil
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		return stdout.Bytes(), stderr.Bytes(), exitError.ExitCode(), err
	}
	return stdout.Bytes(), stderr.Bytes(), 125, err
}

func gitOutput(repository string, arguments ...string) (string, error) {
	stdout, stderr, _, err := commandOutput(repository, os.Environ(), "git", append([]string{"-c", "core.longpaths=true"}, arguments...)...)
	if err != nil {
		return "", fmt.Errorf("git %s: %s", strings.Join(arguments, " "), strings.TrimSpace(string(stderr)))
	}
	return strings.TrimSpace(string(stdout)), nil
}

func trackedState(repository string) (gitState, error) {
	head, err := gitOutput(repository, "rev-parse", "HEAD")
	if err != nil {
		return gitState{}, err
	}
	headTree, err := gitOutput(repository, "rev-parse", "HEAD^{tree}")
	if err != nil {
		return gitState{}, err
	}
	indexTree, err := gitOutput(repository, "write-tree")
	if err != nil {
		return gitState{}, err
	}
	// Ignored and untracked Go files can affect a build just as tracked files
	// can.  A gate claiming to run the exact base therefore rejects all of
	// them rather than merely comparing the tracked diff.
	status, err := gitOutput(repository, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching")
	if err != nil {
		return gitState{}, err
	}
	diff, stderr, _, err := commandOutput(repository, os.Environ(), "git", "-c", "core.longpaths=true", "diff", "--binary", "--no-ext-diff", "HEAD", "--")
	if err != nil {
		return gitState{}, fmt.Errorf("git diff: %s", strings.TrimSpace(string(stderr)))
	}
	sum := sha256.Sum256(diff)
	state := gitState{
		Head:            head,
		HeadTree:        headTree,
		IndexTree:       indexTree,
		PorcelainStatus: status,
		HeadDiffSHA256:  hex.EncodeToString(sum[:]),
	}
	state.CleanExactBase = state.HeadTree == state.IndexTree && state.PorcelainStatus == "" && state.HeadDiffSHA256 == digestBytes(nil)
	return state, nil
}

func digestBytes(value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}

type goEvent struct {
	Action  string  `json:"Action"`
	Package string  `json:"Package"`
	Test    string  `json:"Test"`
	Elapsed float64 `json:"Elapsed"`
	Output  string  `json:"Output"`
}

type normalizedCase struct {
	Package      string `json:"package"`
	Test         string `json:"test"`
	Outcome      string `json:"outcome"`
	TFACCGuarded bool   `json:"tf_acc_guarded"`
}

func parseEvents(requestedPackage string, raw []byte) ([]normalizedCase, error) {
	outputs := make(map[string]string)
	var terminal []goEvent
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		var event goEvent
		if err := json.Unmarshal(line, &event); err != nil {
			return nil, fmt.Errorf("decode go test -json event: %w", err)
		}
		if event.Test != "" && event.Output != "" {
			outputs[event.Test] += event.Output
		}
		if event.Test != "" && (event.Action == "pass" || event.Action == "fail" || event.Action == "skip") {
			terminal = append(terminal, event)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	result := make([]normalizedCase, 0, len(terminal))
	for _, event := range terminal {
		result = append(result, normalizedCase{
			Package:      requestedPackage,
			Test:         event.Test,
			Outcome:      event.Action,
			TFACCGuarded: event.Action == "skip" && strings.Contains(strings.ToUpper(outputs[event.Test]), "TF_ACC"),
		})
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Package != result[j].Package {
			return result[i].Package < result[j].Package
		}
		if result[i].Test != result[j].Test {
			return result[i].Test < result[j].Test
		}
		return result[i].Outcome < result[j].Outcome
	})
	return result, nil
}

func environmentWith(base []string, assignments map[string]string, removals map[string]bool) []string {
	values := make(map[string]string)
	spelling := make(map[string]string)
	for _, entry := range base {
		parts := strings.SplitN(entry, "=", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.ToUpper(parts[0])
		if removals[key] {
			continue
		}
		values[key] = parts[1]
		spelling[key] = parts[0]
	}
	for key, value := range assignments {
		upper := strings.ToUpper(key)
		values[upper] = value
		spelling[upper] = key
	}
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := make([]string, 0, len(keys))
	for _, key := range keys {
		result = append(result, spelling[key]+"="+values[key])
	}
	return result
}

var reservedEnvironment = map[string]bool{
	"GO111MODULE": true,
	"GOFLAGS":     true,
	"GOINSECURE":  true,
	"GONOPROXY":   true,
	"GONOSUMDB":   true,
	"GOPRIVATE":   true,
	"GOPROXY":     true,
	"GOSUMDB":     true,
	"GOTOOLCHAIN": true,
	"GOVCS":       true,
	"TF_ACC":      true,
}

func gateEnvironment(base, extras []string, goFlags, go111Module string) ([]string, error) {
	assignments := make(map[string]string)
	seen := make(map[string]bool)
	for _, entry := range extras {
		parts := strings.SplitN(entry, "=", 2)
		if len(parts) != 2 || parts[0] == "" {
			return nil, fmt.Errorf("invalid -env value %q", entry)
		}
		upper := strings.ToUpper(parts[0])
		if reservedEnvironment[upper] {
			return nil, fmt.Errorf("-env may not override reserved gate variable %s", parts[0])
		}
		if seen[upper] {
			return nil, fmt.Errorf("duplicate -env variable %s", parts[0])
		}
		seen[upper] = true
		assignments[parts[0]] = parts[1]
	}
	// These assignments are deliberately applied after parsing extras.  In
	// addition to GOPROXY=off, the private-module bypass and direct VCS routes
	// are disabled so ambient Go configuration cannot reopen network access.
	assignments["GOFLAGS"] = goFlags
	assignments["GO111MODULE"] = go111Module
	assignments["GOPROXY"] = "off"
	assignments["GOSUMDB"] = "off"
	assignments["GOPRIVATE"] = ""
	assignments["GONOPROXY"] = "none"
	assignments["GONOSUMDB"] = "none"
	assignments["GOINSECURE"] = ""
	assignments["GOVCS"] = "*:off"
	assignments["GOTOOLCHAIN"] = "local"
	return environmentWith(base, assignments, map[string]bool{"TF_ACC": true}), nil
}

func anchoredRegexp(names []string) string {
	quoted := make([]string, len(names))
	for index, name := range names {
		quoted[index] = regexp.QuoteMeta(name)
	}
	return "^(?:" + strings.Join(quoted, "|") + ")$"
}

func sortedUniqueNonempty(names []string) bool {
	for index, name := range names {
		if name == "" || index != 0 && names[index-1] >= name {
			return false
		}
	}
	return true
}

func validatePackageMappings(packages []mappingPackage, label string) error {
	previousPackage := ""
	for index, packageMapping := range packages {
		if packageMapping.Package == "" || index != 0 && previousPackage >= packageMapping.Package {
			return fmt.Errorf("%s packages must be nonempty, unique, and sorted", label)
		}
		previousPackage = packageMapping.Package
		if !sortedUniqueNonempty(packageMapping.MappedNames) || !sortedUniqueNonempty(packageMapping.BasePresentNames) || !sortedUniqueNonempty(packageMapping.BaseAbsentNames) {
			return fmt.Errorf("mapping names for %s must be nonempty, unique, and sorted", packageMapping.Package)
		}
		mapped := make(map[string]bool, len(packageMapping.MappedNames))
		for _, name := range packageMapping.MappedNames {
			mapped[name] = true
		}
		partition := make(map[string]bool, len(packageMapping.MappedNames))
		for _, names := range [][]string{packageMapping.BasePresentNames, packageMapping.BaseAbsentNames} {
			for _, name := range names {
				if !mapped[name] || partition[name] {
					return fmt.Errorf("present/absent names for %s are not an exact partition of mapped_names", packageMapping.Package)
				}
				partition[name] = true
			}
		}
		if len(partition) != len(mapped) {
			return fmt.Errorf("present/absent names for %s are not an exact partition of mapped_names", packageMapping.Package)
		}
		expected := ""
		if len(packageMapping.BasePresentNames) != 0 {
			expected = anchoredRegexp(packageMapping.BasePresentNames)
		}
		if packageMapping.RunRegexp != expected {
			return fmt.Errorf("run_regexp for %s is not the exact anchored base-present expression", packageMapping.Package)
		}
	}
	return nil
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

type packageAccumulator struct {
	mapped  map[string]bool
	present map[string]bool
	absent  map[string]bool
}

func boolKeys(value map[string]bool) []string {
	result := make([]string, 0, len(value))
	for name := range value {
		result = append(result, name)
	}
	sort.Strings(result)
	return result
}

func validateMapping(value mapping) error {
	if value.SchemaVersion != 1 {
		return fmt.Errorf("mapping schema_version must be 1")
	}
	if len(value.Parents) != 2 {
		return fmt.Errorf("site mapping must contain exactly two parent test-side diffs")
	}
	if err := validatePackageMappings(value.Packages, "union mapping"); err != nil {
		return err
	}
	parentIDs := make(map[string]bool)
	union := make(map[string]*packageAccumulator)
	for index, parent := range value.Parents {
		if parent.Parent == "" || parentIDs[parent.Parent] {
			return fmt.Errorf("mapping parents must be nonempty and unique")
		}
		parentIDs[parent.Parent] = true
		if err := validatePackageMappings(parent.Packages, fmt.Sprintf("parent %d mapping", index+1)); err != nil {
			return err
		}
		for _, packageMapping := range parent.Packages {
			accumulator := union[packageMapping.Package]
			if accumulator == nil {
				accumulator = &packageAccumulator{
					mapped:  make(map[string]bool),
					present: make(map[string]bool),
					absent:  make(map[string]bool),
				}
				union[packageMapping.Package] = accumulator
			}
			for _, name := range packageMapping.MappedNames {
				accumulator.mapped[name] = true
			}
			for _, name := range packageMapping.BasePresentNames {
				if accumulator.absent[name] {
					return fmt.Errorf("base presence for %s:%s differs between parents", packageMapping.Package, name)
				}
				accumulator.present[name] = true
			}
			for _, name := range packageMapping.BaseAbsentNames {
				if accumulator.present[name] {
					return fmt.Errorf("base presence for %s:%s differs between parents", packageMapping.Package, name)
				}
				accumulator.absent[name] = true
			}
		}
	}
	if len(union) != len(value.Packages) {
		return fmt.Errorf("union mapping package set does not equal the union of parent mappings")
	}
	for _, packageMapping := range value.Packages {
		accumulator := union[packageMapping.Package]
		if accumulator == nil ||
			!equalStrings(packageMapping.MappedNames, boolKeys(accumulator.mapped)) ||
			!equalStrings(packageMapping.BasePresentNames, boolKeys(accumulator.present)) ||
			!equalStrings(packageMapping.BaseAbsentNames, boolKeys(accumulator.absent)) {
			return fmt.Errorf("union mapping for %s does not equal the union of parent mappings", packageMapping.Package)
		}
	}
	return nil
}

func unrunnableParents(value mapping) []string {
	var result []string
	for _, parent := range value.Parents {
		present := 0
		for _, packageMapping := range parent.Packages {
			present += len(packageMapping.BasePresentNames)
		}
		if present == 0 {
			result = append(result, parent.Parent)
		}
	}
	return result
}

type invocationResult struct {
	Package              string           `json:"package"`
	ExpectedParentNames  []string         `json:"expected_parent_names"`
	Command              []string         `json:"command"`
	ReturnCode           int              `json:"returncode"`
	TimedOut             bool             `json:"timed_out"`
	ElapsedSeconds       float64          `json:"elapsed_seconds"`
	ParseError           string           `json:"parse_error,omitempty"`
	ObservedParentNames  []string         `json:"observed_parent_names"`
	MissingParentNames   []string         `json:"missing_parent_names"`
	PassingParentCount   int              `json:"passing_parent_count"`
	TFACCSkippedParents  []string         `json:"tf_acc_skipped_parents"`
	NormalizedTestEvents []normalizedCase `json:"normalized_test_events"`
	StdoutFile           string           `json:"stdout_file"`
	StderrFile           string           `json:"stderr_file"`
	ArtifactError        string           `json:"artifact_error,omitempty"`
}

func classifyParents(expected []string, events []normalizedCase) (observed, missing, tfacc []string, passing int) {
	terminal := make(map[string]normalizedCase)
	for _, event := range events {
		// A subtest contains a slash; only the exact parent is used to prove
		// that the mapped top-level test ran to a terminal outcome.
		terminal[event.Test] = event
	}
	for _, name := range expected {
		event, ok := terminal[name]
		if !ok {
			missing = append(missing, name)
			continue
		}
		observed = append(observed, name)
		if event.Outcome == "pass" {
			passing++
		}
		if event.Outcome == "skip" && event.TFACCGuarded {
			tfacc = append(tfacc, name)
		}
	}
	return observed, missing, tfacc, passing
}

func safeLogName(packageName string) string {
	value := strings.TrimPrefix(packageName, "./")
	value = strings.ReplaceAll(value, "/", "_")
	value = strings.ReplaceAll(value, "\\", "_")
	if value == "" || value == "." {
		return "root"
	}
	return value
}

func runPackage(ctx context.Context, repository, goExecutable, outputDirectory string, runNumber int, packageMapping mappingPackage, environment []string) invocationResult {
	arguments := []string{"test", packageMapping.Package, "-run", packageMapping.RunRegexp, "-count=1", "-json"}
	commandDisplay := append([]string{goExecutable}, arguments...)
	command := exec.CommandContext(ctx, goExecutable, arguments...)
	command.Dir = repository
	command.Env = environment
	var stdout, stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	started := time.Now()
	err := command.Run()
	elapsed := time.Since(started).Seconds()
	returnCode := 0
	if err != nil {
		var exitError *exec.ExitError
		if errors.As(err, &exitError) {
			returnCode = exitError.ExitCode()
		} else {
			returnCode = 125
		}
	}
	timedOut := errors.Is(ctx.Err(), context.DeadlineExceeded)
	name := fmt.Sprintf("run-%d-%s", runNumber, safeLogName(packageMapping.Package))
	stdoutPath := filepath.Join(outputDirectory, name+".stdout.jsonl")
	stderrPath := filepath.Join(outputDirectory, name+".stderr.txt")
	var artifactErrors []string
	if writeErr := os.WriteFile(stdoutPath, stdout.Bytes(), 0644); writeErr != nil {
		artifactErrors = append(artifactErrors, "stdout: "+writeErr.Error())
	}
	if writeErr := os.WriteFile(stderrPath, stderr.Bytes(), 0644); writeErr != nil {
		artifactErrors = append(artifactErrors, "stderr: "+writeErr.Error())
	}
	events, parseErr := parseEvents(packageMapping.Package, stdout.Bytes())
	observed, missing, tfacc, passing := classifyParents(packageMapping.BasePresentNames, events)
	result := invocationResult{
		Package:              packageMapping.Package,
		ExpectedParentNames:  packageMapping.BasePresentNames,
		Command:              commandDisplay,
		ReturnCode:           returnCode,
		TimedOut:             timedOut,
		ElapsedSeconds:       elapsed,
		ObservedParentNames:  observed,
		MissingParentNames:   missing,
		PassingParentCount:   passing,
		TFACCSkippedParents:  tfacc,
		NormalizedTestEvents: events,
		StdoutFile:           stdoutPath,
		StderrFile:           stderrPath,
		ArtifactError:        strings.Join(artifactErrors, "; "),
	}
	if parseErr != nil {
		result.ParseError = parseErr.Error()
	}
	return result
}

type runResult struct {
	Run                  int                `json:"run"`
	StartedAtUTC         string             `json:"started_at_utc"`
	CompletedAtUTC       string             `json:"completed_at_utc"`
	Before               gitState           `json:"before"`
	After                gitState           `json:"after"`
	StateMatchesBaseline bool               `json:"state_matches_baseline"`
	Invocations          []invocationResult `json:"invocations"`
	PassingParentCount   int                `json:"passing_parent_count"`
	NormalizedSHA256     string             `json:"normalized_sha256"`
}

func canonicalSignature(invocations []invocationResult) (string, error) {
	var cases []normalizedCase
	for _, invocation := range invocations {
		cases = append(cases, invocation.NormalizedTestEvents...)
	}
	sort.Slice(cases, func(i, j int) bool {
		if cases[i].Package != cases[j].Package {
			return cases[i].Package < cases[j].Package
		}
		if cases[i].Test != cases[j].Test {
			return cases[i].Test < cases[j].Test
		}
		if cases[i].Outcome != cases[j].Outcome {
			return cases[i].Outcome < cases[j].Outcome
		}
		return !cases[i].TFACCGuarded && cases[j].TFACCGuarded
	})
	encoded, err := json.Marshal(cases)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:]), nil
}

func stateEqual(left, right gitState) bool { return left == right }

type gateResult struct {
	SchemaVersion              int                    `json:"schema_version"`
	Measurement                string                 `json:"measurement"`
	Status                     string                 `json:"status"`
	Reason                     string                 `json:"reason"`
	Repository                 string                 `json:"repository"`
	Base                       string                 `json:"base"`
	MappingFile                string                 `json:"mapping_file"`
	Baseline                   gitState               `json:"baseline"`
	Protocol                   map[string]interface{} `json:"protocol"`
	UnrunnableParents          []string               `json:"unrunnable_parents"`
	IdenticalNormalizedResults bool                   `json:"identical_normalized_results"`
	NormalizedSignatures       []string               `json:"normalized_signatures"`
	Runs                       []runResult            `json:"runs"`
	CompletedAtUTC             string                 `json:"completed_at_utc"`
}

func utcNow() string { return time.Now().UTC().Format("2006-01-02T15:04:05.000Z") }

func atomicJSON(filename string, value interface{}) error {
	encoded, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	encoded = append(encoded, '\n')
	temporary := filename + ".tmp"
	if err := os.WriteFile(temporary, encoded, 0644); err != nil {
		return err
	}
	return os.Rename(temporary, filename)
}

func main() {
	flags := flag.NewFlagSet("focalgate", flag.ExitOnError)
	repositoryArgument := flags.String("repo", "", "scratch worktree at the exact base")
	mappingArgument := flags.String("mapping", "", "focalmap JSON")
	goArgument := flags.String("go", "", "pinned go executable")
	outputArgument := flags.String("out", "", "gate JSON output")
	timeoutArgument := flags.Duration("timeout", 10*time.Minute, "timeout per package invocation")
	goFlagsArgument := flags.String("goflags", "-vet=off", "exact GOFLAGS value")
	go111Argument := flags.String("go111module", "auto", "exact GO111MODULE value")
	var extraEnvironment stringList
	flags.Var(&extraEnvironment, "env", "additional KEY=VALUE environment (repeatable)")
	flags.Parse(os.Args[1:])
	if *repositoryArgument == "" || *mappingArgument == "" || *goArgument == "" || *outputArgument == "" {
		fmt.Fprintln(os.Stderr, "-repo, -mapping, -go, and -out are required")
		os.Exit(2)
	}

	repository, err := filepath.Abs(*repositoryArgument)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	mappingPath, err := filepath.Abs(*mappingArgument)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	goExecutable, err := filepath.Abs(*goArgument)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	outputPath, err := filepath.Abs(*outputArgument)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if err := os.MkdirAll(filepath.Dir(outputPath), 0755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	artifactDirectory := outputPath + ".artifacts"
	if err := os.MkdirAll(artifactDirectory, 0755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	mappingBytes, err := os.ReadFile(mappingPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	var focalMapping mapping
	if err := json.Unmarshal(mappingBytes, &focalMapping); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if err := validateMapping(focalMapping); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	baseline, err := trackedState(repository)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if baseline.Head != focalMapping.Base {
		fmt.Fprintf(os.Stderr, "scratch HEAD %s does not match mapping base %s\n", baseline.Head, focalMapping.Base)
		os.Exit(2)
	}
	if !baseline.CleanExactBase {
		fmt.Fprintln(os.Stderr, "scratch repository is not a clean exact-base checkout (tracked, untracked, and ignored files are all gated)")
		os.Exit(2)
	}
	environment, err := gateEnvironment(os.Environ(), extraEnvironment, *goFlagsArgument, *go111Argument)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}

	activePackages := make([]mappingPackage, 0)
	for _, packageMapping := range focalMapping.Packages {
		if len(packageMapping.BasePresentNames) == 0 || packageMapping.RunRegexp == "" {
			continue
		}
		activePackages = append(activePackages, packageMapping)
	}
	sort.Slice(activePackages, func(i, j int) bool { return activePackages[i].Package < activePackages[j].Package })

	result := gateResult{
		SchemaVersion:     1,
		Measurement:       "go-focal-base-determinism-gate",
		Status:            "rejected",
		Repository:        repository,
		Base:              focalMapping.Base,
		MappingFile:       mappingPath,
		Baseline:          baseline,
		UnrunnableParents: unrunnableParents(focalMapping),
		Protocol: map[string]interface{}{
			"required_runs":               5,
			"timeout_per_package":         timeoutArgument.String(),
			"determinism_signature":       "sorted (requested package, full Test/subtest name, pass/fail/skip, TF_ACC guard); timing/output/order ignored",
			"go_executable":               goExecutable,
			"GOFLAGS":                     *goFlagsArgument,
			"GO111MODULE":                 *go111Argument,
			"GOPROXY":                     "off",
			"GOSUMDB":                     "off",
			"GOPRIVATE":                   "",
			"GONOPROXY":                   "none",
			"GONOSUMDB":                   "none",
			"GOINSECURE":                  "",
			"GOVCS":                       "*:off",
			"TF_ACC":                      "unset",
			"extra_environment":           []string(extraEnvironment),
			"package_specific_invocation": true,
		},
	}
	if len(result.UnrunnableParents) != 0 {
		result.Reason = "focal mapping rejected: each parent test-side diff must map at least one TestXxx present at the base; unrunnable parent(s): " + strings.Join(result.UnrunnableParents, ", ")
		result.CompletedAtUTC = utcNow()
		_ = atomicJSON(outputPath, result)
		fmt.Fprintln(os.Stderr, result.Reason)
		os.Exit(1)
	}
	if len(activePackages) == 0 {
		result.Reason = "no mapped TestXxx name is present at the base"
		result.CompletedAtUTC = utcNow()
		_ = atomicJSON(outputPath, result)
		fmt.Fprintln(os.Stderr, result.Reason)
		os.Exit(1)
	}

	allRunsValid := true
	allExpectedTFACC := true
	totalExpectedParents := 0
	for runNumber := 1; runNumber <= 5; runNumber++ {
		before, stateErr := trackedState(repository)
		if stateErr != nil {
			fmt.Fprintln(os.Stderr, stateErr)
			os.Exit(1)
		}
		runOutput := runResult{Run: runNumber, StartedAtUTC: utcNow(), Before: before}
		for _, packageMapping := range activePackages {
			ctx, cancel := context.WithTimeout(context.Background(), *timeoutArgument)
			invocation := runPackage(ctx, repository, goExecutable, artifactDirectory, runNumber, packageMapping, environment)
			cancel()
			runOutput.Invocations = append(runOutput.Invocations, invocation)
			runOutput.PassingParentCount += invocation.PassingParentCount
			if invocation.ReturnCode != 0 || invocation.TimedOut || invocation.ParseError != "" || invocation.ArtifactError != "" || len(invocation.MissingParentNames) != 0 {
				allRunsValid = false
			}
			totalExpectedParents += len(invocation.ExpectedParentNames)
			if len(invocation.TFACCSkippedParents) != len(invocation.ExpectedParentNames) {
				allExpectedTFACC = false
			}
		}
		after, stateErr := trackedState(repository)
		if stateErr != nil {
			fmt.Fprintln(os.Stderr, stateErr)
			os.Exit(1)
		}
		runOutput.After = after
		runOutput.StateMatchesBaseline = stateEqual(before, baseline) && stateEqual(after, baseline)
		if !runOutput.StateMatchesBaseline || runOutput.PassingParentCount == 0 {
			allRunsValid = false
		}
		runOutput.NormalizedSHA256, err = canonicalSignature(runOutput.Invocations)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		runOutput.CompletedAtUTC = utcNow()
		result.Runs = append(result.Runs, runOutput)
		result.NormalizedSignatures = append(result.NormalizedSignatures, runOutput.NormalizedSHA256)
		_ = atomicJSON(outputPath+".partial", result)
	}

	result.IdenticalNormalizedResults = len(result.NormalizedSignatures) == 5
	for _, signature := range result.NormalizedSignatures[1:] {
		if signature != result.NormalizedSignatures[0] {
			result.IdenticalNormalizedResults = false
		}
	}
	if allRunsValid && result.IdenticalNormalizedResults {
		result.Status = "eligible"
		result.Reason = "five green runs had identical non-empty normalized focal outcomes and unchanged tracked state"
	} else if totalExpectedParents != 0 && allExpectedTFACC {
		result.Reason = "all mapped base-present focal tests were skipped by TF_ACC guards in every run"
	} else if !allRunsValid {
		result.Reason = "one or more runs failed, timed out, missed an expected parent, had no passing focal parent, or changed tracked state"
	} else {
		result.Reason = "normalized focal outcomes varied across the five runs"
	}
	result.CompletedAtUTC = utcNow()
	if err := atomicJSON(outputPath, result); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	_ = os.Remove(outputPath + ".partial")
	fmt.Printf("%s: %s\n", result.Status, result.Reason)
	if result.Status != "eligible" {
		os.Exit(1)
	}
}
