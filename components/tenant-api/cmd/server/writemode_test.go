package main

import (
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/handler"
)

// TestParseWriteModeContract pins what the legal-value check accepts and what
// it rejects, including the normalization the accepted values go through.
//
// The rows marked pre1559Direct are the exact inputs measured against the
// unfixed wirePRBackend: every one of them resolved to WriteModeDirect and
// printed the same "direct write mode (commit-on-write)" line as a deliberate
// --write-mode=direct. That column is what makes this table a regression
// record rather than a restatement of the new code.
func TestParseWriteModeContract(t *testing.T) {
	for _, tc := range []struct {
		raw string
		// want is the resolved mode when accepted; ignored when wantErr.
		want handler.WriteMode
		// wantErr is true when the value must be refused at startup.
		wantErr bool
		// pre1559Direct records whether the unfixed code silently resolved
		// this input to `direct`.
		pre1559Direct bool
		why           string
	}{
		// The four legal values.
		{raw: "direct", want: handler.WriteModeDirect, pre1559Direct: true, why: "the deliberate direct opt-in"},
		{raw: "pr", want: handler.WriteModePR, why: "GitHub PR mode, canonical spelling"},
		{raw: "pr-github", want: handler.WriteModePRGitHub, why: "explicit GitHub alias"},
		{raw: "pr-gitlab", want: handler.WriteModePRGitLab, why: "GitLab MR mode"},

		// Whitespace is carrier noise, not a different value.
		{raw: " pr", want: handler.WriteModePR, pre1559Direct: true, why: "leading space from a YAML scalar"},
		{raw: "pr ", want: handler.WriteModePR, pre1559Direct: true, why: "trailing space from a YAML scalar"},
		{raw: "\tpr-gitlab\n", want: handler.WriteModePRGitLab, why: "tab/newline carrier noise"},

		// Typos: the defect #1559 is about.
		{raw: "pr-guthub", wantErr: true, pre1559Direct: true, why: "one transposed character used to disable review entirely"},
		{raw: "pr-gitlab-x", wantErr: true, pre1559Direct: true, why: "legal value with a suffix is not a legal value"},
		{raw: "disabled", wantErr: true, pre1559Direct: true, why: "a plausible-looking word that was never a mode"},

		// Case is not folded — see parseWriteMode's doc comment.
		{raw: "PR", wantErr: true, pre1559Direct: true, why: "uppercase is a guess about intent, not a spelling variant"},
		{raw: "Direct", wantErr: true, pre1559Direct: true, why: "same, and it used to LOOK like it worked"},
		{raw: "DIRECT", wantErr: true, pre1559Direct: true, why: "same"},

		// Empty cannot mean 'unset' this far down; see the doc comment.
		{raw: "", wantErr: true, pre1559Direct: true, why: "--write-mode= passed explicitly"},
		{raw: "   ", wantErr: true, pre1559Direct: true, why: "whitespace-only trims to empty"},
	} {
		t.Run(tc.raw, func(t *testing.T) {
			got, err := parseWriteMode(tc.raw)

			if tc.wantErr {
				if err == nil {
					t.Fatalf("parseWriteMode(%q) = %q, nil — want a rejection (%s)", tc.raw, got, tc.why)
				}
				if got == handler.WriteModeDirect {
					t.Fatalf("parseWriteMode(%q) rejected the value but still returned %q; "+
						"returning the fallback alongside the error is the ADR-034 shape this fixes",
						tc.raw, handler.WriteModeDirect)
				}
				return
			}

			if err != nil {
				t.Fatalf("parseWriteMode(%q) = error %v — want %q accepted (%s)", tc.raw, err, tc.want, tc.why)
			}
			if got != tc.want {
				t.Fatalf("parseWriteMode(%q) = %q, want %q (%s)", tc.raw, got, tc.want, tc.why)
			}
		})
	}
}

