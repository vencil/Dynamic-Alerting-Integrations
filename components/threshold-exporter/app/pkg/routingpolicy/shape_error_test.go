package routingpolicy

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

// TestDomainPoliciesShapeError (#2659): the shape check da-guard reports as
// domain_policy_unusable, on a node parse only. Where it is an error,
// ParseDomainPolicies refuses the document with the same error — one
// predicate (domainPoliciesNode), not two copies.
func TestDomainPoliciesShapeError(t *testing.T) {
	t.Parallel()
	for _, src := range []string{
		"domain_policies: null\n", "domain_policies: ~\n", "domain_policies:\n", "domain_policies: []\n",
		"domain_policies: [a]\n", "domain_policies: x\n", "domain_policies: 1\n",
		// merge-supplied, found as lookup finds it
		"base: &b\n  domain_policies: null\n<<: *b\n",
		"base: &b\n  domain_policies: {fin: {}}\n<<: *b\ndomain_policies: ~\n", // own key wins
	} {
		err := DomainPoliciesShapeError([]byte(src))
		if err == nil {
			t.Errorf("%q: nil, want the shape error", src)
			continue
		}
		if _, _, perr := ParseDomainPolicies([]byte(src)); perr == nil || perr.Error() != err.Error() {
			t.Errorf("%q: ParseDomainPolicies err = %v, want the same %v", src, perr, err)
		}
	}
	for _, src := range []string{
		"domain_policies: {}\n", "domain_policies: {fin: {tenants: [t1]}}\n", "other: 1\n", "", "# only a comment\n",
		"base: &b\n  domain_policies: {fin: {}}\n<<: *b\n",
		"base: &b\n  domain_policies: null\n<<: *b\ndomain_policies: {}\n", // own key wins
		// not this predicate's: the caller's own decode judges them
		"[a, b]\n", "x\n", "domain_policies: [unclosed\n",
	} {
		if err := DomainPoliciesShapeError([]byte(src)); err != nil {
			t.Errorf("%q: %v, want nil", src, err)
		}
	}
}

// Nothing is decoded, so lookup's merge expansion is what could loop or
// multiply: a top mapping merged into itself ends, and a merge fan-out is
// listed once per source.
func TestDomainPoliciesShapeError_MergeCycleAndFanOutAreBounded(t *testing.T) {
	t.Parallel()
	fan := "m0: &m0 {a: 1}\n"
	for i := 1; i <= 9; i++ {
		fan += fmt.Sprintf("m%d: &m%d {<<: [%s]}\n", i, i,
			strings.TrimSuffix(strings.Repeat(fmt.Sprintf("*m%d,", i-1), 10), ","))
	}
	for src, wantErr := range map[string]bool{
		"&t {<<: *t, domain_policies: ~}\n":         true,
		"&t {<<: *t, domain_policies: {fin: {}}}\n": false,
		fan + "<<: *m9\ndomain_policies: ~\n":       true,
		fan + "<<: *m9\n":                           false,
	} {
		start := time.Now()
		err := DomainPoliciesShapeError([]byte(src))
		if (err != nil) != wantErr {
			t.Errorf("%.40q: err = %v, want error %v", src, err, wantErr)
		}
		if d := time.Since(start); d > 2*time.Second {
			t.Errorf("%.40q: took %v", src, d)
		}
	}
}
