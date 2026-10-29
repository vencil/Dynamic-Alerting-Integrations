package main

// subtree_undeliverable_test.go — #1976: a key only a subtree `_defaults.yaml`
// names is one the exporter serves no series for (it logs an ERROR and counts
// the tenant on da_config_subtree_undeliverable_tenants). da-guard reports it
// as a subtree_default_undeliverable error (exit 1, no flag needed)
// and `served-values` lists it in `unserved`. The control is the same tree
// with the key declared at the root: it is served, and nothing is reported.
//
// Seams: none — t.TempDir() trees through run().

import (
	"encoding/json"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/internal/testutil"
)

// undeliverableFiles: tenant-a (finance/) inherits redis_evicted_keys from
// finance/_defaults.yaml only; tenant-b (ops/) inherits nothing below the root.
// rootDeclares adds the key to the root defaults (the control).
func undeliverableFiles(rootDeclares bool) map[string]string {
	root := "defaults:\n  mysql_connections: 80\n"
	if rootDeclares {
		root += "  redis_evicted_keys: 500\n"
	}
	return map[string]string{
		"_defaults.yaml":         root,
		"finance/_defaults.yaml": "defaults:\n  mysql_connections: 60\n  redis_evicted_keys: 100\n",
		"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
		"ops/tenant-b.yaml":      "tenants:\n  tenant-b: {}\n",
	}
}

