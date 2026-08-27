package confd

// name_classification_parity_test.go — the tenant-api half of the
// cross-language conf.d name-classification pin (#1537, the #1339 family).
//
// One conf.d tree is read by four independent enumerators with four
// hand-written name rules. Measured before this pin existed: a root-level
// `upper.YAML` was read and served by the exporter, was invisible to both
// Python readers, and was REJECTED here — so GET /tenants returned 1 of 2
// tenant files and, worse, the federation orphan detector built its
// live-tenant set from this predicate, leaving that live tenant out of it and
// proposing its legitimate artifacts for cleanup while the exporter served it.
//
// ⛔ Neither side asserts the other, and no side reads the other's source.
// This repo measured (#1448 blind review) that a guard asserting things about
// another language's source text goes red on legitimate refactors and states
// the opposite of the truth when it does. Every reader asserts the shared
// matrix instead, which makes their agreement transitive rather than claimed.
//
// The rows are NAMES carrying orthogonal properties, not an expected file
// list — the four readers do not share a scope. This half asserts only the
// projection this package implements:
//
//	TenantIDFromFile ok == yaml_extension AND NOT reserved_prefix AND NOT hidden
//	TenantIDFromFile id == stem   (extension folded, STEM CASE PRESERVED)

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/quick"
)

type nameRow struct {
	Name           string `json:"name"`
	YAMLExtension  bool   `json:"yaml_extension"`
	ReservedPrefix bool   `json:"reserved_prefix"`
	Hidden         bool   `json:"hidden"`
	DefaultsFile   bool   `json:"defaults_file"`
	Stem           string `json:"stem"`
	Why            string `json:"why"`
}

// isCarrier reports whether the write plane may address this name as a tenant.
func (r nameRow) isCarrier() bool {
	return r.YAMLExtension && !r.ReservedPrefix && !r.Hidden
}

// findRepoRootForMatrix walks up from the test working directory until it
// finds the directory containing both rule-packs/ and Makefile (the repo root
// in a normal checkout and in a git worktree) — the convention the
// threshold-exporter module's test helpers already use.
func findRepoRootForMatrix(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if fi, serr := os.Stat(filepath.Join(dir, "rule-packs")); serr == nil && fi.IsDir() {
			if fi, serr := os.Stat(filepath.Join(dir, "Makefile")); serr == nil && !fi.IsDir() {
				return dir
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("could not locate repo root (rule-packs/ + Makefile) walking up from the test cwd")
		}
		dir = parent
	}
}

// minMatrixRows is below the shipped row count on purpose — a redundant row
// may legitimately be dropped — but a gutted matrix must not keep every
// consumer green while measuring nothing.
const minMatrixRows = 18

func loadNameMatrix(t *testing.T) []nameRow {
	t.Helper()
	root := findRepoRootForMatrix(t)
	path := filepath.Join(root, "tests", "shared", "confd_name_classification_matrix.json")
	raw, err := os.ReadFile(path) // #nosec G304 -- repo-relative test fixture
	if err != nil {
		t.Fatalf("read name classification matrix: %v", err)
	}
	var doc struct {
		Rows []nameRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal name classification matrix: %v", err)
	}
	if len(doc.Rows) < minMatrixRows {
		t.Fatalf("matrix shrank to %d rows (floor %d) — every consumer of this "+
			"table would stay green while asserting almost nothing, which is the "+
			"empty-set silence #1339 and #1537 are both about",
			len(doc.Rows), minMatrixRows)
	}
	return doc.Rows
}

