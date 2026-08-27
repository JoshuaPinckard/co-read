// Command focalmap maps each base-to-parent test diff to package-specific,
// anchored Go -run expressions without executing repository code.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"blast-radius/instruments/arms/go/internal/focalmap"
)

type stringList []string

func (values *stringList) String() string { return strings.Join(*values, ",") }
func (values *stringList) Set(value string) error {
	*values = append(*values, value)
	return nil
}

type gitRepository struct {
	location    string
	bare        bool
	batch       *exec.Cmd
	batchInput  io.WriteCloser
	batchOutput *bufio.Reader
	batchStderr bytes.Buffer
}

func openGitRepository(location string) (*gitRepository, error) {
	absolute, err := filepath.Abs(location)
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(absolute)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("repository is not a directory: %s", absolute)
	}
	_, dotGitErr := os.Stat(filepath.Join(absolute, ".git"))
	return &gitRepository{location: absolute, bare: errors.Is(dotGitErr, os.ErrNotExist)}, nil
}

func (repository *gitRepository) command(arguments ...string) *exec.Cmd {
	var prefix []string
	if repository.bare {
		prefix = []string{"--git-dir=" + repository.location}
	} else {
		prefix = []string{"-C", repository.location}
	}
	command := exec.Command("git", append(prefix, arguments...)...)
	command.Env = append(os.Environ(), "GIT_NO_LAZY_FETCH=1", "GIT_TERMINAL_PROMPT=0")
	return command
}

func (repository *gitRepository) output(arguments ...string) ([]byte, error) {
	command := repository.command(arguments...)
	output, err := command.Output()
	if err == nil {
		return output, nil
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		return nil, fmt.Errorf("git %s: %s", strings.Join(arguments, " "), strings.TrimSpace(string(exitError.Stderr)))
	}
	return nil, err
}

func (repository *gitRepository) objectName(revision string) (string, error) {
	output, err := repository.output("rev-parse", "--verify", revision+"^{commit}")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(output)), nil
}

func (repository *gitRepository) show(revision, filename string) ([]byte, error) {
	if filename == "" {
		return nil, nil
	}
	if repository.batch == nil {
		command := repository.command("cat-file", "--batch")
		input, err := command.StdinPipe()
		if err != nil {
			return nil, err
		}
		output, err := command.StdoutPipe()
		if err != nil {
			return nil, err
		}
		command.Stderr = &repository.batchStderr
		if err := command.Start(); err != nil {
			return nil, err
		}
		repository.batch = command
		repository.batchInput = input
		repository.batchOutput = bufio.NewReaderSize(output, 128*1024)
	}
	specification := revision + ":" + filename
	if _, err := fmt.Fprintln(repository.batchInput, specification); err != nil {
		return nil, err
	}
	header, err := repository.batchOutput.ReadString('\n')
	if err != nil {
		return nil, fmt.Errorf("git cat-file header for %s: %w: %s", specification, err, strings.TrimSpace(repository.batchStderr.String()))
	}
	fields := strings.Fields(header)
	if len(fields) == 2 && fields[1] == "missing" {
		return nil, fmt.Errorf("git object is missing for %s", specification)
	}
	if len(fields) != 3 || fields[1] != "blob" {
		return nil, fmt.Errorf("unexpected git cat-file header for %s: %q", specification, strings.TrimSpace(header))
	}
	size, err := strconv.ParseInt(fields[2], 10, 64)
	if err != nil || size < 0 {
		return nil, fmt.Errorf("invalid git blob size for %s: %q", specification, fields[2])
	}
	contents := make([]byte, int(size))
	if _, err := io.ReadFull(repository.batchOutput, contents); err != nil {
		return nil, fmt.Errorf("read git blob for %s: %w", specification, err)
	}
	separator, err := repository.batchOutput.ReadByte()
	if err != nil || separator != '\n' {
		return nil, fmt.Errorf("missing git cat-file separator for %s", specification)
	}
	return contents, nil
}

func (repository *gitRepository) close() error {
	if repository.batch == nil {
		return nil
	}
	_ = repository.batchInput.Close()
	err := repository.batch.Wait()
	if err != nil {
		return fmt.Errorf("git cat-file --batch: %w: %s", err, strings.TrimSpace(repository.batchStderr.String()))
	}
	return nil
}

func (repository *gitRepository) testDiff(base, parent string) ([]focalmap.FileDiff, error) {
	output, err := repository.output(
		"diff", "--no-ext-diff", "--no-color", "--unified=0", "--find-renames",
		base, parent, "--", ":(glob)*_test.go", ":(glob)**/*_test.go",
	)
	if err != nil {
		return nil, err
	}
	return focalmap.ParseUnifiedZero(output)
}

