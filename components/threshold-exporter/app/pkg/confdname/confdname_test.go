package confdname

import (
	"strings"
	"testing"
	"unicode"
)

// TestNameRuleTable is the plain table of this package's answers, one row per
// name or path. The shared matrix (confdname_matrix_test.go) pins the name
// predicates column by column; these rows add what the matrix has no column
// for — the path rule (HasHiddenSegment, IsScannedPath) and IsScanned — and
// the tenant-file rows tenant-api's own copy of the rule carried until #3206.
func TestNameRuleTable(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name    string
		scanned bool   // IsScanned
		tenant  string // TenantNamedBy id; "" = not a tenant file
	}{
		{"db-a.yaml", true, "db-a"},
		{"prod-mariadb-01.yml", true, "prod-mariadb-01"},
		{"tenant_123.yaml", true, "tenant_123"}, // a mid-string underscore is fine
		{"Upper.YAML", true, "Upper"},
		{"_defaults.yaml", true, ""},
		{"_rbac.yaml", true, ""},
		{"_domain_policy.yaml", true, ""},
		{"_routing_profiles.yaml", true, ""},
		{"_.yaml", true, ""},
		{".hidden.yaml", false, ""},
		{".gitkeep", false, ""},
		{".yaml", false, ""},
		{"README.md", false, ""},
		{"db-a", false, ""},
		{"db-a.json", false, ""},
		{"db-a.yaml.bak", false, ""},
		// The last extension is the one stripped: `a.yml.yaml` is tenant
		// `a.yml`, never `a` (tenant-api's findingReadable relies on it).
		{"a.yml.yaml", true, "a.yml"},
		{"a.yaml.yml", true, "a.yaml"},
	} {
		if got := IsScanned(tc.name); got != tc.scanned {
			t.Errorf("IsScanned(%q) = %v, want %v", tc.name, got, tc.scanned)
		}
		id, ok := TenantNamedBy(tc.name)
		if ok != (tc.tenant != "") || id != tc.tenant {
			t.Errorf("TenantNamedBy(%q) = (%q, %v), want %q", tc.name, id, ok, tc.tenant)
		}
	}

	for _, tc := range []struct {
		rel            string
		hidden, reader bool // HasHiddenSegment, IsScannedPath
	}{
		{"", false, false}, // the root itself is never pruned
		{"a.yaml", false, true},
		{"a/b.yaml", false, true},
		{"a/b/c.yml", false, true},
		{"sub./x.yaml", false, true}, // a trailing dot is not a prefix
		{"./a.yaml", false, true},    // `.` is the current directory, not a hidden name
		{"a\\b.yaml", false, true},   // either separator (filepath.ToSlash) — on unix `\` is a name byte
		{"_team/t.yaml", false, true},
		{".git/a.yaml", true, false},
		{".cache/deep/x.yaml", true, false}, // every segment, not only the parent
		{"a/.b/c/d.yaml", true, false},
		{"a/b/.c.yaml", true, false},
		{"..odd/x.yaml", true, false},
		{"../x.yaml", true, false}, // leaves the root
		{"a/notes.txt", false, false},
	} {
		if got := HasHiddenSegment(tc.rel); got != tc.hidden {
			t.Errorf("HasHiddenSegment(%q) = %v, want %v", tc.rel, got, tc.hidden)
		}
		if got := IsScannedPath(tc.rel); got != tc.reader {
			t.Errorf("IsScannedPath(%q) = %v, want %v", tc.rel, got, tc.reader)
		}
	}
}

// TestPrefixesNeedNoCaseFolding is the measurement behind IsHidden and
// IsReserved NOT folding, kept as a test rather than as a sentence: sweeping
// the whole Unicode range, no code point's lower- or upper-case image starts
// with '_' or '.' unless it already is '_' or '.'. (Moved from tenant-api,
// whose copy of the rule it used to justify, #1537 → #3206.)
func TestPrefixesNeedNoCaseFolding(t *testing.T) {
	t.Parallel()
	for r := rune(0); r <= unicode.MaxRune; r++ {
		if r >= 0xD800 && r <= 0xDFFF || r == '_' || r == '.' {
			continue
		}
		s := string(r)
		for _, folded := range []string{strings.ToLower(s), strings.ToUpper(s)} {
			if strings.HasPrefix(folded, "_") || strings.HasPrefix(folded, ".") {
				t.Fatalf("U+%04X folds to %q, which starts with a reserved or hidden "+
					"prefix — IsHidden / IsReserved must fold too", r, folded)
			}
		}
	}
}

// TestExtensionBytesHaveNoNonASCIIFoldImage is why SplitCarrier's EqualFold on
// the last bytes and `HasSuffix(ToLower(name), ext)` — the relation the walker
// and tenant-api used before #3206 — give one answer on every name: no code
// point outside ASCII lowercases to, or shares a simple-fold orbit with, a
// byte of `.yaml` / `.yml`. (Contrast `s`, whose orbit holds U+017F, which is
// why IsDefaults compares ToLower and not EqualFold.)
func TestExtensionBytesHaveNoNonASCIIFoldImage(t *testing.T) {
	t.Parallel()
	const extBytes = ".yamlYAML"
	for _, c := range extBytes {
		for f := unicode.SimpleFold(c); f != c; f = unicode.SimpleFold(f) {
			if f > unicode.MaxASCII {
				t.Errorf("%q shares a fold orbit with U+%04X — SplitCarrier's EqualFold "+
					"and a ToLower suffix test now differ", c, f)
			}
		}
	}
	for r := rune(unicode.MaxASCII + 1); r <= unicode.MaxRune; r++ {
		if r >= 0xD800 && r <= 0xDFFF {
			continue
		}
		if strings.ContainsAny(strings.ToLower(string(r)), extBytes) {
			t.Errorf("U+%04X lowercases to %q, which holds a byte of a YAML extension",
				r, strings.ToLower(string(r)))
		}
	}
}
