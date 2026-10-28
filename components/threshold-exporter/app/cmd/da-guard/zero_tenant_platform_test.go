package main

// #2991: what the guard judges without a tenant — the defaults carriers, the
// /metrics gather verdict — is reported when the scope has no tenant, as it
// is when the scope has some. The "no tenants → vacuously safe" exit 0 holds
// only for a run with no finding and nothing undetermined.
//
// #2904: the routing-tree problems (one row per Problem kind the Go routing
// loader had, read from the whole tree at --config-dir) and the
// `_routing_enforced` route are no longer judged by the default check; on a
// scope with no tenant each of those trees is undetermined (exit 4), never
// the vacuous 0. #2905 deleted that loader (pkg/routingpolicy) and its
// finding kinds; the trees stay, each row naming the kind it used to report.

import (
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/internal/testutil"
)

// zeroTenantPlatformCase is one tree on a scope with no tenant: the files of
// the conf.d tree, the --scope (relative to it; "" = the whole tree) and the
// finding the run must carry — or, for a routing row, was: the finding kind
// the CLI carried before #2904, kept as the row's record (#2905 deleted the
// kind itself).
type zeroTenantPlatformCase struct {
	files   map[string]string
	scope   string
	finding findingkind.Kind
	was     string
}

const webhookReceiver = "{type: webhook, url: \"https://w.example/h\"}"

// zeroTenantPlatformCases: one row per Problem kind the deleted Go routing
// loader declared (pkg/routingpolicy, #2905), keyed by the finding kind it
// was reported as. Two kinds need a tenant somewhere in the tree (a
// duplicate id, a policy naming a tenant outside its subtree); their tree
// has tenants, but the --scope has none.
var zeroTenantPlatformCases = map[string]zeroTenantPlatformCase{
	"domain_policy_unusable": {
		files: map[string]string{"_domain_policy.yaml": "domain_policies:\n- fin\n"},
		was:   "domain_policy_unusable",
	},
	"routing_profiles_unusable": {
		files: map[string]string{"_routing_profiles.yaml": "routing_profiles:\n- p-team\n"},
		was:   "routing_profiles_unusable",
	},
	"routing_defaults_routes_ignored": {
		files: map[string]string{"_defaults.yaml": "_routing_defaults:\n  receiver: " + webhookReceiver +
			"\n  routes:\n  - receiver: " + webhookReceiver + "\n"},
		was: "routing_defaults_routes_ignored",
	},
	"routing_in_unread_location": {
		files: map[string]string{"_profiles.yaml": "profiles:\n  p1:\n    cpu: 60\n    _routing:\n" +
			"      receiver: " + webhookReceiver + "\n"},
		was: "routing_in_unread_location",
	},
	"routing_enforced_below_root": {
		files: map[string]string{"team/_defaults.yaml": "_routing_enforced:\n  enabled: true\n" +
			"  receiver: " + webhookReceiver + "\n"},
		was: "routing_enforced_below_root",
	},
	"routing_defaults_null_below_root": {
		files: map[string]string{"team/_defaults.yaml": "_routing_defaults:\n  receiver: null\n"},
		was:   "routing_defaults_null_below_root",
	},
	"routing_profile_duplicate": {
		files: map[string]string{
			"_routing_profiles.yaml": "routing_profiles:\n  p2:\n    receiver: " + webhookReceiver + "\n",
			"_routing_profiles.yml":  "routing_profiles:\n  p2:\n    receiver: " + webhookReceiver + "\n",
		},
		was: "routing_profile_duplicate",
	},
	"duplicate_tenant": {
		files: map[string]string{
			"t-x.yaml":      "tenants:\n  t-x:\n    cpu: 1\n",
			"team/t-x.yaml": "tenants:\n  t-x:\n    cpu: 2\n",
			"empty/":        "",
		},
		scope: "empty",
		was:   "duplicate_tenant",
	},
	"domain_policy_out_of_scope": {
		files: map[string]string{
			"t-root.yaml": "tenants:\n  t-root:\n    cpu: 1\n",
			"team/_domain_policy.yaml": "domain_policies:\n  fin:\n    tenants: [t-root]\n" +
				"    constraints: {forbidden_receiver_types: [slack]}\n",
			"empty/": "",
		},
		scope: "empty",
		was:   "domain_policy_out_of_scope",
	},
	"routing_defaults_not_mapping": {
		files: map[string]string{"_defaults.yaml": "_routing_defaults: [1, 2]\n"},
		was:   "routing_defaults_not_mapping",
	},
	"routing_key_ignored": {
		files: map[string]string{"_defaults.yaml": "_routing_defaults:\n  continue: true\n"},
		was:   "routing_key_ignored",
	},
	"routing_enforced_match_invalid": {
		files: map[string]string{"_noc.yaml": "_routing_enforced:\n  enabled: true\n  receiver: " + webhookReceiver +
			"\n  match: {severity: critical}\n"},
		was: "routing_enforced_match_invalid",
	},
}

