package main

// #2991: what the guard judges without a tenant — platform-file problems
// (guard.CheckInput.PlatformProblems, read from the whole tree at
// --config-dir), the `_routing_enforced` route, the defaults carriers — is
// reported when the scope has no tenant, as it is when the scope has some.
// The "no tenants → vacuously safe" exit 0 holds only for a run with no
// finding.

import (
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
)

// zeroTenantPlatformCase is one routingpolicy Problem kind on a scope with
// no tenant: the files of the conf.d tree, the --scope (relative to it; ""
// = the whole tree) and the finding the run must carry.
type zeroTenantPlatformCase struct {
	files   map[string]string
	scope   string
	finding findingkind.Kind
}

const webhookReceiver = "{type: webhook, url: \"https://w.example/h\"}"

// zeroTenantPlatformCases: one row per routingpolicy Problem kind, keyed by
// it. TestGuard_ZeroTenantPlatformCasesCoverEveryKind holds the keys to the
// kinds pkg/routingpolicy declares, so a new kind fails until it has a row.
// Two kinds need a tenant somewhere in the tree (a duplicate id, a policy
// naming a tenant outside its subtree); their tree has tenants, but the
// --scope has none.
var zeroTenantPlatformCases = map[string]zeroTenantPlatformCase{
	routingpolicy.ProblemDomainPolicyUnusable: {
		files:   map[string]string{"_domain_policy.yaml": "domain_policies:\n- fin\n"},
		finding: findingkind.FindingDomainPolicyUnusable,
	},
	routingpolicy.ProblemRoutingProfilesUnusable: {
		files:   map[string]string{"_routing_profiles.yaml": "routing_profiles:\n- p-team\n"},
		finding: findingkind.FindingRoutingProfilesUnusable,
	},
	routingpolicy.ProblemRoutingDefaultsRoutes: {
		files: map[string]string{"_defaults.yaml": "_routing_defaults:\n  receiver: " + webhookReceiver +
			"\n  routes:\n  - receiver: " + webhookReceiver + "\n"},
		finding: findingkind.FindingRoutingDefaultsRoutesIgnored,
	},
	routingpolicy.ProblemRoutingInUnreadLocation: {
		files: map[string]string{"_profiles.yaml": "profiles:\n  p1:\n    cpu: 60\n    _routing:\n" +
			"      receiver: " + webhookReceiver + "\n"},
		finding: findingkind.FindingRoutingInUnreadLocation,
	},
	routingpolicy.ProblemRoutingEnforcedBelowRoot: {
		files: map[string]string{"team/_defaults.yaml": "_routing_enforced:\n  enabled: true\n" +
			"  receiver: " + webhookReceiver + "\n"},
		finding: findingkind.FindingRoutingEnforcedBelowRoot,
	},
	routingpolicy.ProblemRoutingDefaultsNullBelowRoot: {
		files:   map[string]string{"team/_defaults.yaml": "_routing_defaults:\n  receiver: null\n"},
		finding: findingkind.FindingRoutingDefaultsNullBelowRoot,
	},
	routingpolicy.ProblemRoutingProfileDuplicate: {
		files: map[string]string{
			"_routing_profiles.yaml": "routing_profiles:\n  p2:\n    receiver: " + webhookReceiver + "\n",
			"_routing_profiles.yml":  "routing_profiles:\n  p2:\n    receiver: " + webhookReceiver + "\n",
		},
		finding: findingkind.FindingRoutingProfileDuplicate,
	},
	routingpolicy.ProblemDuplicateTenant: {
		files: map[string]string{
			"t-x.yaml":      "tenants:\n  t-x:\n    cpu: 1\n",
			"team/t-x.yaml": "tenants:\n  t-x:\n    cpu: 2\n",
			"empty/":        "",
		},
		scope:   "empty",
		finding: findingkind.FindingDuplicateTenant,
	},
	routingpolicy.ProblemDomainPolicyOutOfScope: {
		files: map[string]string{
			"t-root.yaml": "tenants:\n  t-root:\n    cpu: 1\n",
			"team/_domain_policy.yaml": "domain_policies:\n  fin:\n    tenants: [t-root]\n" +
				"    constraints: {forbidden_receiver_types: [slack]}\n",
			"empty/": "",
		},
		scope:   "empty",
		finding: findingkind.FindingDomainPolicyOutOfScope,
	},
	routingpolicy.ProblemRoutingDefaultsNotMapping: {
		files:   map[string]string{"_defaults.yaml": "_routing_defaults: [1, 2]\n"},
		finding: findingkind.FindingRoutingDefaultsNotMapping,
	},
	routingpolicy.ProblemRoutingKeyIgnored: {
		files:   map[string]string{"_defaults.yaml": "_routing_defaults:\n  continue: true\n"},
		finding: findingkind.FindingRoutingKeyIgnored,
	},
}

