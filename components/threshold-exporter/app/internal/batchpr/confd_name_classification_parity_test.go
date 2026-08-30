package batchpr

// confd_name_classification_parity_test.go — the WRITE-PLANE half of the
// cross-language conf.d name-classification pin (#1605, the #1339 family).
//
// The two halves that already exist (threshold-exporter's
// confd_name_classification_parity_test.go and tenant-api's
// name_classification_parity_test.go) both pin READERS of an existing conf.d
// tree. This half pins the one component that DECIDES WHAT GETS WRITTEN INTO
// that tree: AllocateFiles buckets each emitted carrier into the pull request
// that will commit it.
//
// ⛔ Neither side asserts the other, and no side reads the other's source.
// This repo measured (#1448 blind review) that a guard asserting things about
// another language's source text goes red on legitimate refactors and states
// the opposite of the truth when it does. Every consumer asserts the shared
// matrix instead, which makes their agreement transitive rather than claimed.
//
// ⛔ WHY THE MATRIX IS THE RIGHT ORACLE HERE, even though this package never
// walks a conf.d tree. The files AllocateFiles routes are PROPOSED CONF.D
// CARRIERS: the PR it picks is the PR that commits them into the tree the
// exporter reads. So a carrier this allocator sends to the wrong PR — or drops
// — is a carrier whose fate disagrees with what the exporter would do with the
// very same name. That is the #1339 shape (one tree, several enumerators,
// different selection rules, silent divergence) on the WRITE side, where the
// consequence is not a stale report but a threshold change that never reaches
// production, or one committed into a file production ignores.
//
// The three projections this allocator implements, stated against the matrix:
//
//	Base PR bucket   == defaults_file
//	tenant chunk     == yaml_extension AND NOT reserved_prefix AND NOT hidden
//	                    (and only into the chunk that owns the row's `stem`)
//	dropped+warned   == everything else
//
// ⛔ `stem` here is the tenant a carrier is NAMED FOR, which is a write-plane
// convention (the emitter writes `safeFilename(id)+".yaml"`, this allocator
// reads the id back out). It is NOT "the tenant the exporter serves out of
// this file" — measured, the exporter takes identity from the `tenants:` keys
// inside the document and never from a filename. What the exporter's name
// rules decide, and all this test holds the allocator to, is CLASSIFICATION:
// is the file read at all, and is it the chain carrier.
//
// ⛔ THE PLAN THIS TEST BUILDS IS DELIBERATELY ADVERSARIAL. A plan's tenant
// ids do NOT come from conf.d stems — `ProposalRef.MemberTenantIDs` is
// documented (types.go) as computed by the caller "from the underlying
// ParsedRule.Labels", i.e. from Prometheus label VALUES, which are arbitrary
// UTF-8 strings. `_rbac`, `.hidden` and `_defaults` are all legal there. So
// the plan below offers this allocator every id it could possibly mis-route
// to: both each row's oracle `stem` AND the naive id a `TrimSuffix` recovers
// from the name. Measured before this pin existed: with a narrower plan the
// reserved- and hidden-prefixed rows came out DROPPED and looked correct, but
// that agreement was an accident of the plan, not a rule this allocator has —
// it holds no reserved-prefix and no hidden rule at all.
//
// The fixture holds CONTENT constant — every file is the same byte string — so
// each answer is attributable to the NAME rather than to parsing.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

type confdNameRow struct {
	Name           string `json:"name"`
	YAMLExtension  bool   `json:"yaml_extension"`
	ReservedPrefix bool   `json:"reserved_prefix"`
	Hidden         bool   `json:"hidden"`
	DefaultsFile   bool   `json:"defaults_file"`
	Stem           string `json:"stem"`
	Why            string `json:"why"`
}

// isTenantCarrier is the exporter's `tenants` projection, restated. Kept as a
// method rather than inlined so a reader can compare it side by side with the
// identical method in the other two halves.
func (r confdNameRow) isTenantCarrier() bool {
	return r.YAMLExtension && !r.ReservedPrefix && !r.Hidden
}

// minConfdMatrixRows sits below the shipped row count on purpose — a redundant
// row may legitimately be dropped — but a gutted matrix must not keep every
// consumer green while measuring nothing.
const minConfdMatrixRows = 18

func findRepoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if fi, err := os.Stat(filepath.Join(dir, "tests", "shared")); err == nil && fi.IsDir() {
			if fi2, err2 := os.Stat(filepath.Join(dir, "Makefile")); err2 == nil && !fi2.IsDir() {
				return dir
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("could not locate repo root (a dir with tests/shared/ + Makefile) walking up from %s", dir)
		}
		dir = parent
	}
}

func loadConfdNameMatrix(t *testing.T) []confdNameRow {
	t.Helper()
	path := filepath.Join(findRepoRoot(t), "tests", "shared", "confd_name_classification_matrix.json")
	raw, err := os.ReadFile(path) // #nosec G304 -- repo-relative test fixture
	if err != nil {
		t.Fatalf("read name classification matrix: %v", err)
	}
	var doc struct {
		Rows []confdNameRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal name classification matrix: %v", err)
	}
	if len(doc.Rows) < minConfdMatrixRows {
		t.Fatalf("matrix shrank to %d rows (floor %d) — this allocator would still be "+
			"compared against a table, and the comparison would prove nothing: the "+
			"empty-set silence #1339 and #1537 are both about",
			len(doc.Rows), minConfdMatrixRows)
	}
	return doc.Rows
}

// TestConfdNameMatrixCoversThisAllocatorsProjections is the anti-truncation
// floor for this half. A row count alone is not enough — twenty all-lowercase
// rows would satisfy it while deleting every case the pin exists for — so the
// floor names the shapes each of the three projections needs in order to be
// able to fail at all.
func TestConfdNameMatrixCoversThisAllocatorsProjections(t *testing.T) {
	t.Parallel()
	rows := loadConfdNameMatrix(t)

	cases := []struct {
		why   string
		match func(confdNameRow) bool
	}{
		{
			why: "a defaults carrier spelled other than all-lowercase `.yaml` — " +
				"without one, folding the Base-PR rule proves nothing",
			match: func(r confdNameRow) bool {
				return r.DefaultsFile && r.Name != strings.ToLower(r.Name)
			},
		},
		{
			why: "a defaults carrier spelled `.yml` — the Base-PR rule has two " +
				"extension branches and a fix applied to one leaves the other broken",
			match: func(r confdNameRow) bool {
				return r.DefaultsFile && strings.HasSuffix(strings.ToLower(r.Name), ".yml")
			},
		},
		{
			why: "a tenant carrier spelled `.yml` — the exporter merges it, so a " +
				"chunk that will not carry it proposes thresholds for a live tenant " +
				"and leaves them out of every PR",
			match: func(r confdNameRow) bool {
				return r.isTenantCarrier() && strings.HasSuffix(strings.ToLower(r.Name), ".yml")
			},
		},
		{
			why: "a tenant carrier whose extension is upper/mixed case while its " +
				"STEM is mixed case too — pins that the extension is folded and the " +
				"id is NOT, which is what keeps the write plane from renaming a tenant",
			match: func(r confdNameRow) bool {
				return r.isTenantCarrier() &&
					r.Name != strings.ToLower(r.Name) &&
					r.Stem != strings.ToLower(r.Stem)
			},
		},
		{
			why: "a reserved-prefix carrier that is NOT the defaults file — the " +
				"exporter watches it but derives no tenant from it, so routing it " +
				"into a tenant chunk commits a carrier production ignores",
			match: func(r confdNameRow) bool {
				return r.ReservedPrefix && !r.DefaultsFile && r.YAMLExtension
			},
		},
		{
			why: "a hidden (dot-prefixed) carrier — the exporter's walker skips it " +
				"outright, so no PR should ever carry it",
			match: func(r confdNameRow) bool { return r.Hidden && r.YAMLExtension },
		},
		{
			why: "a non-YAML name — the drop branch needs something that is not a " +
				"carrier at all",
			match: func(r confdNameRow) bool { return !r.YAMLExtension },
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

// adversarialPlan builds a Plan whose single tenant chunk claims every id this
// allocator could plausibly recover from any matrix row: the oracle `stem` and
// the naive `TrimSuffix` id alike. See the header — a narrower plan makes the
// reserved / hidden rows pass for the wrong reason.
//
// Returns the plan plus the index of the Base item and of the tenant chunk.
func adversarialPlan(rows []confdNameRow) (*Plan, int, int) {
	seen := map[string]bool{}
	var ids []string
	add := func(s string) {
		if s == "" || seen[s] {
			return
		}
		seen[s] = true
		ids = append(ids, s)
	}
	for _, r := range rows {
		add(r.Stem)
		// The naive id: strip whatever YAML-ish extension the name carries.
		base := r.Name
		for _, ext := range []string{".yaml", ".yml"} {
			if len(base) > len(ext) && strings.EqualFold(base[len(base)-len(ext):], ext) {
				base = base[:len(base)-len(ext)]
				break
			}
		}
		add(base)
	}
	sort.Strings(ids)
	return &Plan{Items: []PlanItem{
		{Kind: PlanItemBase, Title: "base"},
		{Kind: PlanItemTenant, Title: "chunk", TenantIDs: ids},
	}}, 0, 1
}

// TestAllocateFilesMatchesNameMatrix is the pin itself. One emit set holding
// one file per matrix row goes through the exported AllocateFiles, and each
// row's destination is compared against what the exporter would do with that
// name.
func TestAllocateFilesMatchesNameMatrix(t *testing.T) {
	t.Parallel()
	rows := loadConfdNameMatrix(t)
	plan, baseIdx, tenantIdx := adversarialPlan(rows)

	const body = "the content is constant so every answer is attributable to the name\n"
	files := make(map[string][]byte, len(rows))
	for _, r := range rows {
		files[r.Name] = []byte(body)
	}
	if len(files) != len(rows) {
		t.Fatalf("matrix has %d rows but only %d distinct names — two rows collided "+
			"and this fixture would silently measure fewer cases than it claims",
			len(rows), len(files))
	}

	got, warnings := AllocateFiles(plan, files)

	// Invert the result: name → bucket index, or -1 when dropped.
	landed := make(map[string]int, len(rows))
	for _, r := range rows {
		landed[r.Name] = -1
	}
	for idx, bucket := range got {
		for name := range bucket {
			if prev, ok := landed[name]; ok && prev != -1 {
				t.Errorf("%q landed in two buckets (%d and %d)", name, prev, idx)
			}
			landed[name] = idx
		}
	}

	warned := func(name string) bool {
		for _, w := range warnings {
			if strings.Contains(w, fmt.Sprintf("%q", name)) {
				return true
			}
		}
		return false
	}

	for _, r := range rows {
		r := r
		t.Run(r.Name, func(t *testing.T) {
			switch {
			case r.DefaultsFile:
				if landed[r.Name] != baseIdx {
					t.Errorf("defaults carrier %q went to bucket %d, want the Base PR (%d).\n"+
						"  the exporter merges this file into the inheritance chain "+
						"(defaults projection), so the Base Infrastructure PR is the "+
						"only PR that may carry it.\n  matrix why: %s",
						r.Name, landed[r.Name], baseIdx, r.Why)
				}
			case r.isTenantCarrier():
				if landed[r.Name] != tenantIdx {
					t.Errorf("tenant carrier %q went to bucket %d, want the tenant chunk (%d).\n"+
						"  the exporter READS this file, the write plane NAMES it for tenant "+
						"%q, and the chunk claims that id — dropping it means the threshold "+
						"change reaches no PR at all while the report says the proposal was "+
						"applied.\n"+
						"  matrix why: %s",
						r.Name, landed[r.Name], tenantIdx, r.Stem, r.Why)
				}
			default:
				if landed[r.Name] != -1 {
					t.Errorf("%q went to bucket %d, want DROPPED.\n"+
						"  the exporter derives no tenant from this name (reserved=%t "+
						"hidden=%t yaml=%t), so a PR carrying it commits a file "+
						"production will not read as that tenant.\n  matrix why: %s",
						r.Name, landed[r.Name], r.ReservedPrefix, r.Hidden, r.YAMLExtension, r.Why)
				} else if !warned(r.Name) {
					t.Errorf("%q was dropped SILENTLY — no warning names it.\n"+
						"  a dropped carrier the operator is never told about is the "+
						"exact failure this family is about.\n  warnings were: %v",
						r.Name, warnings)
				}
			}
		})
	}
}
