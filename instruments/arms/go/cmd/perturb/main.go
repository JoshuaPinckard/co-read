// Command perturb temporarily inserts panic("perturbed") at the start of
// every named function or method body in one Go source file.
//
// The original bytes, rather than printer output, are always used for restore.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
)

const insertedStatement = "\npanic(\"perturbed\")\n"

type report struct {
	File             string `json:"file"`
	Mode             string `json:"mode"`
	FunctionsChanged int    `json:"functions_changed"`
	OriginalSHA256   string `json:"original_sha256"`
	PerturbedSHA256  string `json:"perturbed_sha256"`
	RestoredSHA256   string `json:"restored_sha256"`
	RestoredExact    bool   `json:"restored_exact"`
	CommandExitCode  *int   `json:"command_exit_code,omitempty"`
}

type exclusionError struct {
	reason string
}

func (e *exclusionError) Error() string { return e.reason }

func digest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func exclusionReason(path string, source []byte) string {
	base := strings.ToLower(filepath.Base(path))
	if !strings.HasSuffix(base, ".go") {
		return "target does not have a .go suffix"
	}
	if strings.HasSuffix(base, ".pb.go") {
		return "generated protobuf file (*.pb.go)"
	}
	if strings.HasPrefix(base, "zz_generated") {
		return "generated file (zz_generated* basename)"
	}

	// Build constraints and the standard generated-code marker must occur in
	// the leading comment block to trigger exclusion.
	lines := bytes.Split(source, []byte("\n"))
	for _, raw := range lines {
		line := strings.TrimSpace(strings.TrimSuffix(string(raw), "\r"))
		if line == "" {
			continue
		}
		if !strings.HasPrefix(line, "//") {
			break
		}
		if line == "//go:build ignore" || strings.Contains(line, "//go:build") && buildExpressionHasIgnore(line) {
			return "excluded build constraint (//go:build ... ignore ...)"
		}
		if strings.HasPrefix(line, "// +build") && buildExpressionHasIgnore(line) {
			return "excluded legacy build constraint (// +build ... ignore ...)"
		}
		if strings.HasPrefix(line, "// Code generated ") && strings.HasSuffix(line, " DO NOT EDIT.") {
			return "standard generated-code header"
		}
	}
	return ""
}

func buildExpressionHasIgnore(line string) bool {
	for _, field := range strings.FieldsFunc(line, func(r rune) bool {
		switch r {
		case ' ', '\t', '!', '&', '|', '(', ')', ',':
			return true
		default:
			return false
		}
	}) {
		if field == "ignore" {
			return true
		}
	}
	return false
}

func perturbSource(path string, source []byte) ([]byte, int, error) {
	if reason := exclusionReason(path, source); reason != "" {
		return nil, 0, &exclusionError{reason: reason}
	}

	fset := token.NewFileSet()
	parsed, err := parser.ParseFile(fset, path, source, parser.ParseComments)
	if err != nil {
		return nil, 0, fmt.Errorf("parse %s: %w", path, err)
	}

	offsets := make([]int, 0)
	for _, declaration := range parsed.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if !ok || function.Body == nil {
			continue
		}
		offset := fset.PositionFor(function.Body.Lbrace, false).Offset + 1
		if offset < 1 || offset > len(source) {
			return nil, 0, fmt.Errorf("invalid function-body offset %d in %s", offset, path)
		}
		offsets = append(offsets, offset)
	}
	if len(offsets) == 0 {
		return nil, 0, errors.New("no named function or method bodies")
	}

	// Descending insertion keeps the parser's original byte offsets valid.
	sort.Sort(sort.Reverse(sort.IntSlice(offsets)))
	perturbed := append([]byte(nil), source...)
	for _, offset := range offsets {
		updated := make([]byte, 0, len(perturbed)+len(insertedStatement))
		updated = append(updated, perturbed[:offset]...)
		updated = append(updated, insertedStatement...)
		updated = append(updated, perturbed[offset:]...)
		perturbed = updated
	}
	return perturbed, len(offsets), nil
}

func writeExact(path string, data []byte, mode os.FileMode) error {
	if err := os.WriteFile(path, data, mode.Perm()); err != nil {
		return err
	}
	actual, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if !bytes.Equal(actual, data) {
		return fmt.Errorf("write verification failed for %s", path)
	}
	return nil
}

func commandExitCode(err error) int {
	if err == nil {
		return 0
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		return exitError.ExitCode()
	}
	return 125
}

