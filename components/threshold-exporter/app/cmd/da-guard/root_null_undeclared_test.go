package main

// root_null_undeclared_test.go — #2518: a threshold the conf.d root
// `_defaults.yaml` writes as null is no write, so the root does not declare
// it and a tenant's own value for it is not served (before #2518 the null
// was a 0 that declared the key, and the tenant's value WAS served).
// da-guard names each such (tenant, key) as root_default_null_undeclared;
// `served-values` lists the key in `unserved`. Following either half of the
// finding's fix serves the tenant's value and clears the finding.
//
// Seams: none — t.TempDir() trees through run().

import (
	"encoding/json"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
)

// rootNullFiles is the measured shape: root `mysql_connections: null`, tenant
// tx under sub/ setting 50; ty sets nothing. root replaces the root file.
func rootNullFiles(root string) map[string]string {
	return map[string]string{
		"_defaults.yaml": root,
		"sub/tx.yaml":    "tenants:\n  tx:\n    mysql_connections: 50\n",
		"sub/ty.yaml":    "tenants:\n  ty: {}\n",
	}
}

const rootNullDefaults = "defaults:\n  mysql_connections: null\n  mysql_slow_queries: 5\n"

// guardFindingsOf runs the guard (--format json) over files and returns the
// exit code and every finding.
func guardFindingsOf(t *testing.T, files map[string]string, args ...string) (int, []guard.Finding) {
	t.Helper()
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	argv := append([]string{"--format", "json", "--config-dir", filepath.Join(tmp, "conf.d")}, args...)
	code, stdout, stderr := runOnce(t, argv...)
	var doc struct {
		Report *guard.GuardReport `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON (exit %d): %v\nstderr=%s", code, err, stderr)
	}
	return code, doc.Report.Findings
}

func TestGuard_RootNullTenantValueIsNamed(t *testing.T) {
	t.Parallel()
	code, got := guardFindingsOf(t, rootNullFiles(rootNullDefaults))
	if code != exitOK {
		t.Errorf("exit = %d, want %d (a warning does not fail the run)", code, exitOK)
	}
	if len(got) != 1 {
		t.Fatalf("findings = %+v, want exactly one", got)
	}
	f := got[0]
	if f.Kind != guard.FindingRootDefaultNullUndeclared || f.Severity != guard.SeverityWarn ||
		f.TenantID != "tx" || f.Field != "mysql_connections" {
		t.Errorf("finding = %+v, want warn root_default_null_undeclared / tx / mysql_connections", f)
	}
	for _, want := range []string{"writes `mysql_connections` as null", "optional_overrides:"} {
		if !strings.Contains(f.Message, want) {
			t.Errorf("message lacks %q: %s", want, f.Message)
		}
	}
	if code, _ := guardFindingsOf(t, rootNullFiles(rootNullDefaults), "--warn-as-error"); code != exitFindings {
		t.Errorf("--warn-as-error: exit = %d, want %d", code, exitFindings)
	}

	// served-values: not served, listed in unserved (the same build).
	code, doc, _, stderr := served(t, rootNullFiles(rootNullDefaults), "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	tv := doc.Tenants["tx"]
	if _, has := tv.Values["mysql_connections"]; has {
		t.Errorf("values carries mysql_connections: %v", tv.Values)
	}
	if want := map[string]any{"mysql_connections": "50"}; !reflect.DeepEqual(tv.Unserved, want) {
		t.Errorf("tx unserved = %v, want %v", tv.Unserved, want)
	}
}

// The finding's fix, each half: a number at the root, or the key listed
// under optional_overrides (with the null line kept or removed). Each serves
// the tenant's 50 and leaves no finding.
func TestGuard_RootNullFixServesTheTenantValue(t *testing.T) {
	t.Parallel()
	for name, root := range map[string]string{
		"root number":                 "defaults:\n  mysql_connections: 30\n  mysql_slow_queries: 5\n",
		"optional_overrides, null":    rootNullDefaults + "optional_overrides: [mysql_connections]\n",
		"optional_overrides, no null": "defaults:\n  mysql_slow_queries: 5\noptional_overrides: [mysql_connections]\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if code, got := guardFindingsOf(t, rootNullFiles(root), "--warn-as-error"); code != exitOK || len(got) != 0 {
				t.Errorf("exit = %d, findings = %+v; want 0 and none", code, got)
			}
			code, doc, _, stderr := served(t, rootNullFiles(root), "2026-10-01T00:00:00Z")
			mustOK(t, code, stderr)
			wantValue(t, doc, "tx", "mysql_connections", 50)
		})
	}
}

// One cause, one finding: a value only a subtree `_defaults.yaml` hands down
// under a root null is subtree_default_undeliverable's, never this one's;
// a tenant writing its own value over that subtree is this one's alone; a
// tenant switching the key off is not reported; the other spelling of the
// root null is matched.
func TestGuard_RootNullOneFindingPerCause(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name   string
		files  map[string]string
		kind   guard.FindingKind // "" = no finding
		field  string
		tenant string
	}{
		{"subtree value only", map[string]string{
			"_defaults.yaml":     rootNullDefaults,
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
			"sub/tx.yaml":        "tenants:\n  tx: {}\n",
		}, guard.FindingSubtreeDefaultUndeliverable, "mysql_connections", "tx"},
		{"tenant value over a subtree value", map[string]string{
			"_defaults.yaml":     rootNullDefaults,
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
			"sub/tx.yaml":        "tenants:\n  tx:\n    mysql_connections: 50\n",
		}, guard.FindingRootDefaultNullUndeclared, "mysql_connections", "tx"},
		{"platform entry value", map[string]string{
			"_defaults.yaml": rootNullDefaults,
			"_platform.yaml": "tenants:\n  tx:\n    mysql_connections: 70\n",
			"tx.yaml":        "tenants:\n  tx: {}\n",
		}, guard.FindingRootDefaultNullUndeclared, "mysql_connections", "tx"},
		{"tenant switches it off", map[string]string{
			"_defaults.yaml": rootNullDefaults,
			"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: disable\n",
		}, "", "", ""},
		{"root legacy null, tenant canonical", map[string]string{
			"_defaults.yaml": "defaults:\n  " + aliasLegacy + ": null\n  mysql_slow_queries: 5\n",
			"tx.yaml":        "tenants:\n  tx:\n    " + aliasCanon + ": 50\n",
		}, guard.FindingRootDefaultNullUndeclared, aliasCanon, "tx"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, got := guardFindingsOf(t, tc.files)
			if tc.kind == "" {
				if len(got) != 0 {
					t.Errorf("findings = %+v, want none", got)
				}
				return
			}
			if len(got) != 1 || got[0].Kind != tc.kind || got[0].Field != tc.field || got[0].TenantID != tc.tenant {
				t.Errorf("findings = %+v, want exactly one %s / %s / %s", got, tc.kind, tc.tenant, tc.field)
			}
		})
	}
}

// The critical tier (#2518): a tenant-side `<base>_critical` is served as
// <base>'s critical row only when the root `_defaults.yaml` holds <base>. A
// root null on the base (either spelling) therefore drops it, and the finding
// names it — from the resolver's own entry test and base lookup, not a guard
// string rule. The fix is a number for the base at the root (that also serves
// the base at warning severity); optional_overrides does not serve the row.
// A `_critical` whose base the root never wrote is not root-null-caused and
// is not reported. The product: root base state × the tenant key's spelling
// × where the tenant side gets it (tenant file, platform entry, profile).
func TestGuard_RootNullCriticalRow(t *testing.T) {
	t.Parallel()
	C, L := aliasCanon, aliasLegacy
	roots := []struct {
		name, line string
		null       bool // the root writes the base as null
		serves     bool // the critical row is served
	}{
		{"rC30", "  " + C + ": 30\n", false, true},
		{"rL30", "  " + L + ": 30\n", false, true},
		{"rCnull", "  " + C + ": null\n", true, false},
		{"rLnull", "  " + L + ": null\n", true, false},
		{"r-", "", false, false},
		{"rCnull+oo", "  " + C + ": null\noptional_overrides: [" + C + "]\n", true, false},
	}
	for _, r := range roots {
		for _, key := range []string{C + "_critical", L + "_critical"} {
			for _, src := range []string{"tenant", "platform", "profile"} {
				name := r.name + "/" + key + "/" + src
				t.Run(name, func(t *testing.T) {
					t.Parallel()
					files := map[string]string{
						"_defaults.yaml": "defaults:\n  mysql_slow_queries: 5\n" + r.line,
						"sub/tx.yaml":    "tenants:\n  tx:\n    _profile: std\n",
						"_profiles.yaml": "profiles:\n  std:\n    mysql_slow_queries: 5\n",
					}
					switch src {
					case "tenant":
						files["sub/tx.yaml"] += "    " + key + ": 90\n"
					case "platform":
						files["_platform.yaml"] = "tenants:\n  tx:\n    " + key + ": 90\n"
					case "profile":
						files["_profiles.yaml"] += "    " + key + ": 90\n"
					}
					code, doc, _, stderr := served(t, files, "2026-10-01T00:00:00Z")
					mustOK(t, code, stderr)
					tv := doc.Tenants["tx"]
					got, has := tv.Values[C+"_critical"]
					if r.serves {
						if !has || got != float64(90) || tv.Severities[C+"_critical"] != "critical" {
							t.Errorf("critical row = %v (%v, %q), want 90 critical; values=%v", got, has, tv.Severities[C+"_critical"], tv.Values)
						}
					} else if has {
						t.Errorf("critical row served %v, want none", got)
					}
					_, findings := guardFindingsOf(t, files)
					var named []guard.Finding
					for _, f := range findings {
						if f.Kind == guard.FindingRootDefaultNullUndeclared {
							named = append(named, f)
						}
					}
					// A profile's keys reach the tenant canonicalized (profileFor),
					// so the finding names the key as the merged config spells it.
					field := key
					if src == "profile" {
						field = C + "_critical"
					}
					if r.null {
						if len(named) != 1 || named[0].Field != field || named[0].TenantID != "tx" {
							t.Errorf("root_default_null_undeclared = %+v, want one for tx / %s", named, field)
						}
					} else if len(named) != 0 {
						t.Errorf("root_default_null_undeclared = %+v, want none (no root null)", named)
					}
				})
			}
		}
	}
}

// RNC, followed through: the measured shape is named, and the finding's fix
// (a number for the base at the root) serves 90 at critical with no finding.
func TestGuard_RootNullCriticalFixServesTheRow(t *testing.T) {
	t.Parallel()
	rnc := map[string]string{
		"_defaults.yaml": rootNullDefaults,
		"sub/tx.yaml":    "tenants:\n  tx:\n    mysql_connections_critical: 90\n",
	}
	_, got := guardFindingsOf(t, rnc)
	if len(got) != 1 || got[0].Kind != guard.FindingRootDefaultNullUndeclared || got[0].Field != "mysql_connections_critical" {
		t.Fatalf("findings = %+v, want one root_default_null_undeclared for mysql_connections_critical", got)
	}
	if !strings.Contains(got[0].Message, "Write a number for `mysql_connections`") {
		t.Errorf("fix sentence does not name the base: %s", got[0].Message)
	}
	fixed := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 30\n  mysql_slow_queries: 5\n",
		"sub/tx.yaml":    rnc["sub/tx.yaml"],
	}
	if code, got := guardFindingsOf(t, fixed, "--warn-as-error"); code != exitOK || len(got) != 0 {
		t.Errorf("after the fix: exit = %d, findings = %+v; want 0 and none", code, got)
	}
	code, doc, _, stderr := served(t, fixed, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tx", "mysql_connections_critical", 90)
	if s := doc.Tenants["tx"].Severities["mysql_connections_critical"]; s != "critical" {
		t.Errorf("severity = %q, want critical", s)
	}
}

// The message quotes what the root file holds (blind review B2): with the
// root writing one spelling as null and the tenant the other, it names the
// root's spelling — a grep of the root for the quoted text finds it.
func TestGuard_RootNullMessageNamesTheRootSpelling(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, root, tenant, field, want, deny string
	}{
		{"root canonical, tenant legacy",
			"  " + aliasCanon + ": null\n", "    " + aliasLegacy + ": 70\n", aliasLegacy,
			"writes `" + aliasCanon + "` (the other spelling of `" + aliasLegacy + "`) as null", "writes `" + aliasLegacy + "`"},
		{"root legacy, tenant canonical critical",
			"  " + aliasLegacy + ": null\n", "    " + aliasCanon + "_critical: 90\n", aliasCanon + "_critical",
			"writes `" + aliasLegacy + "` (the other spelling of `" + aliasCanon + "`) as null", "writes `" + aliasCanon + "` as null"},
		{"same spelling",
			"  " + aliasCanon + ": null\n", "    " + aliasCanon + ": 70\n", aliasCanon,
			"writes `" + aliasCanon + "` as null", "other spelling"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, got := guardFindingsOf(t, map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_slow_queries: 5\n" + tc.root,
				"sub/tx.yaml":    "tenants:\n  tx:\n" + tc.tenant,
			})
			if len(got) != 1 || got[0].Kind != guard.FindingRootDefaultNullUndeclared || got[0].Field != tc.field {
				t.Fatalf("findings = %+v, want one root_default_null_undeclared for %s", got, tc.field)
			}
			if m := got[0].Message; !strings.Contains(m, tc.want) || strings.Contains(m, tc.deny) {
				t.Errorf("message = %q; want it to contain %q and not %q", m, tc.want, tc.deny)
			}
		})
	}
}

// For da-guard, optional_overrides: does not declare a non-reserved `_` key
// (#2707). The reader of that list, resolveDeclaredRows, skips every key
// starting with `_`, so listing a root-null `_myth` there serves nothing: the
// finding stays and served-values keeps the tenant's 50 in `unserved`. `_myth` is no
// reserved key and no key resolveBaseRows skips: a number for it at the
// root serves the tenant's 50. The non-`_` control — the same listing
// serving the tenant's value with no finding — is
// TestGuard_RootNullFixServesTheTenantValue's "optional_overrides, null".
func TestGuard_RootNullUnderscoreKeyNotDeclaredByOptionalOverrides(t *testing.T) {
	t.Parallel()
	files := func(root string) map[string]string {
		return map[string]string{
			"_defaults.yaml": root,
			"sub/tx.yaml":    "tenants:\n  tx:\n    _myth: 50\n",
		}
	}
	listed := files("defaults:\n  _myth: null\n  mysql_slow_queries: 5\noptional_overrides: [_myth]\n")

	_, got := guardFindingsOf(t, listed)
	if len(got) != 1 || got[0].Kind != guard.FindingRootDefaultNullUndeclared ||
		got[0].TenantID != "tx" || got[0].Field != "_myth" {
		t.Errorf("findings = %+v, want one root_default_null_undeclared for tx / _myth", got)
	}
	code, doc, _, stderr := served(t, listed, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	tv := doc.Tenants["tx"]
	if _, has := tv.Values["_myth"]; has {
		t.Errorf("values carries _myth: %v", tv.Values)
	}
	if want := map[string]any{"_myth": "50"}; !reflect.DeepEqual(tv.Unserved, want) {
		t.Errorf("tx unserved = %v, want %v", tv.Unserved, want)
	}

	// The root-number half of the fix does serve it.
	fixed := files("defaults:\n  _myth: 30\n  mysql_slow_queries: 5\n")
	if code, got := guardFindingsOf(t, fixed, "--warn-as-error"); code != exitOK || len(got) != 0 {
		t.Errorf("root number: exit = %d, findings = %+v; want 0 and none", code, got)
	}
	code, doc, _, stderr = served(t, fixed, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tx", "_myth", 50)
}