func writeUndeliverableConfD(t *testing.T, rootDeclares bool) string {
	t.Helper()
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range undeliverableFiles(rootDeclares) {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	return filepath.Join(tmp, "conf.d")
}

// undeliverableFindings runs the guard with --format json and returns the exit code and
// the subtree_default_undeliverable findings.
func undeliverableFindings(t *testing.T, args ...string) (int, []reportedFinding) {
	t.Helper()
	code, stdout, stderr := runOnce(t, append([]string{"--format", "json"}, args...)...)
	var doc struct {
		Report *reportedReport `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON (exit %d): %v\nstderr=%s", code, err, stderr)
	}
	var out []reportedFinding
	for _, f := range doc.Report.Findings {
		if f.Kind == findingkind.FindingSubtreeDefaultUndeliverable.String() {
			out = append(out, f)
		}
	}
	return code, out
}

func TestGuard_SubtreeOnlyKeyIsUndeliverableError(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableConfD(t, false)

	code, got := undeliverableFindings(t, "--config-dir", dir)
	if code != exitFindings {
		t.Errorf("exit = %d, want %d (an error fails the run without --warn-as-error)", code, exitFindings)
	}
	if len(got) != 1 {
		t.Fatalf("subtree_default_undeliverable findings = %+v, want exactly one", got)
	}
	f := got[0]
	if f.Severity != guard.SeverityError || f.TenantID != "tenant-a" || f.Field != "redis_evicted_keys" {
		t.Errorf("finding = %+v, want error / tenant-a / redis_evicted_keys", f)
	}
}

func TestGuard_RootDeclaredKeyIsNotUndeliverable(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableConfD(t, true)
	code, got := undeliverableFindings(t, "--config-dir", dir, "--warn-as-error")
	if code != exitOK || len(got) != 0 {
		t.Errorf("exit = %d, findings = %+v; want exit 0 and none (the root declares the key)", code, got)
	}
}

func TestGuard_UndeliverableFollowsScope(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableConfD(t, false)
	for _, tc := range []struct {
		scope string
		want  int
	}{
		{scope: "finance", want: 1},
		{scope: "ops", want: 0}, // tenant-a is outside the scope
	} {
		t.Run(tc.scope, func(t *testing.T) {
			t.Parallel()
			code, got := undeliverableFindings(t, "--config-dir", dir, "--scope", tc.scope)
			if len(got) != tc.want {
				t.Errorf("--scope %s: findings = %+v, want %d", tc.scope, got, tc.want)
			}
			wantCode := exitOK
			if tc.want > 0 {
				wantCode = exitFindings
			}
			if code != wantCode {
				t.Errorf("--scope %s: exit = %d, want %d", tc.scope, code, wantCode)
			}
		})
	}
}

func TestServedValues_UndeliverableKeyIsUnserved(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, undeliverableFiles(false), "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	tv := doc.Tenants["tenant-a"]
	if _, has := tv.Values["redis_evicted_keys"]; has {
		t.Errorf("values carries redis_evicted_keys: %v", tv.Values)
	}
	if want := map[string]any{"redis_evicted_keys": "100"}; !reflect.DeepEqual(tv.Unserved, want) {
		t.Errorf("tenant-a unserved = %v, want %v", tv.Unserved, want)
	}
	wantValue(t, doc, "tenant-a", "mysql_connections", 60)
	if u := doc.Tenants["tenant-b"].Unserved; len(u) != 0 {
		t.Errorf("tenant-b unserved = %v, want none", u)
	}
}

// #1976 r2/r3: which refused keys the finding and `unserved` report. Reserved
// keys and keys the exporter never serves as a row and keys the subtree
// switches off are not reported — nor counted by the exporter's ERROR and
// gauge, which read the same set since #3379 (a switched-off key is the
// exporter's WARN instead). The first two kinds are reported as
// subtree_default_reserved_key instead (#2388, wantReserved): one predicate
// splits the two findings, so no key is in both. An unrecognised `_` key is a
// threshold to the exporter and is reported; declared at the root (the
// finding's fix), the tenant serves the subtree's value.
func TestUndeliverable_SubtreeKeysReportedOrNot(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  mysql_connections: 80\n"
	const stateFilters = "state_filters:\n  maintenance:\n    reasons: [\"x\"]\n    severity: warning\n"
	const scheduled = "  _state_maintenance:\n    default: enable\n    overrides:\n" +
		"      - window: \"00:00-23:59\"\n        value: disable\n"
	subtree := func(rootBody, sub string) map[string]string {
		return map[string]string{
			"_defaults.yaml":         rootBody,
			"finance/_defaults.yaml": sub,
			"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
		}
	}
	for _, tc := range []struct {
		name         string
		files        map[string]string
		wantFindings []string           // Fields of tenant-a's findings
		wantReserved []string           // Fields of tenant-a's subtree_default_reserved_key findings
		wantUnserved map[string]any     // tenant-a's unserved; nil = none
		wantValues   map[string]float64 // tenant-a values that must be served
	}{
		{
			name:         "silent-bogus",
			files:        subtree(root, "defaults:\n  _silent_bogus: 5\n"),
			wantReserved: []string{"_silent_bogus"},
		},
		{
			// The review's t3: a subtree switching a key off.
			// `Disable` / " DISABLE " pin the resolver's case / space
			// normalisation.
			name: "switched-off",
			files: subtree(root, "defaults:\n  redis_evicted_keys: disable\n"+
				"  redis_u: Disable\n  redis_v: \" DISABLE \"\n"),
		},
		{
			// The review's t6. #3045: a `_` key the registry does not list
			// serves no row in any layer, so declaring the subtree's `_myth2`
			// at the root is no fix: it is reported as a key nothing reads
			// (subtree_default_reserved_key, as `_silent_bogus`), not as
			// undeliverable. The root's own `_myth` serves the tenant no row
			// either, and is in its `unserved` with the root's value.
			name:         "unrecognised-underscore-key",
			files:        subtree(root+"  _myth: 5\n", "defaults:\n  _myth2: 7\n"),
			wantReserved: []string{"_myth2"},
			wantUnserved: map[string]any{"_myth": 5.0},
		},
		{
			// The review's t7 without tenant-b. Declared at the root, `_myth2`
			// and `_silent_bogus` serve no row (#3045 for `_myth2`; before it,
			// the root declaration served `_myth2`'s 7), so the root's values
			// are in `unserved`; the subtree's 7s are not carried (#3341 —
			// until then they landed in the tenant's map).
			name:         "unrecognised-underscore-key-declared",
			files:        subtree(root+"  _myth2: 1\n  _silent_bogus: 1\n", "defaults:\n  _myth2: 7\n  _silent_bogus: 7\n"),
			wantUnserved: map[string]any{"_myth2": 1.0, "_silent_bogus": 1.0},
			wantReserved: []string{"_myth2", "_silent_bogus"},
		},
		{
			// A non-reserved `_` key listed in optional_overrides does not
			// count for the build's reachability test (#2707):
			// resolveDeclaredRows skips every `_` key. Since #3045 no layer
			// serves it at all, so it is reported as a key nothing reads, not
			// as undeliverable.
			name: "unrecognised-underscore-key-in-optional-overrides",
			files: subtree(root+"optional_overrides:\n  - _myth\n",
				"defaults:\n  _myth: 7\n"),
			wantReserved: []string{"_myth"},
		},
		{
			// `_metadata`, a scheduled `_state_maintenance` and `_profile`,
			// none declared in optional_overrides: all three are refused.
			name: "reserved-keys",
			files: map[string]string{
				"_defaults.yaml":         "defaults:\n  mysql_connections: 80\n" + stateFilters,
				"finance/_defaults.yaml": "defaults:\n  _metadata: 5\n" + scheduled + "  _profile: disable\n",
				"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			},
			wantReserved: []string{"_metadata", "_profile", "_state_maintenance"},
		},
		{
			// The same keys with `_state_maintenance` and `_metadata` in
			// optional_overrides (the review's exB).
			name: "reserved-keys-declared",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" + stateFilters +
					"optional_overrides:\n  - _state_maintenance\n  - _metadata\n",
				"finance/_defaults.yaml": "defaults:\n  _metadata: 5\n" + scheduled + "  _profile: disable\n",
				"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			},
			wantReserved: []string{"_metadata", "_profile", "_state_maintenance"},
		},
		{
			// A schedule at finance/, a scalar below: since #3341 a subtree
			// carries neither (the review's exC); subtree_default_reserved_key
			// reports it.
			name: "schedule-then-scalar-state",
			files: map[string]string{
				"_defaults.yaml":            "defaults:\n  mysql_connections: 80\n" + stateFilters,
				"finance/_defaults.yaml":    "defaults:\n" + scheduled,
				"finance/us/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
				"finance/us/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			},
			wantReserved: []string{"_state_maintenance"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			tree := map[string]string{}
			for k, v := range tc.files {
				tree["conf.d/"+k] = v
			}
			testutil.WriteTree(t, tmp, tree)
			dir := filepath.Join(tmp, "conf.d")

			code, got := undeliverableFindings(t, "--config-dir", dir)
			_, reserved := reservedFindings(t, "--config-dir", dir)
			var reservedFields []string
			for _, f := range reserved {
				reservedFields = append(reservedFields, f.Field)
			}
			if !reflect.DeepEqual(reservedFields, tc.wantReserved) {
				t.Errorf("subtree_default_reserved_key fields = %v, want %v", reservedFields, tc.wantReserved)
			}
			var fields []string
			for _, f := range got {
				if f.TenantID != "tenant-a" {
					t.Errorf("finding for another tenant: %+v", f)
				}
				fields = append(fields, f.Field)
			}
			if !reflect.DeepEqual(fields, tc.wantFindings) {
				t.Errorf("finding fields = %v, want %v", fields, tc.wantFindings)
			}
			wantCode := exitOK
			if len(tc.wantFindings)+len(tc.wantReserved) > 0 {
				wantCode = exitFindings
			}
			if code != wantCode {
				t.Errorf("exit = %d, want %d", code, wantCode)
			}

			code, doc, _, stderr := served(t, tc.files, "2026-10-01T00:00:00Z")
			mustOK(t, code, stderr)
			u := doc.Tenants["tenant-a"].Unserved
			if len(u) == 0 {
				u = nil
			}
			if !reflect.DeepEqual(u, tc.wantUnserved) {
				t.Errorf("tenant-a unserved = %v, want %v", u, tc.wantUnserved)
			}
			for k, v := range tc.wantValues {
				wantValue(t, doc, "tenant-a", k, v)
			}
			for k := range tc.wantUnserved {
				if v, served := doc.Tenants["tenant-a"].Values[k]; served {
					t.Errorf("unserved key %q is also served: %v", k, v)
				}
			}
		})
	}
}

// A RESERVED key listed in optional_overrides does not make a subtree's
// value of it served: a subtree carries no reserved key (#3341), so the
// subtree `_state_maintenance` schedule whose default is disable serves true
// at 12:00, as the control with no subtree value. Until #3341 the listing
// kept the subtree value applied (false); this pins the sunset.
func TestServedValues_ReservedKeyListedInOptionalOverridesDoesNotCarrySubtreeValue(t *testing.T) {
	t.Parallel()
	root := "defaults:\n  mysql_connections: 80\n" +
		"state_filters:\n  maintenance:\n    reasons: [\"x\"]\n    severity: warning\n" +
		"optional_overrides:\n  - _state_maintenance\n  - _metadata\n"
	for _, tc := range []struct {
		name, subtree string
		want          bool
	}{
		{"subtree schedule, default disable", "defaults:\n  _state_maintenance:\n    default: disable\n" +
			"    overrides:\n      - window: \"00:00-23:59\"\n        value: enable\n", true},
		{"control: no subtree value", "defaults: {}\n", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			code, doc, _, stderr := served(t, map[string]string{
				"_defaults.yaml":         root,
				"finance/_defaults.yaml": tc.subtree,
				"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			}, "2026-10-01T12:00:00Z")
			mustOK(t, code, stderr)
			if got := doc.Tenants["tenant-a"].Values["_state_maintenance"]; got != tc.want {
				t.Errorf("_state_maintenance = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestServedValues_RootDeclaredKeyIsServed(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, undeliverableFiles(true), "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "redis_evicted_keys", 100)
	if u := doc.Tenants["tenant-a"].Unserved; len(u) != 0 {
		t.Errorf("tenant-a unserved = %v, want none", u)
	}
}

// #3379: the shapes on which the exporter's ERROR and gauge used to disagree
// with this finding, through the real CLI. The exporter side of the same
// fixtures (gauge, log, /metrics, and its tenant set equal to this finding's)
// is TestSubtreeUndeliverable_ExporterAndGuardReadOneSet in package main.
func TestUndeliverable_DisableAndRoutingShapes(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  mysql_connections: 80\n"
	fin := func(rootBody, sub string) map[string]string {
		return map[string]string{
			"_defaults.yaml":         rootBody,
			"finance/_defaults.yaml": sub,
			"finance/t-fin.yaml":     "tenants:\n  t-fin: {}\n",
		}
	}
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  []string // tenants reported; nil = none
		// exit is checked when set: a routing key makes the run exit 4
		// (undetermined, the route generator's), whatever this finding says.
		exit int
	}{
		{name: "A1 disable", files: fin(root, "defaults:\n  redis_evicted_keys: disable\n"), exit: exitOK},
		{
			name: "A2 schedule disabled in part of the day",
			files: fin(root, "defaults:\n  redis_evicted_keys:\n    default: disable\n"+
				"    overrides:\n      - window: \"01:00-02:00\"\n        value: 50\n"),
			want: []string{"t-fin"}, exit: exitFindings,
		},
		{
			name: "A2b schedule disabled all day",
			files: fin(root, "defaults:\n  redis_evicted_keys:\n    default: disable\n"+
				"    overrides:\n      - window: \"01:00-02:00\"\n        value: disable\n"),
			exit: exitOK,
		},
		{
			name: "A8 value above, disable below",
			files: map[string]string{
				"_defaults.yaml":            root,
				"finance/_defaults.yaml":    "defaults:\n  redis_evicted_keys: 100\n",
				"finance/t-fin.yaml":        "tenants:\n  t-fin: {}\n",
				"finance/us/_defaults.yaml": "defaults:\n  redis_evicted_keys: disable\n",
				"finance/us/t-fin-us.yaml":  "tenants:\n  t-fin-us: {}\n",
			},
			want: []string{"t-fin"}, exit: exitFindings,
		},
		{
			name: "critical over a declared-only base",
			files: fin("defaults:\n  mysql_connections: 80\noptional_overrides:\n  - redis_evicted_keys\n",
				"defaults:\n  redis_evicted_keys_critical: 200\n"),
			want: []string{"t-fin"}, exit: exitFindings,
		},
		{name: "routing key", files: fin(root, "defaults:\n  _routing_bogus: 5\n"), exit: -1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			tree := map[string]string{}
			for k, v := range tc.files {
				tree["conf.d/"+k] = v
			}
			testutil.WriteTree(t, tmp, tree)
			code, got := undeliverableFindings(t, "--config-dir", filepath.Join(tmp, "conf.d"))
			var tenants []string
			for _, f := range got {
				if f.Severity != guard.SeverityError {
					t.Errorf("finding %+v is not an error", f)
				}
				tenants = append(tenants, f.TenantID)
			}
			if !reflect.DeepEqual(tenants, tc.want) {
				t.Errorf("reported tenants = %v, want %v", tenants, tc.want)
			}
			if tc.exit >= 0 && code != tc.exit {
				t.Errorf("exit = %d, want %d", code, tc.exit)
			}
		})
	}
}
