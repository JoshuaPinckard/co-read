// Package focalmap maps zero-context Go test diffs to enclosing top-level
// TestXxx declarations. It intentionally does not guess which tests use a
// changed package-level helper.
package focalmap

import (
	"bufio"
	"bytes"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

var hunkHeader = regexp.MustCompile(`^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@`)

// Range is an inclusive one-based line interval. A zero Count denotes the
// empty side of an insertion or deletion and never overlaps a function.
type Range struct {
	Start int `json:"start"`
	Count int `json:"count"`
}

// Hunk is the old/new line range from one unified diff hunk.
type Hunk struct {
	Old Range `json:"old"`
	New Range `json:"new"`
}

// FileDiff describes one test file in a patch. /dev/null is represented by an
// empty path.
type FileDiff struct {
	OldPath string `json:"old_path,omitempty"`
	NewPath string `json:"new_path,omitempty"`
	Hunks   []Hunk `json:"hunks"`
}

// Function is a top-level TestXxx declaration and its full AST line span.
type Function struct {
	Name      string `json:"name"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
}

func unquoteDiffPath(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "/dev/null" {
		return "", nil
	}
	if strings.HasPrefix(value, `"`) {
		unquoted, err := strconv.Unquote(value)
		if err != nil {
			return "", err
		}
		value = unquoted
	}
	if strings.HasPrefix(value, "a/") || strings.HasPrefix(value, "b/") {
		value = value[2:]
	}
	return value, nil
}

func parseRange(startText, countText string) (Range, error) {
	start, err := strconv.Atoi(startText)
	if err != nil {
		return Range{}, err
	}
	count := 1
	if countText != "" {
		count, err = strconv.Atoi(countText)
		if err != nil {
			return Range{}, err
		}
	}
	return Range{Start: start, Count: count}, nil
}

// ParseUnifiedZero parses the path headers and hunk ranges emitted by
// `git diff --unified=0`. Non-test files are discarded.
func ParseUnifiedZero(diff []byte) ([]FileDiff, error) {
	var files []FileDiff
	var current *FileDiff
	scanner := bufio.NewScanner(bytes.NewReader(diff))
	// Large generated lines are not focal, but a large source line must not
	// silently truncate parsing.
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case strings.HasPrefix(line, "diff --git "):
			if current != nil && (strings.HasSuffix(current.OldPath, "_test.go") || strings.HasSuffix(current.NewPath, "_test.go")) {
				files = append(files, *current)
			}
			current = &FileDiff{}
		case current != nil && strings.HasPrefix(line, "--- "):
			parsed, err := unquoteDiffPath(strings.TrimPrefix(line, "--- "))
			if err != nil {
				return nil, fmt.Errorf("old path %q: %w", line, err)
			}
			current.OldPath = parsed
		case current != nil && strings.HasPrefix(line, "+++ "):
			parsed, err := unquoteDiffPath(strings.TrimPrefix(line, "+++ "))
			if err != nil {
				return nil, fmt.Errorf("new path %q: %w", line, err)
			}
			current.NewPath = parsed
		case current != nil && strings.HasPrefix(line, "@@ "):
			match := hunkHeader.FindStringSubmatch(line)
			if match == nil {
				return nil, fmt.Errorf("unrecognized hunk header %q", line)
			}
			oldRange, err := parseRange(match[1], match[2])
			if err != nil {
				return nil, err
			}
			newRange, err := parseRange(match[3], match[4])
			if err != nil {
				return nil, err
			}
			current.Hunks = append(current.Hunks, Hunk{Old: oldRange, New: newRange})
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if current != nil && (strings.HasSuffix(current.OldPath, "_test.go") || strings.HasSuffix(current.NewPath, "_test.go")) {
		files = append(files, *current)
	}
	return files, nil
}

func isTestName(name string) bool {
	if name == "TestMain" || !strings.HasPrefix(name, "Test") || len(name) == len("Test") {
		return false
	}
	r, _ := utf8.DecodeRuneInString(name[len("Test"):])
	return !unicode.IsLower(r)
}

// Functions parses source and returns top-level TestXxx functions. Methods,
// TestMain, benchmarks, fuzz targets, examples, helpers, and FuncLit closures
// are outside this mapping rule.
func Functions(filename string, source []byte) ([]Function, error) {
	if len(source) == 0 {
		return nil, nil
	}
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, filename, source, parser.ParseComments)
	if err != nil {
		return nil, err
	}
	var functions []Function
	for _, declaration := range file.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if !ok || function.Recv != nil || function.Body == nil || !isTestName(function.Name.Name) {
			continue
		}
		functions = append(functions, Function{
			Name:      function.Name.Name,
			StartLine: fset.PositionFor(function.Pos(), false).Line,
			EndLine:   fset.PositionFor(function.End(), false).Line,
		})
	}
	return functions, nil
}

func overlaps(function Function, changed Range) bool {
	if changed.Count == 0 {
		return false
	}
	last := changed.Start + changed.Count - 1
	return changed.Start <= function.EndLine && last >= function.StartLine
}

// NamesForRange returns the sorted TestXxx names overlapping changed.
func NamesForRange(functions []Function, changed Range) []string {
	set := make(map[string]struct{})
	for _, function := range functions {
		if overlaps(function, changed) {
			set[function.Name] = struct{}{}
		}
	}
	return sortedKeys(set)
}

func sortedKeys(set map[string]struct{}) []string {
	values := make([]string, 0, len(set))
	for value := range set {
		values = append(values, value)
	}
	sort.Strings(values)
	return values
}

// PackageForFile returns the go-test package argument for a repository path.
func PackageForFile(filename string) string {
	directory := path.Dir(filename)
	if directory == "." || directory == "" {
		return "."
	}
	return "./" + directory
}

// AnchoredRegexp builds the package-specific -run expression.
func AnchoredRegexp(names []string) string {
	copyOfNames := append([]string(nil), names...)
	sort.Strings(copyOfNames)
	quoted := make([]string, 0, len(copyOfNames))
	for _, name := range copyOfNames {
		quoted = append(quoted, regexp.QuoteMeta(name))
	}
	return "^(?:" + strings.Join(quoted, "|") + ")$"
}
