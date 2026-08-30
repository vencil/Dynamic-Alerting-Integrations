package confdname

// confdname_matrix_test.go — the predicate-level pin for this package.
//
// ⛔ WHY THIS EXISTS SEPARATELY from the allocator's parity test. That test
// drives these predicates through `AllocateFiles` and asserts the ROUTING
// outcome, which is the right thing to pin for the allocator — but it cannot
// see a predicate that is wrong in a way the routing happens to absorb. A
// mutation reviewer measured exactly that: deleting the reserved-prefix rule
// from `TenantNamedBy` left every test in this change GREEN, and `SplitCarrier`
// disagreed with the matrix on `.yaml` for the same reason (the name still came
// out dropped, just for a reason that told the operator their `.yaml` file has
// no YAML extension).
//
// This package is the declared single copy of these rules for the Go write
// plane. A shared rule with no test of its own is the next component's
// divergence waiting to happen, so every predicate is checked against every
// column of the shared matrix, cell by cell.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type matrixRow struct {
	Name           string `json:"name"`
	YAMLExtension  bool   `json:"yaml_extension"`
	ReservedPrefix bool   `json:"reserved_prefix"`
	Hidden         bool   `json:"hidden"`
	DefaultsFile   bool   `json:"defaults_file"`
	Stem           string `json:"stem"`
	Why            string `json:"why"`
}

// minRows sits below the shipped row count on purpose — a redundant row may
// legitimately be dropped — but a gutted matrix must not keep this pin green
// while measuring nothing.
const minRows = 18

