package gitops

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/customalerts"
)

// #2405: the eol-expansion guard reads the file a write replaces to learn how
// many end-of-life recipe instances it already has. When that file does not
// parse, replacing it needs platform-wide write permission (the
// replaceUnparseable bit); without it the write is refused. With it the
// baseline is UNKNOWN, and unknown must count as zero — a body without an eol
// recipe passes (so the whole-file PUT can repair the file), a body with one
// is refused (so a broken file cannot launder new eol usage).

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
			if errs := eolGuardErrs([]byte(c.body), nil, eolBody(), eolTestTenant, true, fakeEolViolations); len(errs) > 0 {
				t.Errorf("body without an eol recipe refused over a broken base: %q", errs)
			}
			// With one it is refused: the unknown baseline is zero, not "skip".
			errs := eolGuardErrs([]byte(c.body), nil, eolBody("a"), eolTestTenant, true, fakeEolViolations)
			if len(errs) != 1 || !strings.Contains(errs[0], eolTestRecipe) {
				t.Errorf("body adding an eol recipe over a broken base: errs = %q, want one eol violation", errs)
			}
		})
	}
}

// Without platform-wide write permission the same replacement is refused, with
// the permission message rather than an eol violation (must-trigger for the
// #2405 permission rule at the guard itself).
func TestEolGuard_UnparseableBaseNeedsPlatformWrite(t *testing.T) {
	t.Parallel()
	for _, c := range brokenTenantFiles {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			errs := eolGuardErrs([]byte(c.body), nil, eolBody(), eolTestTenant, false, fakeEolViolations)
			if len(errs) != 1 || !strings.Contains(errs[0], MsgPlatformWriteRepair) {
				t.Errorf("broken base without the bit: errs = %q, want the platform-write refusal", errs)
			}
		})
	}
}

// A base parse-tree refuses is a base that does not parse (#3206, ADR-036
// §7 step 7), even where Extract's one-document decode would read its first
// document: a declared non-UTF-8 sibling id (`!!binary`, which used to keep
// the ACTUAL baseline here), a trailing `---`, a `!` tag. Without the
// platform-write bit the write is refused; with it the baseline is zero.
func TestEolGuard_ParseTreeRefusedBaseIsUnparseable(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ name, base, kind string }{
		{"binary sibling id", "tenants:\n  !!binary dP8=:\n    mysql_connections: \"10\"\n" +
			strings.TrimPrefix(eolBody("a"), "tenants:\n"), "tag_not_allowed"},
		{"trailing empty document", eolBody("a") + "---\n", "multiple_documents"},
		{"second document", eolBody("a") + "---\ntenants: {}\n", "multiple_documents"},
		{"non-specific tag", eolBody("a") + "x: ! \"y\"\n", "non_specific_tag"},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			base := []byte(c.base)
			if _, err := customalerts.Extract(c.base, eolTestTenant); err != nil {
				t.Fatalf("Extract refuses this base itself (%v): the row tests nothing parse-tree adds", err)
			}
			errs := eolGuardErrs(base, nil, eolBody("a"), eolTestTenant, false, fakeEolViolations)
			if len(errs) != 1 || !strings.Contains(errs[0], MsgPlatformWriteRepair) ||
				!strings.Contains(errs[0], "refused by parse-tree (kind "+c.kind+")") {
				t.Errorf("without the bit: errs = %q, want the platform-write refusal naming kind %s", errs, c.kind)
			}
			errs = eolGuardErrs(base, nil, eolBody("a"), eolTestTenant, true, fakeEolViolations)
			if len(errs) != 1 || !strings.Contains(errs[0], eolTestRecipe) {
				t.Errorf("with the bit, keeping the one eol alert: errs = %q, want a violation against a zero baseline", errs)
			}
		})
	}
}

