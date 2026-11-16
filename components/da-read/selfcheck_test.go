package main

import (
	"bytes"
	"flag"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"
)

// updateSelfCheck rewrites the answers built in from what this build
// answers: `go test -run TestSelfCheckAnswers -update-self-check`. Read the
// diff before committing it — it is the change in what da-read answers.
var updateSelfCheck = flag.Bool("update-self-check", false, "rewrite selfcheck/*.want and *.stderr")

const selfCheckPassedOut = `{"self_check":"passed"}` + "\n"
const selfCheckFailedOut = `{"self_check":"failed"}` + "\n"

func TestSelfCheckAnswers(t *testing.T) {
	if !*updateSelfCheck {
		t.Skip("only with -update-self-check")
	}
	for _, c := range selfCheckCases {
		got, err := runSelfCheckCase(c, selfCheckFixtures)
		if err != nil {
			t.Fatal(err)
		}
		if got.rc != c.rc {
			t.Fatalf("%s: exit %d, want %d; stderr=%q", c.name, got.rc, c.rc, got.stderr)
		}
		base := filepath.Join("selfcheck", c.name)
		if err := os.WriteFile(base+".want", got.stdout, 0o644); err != nil {
			t.Fatal(err)
		}
		if c.rc != exitOK {
			if err := os.WriteFile(base+".stderr", got.stderr, 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}
}

// The binary's own self-check passes: every subcommand of the dispatch
// table has a fixture, and every fixture's answer is the one built in.
func TestSelfCheckPasses(t *testing.T) {
	t.Parallel()
	var out, errOut bytes.Buffer
	rc := run([]string{selfCheckCmd}, strings.NewReader(""), &out, &errOut)
	if rc != exitOK || out.String() != selfCheckPassedOut || errOut.Len() != 0 {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want 0 and the passed document", rc, out.String(), errOut.String())
	}
}

func TestSelfCheckTakesNoArguments(t *testing.T) {
	t.Parallel()
	var out, errOut bytes.Buffer
	rc := run([]string{selfCheckCmd, "x"}, strings.NewReader(""), &out, &errOut)
	if rc != exitCallerErr || out.Len() != 0 {
		t.Fatalf("rc=%d stdout=%q; want 2 and nothing on stdout", rc, out.String())
	}
}

// fixturesCopy is the fixtures built in, as a MapFS a test may change.
func fixturesCopy(t *testing.T) fstest.MapFS {
	t.Helper()
	m := fstest.MapFS{}
	err := fs.WalkDir(selfCheckFixtures, ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		data, err := fs.ReadFile(selfCheckFixtures, path)
		m[path] = &fstest.MapFile{Data: data}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return m
}

// assertSelfCheckFails runs the self-check over *dispatch*, *cases* and
// *fixtures* and wants it to fail naming *name*, with the failed document.
func assertSelfCheckFails(t *testing.T, dispatch []string, cases []selfCheckCase, fixtures fs.FS, name string) {
	t.Helper()
	var errOut, out bytes.Buffer
	failed := selfCheck(dispatch, cases, fixtures, &errOut)
	if len(failed) != 1 || failed[0] != name {
		t.Fatalf("failed=%v stderr=%q; want exactly %q", failed, errOut.String(), name)
	}
	if rc := selfCheckReport(failed, &out, io.Discard); rc != exitSelfCheckFailed || out.String() != selfCheckFailedOut {
		t.Fatalf("rc=%d stdout=%q; want 1 and the failed document", rc, out.String())
	}
}

// Every case's expected answer is load-bearing: a changed byte fails it.
func TestSelfCheckFailsOnAChangedAnswer(t *testing.T) {
	t.Parallel()
	for _, c := range selfCheckCases {
		c := c
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			fixtures := fixturesCopy(t)
			want := fixtures["selfcheck/"+c.name+".want"]
			want.Data = append(append([]byte(nil), want.Data[:len(want.Data)-1]...), ' ', '\n')
			assertSelfCheckFails(t, dispatchNames(), selfCheckCases, fixtures, c.name)
		})
	}
}

func TestSelfCheckFailsOnAChangedExit(t *testing.T) {
	t.Parallel()
	cases := append([]selfCheckCase(nil), selfCheckCases...)
	cases[0].rc = exitCallerErr
	assertSelfCheckFails(t, dispatchNames(), cases, selfCheckFixtures, cases[0].name)
}

func TestSelfCheckFailsOnAChangedStderr(t *testing.T) {
	t.Parallel()
	fixtures := fixturesCopy(t)
	f := fixtures["selfcheck/alertmanager-loader-reason.stderr"]
	if f == nil {
		t.Fatal("the loader-reason fixture checks no stderr")
	}
	f.Data = []byte("another reason\n")
	assertSelfCheckFails(t, dispatchNames(), selfCheckCases, fixtures, "alertmanager-loader-reason")
}

// ⛔ The SSOT: the subcommands checked are the dispatch table's. One added to
// it without a fixture fails the self-check.
func TestSelfCheckFailsOnASubcommandWithoutAFixture(t *testing.T) {
	t.Parallel()
	dispatch := append(dispatchNames(), "a-new-subcommand")
	assertSelfCheckFails(t, dispatch, selfCheckCases, selfCheckFixtures, "a-new-subcommand")
}

func TestSelfCheckFailsOnAFixtureOfNoSubcommand(t *testing.T) {
	t.Parallel()
	cases := append(append([]selfCheckCase(nil), selfCheckCases...),
		selfCheckCase{"gone", []string{"gone", "-"}, exitOK})
	assertSelfCheckFails(t, dispatchNames(), cases, selfCheckFixtures, "gone")
}