// TestNameMatrixStillCarriesTheShapesItExistsFor is the anti-truncation floor.
// A bare count is not enough: twenty all-lowercase rows would satisfy it while
// removing every case this pin exists for, so the floor demands the
// load-bearing SHAPES too. It also re-derives every property column from the
// name, because a hand-added row with a wrong expectation would teach all four
// readers the wrong rule at once.
func TestNameMatrixStillCarriesTheShapesItExistsFor(t *testing.T) {
	t.Parallel()
	rows := loadNameMatrix(t)

	seen := make(map[string]bool, len(rows))
	var upperExt, upperDefaults, hiddenYAML, reservedNonDefaults, nonYAML bool
	var mixedStemYAML, mixedStemYML bool
	for _, r := range rows {
		if seen[r.Name] {
			t.Errorf("duplicate row name %q", r.Name)
		}
		seen[r.Name] = true

		low := strings.ToLower(r.Name)
		wantExt := strings.HasSuffix(low, ".yaml") || strings.HasSuffix(low, ".yml")
		if r.YAMLExtension != wantExt {
			t.Errorf("%s: yaml_extension column says %v, the name says %v", r.Name, r.YAMLExtension, wantExt)
		}
		if got := strings.HasPrefix(r.Name, "_"); r.ReservedPrefix != got {
			t.Errorf("%s: reserved_prefix column says %v, the name says %v", r.Name, r.ReservedPrefix, got)
		}
		if got := strings.HasPrefix(r.Name, "."); r.Hidden != got {
			t.Errorf("%s: hidden column says %v, the name says %v", r.Name, r.Hidden, got)
		}
		wantDef := low == "_defaults.yaml" || low == "_defaults.yml"
		if r.DefaultsFile != wantDef {
			t.Errorf("%s: defaults_file column says %v, the name says %v", r.Name, r.DefaultsFile, wantDef)
		}
		// De Morgan'd rather than `!(a && b)` — staticcheck QF1001 rejects the
		// latter, and the implication is what the message states either way.
		if r.DefaultsFile && (!r.ReservedPrefix || !r.YAMLExtension) {
			t.Errorf("%s: defaults_file must imply reserved_prefix AND yaml_extension", r.Name)
		}

		if r.YAMLExtension && r.Name != low {
			upperExt = true
		}
		if r.DefaultsFile && r.Name != low {
			upperDefaults = true
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
		if r.isCarrier() && r.Stem != strings.ToLower(r.Stem) {
			// ⛔ Tracked PER EXTENSION BRANCH. TenantIDFromFile derives the id in
			// a separate branch per extension, and the dogfood run for #1537
			// measured that with a mixed-case stem only on the .yml row,
			// corrupting the .yaml branch alone was caught by NOTHING — every
			// .yaml carrier happened to have an all-lowercase stem.
			if strings.HasSuffix(low, ".yaml") {
				mixedStemYAML = true
			} else {
				mixedStemYML = true
			}
		}
	}

	for _, c := range []struct {
		ok  bool
		why string
	}{
		{upperExt, "a YAML name whose EXTENSION is not all-lowercase — the #1537 row; without it the whole pin is a lowercase-only tautology"},
		{upperDefaults, "a non-lowercase spelling of the _defaults chain carrier — the exporter compares that name folded, so a reader that folds only the extension hashes the file and then drops its defaults: block"},
		{hiddenYAML, "a hidden YAML name — keeps `hidden` from being conflated with `yaml_extension`"},
		{reservedNonDefaults, "a reserved YAML name that is NOT the defaults carrier — separates the two reserved sub-cases"},
		{nonYAML, "a non-YAML name — otherwise nothing pins that the extension test refuses anything"},
		{mixedStemYAML, "a .yaml tenant carrier whose stem is not all-lowercase — pins that folding the extension must not RENAME the tenant, on the .yaml branch"},
		{mixedStemYML, "a .yml tenant carrier whose stem is not all-lowercase — the same pin on the .yml branch; a corruption applied to one branch only escapes otherwise, which is exactly what the dogfood run measured"},
	} {
		if !c.ok {
			t.Errorf("the matrix no longer contains a row for: %s", c.why)
		}
	}
}

// TestTenantIDFromFileMatchesMatrix is this package's projection of the shared
// table: which names are tenant carriers, and what id each one yields.
func TestTenantIDFromFileMatchesMatrix(t *testing.T) {
	t.Parallel()
	for _, r := range loadNameMatrix(t) {
		id, ok := TenantIDFromFile(r.Name)
		if ok != r.isCarrier() {
			t.Errorf("TenantIDFromFile(%q) ok = %v, matrix says %v\n  why this row exists: %s",
				r.Name, ok, r.isCarrier(), r.Why)
			continue
		}
		if id != r.Stem {
			t.Errorf("TenantIDFromFile(%q) id = %q, matrix says %q — the extension is "+
				"folded but the STEM'S CASE MUST SURVIVE; deriving the id from the "+
				"lowercased name silently renames the tenant on the write plane only\n"+
				"  why this row exists: %s", r.Name, id, r.Stem, r.Why)
		}
		if IsTenantConfigFile(r.Name) != r.isCarrier() {
			t.Errorf("IsTenantConfigFile(%q) disagrees with TenantIDFromFile — the two "+
				"must stay one predicate", r.Name)
		}
	}
}

// TestValidatorCallShapeDidNotWiden pins the security-relevant half of #1537.
//
// The enumerators pass a real directory entry name, so folding the extension
// really did change their answer — that is the fix. The VALIDATORS
// (handler.ValidateTenantID, gitops.Writer) instead synthesise `id + ".yaml"`,
// whose suffix is already lowercase, so the extension branch of
// TenantIDFromFile succeeds for EVERY id and acceptance is decided by the
// reserved-name rule alone. That is what "the write-accepted namespace did not
// widen" means concretely, and it is asserted rather than argued: if someone
// later makes the extension test able to REJECT a synthesised name, or makes
// the reserved test case-sensitive in a way that lets `_x` through, this goes
// red instead of quietly handing a caller the ability to address a platform
// control file.
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
		// The only way a synthesised name can be refused is the reserved rule.
		return IsTenantConfigFile(name) == !isReservedName(name)
	}
	for _, id := range corpus {
		if !check(id) {
			t.Errorf("id %q: on the synthesised name %q the extension test changed the "+
				"verdict — the validator's accepted set is no longer decided by the "+
				"reserved rule alone", id, id+".yaml")
		}
	}
	if err := quick.Check(check, nil); err != nil {
		t.Errorf("property violated on a generated id — the validator call shape is no "+
			"longer extension-independent: %v", err)
	}
}

// TestReservedPrefixNeedsNoCaseFolding is the measurement behind
// isReservedName deliberately NOT folding, kept as a test rather than as a
// sentence someone has to trust. Sweeping the whole Unicode range: no code
// point's lower- or upper-case image starts with '_' or '.' unless it already
// is '_' or '.', so folding there could not change an answer. If a future Go
// release changed that, the comment in confd.go would silently go false;
// this goes red instead.
func TestReservedPrefixNeedsNoCaseFolding(t *testing.T) {
	t.Parallel()
	for r := rune(0); r <= 0x10FFFF; r++ {
		if r >= 0xD800 && r <= 0xDFFF { // surrogate halves are not scalar values
			continue
		}
		if r == '_' || r == '.' {
			continue
		}
		s := string(r)
		for _, folded := range []string{strings.ToLower(s), strings.ToUpper(s)} {
			if strings.HasPrefix(folded, "_") || strings.HasPrefix(folded, ".") {
				t.Fatalf("U+%04X folds to %q, which starts with a reserved prefix — "+
					"isReservedName must fold too, and confd.go's note that it need "+
					"not is now false", r, folded)
			}
		}
	}
}
