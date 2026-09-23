package config

// defaults_carriers_test.go — the per-directory carrier selection (#1674, B8)
// as a pure function over a defaults SET, so the rule's cells that need two
// case variants of ONE name (which a case-insensitive filesystem cannot hold,
// and which tree_scan_parity_test.go therefore cannot build everywhere) are
// pinned on every platform. The same rule is restated in Python as
// `_lib_confd.select_defaults_carrier` and pinned there by
// tests/shared/test_defaults_carrier_selection.py against this table's rows.
//
// Seams: none — pure function, synthetic paths.

import (
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestSelectDefaultsCarriers_OneRulePerDirectory(t *testing.T) {
	t.Parallel()
	root := filepath.FromSlash("/conf.d")
	cases := []struct {
		name  string
		names []string // carriers in ONE directory
		want  string   // the one the chain reads
	}{
		{"single lower-case", []string{"_defaults.yaml"}, "_defaults.yaml"},
		{"single upper-case .yaml is a carrier", []string{"_DEFAULTS.YAML"}, "_DEFAULTS.YAML"},
		{"single upper-case .yml is a carrier", []string{"_DEFAULTS.YML"}, "_DEFAULTS.YML"},
		{".yaml beats .yml (lower-case pair)", []string{"_defaults.yaml", "_defaults.yml"}, "_defaults.yaml"},
		{".yaml beats .yml even when the .yml sorts first", []string{"_DEFAULTS.YML", "_defaults.yaml"}, "_defaults.yaml"},
		{"folded .yaml beats lower-case .yml", []string{"_DEFAULTS.YAML", "_defaults.yml"}, "_DEFAULTS.YAML"},
		// ⚠️ THE ASYMMETRY the owner ruling keeps: LAST .yaml, FIRST .yml.
		{"two .yaml case variants: the LAST in walk order", []string{"_Defaults.yaml", "_defaults.YAML"}, "_defaults.YAML"},
		{"two .yml case variants: the FIRST in walk order", []string{"_DEFAULTS.YML", "_defaults.yml"}, "_DEFAULTS.YML"},
	}
	for _, tc := range cases {
		set := map[string]bool{}
		for _, n := range tc.names {
			set[filepath.Join(root, n)] = true
		}
		sel := SelectDefaultsCarriers(set)
		if got := sel.ByDir[root]; got != filepath.Join(root, tc.want) {
			t.Errorf("%s: chain reads %q, want %q", tc.name, filepath.Base(got), tc.want)
		}
		wantAmbiguous := len(tc.names) > 1
		if _, amb := sel.Ambiguous[root]; amb != wantAmbiguous {
			t.Errorf("%s: Ambiguous[root] present=%v, want %v", tc.name, amb, wantAmbiguous)
		}
	}
}

// The POSIX flavour (in-memory simulate source) must pick what the native
// one picks: one rule, two path libraries.
func TestSelectDefaultsCarriers_POSIXAgreesWithNative(t *testing.T) {
	t.Parallel()
	set := map[string]bool{"/sim/a/_defaults.yml": true, "/sim/a/_DEFAULTS.YAML": true, "/sim/_defaults.yaml": true}
	got := selectDefaultsCarriers(set, posixPathOps)
	want := map[string]string{"/sim/a": "/sim/a/_DEFAULTS.YAML", "/sim": "/sim/_defaults.yaml"}
	if !reflect.DeepEqual(got.ByDir, want) {
		t.Errorf("POSIX ByDir = %v, want %v", got.ByDir, want)
	}
	if chain := CollectDefaultsChainPOSIX("/sim/a", "/sim", set); !reflect.DeepEqual(chain, []string{"/sim/_defaults.yaml", "/sim/a/_DEFAULTS.YAML"}) {
		t.Errorf("POSIX chain = %v", chain)
	}
}

// ⛔ The WARN text is asserted, not just its presence: it is the only signal
// an operator gets that a file in their tree is ignored on every plane.
func TestDefaultsCarriers_AmbiguityWarningNamesEveryFile(t *testing.T) {
	t.Parallel()
	root := filepath.FromSlash("/conf.d")
	sub := filepath.Join(root, "sub")
	set := map[string]bool{
		filepath.Join(root, "_defaults.yaml"): true,
		filepath.Join(root, "_defaults.yml"):  true,
		filepath.Join(sub, "_defaults.yaml"):  true, // unambiguous: no line
	}
	w := SelectDefaultsCarriers(set).AmbiguityWarnings()
	if len(w) != 1 {
		t.Fatalf("want exactly one WARN (one ambiguous directory), got %d: %q", len(w), w)
	}
	for _, frag := range []string{"WARN:", root, "2 defaults carriers", "_defaults.yaml, _defaults.yml", "only _defaults.yaml is read", "_defaults.yml is ignored"} {
		if !strings.Contains(w[0], frag) {
			t.Errorf("WARN %q lacks %q", w[0], frag)
		}
	}
}