// zeroTenantTreeLevelCases: the findings judged without a tenant — the
// defaults carriers (#2386) and the /metrics gather verdict. Not a list the
// code reads: the guard runs every check on zero tenants, these rows only
// pin that.
var zeroTenantTreeLevelCases = map[string]zeroTenantPlatformCase{
	"root defaults without the defaults: mapping": {
		files:   map[string]string{"_defaults.yaml": "cpu: 80\n"},
		finding: findingkind.FindingRootDefaultsUnwrapped,
	},
	"top-level key beside defaults:": {
		files:   map[string]string{"_defaults.yaml": "defaults:\n  cpu: 80\nmem: 70\n"},
		finding: findingkind.FindingDefaultsTopLevelIgnored,
	},
	"_critical key in the root defaults": {
		files:   map[string]string{"_defaults.yaml": "defaults:\n  cpu: 80\n  cpu_critical: 90\n"},
		finding: findingkind.FindingRootDefaultsCriticalKey,
	},
	// #3140: a non-finite root default is judged in the carrier itself.
	"non-finite number in the root defaults": {
		files:   map[string]string{"_defaults.yaml": "defaults:\n  cpu: .nan\n"},
		finding: findingkind.FindingRootDefaultRefused,
	},
	// #3250: a reserved key in the root defaults is judged in the carrier.
	"reserved key in the root defaults": {
		files:   map[string]string{"_defaults.yaml": "defaults:\n  cpu: 80\n  _silent_mode: warning\n"},
		finding: findingkind.FindingRootDefaultsReservedKey,
	},
	// #2031: one series two keys share fails the whole tree's scrape,
	// whatever the scope; the tenant is outside it.
	"/metrics not gatherable": {
		files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"team/tx.yaml": "tenants:\n  tx:\n    mysql_connections: \"70:critical\"\n" +
				"    mysql_connections_critical: 95\n",
			"empty/": "",
		},
		scope:   "empty",
		finding: findingkind.FindingMetricsNotGatherable,
	},
}

// kindNeedsTenant / kindTreeLevel are the two classes of zeroTenantKindClass.
const (
	kindNeedsTenant = "needs a tenant"
	kindTreeLevel   = "tree level"
)

// zeroTenantKindClass sorts every registered finding kind: kindNeedsTenant
// when every finding of it judges a tenant in scope (none can come out of a
// scope with no tenant), kindTreeLevel when some finding of it is judged
// without one. TestGuard_EveryFindingKindIsClassifiedForZeroTenants holds
// it to findingkind.All() (the routing kinds, which the CLI reported on no
// tree since #2904, left the registry with #2905) and holds every kindTreeLevel kind to a
// row of zeroTenantTreeLevelCases — so a new kind is red until someone says
// which it is, and a tree-level one until a zero-tenant run is shown to
// report it.
var zeroTenantKindClass = map[string]string{
	// Judged per tenant in EffectiveConfigs / the per-tenant maps
	// buildCheckInput fills from scoped.Tenants.
	"missing_required":              kindNeedsTenant,
	"redundant_override":            kindNeedsTenant,
	"cardinality_exceeded":          kindNeedsTenant,
	"cardinality_warning":           kindNeedsTenant,
	"root_default_null_undeclared":  kindNeedsTenant,
	"subtree_default_undeliverable": kindNeedsTenant,
	"subtree_default_reserved_key":  kindNeedsTenant,
	// config.ScopedTenants.ScheduleNulls is collected along the in-scope
	// tenants' defaults chains: a subtree defaults file no tenant inherits
	// yields none, in the exporter's input, not in the guard.
	"schedule_null_value":           kindNeedsTenant,
	"value_not_served":              kindNeedsTenant,
	"custom_alert_duplicate_series": kindNeedsTenant,
	"custom_alert_rejected":         kindNeedsTenant,
	"custom_alert_over_cap":         kindNeedsTenant,

	// The defaults carriers (DefaultsFiles).
	"root_defaults_unwrapped":    kindTreeLevel,
	"defaults_toplevel_ignored":  kindTreeLevel,
	"root_defaults_critical_key": kindTreeLevel,
	"root_default_refused":       kindTreeLevel,
	"root_defaults_reserved_key": kindTreeLevel,
	// The whole tree's /metrics.
	"metrics_not_gatherable": kindTreeLevel,
}

