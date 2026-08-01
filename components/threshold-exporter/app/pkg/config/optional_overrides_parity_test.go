package config

// optional_overrides_parity_test.go — the Go half of the Go↔Python membership
// parity pin (#1189 / TRK-337).
//
// ValidateTenantKeys and _grar_validate.validate_tenant_keys are independent
// hand-copies of one rule, read by different gates (the tenant-api write gate
// and CI's config validator). Two suites that each assert their own side can
// both be green while disagreeing — that disagreement IS #1189, the defect
// this change exists to end. So neither side asserts the other: both assert
// tests/shared/optional_overrides_membership_matrix.json, which makes their
// agreement transitive instead of merely claimed.
//
// It earns its keep on the composite shapes. The two cascades order their
// branches differently — Go tests the _critical and dimensional shapes BEFORE
// membership, Python tests membership first — so any key that is both
// "declared" and composite is a place where one side silently shadows the
// other. Three such shapes diverged when this table was first drawn.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type membershipMatrix struct {
	Defaults []string `json:"defaults"`
	Cases    []struct {
		Name     string   `json:"name"`
		Declared []string `json:"declared"`
		Key      string   `json:"key"`
		Verdict  string   `json:"verdict"`
		Why      string   `json:"why"`
	} `json:"cases"`
}

func TestMembershipParityMatrix(t *testing.T) {
	t.Parallel()

	root := findRepoRootForRegistry(t)
	path := filepath.Join(root, "tests", "shared", "optional_overrides_membership_matrix.json")
	raw, err := os.ReadFile(path) // #nosec G304 -- repo-relative test fixture
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m membershipMatrix
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	// A fixture that silently shrinks to nothing would make this pass forever
	// while measuring nothing — the empty-set failure mode this repo keeps
	// finding in its own gates.
	if len(m.Cases) < 20 {
		t.Fatalf("matrix looks truncated: %d cases", len(m.Cases))
	}

	defaults := make(map[string]float64, len(m.Defaults))
	for _, k := range m.Defaults {
		defaults[k] = 80
	}

	for _, tc := range m.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			t.Parallel()
			switch tc.Verdict {
			case "accept", "refuse":
			default:
				t.Fatalf("unknown verdict %q — the matrix is the contract, typos in it are silent", tc.Verdict)
			}

			cfg := &ThresholdConfig{
				Defaults:          defaults,
				OptionalOverrides: tc.Declared,
				Tenants: map[string]map[string]ScheduledValue{
					"t-1": {tc.Key: {Default: "5"}},
				},
			}

			// accept == no BLOCKING message. Deprecation Notices are advisory
			// on both sides and never count as a refusal.
			errs := cfg.ValidateTenantKeys().Errors
			accepted := len(errs) == 0

			if accepted != (tc.Verdict == "accept") {
				t.Fatalf("verdict mismatch for key %q with declared=%v\n  want: %s\n  got:  %s\n  why the matrix says so: %s\n  errors: %v",
					tc.Key, tc.Declared, tc.Verdict, verdictWord(accepted), tc.Why, errs)
			}
		})
	}
}

func verdictWord(accepted bool) string {
	if accepted {
		return "accept"
	}
	return "refuse"
}