// Must-trigger controls: the guard's behavior on a PARSEABLE base, on a
// missing file and on an unreadable one is what it was before #2405.
func TestEolGuard_KnownBaselineUnchanged(t *testing.T) {
	t.Parallel()
	healthy := []byte(eolBody("a"))
	if errs := eolGuardErrs(healthy, nil, eolBody("a-renamed"), eolTestTenant, false, fakeEolViolations); len(errs) > 0 {
		t.Errorf("keeping the one existing eol alert refused: %q", errs)
	}
	if errs := eolGuardErrs(healthy, nil, eolBody("a", "b"), eolTestTenant, false, fakeEolViolations); len(errs) != 1 {
		t.Errorf("growing eol usage on a healthy base: errs = %q, want one violation", errs)
	}
	missing := &fs.PathError{Op: "open", Path: "x.yaml", Err: fs.ErrNotExist}
	if errs := eolGuardErrs(nil, missing, eolBody("a"), eolTestTenant, false, fakeEolViolations); len(errs) != 1 {
		t.Errorf("new tenant adding an eol recipe: errs = %q, want one violation", errs)
	}
	if errs := eolGuardErrs(nil, missing, eolBody(), eolTestTenant, false, fakeEolViolations); len(errs) > 0 {
		t.Errorf("new tenant without an eol recipe refused: %q", errs)
	}
	unreadable := &fs.PathError{Op: "read", Path: "x.yaml", Err: errors.New("input/output error")}
	errs := eolGuardErrs(nil, unreadable, eolBody(), eolTestTenant, false, fakeEolViolations)
	if len(errs) != 1 || !strings.Contains(errs[0], "cannot read the current tenant file") {
		t.Errorf("unreadable base: errs = %q, want the read-failure refusal", errs)
	}
}

// End to end through validate (the write choke point every write gate calls):
// a broken current file no longer blocks a legitimate replacement by a caller
// with the bit, and still blocks one without it (validate is the merge paths'
// entry and never carries the bit).
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
			if errs, _ := validateReplacing(dir, eolTestTenant, path, eolBody(), true, nil); len(errs) > 0 {
				t.Errorf("validate refused a valid replacement of a broken file: %q", errs)
			}
			errs, _ := validate(dir, eolTestTenant, path, eolBody())
			if len(errs) != 1 || !strings.Contains(errs[0], MsgPlatformWriteRepair) {
				t.Errorf("validate without the bit over a broken file: errs = %q, want the platform-write refusal", errs)
			}
		})
	}
}

// The refusal says why the base cannot be read and how to repair it (#3206):
// parse-tree's kind when parse-tree refuses the file — often valid YAML, so
// "cannot be parsed" would point at a syntax error that is not there — else
// that the custom-alerts decode failed; and in both, who may replace the file
// through the API and what everyone else does.
func TestEolGuard_RefusalNamesKindAndRepair(t *testing.T) {
	t.Parallel()
	kinds := map[string]string{
		"unclosed_flow": "syntax_error", "not_yaml": "syntax_error", "top_level_list": "top_level_not_mapping",
		"duplicate_tenants_key": "duplicate_key", "tenants_scalar": "", "tenants_list": "",
	}
	for _, c := range brokenTenantFiles {
		kind, ok := kinds[c.name]
		if !ok {
			t.Fatalf("%s: no expected kind for this row", c.name)
		}
		errs := eolGuardErrs([]byte(c.body), nil, eolBody(), eolTestTenant, false, fakeEolViolations)
		if len(errs) != 1 {
			t.Fatalf("%s: errs = %q, want one refusal", c.name, errs)
		}
		msg := errs[0]
		for _, want := range []string{"write permission on all tenants", "whole-file PUT", "repair the file in git", "ask a caller"} {
			if !strings.Contains(msg, want) {
				t.Errorf("%s: %q lacks the repair guidance %q", c.name, msg, want)
			}
		}
		if kind != "" && !strings.Contains(msg, "refused by parse-tree (kind "+kind+")") {
			t.Errorf("%s: %q does not name parse-tree's kind %s", c.name, msg, kind)
		}
		if kind == "" && (strings.Contains(msg, "parse-tree") || !strings.Contains(msg, "cannot be decoded for the end-of-life check")) {
			t.Errorf("%s: %q, want the custom-alerts decode reason, not parse-tree's", c.name, msg)
		}
	}
}
