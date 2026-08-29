package main

import (
	"flag"
	"io"
	"os"
	"os/exec"
	"strings"
	"testing"
)

// envBoolCase is one row of the parse contract. want/wantErr describe
// parseEnvBool; flagRejects records what the command line does with the SAME
// string, so the two columns can be compared row by row (#1599 / ADR-034).
type envBoolCase struct {
	raw          string
	want         bool
	wantErr      bool
	flagRejects  bool
	divergenceOK string // non-empty = deliberate, documented divergence from flag.Bool
}

// The rows are the probe matrix from #1599's follow-up measurement, kept
// verbatim so a future change to the parser has to restate the contract
// rather than quietly widen it.
var envBoolCases = []envBoolCase{
	// Unset / layout-only: the flag keeps its own default, no error.
	{raw: "", want: false, flagRejects: true, divergenceOK: "unset env var is silence, not input"},
	{raw: "   ", want: false, flagRejects: true, divergenceOK: "YAML layout whitespace is carrier noise"},

	// Accepted by both, identically.
	{raw: "true", want: true},
	{raw: "True", want: true},
	{raw: "TRUE", want: true},
	{raw: "t", want: true},
	{raw: "T", want: true},
	{raw: "1", want: true},
	{raw: "false", want: false},
	{raw: "False", want: false},
	{raw: "FALSE", want: false},
	{raw: "f", want: false},
	{raw: "F", want: false},
	{raw: "0", want: false},

	// Trimmed, then accepted — the one deliberate divergence.
	{raw: " true", want: true, flagRejects: true, divergenceOK: "TrimSpace before parse"},
	{raw: "true ", want: true, flagRejects: true, divergenceOK: "TrimSpace before parse"},

	// Rejected by both. "yes"/"on" were accepted before #1599; the command
	// line never took them, and now neither does the env var.
	{raw: "yes", wantErr: true, flagRejects: true},
	{raw: "YES", wantErr: true, flagRejects: true},
	{raw: "on", wantErr: true, flagRejects: true},
	{raw: "ON", wantErr: true, flagRejects: true},
	{raw: "no", wantErr: true, flagRejects: true},
	{raw: "off", wantErr: true, flagRejects: true},
	{raw: "tRuE", wantErr: true, flagRejects: true},
	{raw: "ture", wantErr: true, flagRejects: true},
	{raw: " ture ", wantErr: true, flagRejects: true},
}

func TestParseEnvBoolContract(t *testing.T) {
	t.Parallel()
	for _, tc := range envBoolCases {
		t.Run("raw="+strings.ReplaceAll(tc.raw, " ", "_"), func(t *testing.T) {
			t.Parallel()
			got, err := parseEnvBool(tc.raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("parseEnvBool(%q) = (%v, nil); want an error — an unrecognized value must not resolve to a legal one (ADR-034)", tc.raw, got)
				}
				if got {
					t.Fatalf("parseEnvBool(%q) returned true alongside its error; the bool must stay false on the error path", tc.raw)
				}
				return
			}
			if err != nil {
				t.Fatalf("parseEnvBool(%q) = unexpected error %v", tc.raw, err)
			}
			if got != tc.want {
				t.Fatalf("parseEnvBool(%q) = %v; want %v", tc.raw, got, tc.want)
			}
		})
	}
}

// TestParseEnvBoolMatchesFlagBool is the regression nail for #1599's root
// cause: the env path had its own, looser parser than the flag path. Every
// row must either agree with flag.Bool or carry a written reason not to.
func TestParseEnvBoolMatchesFlagBool(t *testing.T) {
	t.Parallel()
	for _, tc := range envBoolCases {
		t.Run("raw="+strings.ReplaceAll(tc.raw, " ", "_"), func(t *testing.T) {
			t.Parallel()
			fs := flag.NewFlagSet("probe", flag.ContinueOnError)
			fs.SetOutput(io.Discard)
			got := fs.Bool("x", false, "")
			flagErr := fs.Parse([]string{"-x=" + tc.raw})

			if (flagErr != nil) != tc.flagRejects {
				t.Fatalf("flag.Bool(-x=%q): rejected=%v; the table says %v — the table is what the divergence claims are checked against, so fix it first", tc.raw, flagErr != nil, tc.flagRejects)
			}

			envErr := func() error { _, e := parseEnvBool(tc.raw); return e }()
			agree := (flagErr != nil) == (envErr != nil)
			if !agree && tc.divergenceOK == "" {
				t.Fatalf("parseEnvBool(%q) and flag.Bool disagree on acceptance (env err=%v, flag err=%v) with no documented reason — either fix the parser or record why this input may diverge", tc.raw, envErr, flagErr)
			}
			if agree && tc.divergenceOK != "" {
				t.Fatalf("row %q is marked as a divergence (%q) but the two parsers now agree — drop the marker so it cannot mask a real one later", tc.raw, tc.divergenceOK)
			}
			if flagErr == nil && envErr == nil && *got != tc.want {
				t.Fatalf("flag.Bool(-x=%q) = %v but parseEnvBool = %v; accepted values must resolve identically", tc.raw, *got, tc.want)
			}
		})
	}
}

// TestEnvBoolFatalOnUnparseable proves the wrapper really exits — the whole
// point of #1599 is that this input must NOT be survivable as false.
func TestEnvBoolFatalOnUnparseable(t *testing.T) {
	if os.Getenv("ENVBOOL_CRASHER") == "1" {
		envBool("TA_ENVBOOL_PROBE")
		// Unreachable when envBool behaves; reaching it is the failure the
		// parent asserts against via exit code 0.
		return
	}

	cmd := exec.Command(os.Args[0], "-test.run=TestEnvBoolFatalOnUnparseable")
	cmd.Env = append(os.Environ(), "ENVBOOL_CRASHER=1", "TA_ENVBOOL_PROBE=ture")
	out, err := cmd.CombinedOutput()

	if err == nil {
		t.Fatalf("envBool(\"TA_ENVBOOL_PROBE\") with value \"ture\" exited 0; it must be fatal, not a silent false. Output:\n%s", out)
	}
	for _, want := range []string{"TA_ENVBOOL_PROBE", "ture", "ADR-034"} {
		if !strings.Contains(string(out), want) {
			t.Errorf("fatal message does not mention %q — the operator needs the var name, the rejected value and the rule. Got:\n%s", want, out)
		}
	}
}

// TestEnvBoolAcceptsLegalValues guards the other direction: the fatal path
// must not fire for values the contract accepts.
func TestEnvBoolAcceptsLegalValues(t *testing.T) {
	for _, tc := range []struct {
		raw  string
		want bool
	}{{"", false}, {"1", true}, {"true", true}, {"0", false}, {"FALSE", false}, {" true ", true}} {
		t.Setenv("TA_ENVBOOL_PROBE", tc.raw)
		if got := envBool("TA_ENVBOOL_PROBE"); got != tc.want {
			t.Errorf("envBool with %q = %v; want %v", tc.raw, got, tc.want)
		}
	}
}
