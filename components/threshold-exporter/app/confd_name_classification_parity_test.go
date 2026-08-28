package main

// confd_name_classification_parity_test.go — the threshold-exporter half of
// the cross-language conf.d name-classification pin (#1537, the #1339 family).
//
// One conf.d tree is read by four independent enumerators with four
// hand-written name rules. The exporter is the ORACLE of that group: what it
// classifies is what /metrics serves, so when the write plane and the Python
// tools disagreed with it, they were the ones describing a configuration
// nobody was running. Measured before this pin existed: a root-level
// `upper.YAML` was read and merged here, was invisible to both Python readers,
// and was rejected by the write plane — which also dropped that live tenant
// out of the set federation orphan detection subtracts against, so its
// legitimate artifacts were proposed for cleanup while this process served it.
//
// ⛔ Neither side asserts the other, and no side reads the other's source.
// This repo measured (#1448 blind review) that a guard asserting things about
// another language's source text goes red on legitimate refactors and states
// the opposite of the truth when it does. Every reader asserts the shared
// matrix instead, which makes their agreement transitive rather than claimed.
//
// ⛔ THE ENUMERATOR UNDER TEST IS scanDirHierarchical — the production
// hot-reload scanner (ConfigManager reload calls
// scanDirHierarchicalWithMetrics), i.e. the thing that actually decides
// membership for what this process exposes. It is deliberately NOT
// pkg/config.ScopeEffective's SourceFiles, which is a convenient-looking but
// STRICT SUBSET: that one drops `_`-prefixed files and any file whose
// `tenants:` block does not parse, so a pin built on it would measure less
// than it claims and would go green on a defect in either dropped class.
//
// The rows are NAMES carrying orthogonal properties, not an expected file
// list. This half asserts the three projections this scanner implements:
//
//	hashes   (watched / change-detected) == yaml_extension AND NOT hidden
//	defaults (inheritance chain carriers) == defaults_file
//	tenants  (tenant carriers)            == yaml_extension AND NOT
//	                                         reserved_prefix AND NOT hidden
//
// The fixture holds CONTENT constant — every file is the same valid
// single-tenant document, differing only in the tenant id so cross-file
// duplicate detection stays out of the way — so each answer is attributable to
// the NAME rather than to parsing.

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

func (r confdNameRow) isTenantCarrier() bool {
	return r.YAMLExtension && !r.ReservedPrefix && !r.Hidden
}

// minConfdMatrixRows sits below the shipped row count on purpose — a redundant
// row may legitimately be dropped — but a gutted matrix must not keep every
// consumer green while measuring nothing.
const minConfdMatrixRows = 18

func loadConfdNameMatrix(t *testing.T) []confdNameRow {
	t.Helper()
	root := findRepoRoot(t)
	path := filepath.Join(root, "tests", "shared", "confd_name_classification_matrix.json")
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
		t.Fatalf("matrix shrank to %d rows (floor %d) — this scanner would still be "+
			"compared against a table, and the comparison would prove nothing: the "+
			"empty-set silence #1339 and #1537 are both about",
			len(doc.Rows), minConfdMatrixRows)
	}
	return doc.Rows
}

// TestConfdNameMatrixCoversThisScannersProjections is the anti-truncation
// floor for this half. A count alone is not enough — twenty all-lowercase rows
// would satisfy it while deleting every case the pin exists for — so the floor
// names the shapes each of the three projections needs in order to be able to
// fail at all.
func TestConfdNameMatrixCoversThisScannersProjections(t *testing.T) {
	t.Parallel()
	rows := loadConfdNameMatrix(t)

	var foldedExt, foldedDefaults, hiddenYAML, reservedNonDefaults, nonYAML bool
	for _, r := range rows {
		low := strings.ToLower(r.Name)
		if r.YAMLExtension && r.Name != low {
			foldedExt = true
		}
		if r.DefaultsFile && r.Name != low {
			foldedDefaults = true
		}
		if r.Hidden && r.YAMLExtension {
			hiddenYAML = true
		}
		if r.ReservedPrefix && r.YAMLExtension && !r.DefaultsFile {
			reservedNonDefaults = true
		}
		if !r.YAMLExtension {
			nonYAML = true
		}
	}
	for _, c := range []struct {
		ok  bool
		why string
	}{
		{foldedExt, "a YAML name whose extension is not all-lowercase — without it every projection below is a lowercase-only tautology (#1537's own row)"},
		{foldedDefaults, "a non-lowercase spelling of _defaults — the ONLY thing that can make the `defaults` projection fail, since this scanner compares that name folded"},
		{hiddenYAML, "a hidden YAML name — the only row that separates the `hashes` projection from a plain extension test"},
		{reservedNonDefaults, "a reserved YAML name that is not _defaults — the only row that separates `hashes` from `tenants` and `defaults` from `reserved`"},
		{nonYAML, "a non-YAML name — otherwise nothing pins that this scanner refuses anything at all"},
	} {
		if !c.ok {
			t.Errorf("the matrix no longer contains a row for: %s", c.why)
		}
	}
}

