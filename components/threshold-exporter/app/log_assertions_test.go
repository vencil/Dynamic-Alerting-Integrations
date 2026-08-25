package main

// Sweep B-5 of #1521 (PR #1569): assertions that read the WHOLE log.
//
// ⛔ WHY THIS IS ITS OWN SWEEP. `strings.Contains(wholeLog, x)` asks "does
// this string appear anywhere", which is almost never the question the test
// means. The question is "does THE line that reports X say Y". The two differ
// exactly when some OTHER line also contains the substring — and the line most
// likely to do that is the one the defect itself emits.
//
// Measured on this PR: `TestARelativeConfigDirStillTellsTheRootApart` asserted
// `Contains(wholeLog, "redis_evicted_keys")` as its control. The bug it was
// written to catch emits `WARN: unknown key "redis_evicted_keys"`, so the
// assertion was satisfied BY THE BUG. A blind reviewer found it by mutating
// the predicate to refuse nothing and watching the test stay green.
//
// So: anchor on the line that identifies the producer, then assert on that
// line. The helpers below are the shared form.
//
// ⛔ THE TALLY, MEASURED RATHER THAN ASSUMED. Seven whole-log assertions were
// converted; each was then mutation-tested for a case that is green under the
// old form and red under the new one. Three have one:
//
//	TestRecomputeMergedHash_DefaultsParseFailureEmitsErrorAndMetric
//	TestConfigManager_LoadDir_UnparseableDefaultsErrorAndMetric
//	  — making the emitter print a constant other filename left the OLD form
//	    green across the WHOLE package (`go test .` rc=0). Both tests claimed
//	    to verify the ERROR names the unparseable file; both verified only
//	    that some ERROR of that class existed.
//	TestTheDivergenceAuditRunsOnEveryCommit
//	  — an audit naming a constant wrong tenant left the old form green.
//
// The other four are COSMETIC against every mutation tried, and each call site
// says so. That includes one where I predicted a false-green path and measured
// myself wrong; the prediction is recorded at that call site rather than
// deleted, because "I checked and it was not exploitable" and "I did not
// check" have to stay distinguishable.

import (
	"strings"
	"testing"
)

const divergenceAnchor = "conf.d scanner divergence"

// logLinesWith returns every line of logs containing anchor, in order.
func logLinesWith(logs, anchor string) []string {
	var out []string
	for _, line := range strings.Split(logs, "\n") {
		if strings.Contains(line, anchor) {
			out = append(out, line)
		}
	}
	return out
}

// assertLogLineWith finds the single line identified by anchor and asserts
// every string in must appears IN THAT LINE.
//
// Fails distinctly for "no such line" and "the line does not say that": the
// two have different causes and conflating them is how a missing log line gets
// read as a wrong log line.
func assertLogLineWith(t *testing.T, logs, anchor string, must ...string) {
	t.Helper()
	lines := logLinesWith(logs, anchor)
	if len(lines) == 0 {
		t.Errorf("no log line contains %q, so it cannot say %q; log:\n%s", anchor, must, logs)
		return
	}
	assertLineSays(t, lines[0], anchor, must...)
}

// assertLastLogLineWith is assertLogLineWith against the LAST matching line —
// for tests whose subject is "the most recent line an operator has".
func assertLastLogLineWith(t *testing.T, logs, anchor string, must ...string) {
	t.Helper()
	lines := logLinesWith(logs, anchor)
	if len(lines) == 0 {
		t.Errorf("no log line contains %q, so it cannot say %q; log:\n%s", anchor, must, logs)
		return
	}
	assertLineSays(t, lines[len(lines)-1], anchor, must...)
}

func assertLineSays(t *testing.T, line, anchor string, must ...string) {
	t.Helper()
	for _, want := range must {
		if !strings.Contains(line, want) {
			t.Errorf("the %q line does not say %q; line:\n%s", anchor, want, line)
		}
	}
}