func runCommand(arguments []string, interrupts <-chan os.Signal) int {
	command := exec.Command(arguments[0], arguments[1:]...)
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	if err := command.Start(); err != nil {
		return commandExitCode(err)
	}
	done := make(chan error, 1)
	go func() { done <- command.Wait() }()
	select {
	case err := <-done:
		return commandExitCode(err)
	case <-interrupts:
		// Keep the wrapper alive long enough to execute its deferred restore.
		// The child is killed because forwarding Interrupt is not implemented
		// consistently by os.Process on Windows.
		_ = command.Process.Kill()
		<-done
		return 130
	}
}

func interruptPending(interrupts <-chan os.Signal) bool {
	select {
	case <-interrupts:
		return true
	default:
		return false
	}
}

func runWithInterrupts(interrupts <-chan os.Signal) (result report, exitCode int, returnedErr error) {
	flags := flag.NewFlagSet("perturb", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	target := flags.String("file", "", "Go source file to perturb")
	roundTrip := flags.Bool("roundtrip", false, "perturb and immediately restore without running a command")
	if err := flags.Parse(os.Args[1:]); err != nil {
		return result, 2, err
	}
	if *target == "" {
		return result, 2, errors.New("-file is required")
	}
	commandArguments := flags.Args()
	if *roundTrip && len(commandArguments) != 0 {
		return result, 2, errors.New("-roundtrip does not accept a command")
	}
	if !*roundTrip && len(commandArguments) == 0 {
		return result, 2, errors.New("supply -roundtrip or a command after --")
	}

	absTarget, err := filepath.Abs(*target)
	if err != nil {
		return result, 2, err
	}
	original, err := os.ReadFile(absTarget)
	if err != nil {
		return result, 2, err
	}
	info, err := os.Stat(absTarget)
	if err != nil {
		return result, 2, err
	}
	perturbed, count, err := perturbSource(absTarget, original)
	if err != nil {
		return result, 2, err
	}

	result = report{
		File:             absTarget,
		Mode:             "command",
		FunctionsChanged: count,
		OriginalSHA256:   digest(original),
		PerturbedSHA256:  digest(perturbed),
	}
	if *roundTrip {
		result.Mode = "roundtrip"
	}
	if result.OriginalSHA256 == result.PerturbedSHA256 {
		return result, 2, errors.New("perturbation did not change the file hash")
	}
	if interruptPending(interrupts) {
		return result, 130, errors.New("interrupted before perturbation")
	}

	// Install restoration before the first mutation.  This also covers a
	// partial/truncated initial write or a failure while verifying that write.
	// Like any process-level cleanup, it cannot run after an uncatchable process
	// kill or power loss.
	defer func() {
		restoreErr := writeExact(absTarget, original, info.Mode())
		if restoreErr == nil {
			var restored []byte
			restored, restoreErr = os.ReadFile(absTarget)
			if restoreErr == nil {
				result.RestoredSHA256 = digest(restored)
				result.RestoredExact = bytes.Equal(restored, original) && result.RestoredSHA256 == result.OriginalSHA256
				if !result.RestoredExact {
					restoreErr = fmt.Errorf("restore hash mismatch: want %s, got %s", result.OriginalSHA256, result.RestoredSHA256)
				}
			}
		}
		if restoreErr != nil {
			exitCode = 125
			returnedErr = fmt.Errorf("FATAL: restore failed for %s: %w", absTarget, restoreErr)
		}
	}()

	if err := writeExact(absTarget, perturbed, info.Mode()); err != nil {
		return result, 125, fmt.Errorf("write perturbation: %w", err)
	}

	exitCode = 0
	if *roundTrip {
		if interruptPending(interrupts) {
			exitCode = 130
		}
	} else {
		exitCode = runCommand(commandArguments, interrupts)
		// If the child completed concurrently with an interrupt, the done case
		// may win the select.  Preserve interrupt status while the outer signal
		// handler remains active for restoration.
		if exitCode != 130 && interruptPending(interrupts) {
			exitCode = 130
		}
		result.CommandExitCode = &exitCode
	}
	return result, exitCode, returnedErr
}

func run() (result report, exitCode int, returnedErr error) {
	// Own Interrupt for the complete transaction, not only while a child is
	// waiting.  runWithInterrupts restores before returning, so this outer
	// defer keeps notification active through restoration as well as the
	// pre-child and round-trip windows.
	interrupts := make(chan os.Signal, 1)
	signal.Notify(interrupts, os.Interrupt)
	defer signal.Stop(interrupts)
	return runWithInterrupts(interrupts)
}

func main() {
	result, exitCode, err := run()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
	if result.File != "" {
		encoded, encodeErr := json.Marshal(result)
		if encodeErr != nil {
			fmt.Fprintln(os.Stderr, encodeErr)
			os.Exit(125)
		}
		fmt.Println(string(encoded))
	}
	os.Exit(exitCode)
}