// fsIsCaseInsensitive reports whether the filesystem behind dir folds names.
func fsIsCaseInsensitive(t *testing.T, dir string) bool {
	t.Helper()
	probe := filepath.Join(dir, "_case_probe_lower")
	if err := os.WriteFile(probe, []byte("x"), 0o600); err != nil {
		t.Fatalf("write case probe: %v", err)
	}
	_, err := os.Stat(filepath.Join(dir, "_CASE_PROBE_LOWER"))
	return err == nil
}

// materialiseConfdMatrix writes one file per matrix row into a fresh temp dir.
func materialiseConfdMatrix(t *testing.T, rows []confdNameRow) string {
	t.Helper()
	if fsIsCaseInsensitive(t, t.TempDir()) {
		t.Skip("filesystem is case-insensitive, so rows differing only by case " +
			"(_defaults.yaml/_DEFAULTS.YAML, .hidden.yaml/.HIDDEN.YAML) cannot " +
			"coexist: this scanner CANNOT BE MEASURED here. That is a different " +
			"outcome from measuring it and finding it correct.")
	}
	root := t.TempDir()
	for i, r := range rows {
		body := fmt.Sprintf("tenants:\n  t%d: {}\n", i)
		if err := os.WriteFile(filepath.Join(root, r.Name), []byte(body), 0o600); err != nil {
			t.Fatalf("write fixture %q: %v", r.Name, err)
		}
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatalf("read fixture dir: %v", err)
	}
	if len(entries) != len(rows) {
		t.Fatalf("fixture materialised %d of %d rows — the assertions below would "+
			"have been silently weaker than they claim to be", len(entries), len(rows))
	}
	return root
}

func baseNameSet(paths []string) map[string]bool {
	out := make(map[string]bool, len(paths))
	for _, p := range paths {
		out[filepath.Base(p)] = true
	}
	return out
}

func diffSets(got, want map[string]bool) (missing, extra []string) {
	for n := range want {
		if !got[n] {
			missing = append(missing, n)
		}
	}
	for n := range got {
		if !want[n] {
			extra = append(extra, n)
		}
	}
	sort.Strings(missing)
	sort.Strings(extra)
	return missing, extra
}

// TestScanDirHierarchicalMatchesNameMatrix runs the production scanner over a
// directory holding one file per matrix row and checks all three of its
// classification outputs against the shared table.
func TestScanDirHierarchicalMatchesNameMatrix(t *testing.T) {
	t.Parallel()
	rows := loadConfdNameMatrix(t)
	root := materialiseConfdMatrix(t, rows)

	fresh, _ := freshMetrics(t)
	tenants, defaults, hashes, _, _, err := scanDirHierarchicalWithMetrics(root, nil, fresh, nil)
	if err != nil {
		t.Fatalf("scanDirHierarchical: %v", err)
	}

	wantHashed := map[string]bool{}
	wantDefaults := map[string]bool{}
	wantTenants := map[string]bool{}
	for _, r := range rows {
		if r.YAMLExtension && !r.Hidden {
			wantHashed[r.Name] = true
		}
		if r.DefaultsFile {
			wantDefaults[r.Name] = true
		}
		if r.isTenantCarrier() {
			wantTenants[r.Name] = true
		}
	}

	hashedPaths := make([]string, 0, len(hashes))
	for p := range hashes {
		hashedPaths = append(hashedPaths, p)
	}
	defaultPaths := make([]string, 0, len(defaults))
	for p := range defaults {
		defaultPaths = append(defaultPaths, p)
	}
	tenantPaths := make([]string, 0, len(tenants))
	for _, p := range tenants {
		tenantPaths = append(tenantPaths, p)
	}

	for _, c := range []struct {
		label      string
		got, want  map[string]bool
		projection string
		stakes     string
	}{
		{
			label:      "hashes",
			got:        baseNameSet(hashedPaths),
			want:       wantHashed,
			projection: "yaml_extension AND NOT hidden",
			stakes: "a file missing from `hashes` is not watched for change — it is " +
				"read once at whatever state it had and never reloaded",
		},
		{
			label:      "defaults",
			got:        baseNameSet(defaultPaths),
			want:       wantDefaults,
			projection: "defaults_file",
			stakes: "a chain carrier missing from `defaults` drops out of every " +
				"tenant's inheritance chain in its subtree, silently serving the " +
				"level above's values",
		},
		{
			label:      "tenants",
			got:        baseNameSet(tenantPaths),
			want:       wantTenants,
			projection: "yaml_extension AND NOT reserved_prefix AND NOT hidden",
			stakes: "this is the set the other three readers must agree with; a " +
				"disagreement here means some plane is describing a configuration " +
				"this process is not running",
		},
	} {
		missing, extra := diffSets(c.got, c.want)
		if len(missing) > 0 || len(extra) > 0 {
			t.Errorf("scanDirHierarchical %s disagrees with the matrix (projection: %s)\n"+
				"  missing (matrix says in, scanner left out): %v\n"+
				"  extra   (scanner put in, matrix says out):  %v\n"+
				"  what that costs: %s",
				c.label, c.projection, missing, extra, c.stakes)
		}
	}
}
