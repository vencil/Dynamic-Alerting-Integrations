package handler

import (
	"testing"
	"testing/quick"

	"github.com/vencil/tenant-api/internal/confd"
)

func TestValidateTenantID(t *testing.T) {
	t.Parallel()
	tests := []struct {
		id      string
		wantErr bool
	}{
		// Valid
		{"db-a", false},
		{"prod-mariadb-01", false},
		{"tenant_123", false},

		// Empty
		{"", true},

		// Path traversal
		{"../etc/passwd", true},
		{"..%2f..%2fetc", true}, // %2f decoded by chi before reaching handler
		{"foo/../bar", true},
		{"..", true},

		// Slashes
		{"foo/bar", true},
		{"foo\\bar", true},

		// Hidden files
		{".hidden", true},
		{".git", true},

		// Reserved control-file prefix (scanner skips "_" files)
		{"_domain_policy", true},
		{"_rbac", true},
		{"_defaults", true},
		{"_", true},

		// filepath.Base mismatch
		{".", true},
	}

	for _, tt := range tests {
		t.Run(tt.id, func(t *testing.T) {
			t.Parallel()
			err := ValidateTenantID(tt.id)
			if (err != nil) != tt.wantErr {
				t.Errorf("ValidateTenantID(%q) error = %v, wantErr %v", tt.id, err, tt.wantErr)
			}
		})
	}
}

// TestValidateTenantID_AcceptedIDsAreTenantFiles pins the load-bearing security
// implication: every id ValidateTenantID ACCEPTS must map to a file the conf.d
// scanners would pick up as a tenant (confd.IsTenantConfigFile) — i.e. no
// accepted id can name a reserved control file. It is a one-directional
// implication, NOT equality: ValidateTenantID is stricter (it also rejects
// separators / ".." / non-base names), so many ids are rejected by the
// validator yet would still be a "tenant file" by name alone.
//
// This is the real anti-drift guard the (deleted) tautological confd invariant
// test failed to be: if a future edit makes ValidateTenantID stop delegating to
// confd (e.g. reverts to a hand-rolled partial prefix check), the reserved
// corpus below flips it red.
func TestValidateTenantID_AcceptedIDsAreTenantFiles(t *testing.T) {
	t.Parallel()

	// Explicit corpus — includes the shapes a hand-rolled check tends to miss.
	corpus := []string{
		"db-a", "prod-01", "tenant_123", // accepted, real tenants
		"_", "_rbac", "_domain_policy", "_defaults", // reserved: must be rejected
		".git", ".hidden", "..", // hidden / traversal
		"a/b", "a\\b", "", "a..b", // separators / empty / embedded ".."
		"_x.disabled", ".config", "foo.yaml", // extension / prefix edge shapes
	}
	assertImplication := func(id string) {
		if ValidateTenantID(id) == nil && !confd.IsTenantConfigFile(id+".yaml") {
			t.Errorf("id %q: ValidateTenantID ACCEPTS it but confd would NOT scan %q.yaml as a tenant — an accepted id names a control file (namespaces diverged)", id, id)
		}
	}
	for _, id := range corpus {
		assertImplication(id)
	}

	// Property-based breadth: no randomly-generated id may violate the
	// implication either. quick returns false to signal a counterexample.
	if err := quick.Check(func(id string) bool {
		if ValidateTenantID(id) == nil {
			return confd.IsTenantConfigFile(id + ".yaml")
		}
		return true
	}, nil); err != nil {
		t.Errorf("property violated — an accepted id does not map to a tenant file: %v", err)
	}
}

// TestValidateTenantIDAgreesWithTheSharedPredicate replaces a delegation that
// used to sit at the end of ValidateTenantID as a belt. Measured over this
// corpus it never fired — an unreachable branch, not a guard — so the property
// it was reaching for is asserted here instead: whatever this function accepts,
// the predicate the write plane gates on must accept too, and vice versa. A
// divergence in EITHER direction means the write-accepted id namespace and the
// scanned-file namespace have come apart, which is the #1339 family shape.
func TestValidateTenantIDAgreesWithTheSharedPredicate(t *testing.T) {
	corpus := []string{
		"", "db-a", "Upper", "a/b", `a\b`, "..", "../x", "a/../b", "a..b", "...",
		"/abs", "_defaults", ".hidden", "x.yaml", "x.yml", " ", "a b", "日本語",
		"a.", ".", "..a", "a..", "UPPER.YAML", "x/", "/", `\`, "a-b_c.d",
	}
	agreed := 0
	for _, id := range corpus {
		accepted := ValidateTenantID(id) == nil
		predicate := confd.IsAddressableTenantID(id)
		if accepted != predicate {
			t.Errorf("id %q: ValidateTenantID accepts=%v but IsAddressableTenantID=%v",
				id, accepted, predicate)
			continue
		}
		agreed++
	}
	if agreed != len(corpus) {
		t.Errorf("agreed on %d of %d inputs", agreed, len(corpus))
	}
	// Anti-vacuity: a corpus that only contains rejects would agree trivially.
	accepts := 0
	for _, id := range corpus {
		if ValidateTenantID(id) == nil {
			accepts++
		}
	}
	if accepts < 3 {
		t.Fatalf("corpus has only %d accepted ids — it cannot show agreement on the accept side", accepts)
	}
}
