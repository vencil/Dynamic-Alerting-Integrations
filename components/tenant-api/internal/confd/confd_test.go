package confd

import (
	"testing"
	"testing/quick"

	"github.com/vencil/threshold-exporter/pkg/confdname"
)

// TestIsAddressableTenantIDRejectsSeparators pins the gap this predicate
// exists for: IsTenantConfigFile answers a NAME question and says yes to ids
// carrying separators, so a write site gating on it alone builds a path it did
// not mean to (#1681).
func TestIsAddressableTenantIDRejectsSeparators(t *testing.T) {
	for _, tc := range []struct {
		id   string
		want bool
	}{
		{"db-a", true},
		{"Upper", true},
		{"", false},
		{"a/b", false},
		{`a\b`, false},
		{"..", false},
		{"../x", false},
		{"a/../../b", false},
		// ⛔ ".." with NO separator. The separator check does not catch these,
		// so they are the only inputs the ".." clause decides on its own —
		// measured: deleting that clause turned nothing red until these rows
		// existed. They are rejected to keep this predicate's accept set
		// identical to handler.ValidateTenantID's, which has always refused
		// them (#1911 family: two planes, one namespace).
		{"a..b", false},
		{"..a", false},
		{"/abs", false},
		{"_defaults", false},
		{".hidden", false},
	} {
		if got := IsAddressableTenantID(tc.id); got != tc.want {
			t.Errorf("IsAddressableTenantID(%q) = %v, want %v", tc.id, got, tc.want)
		}
		// Every id this predicate accepts must also satisfy the name rule the
		// scanners skip on — the two namespaces stay structurally equal.
		if tc.want && !IsTenantConfigFile(tc.id+".yaml") {
			t.Errorf("%q is addressable but would not be scanned", tc.id)
		}
	}
}

// TestValidatorCallShapeDidNotWiden pins the security-relevant half of #1537.
//
// The enumerators pass a real directory entry name, so folding the extension
// really did change their answer — that is the fix. The VALIDATORS
// (handler.ValidateTenantID, gitops.Writer) instead synthesise `id + ".yaml"`,
// whose suffix is already lowercase, so the extension half of the name rule
// (confdname.TenantNamedBy) succeeds for EVERY id and acceptance is decided by
// the reserved and hidden prefixes alone. That is what "the write-accepted
// namespace did not widen" means concretely, and it is asserted rather than
// argued: if someone later makes the extension test able to REJECT a
// synthesised name, or makes a prefix test case-sensitive in a way that lets
// `_x` through, this goes red instead of quietly handing a caller the ability
// to address a platform control file.
func TestValidatorCallShapeDidNotWiden(t *testing.T) {
	t.Parallel()

	corpus := []string{
		"db-a", "prod-01", "tenant_123", "PROD-01", "UPPER", "MiXeD",
		"_", "_rbac", "_domain_policy", "_defaults", "_DEFAULTS", "_A",
		".git", ".hidden", ".", "..", "...", ".a",
		"", "a..b", "_x.disabled", ".config",
		"foo.yaml", "foo.YAML", "FOO.YML", "x.YaMl", "a.", "A_",
		"Ünïcode", "日本語", "İ", "ẞ",
	}
	check := func(id string) bool {
		name := id + ".yaml"
		// The only way a synthesised name can be refused is a prefix rule.
		return IsTenantConfigFile(name) == (!confdname.IsReserved(name) && !confdname.IsHidden(name))
	}
	for _, id := range corpus {
		if !check(id) {
			t.Errorf("id %q: on the synthesised name %q the extension test changed the "+
				"verdict — the validator's accepted set is no longer decided by the "+
				"prefix rules alone", id, id+".yaml")
		}
	}
	if err := quick.Check(check, nil); err != nil {
		t.Errorf("property violated on a generated id — the validator call shape is no "+
			"longer extension-independent: %v", err)
	}
}