func TestGuard_ZeroTenantPlatformCasesCoverEveryKind(t *testing.T) {
	t.Parallel()
	declared := map[string]bool{}
	for name, kind := range testutil.RoutingPolicyProblemKinds(t) {
		declared[kind] = true
		if _, ok := zeroTenantPlatformCases[kind]; !ok {
			t.Errorf("routingpolicy.%s (%q) has no row in zeroTenantPlatformCases: add a zero-tenant tree that reports it", name, kind)
		}
	}
	for kind := range zeroTenantPlatformCases {
		if !declared[kind] {
			t.Errorf("zeroTenantPlatformCases row %q is not a Problem kind pkg/routingpolicy declares", kind)
		}
	}
}

// zeroTenantTreeLevelCases: the other findings judged without a tenant —
// the `_routing_enforced` route the generator renders whatever the tenants
// (#2503) and the defaults carriers (#2386). Not a list the code reads:
// the guard runs every check on zero tenants, these rows only pin that.
var zeroTenantTreeLevelCases = map[string]zeroTenantPlatformCase{
	// generate_alertmanager_routes.py --validate --strict on this tree:
	// "ERROR: _routing_enforced: group_by[1] is int 5, not a string", rc 1.
	"enforced group_by, single shape": {
		files: map[string]string{"_noc.yaml": "_routing_enforced:\n  enabled: true\n  receiver: " + webhookReceiver +
			"\n  group_by: [\"alertname\", 5]\n"},
		finding: findingkind.FindingRoutingGroupByInvalid,
	},
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

// zeroTenantKindClass sorts every registered finding kind: kindNeedsTenant
// when every finding of it judges a tenant in scope (none can come out of a
// scope with no tenant), kindTreeLevel when some finding of it is judged
// without one. TestGuard_EveryFindingKindIsClassifiedForZeroTenants holds
// it to findingkind.All() and holds every kindTreeLevel kind to a row of
// zeroTenantPlatformCases / zeroTenantTreeLevelCases — so a new kind is red
// until someone says which it is, and a tree-level one until a zero-tenant
// run is shown to report it.
const (
	kindNeedsTenant = "needs a tenant"
	kindTreeLevel   = "tree level"
)

var zeroTenantKindClass = map[string]string{
	// Judged per tenant in EffectiveConfigs / RoutingByTenant / the
	// per-tenant maps buildCheckInput fills from scoped.Tenants.
	"missing_required":              kindNeedsTenant,
	"redundant_override":            kindNeedsTenant,
	"unknown_receiver_type":         kindNeedsTenant,
	"missing_receiver_field":        kindNeedsTenant,
	"conflicting_receiver_field":    kindNeedsTenant,
	"invalid_receiver_field":        kindNeedsTenant,
	"empty_override_matcher":        kindNeedsTenant,
	"conflicting_override_matcher":  kindNeedsTenant,
	"duplicate_override_matcher":    kindNeedsTenant,
	"redundant_override_receiver":   kindNeedsTenant,
	"invalid_route_entry":           kindNeedsTenant,
	"routing_value_not_string":      kindNeedsTenant,
	"domain_policy_violation":       kindNeedsTenant,
	"critical_escalation_missing":   kindNeedsTenant,
	"critical_escalation_leak":      kindNeedsTenant,
	"unknown_routing_profile":       kindNeedsTenant,
	"routing_not_mapping":           kindNeedsTenant,
	"invalid_tenant_id":             kindNeedsTenant,
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

	// PlatformProblems (whole tree at --config-dir).
	"domain_policy_unusable":           kindTreeLevel,
	"routing_profiles_unusable":        kindTreeLevel,
	"routing_defaults_routes_ignored":  kindTreeLevel,
	"routing_in_unread_location":       kindTreeLevel,
	"routing_key_ignored":              kindTreeLevel, // also per tenant
	"routing_defaults_not_mapping":     kindTreeLevel,
	"routing_enforced_below_root":      kindTreeLevel,
	"routing_defaults_null_below_root": kindTreeLevel,
	"routing_profile_duplicate":        kindTreeLevel,
	"duplicate_tenant":                 kindTreeLevel,
	"domain_policy_out_of_scope":       kindTreeLevel,
	// The single-shape `_routing_enforced` route (also per tenant).
	"routing_group_by_invalid": kindTreeLevel,
	// The defaults carriers (DefaultsFiles).
	"root_defaults_unwrapped":    kindTreeLevel,
	"defaults_toplevel_ignored":  kindTreeLevel,
	"root_defaults_critical_key": kindTreeLevel,
	// The whole tree's /metrics.
	"metrics_not_gatherable": kindTreeLevel,
}

func TestGuard_EveryFindingKindIsClassifiedForZeroTenants(t *testing.T) {
	t.Parallel()
	covered := map[string]bool{}
	for _, table := range []map[string]zeroTenantPlatformCase{zeroTenantPlatformCases, zeroTenantTreeLevelCases} {
		for name, tc := range table {
			covered[tc.finding.String()] = true
			if zeroTenantKindClass[tc.finding.String()] != kindTreeLevel {
				t.Errorf("row %q reports %q, which zeroTenantKindClass does not call %q", name, tc.finding, kindTreeLevel)
			}
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

func TestGuard_PlatformProblemOnZeroTenantScopeFails(t *testing.T) {
	t.Parallel()
	for kind, tc := range zeroTenantPlatformCases {
		t.Run(kind, func(t *testing.T) {
			t.Parallel()
			assertZeroTenantFinding(t, tc)
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
// tenant, so with no tenant there is no route to judge: the generator
// (--validate --strict) prints no group_by error for it, and neither does
// da-guard — the vacuous 0. With a tenant, both report it.
func TestGuard_EnforcedTenantShapeGroupBy_FollowsTheTenants(t *testing.T) {
	t.Parallel()
	tc := zeroTenantPlatformCase{
		files: map[string]string{"_noc.yaml": "_routing_enforced:\n  enabled: true\n" +
			"  receiver: {type: webhook, url: \"https://noc.example.com/{{tenant}}\"}\n" +
			"  group_by: [\"alertname\", 5]\n"},
	}
	code, fs := guardFindingsOf(t, tc.files)
	if code != exitOK || len(fs) != 0 {
		t.Errorf("no tenant: exit %d, findings %+v; want 0 and none", code, fs)
	}
	code, fs = guardFindingsOf(t, tc.withCleanTenant())
	n := 0
	for _, f := range fs {
		if f.Kind == findingkind.FindingRoutingGroupByInvalid.String() {
			n++
		}
	}
	if code != exitFindings || n != 1 {
		t.Errorf("one tenant: exit %d, findings %+v; want 1 and one %s", code, fs, findingkind.FindingRoutingGroupByInvalid)
	}
}

// The brand-new domain skeleton stays vacuously safe: platform files the
// routing checks can use, no tenant — exit 0, the empty report.
func TestGuard_CleanSkeletonWithNoTenantIsVacuous(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":         "defaults:\n  cpu: 80\n_routing_defaults:\n  receiver: " + webhookReceiver + "\n",
		"conf.d/_routing_profiles.yaml": "routing_profiles:\n  p1:\n    receiver: " + webhookReceiver + "\n",
		"conf.d/team/_defaults.yaml":    "defaults:\n  cpu: 70\n",
	})
	code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitOK || !strings.Contains(stdout, "vacuously safe") {
		t.Errorf("exit %d, stdout %q, stderr %q: want exit 0 and the vacuous report", code, stdout, stderr)
	}
}

// The markdown report of a zero-tenant run with a platform problem is the
// ordinary report — 0 tenants, the error listed — not the vacuous one.
func TestGuard_PlatformProblemOnZeroTenantScope_Markdown(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{"conf.d/_defaults.yaml": "_routing_defaults: [1, 2]\n"})
	code, stdout, _ := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitFindings {
		t.Errorf("exit %d, want %d", code, exitFindings)
	}
	for _, want := range []string{"Tenants in scope: **0**", "Errors: **1**", findingkind.FindingRoutingDefaultsNotMapping.String()} {
		if !strings.Contains(stdout, want) {
			t.Errorf("report lacks %q:\n%s", want, stdout)
		}
	}
	if strings.Contains(stdout, "vacuously safe") {
		t.Errorf("report calls the run vacuously safe:\n%s", stdout)
	}
}