func TestGuard_EveryFindingKindIsClassifiedForZeroTenants(t *testing.T) {
	t.Parallel()
	covered := map[string]bool{}
	for name, tc := range zeroTenantTreeLevelCases {
		covered[tc.finding.String()] = true
		if zeroTenantKindClass[tc.finding.String()] != kindTreeLevel {
			t.Errorf("row %q reports %q, which zeroTenantKindClass does not call %q", name, tc.finding, kindTreeLevel)
		}
	}
	registered := map[string]bool{}
	for _, e := range findingkind.All() {
		name := e.Kind.String()
		registered[name] = true
		switch zeroTenantKindClass[name] {
		case kindNeedsTenant:
		case kindTreeLevel:
			if !covered[name] {
				t.Errorf("%q is %s but no zero-tenant row reports it: add one to zeroTenantTreeLevelCases", name, kindTreeLevel)
			}
		default:
			t.Errorf("finding kind %q is not in zeroTenantKindClass: say whether a scope with no tenant can report it", name)
		}
	}
	for name := range zeroTenantKindClass {
		if !registered[name] {
			t.Errorf("zeroTenantKindClass names %q, which findingkind does not register", name)
		}
	}
}

// scopeArgs is the --scope of tc, if any.
func (tc zeroTenantPlatformCase) scopeArgs() []string {
	if tc.scope == "" {
		return nil
	}
	return []string{"--scope", tc.scope}
}

// withCleanTenant is tc's tree plus one clean tenant inside its scope.
func (tc zeroTenantPlatformCase) withCleanTenant() map[string]string {
	files := map[string]string{}
	for k, v := range tc.files {
		files[k] = v
	}
	files[filepath.ToSlash(filepath.Join(tc.scope, "t-extra.yaml"))] = "tenants:\n  t-extra:\n    cpu: 1\n"
	return files
}

// assertZeroTenantFinding: the run over tc (no tenant in scope) carries
// tc.finding and exits as that finding decides — 1 for an error; 0 for a
// warning, 1 under --warn-as-error, as with tenants.
func assertZeroTenantFinding(t *testing.T, tc zeroTenantPlatformCase) {
	t.Helper()
	args := tc.scopeArgs()
	code, fs := guardFindingsOf(t, tc.files, args...)
	var got []reportedFinding
	for _, f := range fs {
		if f.Kind == tc.finding.String() {
			got = append(got, f)
		}
		if f.TenantID != "" {
			t.Errorf("finding %+v names a tenant: the scope has none", f)
		}
	}
	if len(got) == 0 {
		t.Fatalf("exit %d, findings %+v: want a %s finding", code, fs, tc.finding)
	}
	want := exitFindings
	if got[0].Severity == guard.SeverityWarn {
		want = exitOK
		if c, _ := guardFindingsOf(t, tc.files, append(args, "--warn-as-error")...); c != exitFindings {
			t.Errorf("--warn-as-error: exit %d, want %d", c, exitFindings)
		}
	}
	if code != want {
		t.Errorf("exit %d, want %d (findings %+v)", code, want, fs)
	}
}

// zeroTenantRoutingCases are the other routing trees with no tenant in
// scope: the single-shape `_routing_enforced` route the generator renders
// whatever the tenants (#2503; generate_alertmanager_routes.py --validate
// --strict: "ERROR: _routing_enforced: group_by[1] is int 5, not a string",
// rc 1).
var zeroTenantRoutingCases = map[string]zeroTenantPlatformCase{
	"enforced group_by, single shape": {
		files: map[string]string{"_noc.yaml": "_routing_enforced:\n  enabled: true\n  receiver: " + webhookReceiver +
			"\n  group_by: [\"alertname\", 5]\n"},
		was: "routing_group_by_invalid",
	},
}

// #2904 (absorbs #2991): every routing-tree problem on a scope with no
// tenant is undetermined — exit 4, no finding, the evidence listed — where
// the run used to report it (exit 1) and must never be the vacuous 0.
func TestGuard_RoutingProblemOnZeroTenantScopeIsUndetermined(t *testing.T) {
	t.Parallel()
	all := map[string]zeroTenantPlatformCase{}
	for k, v := range zeroTenantPlatformCases {
		all["platform/"+k] = v
	}
	for k, v := range zeroTenantRoutingCases {
		all["routing/"+k] = v
	}
	for name, tc := range all {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got := runJudged(t, tc.files, tc.scope)
			if got.code != exitUndetermined || len(got.findings) != 0 || len(got.undetermined) == 0 {
				t.Errorf("exit %d, findings %v, undetermined %v; want 4, none, some (was %s)\nstderr=%s",
					got.code, got.findings, got.undetermined, tc.was, got.stderr)
			}
		})
	}
}

func TestGuard_TreeLevelFindingOnZeroTenantScopeFails(t *testing.T) {
	t.Parallel()
	for name, tc := range zeroTenantTreeLevelCases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			assertZeroTenantFinding(t, tc)
		})
	}
}

