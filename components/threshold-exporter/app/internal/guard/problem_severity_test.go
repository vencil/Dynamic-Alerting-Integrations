package guard

// problem_severity_test.go — binds the merge-key corpus test's emulation of
// "da-guard blocks on this Problem" (blocksDaGuard in
// pkg/routingpolicy/merge_key_corpus_test.go: every Problem kind fails the
// run except routing_profiles_unusable) to the real severity mapping,
// platformProblemFindings. That package cannot import this one, so the
// emulation is a copy; if a Problem kind ever becomes a warning here, the
// corpus test would hold da-guard to "blocks" while da-guard lets the file
// through — a looser Go reader hidden as an equal one (ADR-036 step 2).
// The kinds are read from routingpolicy's own source (every `Problem*`
// string constant, testutil.RoutingPolicyProblemKinds), so a new kind is
// covered without editing this test.

import (
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
)

func TestPlatformProblemSeverityMatchesCorpusEmulation(t *testing.T) {
	kinds := testutil.RoutingPolicyProblemKinds(t)
	// The kinds the source declares today; a smaller set means the reader
	// above lost some, not that routingpolicy dropped them.
	const minKinds = 10
	if len(kinds) < minKinds {
		t.Fatalf("read %d Problem kinds from pkg/routingpolicy, want at least %d: %v", len(kinds), minKinds, kinds)
	}
	if kinds["ProblemDomainPolicyUnusable"] != routingpolicy.ProblemDomainPolicyUnusable ||
		kinds["ProblemRoutingProfilesUnusable"] != routingpolicy.ProblemRoutingProfilesUnusable {
		t.Fatalf("Problem kinds read from source look wrong: %v", kinds)
	}
	for name, kind := range kinds {
		got := platformProblemFindings([]routingpolicy.Problem{{Kind: kind, Message: "m"}})
		if len(got) != 1 {
			t.Fatalf("%s: %d findings, want 1", name, len(got))
		}
		// What blocksDaGuard (merge_key_corpus_test.go) assumes.
		want := SeverityError
		if kind == routingpolicy.ProblemRoutingProfilesUnusable {
			want = SeverityWarn
		}
		if got[0].Severity != want {
			t.Errorf("%s (%q): platformProblemFindings severity %q, the corpus test's blocksDaGuard assumes %q"+
				" — update both together", name, kind, got[0].Severity, want)
		}
	}
}
