package main

import (
	"bytes"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/internal/guard"
)

// selfCheckCmd runs every subcommand, and the default check, on a fixture
// built into the binary and compares what each answers with the answer
// built in beside it. The generator service
// (scripts/tools/ops/generator_service.py) is ready only once this passes: a
// da-guard that is missing, broken or of another build answers something
// else.
//
// The contract is the same in da-guard, da-read and da-crdecode
// (tests/shared/test_reader_self_check.py holds all three to the Python
// caller's definition): exit 0 and exactly the passed document on stdout,
// or exit 1 and the failed document, each failing fixture named on stderr.
// Arguments after `self-check` are a usage error (exit 2, nothing on stdout).
//
// The fixtures that read a conf.d tree each get their own copy of
// selfcheck/tree, written below $TMPDIR (os.MkdirTemp) and removed after;
// `{tree}` in a fixture's arguments is that copy, and the copy's path in
// what it prints reads `{tree}` again before the comparison.
const selfCheckCmd = "self-check"

// The self-check documents: {"self_check": "passed"} or {"self_check": "failed"}.
const (
	selfCheckKey    = "self_check"
	selfCheckPassed = "passed"
	selfCheckFailed = "failed"
)

// exitSelfCheckFailed is the self-check's exit when a fixture's answer is
// not the one built in: it ran and found the binary wrong.
const exitSelfCheckFailed = 1

// selfCheckTree is the placeholder of a fixture's conf.d copy.
const selfCheckTree = "{tree}"

// ⛔ all: — the tree's `_defaults.yaml` starts with `_`, which a plain
// embed of the directory leaves out.
//
//go:embed all:selfcheck
var selfCheckFixtures embed.FS

// selfCheckCase is one fixture: dispatch() with args, and
// selfcheck/<name>.input on standard input when that file is there, must
// exit rc and print exactly selfcheck/<name>.want on stdout — and exactly
// selfcheck/<name>.stderr on stderr, when that file is there.
type selfCheckCase struct {
	name string
	args []string
	rc   int
}

// selfCheckCases covers every entry of `subcommands` and the default check
// (caseMode names which); selfCheck fails on one without a case.
var selfCheckCases = []selfCheckCase{
	// 4: the tree's `_severity_dedup` (read by severity-dedup) is a key only
	// the route generator judges
	{"default", []string{"--config-dir", selfCheckTree, "--format", "json"}, exitUndetermined},
	{"served-values", []string{servedValuesCmd, "--config-dir", selfCheckTree, "--at", "2026-01-01T00:00:00Z"}, exitOK},
	{"effective", []string{effectiveCmd, "--config-dir", selfCheckTree}, exitOK},
	{"key-verdict", []string{keyVerdictCmd, "--config-dir", selfCheckTree, "--at", "2026-01-01T00:00:00Z"}, exitOK},
	{"key-refs", []string{keyRefsCmd, "--config-dir", selfCheckTree, "--metric", "mysql_connection_usage_percent"}, exitOK},
	{"parse-tree", []string{parseTreeCmd, "--config-dir", selfCheckTree}, exitOK},
	{"compile-custom-alerts", []string{compileCustomAlertsCmd, "--config-dir", selfCheckTree}, exitOK},
	{"write", []string{writeCmd, "--config-dir", selfCheckTree, "--input", "-", "--json"}, exitOK},
	{"threshold-values", []string{thresholdValuesCmd}, exitOK},
	{"key-spellings", []string{keySpellingsCmd}, exitOK},
	{"severity-dedup", []string{severityDedupCmd, "--config-dir", selfCheckTree}, exitOK},
}

// caseMode is the subcommand of *args* as dispatch() reads it, defaultMode
// for the default check.
func caseMode(args []string) string {
	if len(args) > 0 {
		if _, ok := subcommands[args[0]]; ok {
			return args[0]
		}
	}
	return defaultMode
}

// runSelfCheck is `da-guard self-check`.
func runSelfCheck(operands []string, stdout, errOut io.Writer) int {
	if len(operands) != 0 {
		fmt.Fprintf(errOut, "usage: %s %s\n", programName, selfCheckCmd)
		return exitCallerErr
	}
	failed := selfCheck(dispatchNames(), selfCheckCases, selfCheckFixtures, errOut)
	return selfCheckReport(failed, stdout, errOut)
}

// dispatchNames is every subcommand of the dispatch table and the default
// check (defaultMode), sorted.
func dispatchNames() []string {
	names := []string{defaultMode}
	for name := range subcommands {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// selfCheckRun is what one case answered.
type selfCheckRun struct {
	rc             int
	stdout, stderr []byte
}

// copyTree writes *fixtures*' selfcheck/tree into a new directory below
// $TMPDIR and returns it.
func copyTree(fixtures fs.FS) (string, error) {
	dir, err := os.MkdirTemp("", "da-guard-self-check-")
	if err != nil {
		return "", err
	}
	const root = "selfcheck/tree"
	err = fs.WalkDir(fixtures, root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel := strings.TrimPrefix(strings.TrimPrefix(p, root), "/")
		target := filepath.Join(dir, filepath.FromSlash(rel))
		if d.IsDir() {
			return os.MkdirAll(target, 0o700)
		}
		data, err := fs.ReadFile(fixtures, p)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, 0o600)
	})
	if err != nil {
		_ = os.RemoveAll(dir)
		return "", err
	}
	return dir, nil
}

