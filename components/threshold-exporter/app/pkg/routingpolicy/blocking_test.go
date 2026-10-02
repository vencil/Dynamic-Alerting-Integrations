package routingpolicy

import "testing"

// allProblemKinds is every Problem kind this package reports. The test below
// also checks it against every platform kind the parity matrix names, so a
// kind added to the table without a row here reddens it.
var allProblemKinds = []string{
	ProblemDomainPolicyUnusable, ProblemRoutingProfilesUnusable, ProblemRoutingDefaultsRoutes,
	ProblemRoutingInUnreadLocation, ProblemRoutingEnforcedBelowRoot, ProblemRoutingDefaultsNullBelowRoot,
	ProblemRoutingProfileDuplicate, ProblemDuplicateTenant, ProblemDomainPolicyOutOfScope,
	ProblemRoutingDefaultsNotMapping,
}

// TestIsBlockingMatchesTheTable (#2326 review F5): IsBlocking answers true for
// exactly the matrix's blocking_kinds, which the Python half asserts equal to
// _grar_parse.BLOCKING_TREE_KINDS — so the two blocking lists cannot drift.
func TestIsBlockingMatchesTheTable(t *testing.T) {
	t.Parallel()
	m := loadParityMatrix(t)
	if len(m.BlockingKinds) == 0 {
		t.Fatal("matrix names no blocking kind")
	}
	want := map[string]bool{}
	for _, k := range m.BlockingKinds {
		want[k] = true
	}
	known := map[string]bool{}
	for _, k := range allProblemKinds {
		known[k] = true
		if IsBlocking(k) != want[k] {
			t.Errorf("IsBlocking(%q) = %v, table says %v", k, IsBlocking(k), want[k])
		}
	}
	for k := range want {
		if !known[k] {
			t.Errorf("table's blocking kind %q is not a Problem kind of this package", k)
		}
	}
	for _, tree := range m.Trees {
		for _, row := range tree.Platform {
			if !known[row[0]] {
				t.Errorf("%s: platform kind %q missing from allProblemKinds", tree.Name, row[0])
			}
		}
	}
}
