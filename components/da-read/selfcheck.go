package main

import (
	"bytes"
	"embed"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"sort"
	"strconv"
)

// selfCheckCmd runs every subcommand on a fixture built into the binary and
// compares what it answers with the answer built in beside it. The generator
// service (scripts/tools/ops/generator_service.py) is ready only once this
// passes: a da-read that is missing, broken or of another build answers
// something else.
//
// The contract is the same in da-read, da-guard and da-crdecode
// (tests/shared/test_reader_self_check.py holds all three to the Python
// caller's definition): exit 0 and exactly the passed document on stdout,
// or exit 1 and the failed document, each failing fixture named on stderr.
// Arguments after `self-check` are a usage error (exit 2, nothing on stdout).
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

//go:embed selfcheck
var selfCheckFixtures embed.FS

// selfCheckCase is one fixture: run() with args and selfcheck/<name>.input
// on standard input must exit rc and print exactly selfcheck/<name>.want on
// stdout — and exactly selfcheck/<name>.stderr on stderr, when that file is
// there.
type selfCheckCase struct {
	name string
	args []string
	rc   int
}

// selfCheckCases covers every entry of `subcommands` (args[0] names it);
// selfCheck fails on an entry without a case and on a case naming no entry.
var selfCheckCases = []selfCheckCase{
	{"alertmanager", []string{"alertmanager", "-"}, exitOK},
	// the mode's one difference is the loader's reason on a refusal
	{"alertmanager-loader-reason", []string{"alertmanager", showLoaderReasonFlag, "-"}, exitCallerErr},
	{"prometheus", []string{"prometheus", "-"}, exitOK},
	{"prometheus-rules", []string{"prometheus-rules", "-"}, exitOK},
	{"promql-selectors", []string{"promql-selectors", `user_state_filter{filter="maintenance"}`, "-"}, exitOK},
	{"promql-label-flow", []string{"promql-label-flow", "tenant", "-"}, exitOK},
}

// runSelfCheck is `da-read self-check`.
func runSelfCheck(operands []string, stdout, stderr io.Writer) int {
	if len(operands) != 0 {
		say(stderr, "usage: %s %s\n", programName, selfCheckCmd)
		return exitCallerErr
	}
	failed := selfCheck(dispatchNames(), selfCheckCases, selfCheckFixtures, stderr)
	return selfCheckReport(failed, stdout, stderr)
}

// dispatchNames is every subcommand of the dispatch table, sorted.
func dispatchNames() []string {
	names := make([]string, 0, len(subcommands))
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

// runSelfCheckCase runs case *c* on its input from *fixtures*.
func runSelfCheckCase(c selfCheckCase, fixtures fs.FS) (selfCheckRun, error) {
	input, err := fs.ReadFile(fixtures, "selfcheck/"+c.name+".input")
	if err != nil {
		return selfCheckRun{}, err
	}
	var out, errOut bytes.Buffer
	rc := run(c.args, bytes.NewReader(input), &out, &errOut)
	return selfCheckRun{rc, out.Bytes(), errOut.Bytes()}, nil
}

// selfCheck runs *cases* and returns the names of what failed: a case whose
// answer differs, a subcommand of *dispatch* no case runs, a case whose
// subcommand is not in *dispatch*. Each is said on stderr.
func selfCheck(dispatch []string, cases []selfCheckCase, fixtures fs.FS, stderr io.Writer) []string {
	var failed []string
	fail := func(name, why string) {
		failed = append(failed, name)
		say(stderr, "%s: %s: %s: %s\n", programName, selfCheckCmd, name, why)
	}
	known := map[string]bool{}
	for _, name := range dispatch {
		known[name] = true
	}
	covered := map[string]bool{}
	for _, c := range cases {
		if len(c.args) == 0 || !known[c.args[0]] {
			fail(c.name, "the fixture names no subcommand of the dispatch table")
			continue
		}
		covered[c.args[0]] = true
		got, err := runSelfCheckCase(c, fixtures)
		if err != nil {
			fail(c.name, err.Error())
			continue
		}
		want, err := fs.ReadFile(fixtures, "selfcheck/"+c.name+".want")
		if err != nil {
			fail(c.name, err.Error())
			continue
		}
		wantErr, err := fs.ReadFile(fixtures, "selfcheck/"+c.name+".stderr")
		checkErr := err == nil
		if err != nil && !errors.Is(err, fs.ErrNotExist) {
			fail(c.name, err.Error())
			continue
		}
		switch {
		case got.rc != c.rc:
			fail(c.name, "exit "+strconv.Itoa(got.rc)+", want "+strconv.Itoa(c.rc))
		case !bytes.Equal(got.stdout, want):
			fail(c.name, "stdout is not the answer built in")
		case checkErr && !bytes.Equal(got.stderr, wantErr):
			fail(c.name, "stderr is not the answer built in")
		}
	}
	for _, name := range dispatch {
		if !covered[name] {
			fail(name, "no fixture runs this subcommand")
		}
	}
	return failed
}

// selfCheckReport prints the passed document (exit 0) when nothing failed,
// else the failed document (exit 1).
func selfCheckReport(failed []string, stdout, stderr io.Writer) int {
	verdict, rc := selfCheckPassed, exitOK
	if len(failed) > 0 {
		verdict, rc = selfCheckFailed, exitSelfCheckFailed
	}
	b, err := json.Marshal(map[string]string{selfCheckKey: verdict})
	if err != nil {
		say(stderr, "%s: %v\n", programName, err)
		return exitCallerErr
	}
	return write(stdout, stderr, append(b, '\n'), rc)
}