func (repository *gitRepository) baseTestFiles(base string) (map[string][]string, error) {
	output, err := repository.output(
		"ls-tree", "-r", "--name-only", "-z", base,
	)
	if err != nil {
		return nil, err
	}
	grouped := make(map[string][]string)
	for _, raw := range strings.Split(string(output), "\x00") {
		if raw == "" {
			continue
		}
		if !strings.HasSuffix(raw, "_test.go") {
			continue
		}
		packageName := focalmap.PackageForFile(raw)
		grouped[packageName] = append(grouped[packageName], raw)
	}
	for packageName := range grouped {
		sort.Strings(grouped[packageName])
	}
	return grouped, nil
}

type hunkResult struct {
	Old                 focalmap.Range `json:"old"`
	New                 focalmap.Range `json:"new"`
	OldMappedTestNames  []string       `json:"old_mapped_test_names"`
	NewMappedTestNames  []string       `json:"new_mapped_test_names"`
	MappedNamedTestBody bool           `json:"mapped_named_test_body"`
}

type fileResult struct {
	OldPath string       `json:"old_path,omitempty"`
	NewPath string       `json:"new_path,omitempty"`
	Hunks   []hunkResult `json:"hunks"`
}

type packageResult struct {
	Package          string   `json:"package"`
	MappedNames      []string `json:"mapped_names"`
	BasePresentNames []string `json:"base_present_names"`
	BaseAbsentNames  []string `json:"base_absent_names"`
	RunRegexp        string   `json:"run_regexp"`
}

type parentResult struct {
	Parent        string          `json:"parent"`
	Files         []fileResult    `json:"files"`
	Packages      []packageResult `json:"packages"`
	MappedHunks   int             `json:"mapped_hunks"`
	UnmappedHunks int             `json:"unmapped_hunks"`
}

type mappingResult struct {
	SchemaVersion int             `json:"schema_version"`
	Method        string          `json:"method"`
	Repository    string          `json:"repository"`
	Base          string          `json:"base"`
	Parents       []parentResult  `json:"parents"`
	Packages      []packageResult `json:"packages"`
}

func setToSorted(set map[string]struct{}) []string {
	values := make([]string, 0, len(set))
	for value := range set {
		values = append(values, value)
	}
	sort.Strings(values)
	return values
}

func addNames(target map[string]map[string]struct{}, packageName string, names []string) {
	if len(names) == 0 {
		return
	}
	if target[packageName] == nil {
		target[packageName] = make(map[string]struct{})
	}
	for _, name := range names {
		target[packageName][name] = struct{}{}
	}
}

type inventory struct {
	repository *gitRepository
	base       string
	files      map[string][]string
	packages   map[string]map[string]struct{}
}

func (value *inventory) names(packageName string) (map[string]struct{}, error) {
	if names, ok := value.packages[packageName]; ok {
		return names, nil
	}
	names := make(map[string]struct{})
	for _, filename := range value.files[packageName] {
		source, err := value.repository.show(value.base, filename)
		if err != nil {
			return nil, err
		}
		functions, err := focalmap.Functions(filename, source)
		if err != nil {
			return nil, fmt.Errorf("parse %s at base: %w", filename, err)
		}
		for _, function := range functions {
			names[function.Name] = struct{}{}
		}
	}
	value.packages[packageName] = names
	return names, nil
}

func packageResults(mapped map[string]map[string]struct{}, baseInventory *inventory) ([]packageResult, error) {
	packageNames := make([]string, 0, len(mapped))
	for packageName := range mapped {
		packageNames = append(packageNames, packageName)
	}
	sort.Strings(packageNames)
	results := make([]packageResult, 0, len(packageNames))
	for _, packageName := range packageNames {
		mappedNames := setToSorted(mapped[packageName])
		baseNames, err := baseInventory.names(packageName)
		if err != nil {
			return nil, err
		}
		var present, absent []string
		for _, name := range mappedNames {
			if _, ok := baseNames[name]; ok {
				present = append(present, name)
			} else {
				absent = append(absent, name)
			}
		}
		regexp := ""
		if len(present) != 0 {
			regexp = focalmap.AnchoredRegexp(present)
		}
		results = append(results, packageResult{
			Package:          packageName,
			MappedNames:      mappedNames,
			BasePresentNames: present,
			BaseAbsentNames:  absent,
			RunRegexp:        regexp,
		})
	}
	return results, nil
}

