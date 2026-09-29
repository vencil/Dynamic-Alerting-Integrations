package gitops

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// #2405: the eol-expansion guard reads the file a write replaces to learn how
// many end-of-life recipe instances it already has. When that file does not
// parse, the baseline is UNKNOWN, and unknown must count as zero — a body
// without an eol recipe passes (so the whole-file PUT can repair the file),
// a body with one is refused (so a broken file cannot launder new eol usage).

const eolTestTenant = "svc-alpha"

// eolTestRecipe stands in for an end-of-life recipe: the embedded status map
// has none, so the guard's violations function is injected (fakeEolViolations)
// rather than the package-global status map being swapped under t.Parallel.
const eolTestRecipe = "legacy_recipe"

// fakeEolViolations is EolExpansionViolations' contract with eolTestRecipe as
// the only eol recipe: one violation when next has more instances than current.
func fakeEolViolations(current, next []map[string]any) []string {
	count := func(insts []map[string]any) int {
		n := 0
		for _, in := range insts {
			if r, _ := in["recipe"].(string); r == eolTestRecipe {
				n++
			}
		}
		return n
	}
	if c, n := count(current), count(next); n > c {
		return []string{fmt.Sprintf("recipe %q is end-of-life (have %d, write requests %d)", eolTestRecipe, c, n)}
	}
	return nil
}

func eolAlert(name string) string {
	return "      - recipe: " + eolTestRecipe + "\n        name: " + name + "\n"
}

func eolBody(eolNames ...string) string {
	b := "tenants:\n  " + eolTestTenant + ":\n    mysql_connections: \"75\"\n"
	if len(eolNames) == 0 {
		return b
	}
	b += "    _custom_alerts:\n"
	for _, n := range eolNames {
		b += eolAlert(n)
	}
	return b
}

// brokenTenantFiles are the current-file shapes #2405 measured the whole-file
// PUT unable to replace. Every one fails customalerts.Extract.
var brokenTenantFiles = []struct{ name, body string }{
	{"unclosed_flow", "tenants:\n  " + eolTestTenant + ": [unclosed\n"},
	{"not_yaml", "{{not yaml\n"},
	{"top_level_list", "- a\n- b\n"},
	{"tenants_scalar", "tenants: oops\n"},
	{"duplicate_tenants_key", "tenants:\n  " + eolTestTenant + ":\n    mysql_connections: \"70\"\ntenants:\n  " + eolTestTenant + ":\n    mysql_connections: \"71\"\n"},
	{"tenants_list", "tenants:\n  - " + eolTestTenant + "\n"},
}

func TestEolGuard_UnparseableBaseForbidsEveryEolRecipe(t *testing.T) {
	t.Parallel()
	for _, c := range brokenTenantFiles {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			// Without an eol recipe the replacement goes through: this is the repair.
			if errs := eolGuardErrs([]byte(c.body), nil, eolBody(), eolTestTenant, fakeEolViolations); len(errs) > 0 {
				t.Errorf("body without an eol recipe refused over a broken base: %q", errs)
			}
			// With one it is refused: the unknown baseline is zero, not "skip".
			errs := eolGuardErrs([]byte(c.body), nil, eolBody("a"), eolTestTenant, fakeEolViolations)
			if len(errs) != 1 || !strings.Contains(errs[0], eolTestRecipe) {
				t.Errorf("body adding an eol recipe over a broken base: errs = %q, want one eol violation", errs)
			}
		})
	}
}

// Must-trigger controls: the guard's behavior on a PARSEABLE base, on a
// missing file and on an unreadable one is what it was before #2405.
func TestEolGuard_KnownBaselineUnchanged(t *testing.T) {
	t.Parallel()
	healthy := []byte(eolBody("a"))
	if errs := eolGuardErrs(healthy, nil, eolBody("a-renamed"), eolTestTenant, fakeEolViolations); len(errs) > 0 {
		t.Errorf("keeping the one existing eol alert refused: %q", errs)
	}
	if errs := eolGuardErrs(healthy, nil, eolBody("a", "b"), eolTestTenant, fakeEolViolations); len(errs) != 1 {
		t.Errorf("growing eol usage on a healthy base: errs = %q, want one violation", errs)
	}
	missing := &fs.PathError{Op: "open", Path: "x.yaml", Err: fs.ErrNotExist}
	if errs := eolGuardErrs(nil, missing, eolBody("a"), eolTestTenant, fakeEolViolations); len(errs) != 1 {
		t.Errorf("new tenant adding an eol recipe: errs = %q, want one violation", errs)
	}
	if errs := eolGuardErrs(nil, missing, eolBody(), eolTestTenant, fakeEolViolations); len(errs) > 0 {
		t.Errorf("new tenant without an eol recipe refused: %q", errs)
	}
	unreadable := &fs.PathError{Op: "read", Path: "x.yaml", Err: errors.New("input/output error")}
	errs := eolGuardErrs(nil, unreadable, eolBody(), eolTestTenant, fakeEolViolations)
	if len(errs) != 1 || !strings.Contains(errs[0], "cannot read current custom alerts") {
		t.Errorf("unreadable base: errs = %q, want the read-failure refusal", errs)
	}
}

// End to end through validate (the write choke point every write gate calls):
// a broken current file no longer blocks a legitimate replacement.
func TestValidate_BrokenBaseDoesNotBlockReplacement(t *testing.T) {
	t.Parallel()
	for _, c := range brokenTenantFiles {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			path := filepath.Join(dir, eolTestTenant+".yaml")
			if err := os.WriteFile(path, []byte(c.body), 0o644); err != nil {
				t.Fatal(err)
			}
			defaults := "defaults:\n  mysql_connections: 80\n"
			if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(defaults), 0o644); err != nil {
				t.Fatal(err)
			}
			if errs, _ := validate(dir, eolTestTenant, path, eolBody()); len(errs) > 0 {
				t.Errorf("validate refused a valid replacement of a broken file: %q", errs)
			}
		})
	}
}