func loadMatrix(t *testing.T) []matrixRow {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	var root string
	for {
		if fi, err := os.Stat(filepath.Join(dir, "tests", "shared")); err == nil && fi.IsDir() {
			if fi2, err2 := os.Stat(filepath.Join(dir, "Makefile")); err2 == nil && !fi2.IsDir() {
				root = dir
				break
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("could not locate repo root walking up from %s", dir)
		}
		dir = parent
	}
	raw, err := os.ReadFile(filepath.Join(root, "tests", "shared", "confd_name_classification_matrix.json")) // #nosec G304 -- repo-relative test fixture
	if err != nil {
		t.Fatalf("read name classification matrix: %v", err)
	}
	var doc struct {
		Rows []matrixRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal name classification matrix: %v", err)
	}
	if len(doc.Rows) < minRows {
		t.Fatalf("matrix shrank to %d rows (floor %d) — these predicates would still "+
			"be compared against a table, and the comparison would prove nothing",
			len(doc.Rows), minRows)
	}
	return doc.Rows
}

// TestPredicatesMatchEveryMatrixCell checks each predicate against the column
// it restates, for every row. One subtest per row so a failure names the name.
func TestPredicatesMatchEveryMatrixCell(t *testing.T) {
	t.Parallel()
	for _, r := range loadMatrix(t) {
		r := r
		t.Run(r.Name, func(t *testing.T) {
			t.Parallel()

			if _, ok := SplitCarrier(r.Name); ok != r.YAMLExtension {
				t.Errorf("SplitCarrier(%q) ok = %t, matrix yaml_extension = %t\n"+
					"  ⛔ carrying a YAML extension and being READ as a tenant carrier "+
					"are different questions — a hidden or reserved name still has the "+
					"extension, and saying otherwise puts a false reason in front of "+
					"the operator.\n  matrix why: %s",
					r.Name, ok, r.YAMLExtension, r.Why)
			}
			if got := IsHidden(r.Name); got != r.Hidden {
				t.Errorf("IsHidden(%q) = %t, matrix hidden = %t\n  matrix why: %s",
					r.Name, got, r.Hidden, r.Why)
			}
			if got := IsReserved(r.Name); got != r.ReservedPrefix {
				t.Errorf("IsReserved(%q) = %t, matrix reserved_prefix = %t\n  matrix why: %s",
					r.Name, got, r.ReservedPrefix, r.Why)
			}
			if got := IsDefaults(r.Name); got != r.DefaultsFile {
				t.Errorf("IsDefaults(%q) = %t, matrix defaults_file = %t\n"+
					"  ⛔ the chain carrier is the two whole literals, never a "+
					"`_defaults` PREFIX.\n  matrix why: %s",
					r.Name, got, r.DefaultsFile, r.Why)
			}

			wantTenant := r.YAMLExtension && !r.ReservedPrefix && !r.Hidden
			gotID, gotOK := TenantNamedBy(r.Name)
			if gotOK != wantTenant {
				t.Errorf("TenantNamedBy(%q) ok = %t, matrix (yaml AND NOT reserved AND "+
					"NOT hidden) = %t\n  matrix why: %s",
					r.Name, gotOK, wantTenant, r.Why)
			}
			if gotOK && gotID != r.Stem {
				t.Errorf("TenantNamedBy(%q) id = %q, matrix stem = %q\n"+
					"  ⛔ the extension is folded, the stem is NOT — folding it would "+
					"rename the tenant on the write plane.\n  matrix why: %s",
					r.Name, gotID, r.Stem, r.Why)
			}
		})
	}
}

// TestMatrixStillCoversWhatThesePredicatesCanGetWrong is the anti-truncation
// floor for this package.
//
// ⛔ THE LAST TWO CLAUSES ARE HERE BECAUSE A REVIEWER MEASURED THEIR ABSENCE.
// Deleting five rows that no existing coverage clause named still satisfied
// every floor in the repo, and on that gutted matrix two mutants that model
// REAL historical bugs — a `_defaults` PREFIX implementation (shipped briefly
// in #1588) and computing the stem offset against a lowercased copy (the
// Turkish-İ length change) — both went green.
func TestMatrixStillCoversWhatThesePredicatesCanGetWrong(t *testing.T) {
	t.Parallel()
	rows := loadMatrix(t)

	cases := []struct {
		why   string
		match func(matrixRow) bool
	}{
		{
			why: "a name whose extension is upper or mixed case — without one, " +
				"folding the extension proves nothing",
			match: func(r matrixRow) bool {
				return r.YAMLExtension && r.Name != strings.ToLower(r.Name)
			},
		},
		{
			why: "a `.yml` name — every predicate here has two extension branches " +
				"and a fix applied to one leaves the other broken",
			match: func(r matrixRow) bool {
				return strings.HasSuffix(strings.ToLower(r.Name), ".yml")
			},
		},
		{
			why: "a mixed-case STEM on the `.yaml` branch specifically — the two " +
				"extension branches are separate code paths in every classifier " +
				"here, and a reviewer measured that with only ONE mixed-stem row " +
				"the floors stayed green while a fold applied to just the other " +
				"branch silently renamed a tenant on the write plane",
			match: func(r matrixRow) bool {
				return r.YAMLExtension && !r.ReservedPrefix && !r.Hidden &&
					strings.HasSuffix(strings.ToLower(r.Name), ".yaml") &&
					r.Stem != strings.ToLower(r.Stem)
			},
		},
		{
			why: "a mixed-case STEM on the `.yml` branch specifically — the two " +
				"extension branches are separate code paths in every classifier " +
				"here, and a reviewer measured that with only ONE mixed-stem row " +
				"the floors stayed green while a fold applied to just the other " +
				"branch silently renamed a tenant on the write plane",
			match: func(r matrixRow) bool {
				return r.YAMLExtension && !r.ReservedPrefix && !r.Hidden &&
					strings.HasSuffix(strings.ToLower(r.Name), ".yml") &&
					r.Stem != strings.ToLower(r.Stem)
			},
		},
		{
			why: "a name that IS exactly a YAML extension (`.yaml`) — it carries the " +
				"extension and is excluded by being hidden, which is the one row that " +
				"separates `has an extension` from `is a tenant carrier`",
			match: func(r matrixRow) bool {
				for _, ext := range yamlSuffixes {
					if strings.EqualFold(r.Name, ext) {
						return true
					}
				}
				return false
			},
		},
		{
			why: "a tenant carrier whose STEM changes BYTE LENGTH when lowercased " +
				"(e.g. Turkish İ) — the row that catches a stem offset computed " +
				"against a lowercased copy, and the one whose absence let that " +
				"mutant go green on a gutted matrix",
			match: func(r matrixRow) bool {
				return r.YAMLExtension && !r.ReservedPrefix && !r.Hidden &&
					len(r.Stem) != len(strings.ToLower(r.Stem))
			},
		},
		{
			why: "a name that starts with `_defaults` but is NOT the chain carrier " +
				"— the row that catches a PREFIX implementation of IsDefaults, which " +
				"this repo actually shipped once (#1588)",
			match: func(r matrixRow) bool {
				return strings.HasPrefix(strings.ToLower(r.Name), "_defaults") && !r.DefaultsFile
			},
		},
		{
			why: "a reserved-prefix name that is NOT the chain carrier — separates " +
				"`reserved` from `defaults`",
			match: func(r matrixRow) bool { return r.ReservedPrefix && !r.DefaultsFile },
		},
		{
			why: "a hidden name that also carries a YAML extension — keeps `hidden` " +
				"and `yaml_extension` from being conflated",
			match: func(r matrixRow) bool { return r.Hidden && r.YAMLExtension },
		},
		{
			why:   "a name with no YAML extension at all",
			match: func(r matrixRow) bool { return !r.YAMLExtension },
		},
	}

	for _, c := range cases {
		found := false
		for _, r := range rows {
			if c.match(r) {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("the matrix no longer contains a row for: %s", c.why)
		}
	}
}
