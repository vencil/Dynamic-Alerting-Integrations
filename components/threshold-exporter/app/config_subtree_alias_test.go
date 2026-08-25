package main

// The subtree overlay × deprecated-alias case, in its own file.
//
// ⛔ WHY IT IS NOT WITH ITS SIBLINGS. #1231 retired `mysql_cpu` in favour of
// `mysql_threads_running`, and a pre-commit guard forbids re-introducing the
// retired key "as a live value" — key position carrying a number, `disable`,
// or a dimensional brace. A fixture that exercises the alias path has to write
// exactly that shape, so the guard names the handful of files whose JOB is the
// alias behaviour and excludes them. Putting this one case here keeps that
// exception the size of the thing it excuses, instead of opening the whole
// reachability suite to a key nothing else there should ever mention.
//
// ⚠️ It is a REAL alias, not an invented one: `deprecatedKeyAliases` has
// exactly one entry, so there is nothing else to test with, and a fake alias
// would test the test rather than the loader.

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

// TestARetiredSpellingInASubtreeIsNotRefused pins that the reachability check
// canonicalizes before it gives up.
//
// ⛔ MEASURED as a defect first: `ResolveAtWithStats` runs
// `canonicalizeDefaults` / `canonicalizeOverrides` before any row is built, so
// a subtree key written with the retired spelling DOES reach the output plane
// — under its canonical name and, during the transition window, its legacy
// twin as well. An exact-string membership test said otherwise and refused it,
// so the inheriting tenant sat at the root's value while a tenant authoring
// the identical key in its own file got 42 on both spellings.
func TestARetiredSpellingInASubtreeIsNotRefused(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_threads_running: 80\n")
	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  mysql_cpu: 42\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"), "tenants:\n  t1: {}\n")

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	if got, ok := seriesFor(t, m, "t1", "threads_running"); !ok || got != 42 {
		t.Errorf("the inherited retired spelling did not reach the canonical series: "+
			"got %v (present=%v), want 42; tenant map = %v", got, ok, m.GetConfig().Tenants["t1"])
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("a deliverable key was reported as a divergence (gauge=%v)", got)
	}
	if strings.Contains(logBuf.String(), "conf.d scanner divergence") {
		t.Errorf("divergence ERROR on a healthy tree:\n%s", logBuf.String())
	}
}

// TestTheAliasRelationIsWalkedBothWays extends the reach matrix
// (`config_subtree_reach_matrix_test.go`) with the two rows whose fixtures
// need the retired spelling as a LIVE value — which the #1231 pre-commit hook
// forbids everywhere except this file.
//
// ⛔ THE PREDICATE ONLY WALKED ONE WAY. `keyCanReachTheOutputPlane`
// canonicalizes the SUBTREE's key before giving up, covering "the subtree
// still uses the retired spelling". The other half — the ROOT still uses it
// and the subtree has moved to the canonical spelling, which is what the
// rename NOTICE tells authors to do — was refused. Both resolvers walk a
// canonicalized view of their declaration surface, so both halves reach the
// plane, and both belong here.
func TestTheAliasRelationIsWalkedBothWays(t *testing.T) {
	cases := []struct {
		name    string
		root    string
		because string
	}{
		{
			"a legacy platform DEFAULT",
			"defaults:\n  mysql_connections: 80\n  " + retiredThresholdKey + ": 80\n" +
				"state_filters:\n  maintenance:\n    severity: warning\n",
			"resolveBaseRows walks canonicalizeDefaults(cfg.Defaults), so the root's legacy default already IS the canonical key by the time rows are built",
		},
		{
			"a legacy platform DECLARATION",
			"defaults:\n  mysql_connections: 80\noptional_overrides:\n  - " + retiredThresholdKey + "\n" +
				"state_filters:\n  maintenance:\n    severity: warning\n",
			"resolveDeclaredRows walks canonicalizeOptionalOverrides(cfg.OptionalOverrides) — the same symmetry, the other declaration surface",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ev := classifyInheritedKey(t, tc.root, "  mysql_threads_running: 42\n", "mysql_threads_running")
			if got != verdictDelivered {
				t.Fatalf("the canonical spelling over %s: got %v, want delivered (%s)\n%s",
					tc.name, got, tc.because, ev)
			}
		})
	}
}

// retiredThresholdKey is the #1231 retired spelling, assembled rather than
// written literally so a grep for the live key does not match this file's
// intent line by accident. The hook's exemption covers this file either way;
// this keeps the exemption from reading as carelessness.
var retiredThresholdKey = "mysql_" + "cpu"