func mapParent(repository *gitRepository, base, parent string, baseInventory *inventory) (parentResult, map[string]map[string]struct{}, error) {
	diffs, err := repository.testDiff(base, parent)
	if err != nil {
		return parentResult{}, nil, err
	}
	mapped := make(map[string]map[string]struct{})
	result := parentResult{Parent: parent}
	for _, fileDiff := range diffs {
		var oldFunctions, newFunctions []focalmap.Function
		if fileDiff.OldPath != "" {
			source, showErr := repository.show(base, fileDiff.OldPath)
			if showErr != nil {
				return parentResult{}, nil, showErr
			}
			oldFunctions, err = focalmap.Functions(fileDiff.OldPath, source)
			if err != nil {
				return parentResult{}, nil, fmt.Errorf("parse old %s: %w", fileDiff.OldPath, err)
			}
		}
		if fileDiff.NewPath != "" {
			source, showErr := repository.show(parent, fileDiff.NewPath)
			if showErr != nil {
				return parentResult{}, nil, showErr
			}
			newFunctions, err = focalmap.Functions(fileDiff.NewPath, source)
			if err != nil {
				return parentResult{}, nil, fmt.Errorf("parse new %s: %w", fileDiff.NewPath, err)
			}
		}

		fileOutput := fileResult{OldPath: fileDiff.OldPath, NewPath: fileDiff.NewPath}
		for _, hunk := range fileDiff.Hunks {
			oldNames := focalmap.NamesForRange(oldFunctions, hunk.Old)
			newNames := focalmap.NamesForRange(newFunctions, hunk.New)
			mappedBody := len(oldNames) != 0 || len(newNames) != 0
			if mappedBody {
				result.MappedHunks++
			} else {
				result.UnmappedHunks++
			}
			if fileDiff.OldPath != "" {
				addNames(mapped, focalmap.PackageForFile(fileDiff.OldPath), oldNames)
			}
			if fileDiff.NewPath != "" {
				addNames(mapped, focalmap.PackageForFile(fileDiff.NewPath), newNames)
			}
			fileOutput.Hunks = append(fileOutput.Hunks, hunkResult{
				Old:                 hunk.Old,
				New:                 hunk.New,
				OldMappedTestNames:  oldNames,
				NewMappedTestNames:  newNames,
				MappedNamedTestBody: mappedBody,
			})
		}
		result.Files = append(result.Files, fileOutput)
	}
	result.Packages, err = packageResults(mapped, baseInventory)
	return result, mapped, err
}

func mergePackageSets(target, source map[string]map[string]struct{}) {
	for packageName, names := range source {
		addNames(target, packageName, setToSorted(names))
	}
}

func main() {
	flags := flag.NewFlagSet("focalmap", flag.ExitOnError)
	repositoryPath := flags.String("repo", "", "bare mirror or worktree path")
	baseArgument := flags.String("base", "", "base commit")
	outputPath := flags.String("out", "", "write JSON to this path instead of stdout")
	var parentArguments stringList
	flags.Var(&parentArguments, "parent", "parent commit (repeat exactly twice for a site)")
	flags.Parse(os.Args[1:])
	if *repositoryPath == "" || *baseArgument == "" || len(parentArguments) == 0 {
		fmt.Fprintln(os.Stderr, "-repo, -base, and at least one -parent are required")
		os.Exit(2)
	}

	repository, err := openGitRepository(*repositoryPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	defer func() {
		if closeErr := repository.close(); closeErr != nil {
			fmt.Fprintln(os.Stderr, closeErr)
		}
	}()
	base, err := repository.objectName(*baseArgument)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	baseFiles, err := repository.baseTestFiles(base)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	baseInventory := &inventory{repository: repository, base: base, files: baseFiles, packages: make(map[string]map[string]struct{})}
	result := mappingResult{
		SchemaVersion: 1,
		Method:        "zero-context B-to-parent hunks intersecting top-level TestXxx AST spans; helper/import hunks unmapped",
		Repository:    repository.location,
		Base:          base,
	}
	union := make(map[string]map[string]struct{})
	for _, parentArgument := range parentArguments {
		parent, resolveErr := repository.objectName(parentArgument)
		if resolveErr != nil {
			fmt.Fprintln(os.Stderr, resolveErr)
			os.Exit(2)
		}
		parentOutput, mapped, mapErr := mapParent(repository, base, parent, baseInventory)
		if mapErr != nil {
			fmt.Fprintln(os.Stderr, mapErr)
			os.Exit(1)
		}
		result.Parents = append(result.Parents, parentOutput)
		mergePackageSets(union, mapped)
	}
	result.Packages, err = packageResults(union, baseInventory)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	encoded, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	encoded = append(encoded, '\n')
	if *outputPath == "" {
		_, _ = os.Stdout.Write(encoded)
		return
	}
	absoluteOutput, err := filepath.Abs(*outputPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if err := os.MkdirAll(filepath.Dir(absoluteOutput), 0755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	temporary := absoluteOutput + ".tmp"
	if err := os.WriteFile(temporary, encoded, 0644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := os.Rename(temporary, absoluteOutput); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
