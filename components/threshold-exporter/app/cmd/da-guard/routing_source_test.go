package main

// routing_source_test.go — #2291: da-guard judges the routing the route
// generator RENDERS, read from where the generator reads it, and names the
// routing written anywhere else.
//
// Before, the routing checks resolved over the tenant's EFFECTIVE config,
// which also carries the defaults chain and the threshold profile. A
// `_routing` in a `defaults:` block or a profile — never rendered — was
// judged as the tenant's routing (an unknown receiver type blocked the
// merge for a route that does not exist), and `--required-fields
// _routing.*` answered off the same map, so a receiver supplied the right
// way (`_routing_defaults`) was reported missing.

import (
	"encoding/json"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// runRoutingTree writes files under <tmp>/conf.d and runs da-guard there
// (--format json plus args); scope, when non-empty, is a path under conf.d.
// It returns the exit code and "severity kind tenant field" per finding.
func runRoutingTree(t *testing.T, files map[string]string, scope string, args ...string) (int, []string) {
	t.Helper()
	tmp := t.TempDir()
	tree := make(map[string]string, len(files))
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	root := filepath.Join(tmp, "conf.d")
	argv := append([]string{"--config-dir", root, "--format", "json"}, args...)
	if scope != "" {
		argv = append(argv, "--scope", filepath.Join(root, scope))
	}
	code, stdout, stderr := runOnce(t, argv...)
	var doc struct {
		Report *struct {
			Findings []jsonFinding `json:"findings"`
		} `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("exit %d, report is not JSON: %v\nstdout=%s\nstderr=%s", code, err, stdout, stderr)
	}
	out := []string{}
	if doc.Report != nil {
		for _, f := range doc.Report.Findings {
			out = append(out, strings.Join([]string{f.Severity, f.Kind, f.TenantID, f.Field}, " "))
		}
	}
	sort.Strings(out)
	return code, out
}

const (
	rsDefaults = "defaults:\n  mysql_connections: 80\n"
	rsTenant   = "tenants:\n  tx:\n    mysql_connections: \"50\"\n"
	rsBadRoute = "\n      receiver:\n        type: bogus\n"
	rsOKRoute  = "\n      receiver:\n        type: webhook\n        url: https://t.example/h\n"
)

func TestRun_RoutingSource(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name     string
		files    map[string]string
		scope    string
		args     []string
		wantCode int
		want     []string
	}{
		// --- routing the generator never reads: named, never judged ---
		{
			name: "profile-routing-is-named-not-judged",
			files: map[string]string{
				"_defaults.yaml": rsDefaults,
				"_profiles.yaml": "profiles:\n  p1:\n    mysql_connections: 60\n    _routing:" + rsBadRoute,
				"tx.yaml":        "tenants:\n  tx:\n    _profile: p1\n",
			},
			wantCode: exitFindings,
			want:     []string{"error routing_in_unread_location  _profiles.yaml:profiles.p1._routing"},
		},
		{
			name: "unwrapped-root-defaults-routing-is-named-not-judged",
			files: map[string]string{
				"_defaults.yaml": "_routing:" + rsBadRoute,
				"tx.yaml":        rsTenant,
			},
			wantCode: exitFindings,
			want:     []string{"error routing_in_unread_location  _defaults.yaml:_routing"},
		},
		{
			name: "nested-defaults-block-routing-is-named-not-judged",
			files: map[string]string{
				"_defaults.yaml":        rsDefaults,
				"team-a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n  _routing:" + rsBadRoute,
				"team-a/tx.yaml":        rsTenant,
			},
			wantCode: exitFindings,
			want:     []string{"error routing_in_unread_location  team-a/_defaults.yaml:defaults._routing"},
		},
		{
			// A VALID route in the same place is still never rendered.
			name: "valid-routing-in-a-defaults-block-is-still-named",
			files: map[string]string{
				"_defaults.yaml":        rsDefaults,
				"team-a/_defaults.yaml": "defaults:\n  _routing:" + rsOKRoute,
				"team-a/tx.yaml":        rsTenant,
			},
			wantCode: exitFindings,
			want:     []string{"error routing_in_unread_location  team-a/_defaults.yaml:defaults._routing"},
		},
		{
			name: "routing-profile-reference-in-a-defaults-block-is-named",
			files: map[string]string{
				"_defaults.yaml":         rsDefaults,
				"_routing_profiles.yaml": "routing_profiles:\n  team:\n    receiver:\n      type: bogus\n",
				"team-a/_defaults.yaml":  "defaults:\n  _routing_profile: team\n",
				"team-a/tx.yaml":         rsTenant,
			},
			wantCode: exitFindings,
			want:     []string{"error routing_in_unread_location  team-a/_defaults.yaml:defaults._routing_profile"},
		},
		{
			// Nested carriers are never read by the (flat) generator, so
			// even `_routing_defaults` there is unread.
			name: "routing-defaults-in-an-unwrapped-nested-carrier-is-named",
			files: map[string]string{
				"_defaults.yaml":        rsDefaults,
				"team-a/_defaults.yaml": "_routing_defaults:" + rsOKRoute,
				"team-a/tx.yaml":        rsTenant,
			},
			wantCode: exitFindings,
			want:     []string{"error routing_in_unread_location  team-a/_defaults.yaml:_routing_defaults"},
		},
		{
			// --scope: a sibling directory's defaults do not bear on it.
			name: "sibling-directory-defaults-are-out-of-scope",
			files: map[string]string{
				"_defaults.yaml":        rsDefaults,
				"team-a/_defaults.yaml": "defaults:\n  _routing:" + rsBadRoute,
				"team-a/tx.yaml":        rsTenant,
				"team-b/ty.yaml":        "tenants:\n  ty:\n    mysql_connections: \"40\"\n",
			},
			scope:    "team-b",
			wantCode: exitOK,
			want:     []string{},
		},
		{
			// A file the exporter drops is exit 3's, named once.
			name: "parse-failed-defaults-gets-no-finding",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _routing:" + rsBadRoute,
				"tx.yaml":        rsTenant,
			},
			wantCode: exitParseFailed,
			want:     []string{},
		},

		// --- the correct places: read, judged, not named ---
		{
			// `_routing_defaults` at the root carrier's top level is the
			// documented spelling; `defaults:` beside it changes nothing.
			name: "root-routing-defaults-is-legal-and-judged",
			files: map[string]string{
				"_defaults.yaml": rsDefaults + "_routing_defaults:" + rsBadRoute,
				"tx.yaml":        rsTenant,
			},
			wantCode: exitFindings,
			want:     []string{"error unknown_receiver_type tx receiver.type"},
		},
		{
			name: "root-routing-defaults-in-an-unwrapped-carrier-is-legal",
			files: map[string]string{
				"_defaults.yaml": "mysql_connections: 80\n_routing_defaults:" + rsOKRoute,
				"tx.yaml":        rsTenant,
			},
			wantCode: exitOK,
			want:     []string{},
		},
		{
			name: "platform-overlay-routing-is-judged",
			files: map[string]string{
				"_defaults.yaml": rsDefaults,
				"_platform.yaml": "tenants:\n  tx:\n    _routing:" + rsBadRoute,
				"tx.yaml":        rsTenant,
			},
			wantCode: exitFindings,
			want:     []string{"error unknown_receiver_type tx receiver.type"},
		},
		{
			// #2291 review: a `_routing` supplied through a YAML merge key
			// is read — the generator's decoder expands `<<:` too.
			name: "platform-overlay-routing-through-a-merge-key-is-judged",
			files: map[string]string{
				"_defaults.yaml": rsDefaults,
				"_platform.yaml": "base: &b\n  _routing:" + strings.ReplaceAll(rsBadRoute, "\n  ", "\n") +
					"tenants:\n  tx:\n    <<: *b\n",
				"tx.yaml": rsTenant,
			},
			args:     []string{"--required-fields", "_routing.receiver.type"},
			wantCode: exitFindings,
			want:     []string{"error unknown_receiver_type tx receiver.type"},
		},
		{
			// The tenant file's `_routing` replaces the platform's WHOLE.
			name: "tenant-routing-replaces-the-platform-overlay",
			files: map[string]string{
				"_defaults.yaml": rsDefaults,
				"_platform.yaml": "tenants:\n  tx:\n    _routing:" + rsBadRoute,
				"tx.yaml":        "tenants:\n  tx:\n    _routing:" + rsOKRoute,
			},
			wantCode: exitOK,
			want:     []string{},
		},
		{
			name: "platform-overlay-routing-profile-reference-is-read",
			files: map[string]string{
				"_defaults.yaml":         rsDefaults,
				"_routing_profiles.yaml": "routing_profiles:\n  team:\n    receiver:\n      type: bogus\n",
				"_platform.yaml":         "tenants:\n  tx:\n    _routing_profile: team\n",
				"tx.yaml":                rsTenant,
			},
			wantCode: exitFindings,
			want:     []string{"error unknown_receiver_type tx receiver.type"},
		},

		// --- --required-fields _routing.* reads the resolved routing ---
		{
			name: "required-routing-field-supplied-by-routing-defaults",
			files: map[string]string{
				"_defaults.yaml": rsDefaults + "_routing_defaults:" + rsOKRoute,
				"tx.yaml":        rsTenant,
			},
			args:     []string{"--required-fields", "_routing.receiver.type"},
			wantCode: exitOK,
			want:     []string{},
		},
		{
			name: "required-routing-field-supplied-by-the-platform-overlay",
			files: map[string]string{
				"_defaults.yaml": rsDefaults,
				"_platform.yaml": "tenants:\n  tx:\n    _routing:" + rsOKRoute,
				"tx.yaml":        rsTenant,
			},
			args:     []string{"--required-fields", "_routing.receiver.type,_routing"},
			wantCode: exitOK,
			want:     []string{},
		},
		{
			name: "required-routing-field-only-in-a-profile-is-missing",
			files: map[string]string{
				"_defaults.yaml": rsDefaults,
				"_profiles.yaml": "profiles:\n  p1:\n    _routing:" + rsOKRoute,
				"tx.yaml":        "tenants:\n  tx:\n    _profile: p1\n",
			},
			args:     []string{"--required-fields", "_routing.receiver.type"},
			wantCode: exitFindings,
			want: []string{
				"error missing_required tx _routing.receiver.type",
				"error routing_in_unread_location  _profiles.yaml:profiles.p1._routing",
			},
		},
		{
			// An opted-out tenant is still reported, as an opt-out.
			name: "required-routing-field-on-a-disabled-tenant",
			files: map[string]string{
				"_defaults.yaml": rsDefaults + "_routing_defaults:" + rsOKRoute,
				"tx.yaml":        "tenants:\n  tx:\n    _routing: disable\n",
			},
			args:     []string{"--required-fields", "_routing.receiver.type"},
			wantCode: exitFindings,
			want:     []string{"error missing_required tx _routing.receiver.type"},
		},
		{
			// Control: a non-routing required field still reads the
			// effective config (the defaults chain supplies it).
			name: "required-threshold-field-still-reads-the-effective-config",
			files: map[string]string{
				"_defaults.yaml": rsDefaults,
				"tx.yaml":        rsTenant,
			},
			args:     []string{"--required-fields", "mysql_connections"},
			wantCode: exitOK,
			want:     []string{},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			code, got := runRoutingTree(t, tc.files, tc.scope, tc.args...)
			if code != tc.wantCode {
				t.Errorf("exit = %d, want %d; findings %v", code, tc.wantCode, got)
			}
			if !equalStrings(got, tc.want) {
				t.Errorf("findings:\n got %q\nwant %q", got, tc.want)
			}
		})
	}
}
