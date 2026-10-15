package main

// #2991: a platform-file problem (guard.CheckInput.PlatformProblems, read
// from the whole tree at --config-dir) is reported when the scope has no
// tenant, as it is when the scope has some — the "no tenants → vacuously
// safe" exit 0 holds only when there is nothing whole-tree to report.

import (
	"path/filepath"
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

func TestGuard_PlatformProblemOnZeroTenantScopeFails(t *testing.T) {
	t.Parallel()
	for kind, tc := range zeroTenantPlatformCases {
		t.Run(kind, func(t *testing.T) {
			t.Parallel()
			var args []string
			if tc.scope != "" {
				args = append(args, "--scope", tc.scope)
			}
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
				// A warning fails the run under --warn-as-error, as with tenants.
				want = exitOK
				if c, _ := guardFindingsOf(t, tc.files, append(args, "--warn-as-error")...); c != exitFindings {
					t.Errorf("--warn-as-error: exit %d, want %d", c, exitFindings)
				}
			}
			if code != want {
				t.Errorf("exit %d, want %d (findings %+v)", code, want, fs)
			}
		})
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