// TestParseWriteModeRejectsEveryPre1559Fallback is the anti-no-op witness.
//
// Before #1559 each of these resolved to `direct` with no error and no
// distinguishable log line. If a later refactor widens the legal-value set —
// or drops the check — this test goes red instead of the defect coming back
// silently, which is exactly how it went unnoticed the first time.
func TestParseWriteModeRejectsEveryPre1559Fallback(t *testing.T) {
	silentlyDirectBefore := []string{
		"pr-guthub", "PR", "", "disabled", "Direct", "DIRECT", "pr-gitlab-x", "   ",
	}
	for _, raw := range silentlyDirectBefore {
		got, err := parseWriteMode(raw)
		if err == nil {
			t.Errorf("parseWriteMode(%q) = %q, nil — this input silently became `direct` before #1559 "+
				"and must now be refused at startup", raw, got)
		}
	}
}

// TestWirePRBackendFatalOnUnknownMode proves the wrapper really exits. The
// point of #1559 is that an unrecognized mode must not be survivable as
// direct commit-on-write, so a unit test on parseWriteMode alone would not
// settle it.
func TestWirePRBackendFatalOnUnknownMode(t *testing.T) {
	if os.Getenv("WRITEMODE_CRASHER") == "1" {
		wirePRBackend(prBackendFlags{Mode: "pr-guthub"})
		// Unreachable when wirePRBackend behaves; reaching it is the
		// failure the parent asserts against via exit code 0.
		return
	}

	cmd := exec.Command(os.Args[0], "-test.run=TestWirePRBackendFatalOnUnknownMode")
	cmd.Env = append(os.Environ(), "WRITEMODE_CRASHER=1")
	out, err := cmd.CombinedOutput()

	if err == nil {
		t.Fatalf("wirePRBackend(Mode: \"pr-guthub\") exited 0; it must be fatal, not a silent direct commit. Output:\n%s", out)
	}
	// The operator needs the rejected value, the legal set, and the rule.
	for _, want := range []string{"pr-guthub", "direct, pr, pr-github, pr-gitlab", "ADR-034"} {
		if !strings.Contains(string(out), want) {
			t.Errorf("fatal message does not mention %q. Got:\n%s", want, out)
		}
	}
}

// TestWirePRBackendDirectUnchanged guards the other direction: the legal
// direct path must still wire up exactly as before — no client, no tracker.
func TestWirePRBackendDirectUnchanged(t *testing.T) {
	client, tracker, wm := wirePRBackend(prBackendFlags{Mode: "direct"})
	if wm != handler.WriteModeDirect {
		t.Errorf("wirePRBackend(direct) mode = %q, want %q", wm, handler.WriteModeDirect)
	}
	if client != nil {
		t.Errorf("wirePRBackend(direct) returned a non-nil platform client; direct mode needs no platform integration")
	}
	if tracker != nil {
		t.Errorf("wirePRBackend(direct) returned a non-nil tracker; direct mode needs no tracker")
	}
}

// TestWriteModeLegalValuesMatchesIsPRMode ties the legal-value set to the
// handler package's own notion of which modes are PR modes.
//
// Both ways of disagreeing are already fail-loud — a mode in IsPRMode but not
// in the set is refused at startup, and a mode in the set with no switch arm
// hits wirePRBackend's BUG: default — so this test exists to name the
// invariant, and to fail at build time rather than at a deployment's startup.
func TestWriteModeLegalValuesMatchesIsPRMode(t *testing.T) {
	var prModes, directModes int
	for _, wm := range writeModeLegalValues {
		if wm.IsPRMode() {
			prModes++
		} else {
			directModes++
		}
	}
	if prModes != 3 {
		t.Errorf("writeModeLegalValues holds %d PR modes, want 3 (pr, pr-github, pr-gitlab); "+
			"a new PR mode must be added to the set too or deployments using it will refuse to start", prModes)
	}
	if directModes != 1 {
		t.Errorf("writeModeLegalValues holds %d non-PR modes, want exactly 1 (direct)", directModes)
	}
}