// runSelfCheckCase runs case *c* with its input from *fixtures*, on a copy
// of the tree when its arguments name one.
func runSelfCheckCase(c selfCheckCase, fixtures fs.FS) (selfCheckRun, error) {
	input, err := fs.ReadFile(fixtures, path.Join("selfcheck", c.name+".input"))
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return selfCheckRun{}, err
	}
	args := append([]string(nil), c.args...)
	tree := ""
	if slices.Contains(args, selfCheckTree) {
		if tree, err = copyTree(fixtures); err != nil {
			return selfCheckRun{}, err
		}
		defer func() { _ = os.RemoveAll(tree) }()
		for i, a := range args {
			if a == selfCheckTree {
				args[i] = tree
			}
		}
	}
	var out, errOut bytes.Buffer
	rc := dispatch(args, bytes.NewReader(input), &out, &errOut, guard.CheckDefaultsImpact)
	got := selfCheckRun{rc, out.Bytes(), errOut.Bytes()}
	if tree != "" {
		got.stdout = bytes.ReplaceAll(got.stdout, []byte(tree), []byte(selfCheckTree))
		got.stderr = bytes.ReplaceAll(got.stderr, []byte(tree), []byte(selfCheckTree))
	}
	// The build's version (main.Version, set by -ldflags) is no answer of the
	// code: a release build and a test build answer alike.
	if v, err := json.Marshal(Version); err == nil {
		got.stdout = bytes.ReplaceAll(got.stdout,
			append([]byte(`"da_guard_version":`), v...), []byte(`"da_guard_version":"`+selfCheckVersion+`"`))
	}
	return got, nil
}

// selfCheckVersion is the placeholder of the build's version in an answer.
const selfCheckVersion = "{version}"

// selfCheck runs *cases* and returns the names of what failed: a case whose
// answer differs, an entry of *dispatch* no case runs. Each is said on
// errOut.
func selfCheck(dispatch []string, cases []selfCheckCase, fixtures fs.FS, errOut io.Writer) []string {
	var failed []string
	fail := func(name, why string) {
		failed = append(failed, name)
		fmt.Fprintf(errOut, "%s: %s: %q: %s\n", programName, selfCheckCmd, name, why)
	}
	known := map[string]bool{}
	for _, name := range dispatch {
		known[name] = true
	}
	covered := map[string]bool{}
	for _, c := range cases {
		mode := caseMode(c.args)
		if !known[mode] {
			fail(c.name, "the fixture names no entry of the dispatch table")
			continue
		}
		covered[mode] = true
		got, err := runSelfCheckCase(c, fixtures)
		if err != nil {
			fail(c.name, err.Error())
			continue
		}
		want, err := fs.ReadFile(fixtures, path.Join("selfcheck", c.name+".want"))
		if err != nil {
			fail(c.name, err.Error())
			continue
		}
		wantErr, err := fs.ReadFile(fixtures, path.Join("selfcheck", c.name+".stderr"))
		checkErr := err == nil
		if err != nil && !errors.Is(err, fs.ErrNotExist) {
			fail(c.name, err.Error())
			continue
		}
		switch {
		case got.rc != c.rc:
			fail(c.name, fmt.Sprintf("exit %d, want %d", got.rc, c.rc))
		case !bytes.Equal(got.stdout, want):
			fail(c.name, "stdout is not the answer built in")
		case checkErr && !bytes.Equal(got.stderr, wantErr):
			fail(c.name, "stderr is not the answer built in")
		}
	}
	for _, name := range dispatch {
		if !covered[name] {
			fail(name, "no fixture runs this entry of the dispatch table")
		}
	}
	return failed
}

// selfCheckReport prints the passed document (exit 0) when nothing failed,
// else the failed document (exit 1).
func selfCheckReport(failed []string, stdout, errOut io.Writer) int {
	verdict, rc := selfCheckPassed, exitOK
	if len(failed) > 0 {
		verdict, rc = selfCheckFailed, exitSelfCheckFailed
	}
	b, err := json.Marshal(map[string]string{selfCheckKey: verdict})
	if err != nil {
		fmt.Fprintf(errOut, "%s: %v\n", programName, err)
		return exitCallerErr
	}
	if _, err := stdout.Write(append(b, '\n')); err != nil {
		fmt.Fprintf(errOut, "%s: writing stdout: %v\n", programName, err)
		return exitCallerErr
	}
	return rc
}
