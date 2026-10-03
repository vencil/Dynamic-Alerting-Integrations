package routingpolicy

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

// policyWithTopLevelKeys is the shape #2681 measured: n distinct top-level
// keys, then an empty domain_policies.
func policyWithTopLevelKeys(n int) []byte {
	var b strings.Builder
	for i := 0; i < n; i++ {
		fmt.Fprintf(&b, "k%d: 1\n", i)
	}
	b.WriteString("domain_policies: {}\n")
	return []byte(b.String())
}

// fastestParse is the quickest of a few UnmarshalPolicy runs: the minimum is
// the reading least disturbed by the scheduler, GC and the other tests.
func fastestParse(t *testing.T, data []byte) time.Duration {
	t.Helper()
	best := time.Duration(1<<63 - 1)
	for i := 0; i < 3; i++ {
		var out map[string]any
		start := time.Now()
		if err := UnmarshalPolicy(data, &out); err != nil {
			t.Fatal(err)
		}
		if d := time.Since(start); d < best {
			best = d
		}
		if len(out) == 0 {
			t.Fatal("decoded nothing")
		}
	}
	return best
}

// TestUnmarshalPolicy_LargeMappingScalesLinearly pins #2681: yaml.v3 v3.0.1
// compared every pair of keys in a mapping, so a 32000-key policy file took
// seconds. The vendored copy (third_party/yaml.v3) checks duplicates with a
// map above 48 keys.
//
// ⛔ A ratio, not a wall-clock budget: CI machines differ by several times
// in speed (and -race slows everything again), but both sizes run on the
// same machine in the same process. 8x the keys costs about 8-9x when
// linear (also under -race); with v3.0.1's pairwise check it cost about
// 90x (measured in the commit adding this test). The bound of 30 sits
// between the two with a margin of 3x either way.
func TestUnmarshalPolicy_LargeMappingScalesLinearly(t *testing.T) {
	if testing.Short() {
		t.Skip("timing test; skipped with -short")
	}
	const small, large, bound = 4000, 32000, 30.0

	smallData, largeData := policyWithTopLevelKeys(small), policyWithTopLevelKeys(large)
	fastestParse(t, smallData) // warm-up: first-run allocation and page faults
	ts := fastestParse(t, smallData)
	tl := fastestParse(t, largeData)

	ratio := float64(tl) / float64(ts)
	t.Logf("%d keys: %v, %d keys: %v, ratio %.1f (bound %.0f)", small, ts, large, tl, ratio, bound)
	if ratio > bound {
		t.Fatalf("%dx the keys took %.1fx the time (bound %.0f): duplicate-key detection "+
			"is superlinear again — is gopkg.in/yaml.v3 still replaced by "+
			"third_party/yaml.v3? (#2681)", large/small, ratio, bound)
	}
}

// TestUnmarshalPolicy_LargeMappingStillRejectsDuplicates: the linear path
// (taken above 48 keys) reports a duplicate as the pairwise one did.
func TestUnmarshalPolicy_LargeMappingStillRejectsDuplicates(t *testing.T) {
	t.Parallel()
	data := append(policyWithTopLevelKeys(100), "k7: 2\n"...)
	var out map[string]any
	err := UnmarshalPolicy(data, &out)
	if err == nil || !strings.Contains(err.Error(), `line 102: mapping key "k7" already defined at line 8`) {
		t.Fatalf("want the duplicate k7 reported, got %v", err)
	}
}