// Every row above, run again with one clean tenant added to the scope: the
// findings that name no tenant are the same — the zero-tenant run drops
// nothing a run with tenants reports.
func TestGuard_ZeroTenantRunReportsWhatATenantRunDoes(t *testing.T) {
	t.Parallel()
	all := map[string]zeroTenantPlatformCase{}
	for k, v := range zeroTenantPlatformCases {
		all["platform/"+k] = v
	}
	for k, v := range zeroTenantRoutingCases {
		all["routing/"+k] = v
	}
	for k, v := range zeroTenantTreeLevelCases {
		all["tree/"+k] = v
	}
	untenanted := func(fs []reportedFinding) []string {
		var out []string
		for _, f := range fs {
			if f.TenantID == "" {
				out = append(out, f.Kind+" "+f.Field)
			}
		}
		sort.Strings(out)
		return out
	}
	for name, tc := range all {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			_, zero := guardFindingsOf(t, tc.files, tc.scopeArgs()...)
			_, one := guardFindingsOf(t, tc.withCleanTenant(), tc.scopeArgs()...)
			if z, o := untenanted(zero), untenanted(one); !reflect.DeepEqual(z, o) {
				t.Errorf("findings naming no tenant differ:\nzero tenants: %v\none tenant:   %v", z, o)
			}
		})
	}
}

// The `{{tenant}}` shape of `_routing_enforced` renders one route per
// tenant: before #2904 the CLI judged its group_by only with a tenant (exit 0
// without one, 1 with one), as the generator does. Since #2904 it judges
// neither: both runs are undetermined, with no finding.
func TestGuard_EnforcedTenantShapeGroupBy_IsUndetermined(t *testing.T) {
	t.Parallel()
	tc := zeroTenantPlatformCase{
		files: map[string]string{"_noc.yaml": "_routing_enforced:\n  enabled: true\n" +
			"  receiver: {type: webhook, url: \"https://noc.example.com/{{tenant}}\"}\n" +
			"  group_by: [\"alertname\", 5]\n"},
	}
	for label, files := range map[string]map[string]string{"no tenant": tc.files, "one tenant": tc.withCleanTenant()} {
		if got := runJudged(t, files, ""); got.code != exitUndetermined || len(got.findings) != 0 {
			t.Errorf("%s: exit %d, findings %v; want 4 and none", label, got.code, got.findings)
		}
	}
}

// The brand-new domain skeleton stays vacuously safe when it holds no
// routing: no tenant — exit 0, the empty report. With routing in it
// (`_routing_defaults`, a routing profiles file) it is undetermined since
// #2904: exit 4, the evidence listed, not called safe.
func TestGuard_CleanSkeletonWithNoTenantIsVacuous(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name  string
		files map[string]string
		code  int
	}{
		{"no routing", map[string]string{
			"conf.d/_defaults.yaml":      "defaults:\n  cpu: 80\n",
			"conf.d/team/_defaults.yaml": "defaults:\n  cpu: 70\n",
		}, exitOK},
		{"routing", map[string]string{
			"conf.d/_defaults.yaml":         "defaults:\n  cpu: 80\n_routing_defaults:\n  receiver: " + webhookReceiver + "\n",
			"conf.d/_routing_profiles.yaml": "routing_profiles:\n  p1:\n    receiver: " + webhookReceiver + "\n",
			"conf.d/team/_defaults.yaml":    "defaults:\n  cpu: 70\n",
		}, exitUndetermined},
	} {
		tmp := t.TempDir()
		testutil.WriteTree(t, tmp, tc.files)
		code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
		if code != tc.code || strings.Contains(stdout, "vacuously safe") != (tc.code == exitOK) {
			t.Errorf("%s: exit %d, stdout %q, stderr %q: want exit %d, the vacuous report iff 0", tc.name, code, stdout, stderr, tc.code)
		}
	}
}

// The markdown report of a zero-tenant run with a tree-level finding is the
// ordinary report — 0 tenants, the error listed — not the vacuous one.
func TestGuard_PlatformProblemOnZeroTenantScope_Markdown(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{"conf.d/_defaults.yaml": "cpu: 80\n"})
	code, stdout, _ := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitFindings {
		t.Errorf("exit %d, want %d", code, exitFindings)
	}
	for _, want := range []string{"Tenants in scope: **0**", "Errors: **1**", findingkind.FindingRootDefaultsUnwrapped.String()} {
		if !strings.Contains(stdout, want) {
			t.Errorf("report lacks %q:\n%s", want, stdout)
		}
	}
	if strings.Contains(stdout, "vacuously safe") {
		t.Errorf("report calls the run vacuously safe:\n%s", stdout)
	}
}
