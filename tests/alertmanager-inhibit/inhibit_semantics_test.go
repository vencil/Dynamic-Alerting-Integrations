package aminhibit

import (
	"slices"
	"testing"

	"github.com/prometheus/alertmanager/config/common"
)

// Fixtures are REAL alerts with the label set Alertmanager actually receives —
// which is NOT the same as a rule pack's static `labels:` block:
//
//   - `alertname` is added by Prometheus, not declared in the pack.
//   - `tenant` is templated from $labels in every pack (all 115 alerts carry it).
//   - `metric_group` IS declared statically, and is deliberately ABSENT on many
//     alerts — absence opts an alert out of Severity Dedup (see the "by-design"
//     note on kubernetes-ha-replicas in rule-pack-kubernetes.yaml). 16 of 38
//     criticals and 49 of 71 warnings have no metric_group, so the metric_group-
//     less pairing below is the common case, not an edge case.
//   - `name` on Custom_* alerts is NOT in the pack's labels block at all — it
//     rides in via group_left(name, mode) from the recording rule
//     (scripts/tools/dx/custom_alerts/recipes.py). Deriving label sets by
//     reading `labels:` would therefore get these fixtures WRONG; they are
//     written by hand against runtime reality on purpose.
//
// Tenant ids (dev-rules #2): try-local's inhibit rules — and k8s's Silent Mode /
// Custom Alert rules — pin NO tenant. They are tenant-agnostic, so any two
// distinct ids exercise them fully and the synthetic pair below is deliberate
// (a real tenant id would imply a coupling that does not exist). Only the k8s
// per-tenant dedup rules pin a literal tenant; that suite reads the ids back out
// of the config via dedupTenants() instead of naming them, so a fixture can
// never quietly stop matching the rule it is meant to exercise.
const (
	tenantX = "tenant-x"
	tenantY = "tenant-y"
)

// ---- try-local (loads rule-pack-mariadb + rule-pack-operational only) ----

var (
	tlCriticalConnections = lset{
		"alertname": "MariaDBHighConnectionsCritical",
		"severity":  "critical", "tenant": tenantX, "metric_group": "connections",
	}
	tlWarningConnections = lset{
		"alertname": "MariaDBHighConnections",
		"severity":  "warning", "tenant": tenantX, "metric_group": "connections",
	}
	tlWarningCPU = lset{
		"alertname": "MariaDBHighThreadsRunning",
		"severity":  "warning", "tenant": tenantX, "metric_group": "cpu",
	}
	tlWarningOtherTenant = lset{
		"alertname": "MariaDBHighConnections",
		"severity":  "warning", "tenant": tenantY, "metric_group": "connections",
	}
	// critical with NO metric_group — deliberately opted out of dedup.
	tlCriticalNoGroup = lset{
		"alertname": "MariaDBExporterAbsent",
		"severity":  "critical", "tenant": tenantX,
	}
	// warning with NO metric_group — the alert #1132 was silently eating.
	tlWarningNoGroup = lset{
		"alertname": "TenantConfigEvent",
		"severity":  "warning", "tenant": tenantX,
	}
	tlSilentWarnSentinel = lset{
		"alertname": "TenantSilentWarning",
		"severity":  "none", "component": "sentinel", "tenant": tenantX,
	}
	tlSilentCritSentinel = lset{
		"alertname": "TenantSilentCritical",
		"severity":  "none", "component": "sentinel", "tenant": tenantX,
	}
	tlDedupSentinel = lset{
		"alertname": "TenantSeverityDedupEnabled",
		"severity":  "none", "component": "sentinel", "tenant": tenantX,
	}
)

func TestTryLocalInhibitSemantics(t *testing.T) {
	rules := loadPlainAM(t, "try-local/alertmanager.yml")

	run(t, rules, []check{
		{
			name: "dedup_suppresses_paired_warning",
			src:  tlCriticalConnections, tgt: tlWarningConnections, want: true,
			why: "the headline demo behaviour: a critical must suppress its paired warning",
		},
		{
			name: "dedup_is_scoped_to_metric_group",
			src:  tlCriticalConnections, tgt: tlWarningCPU, want: false,
			why: "a connections critical must not silence an unrelated cpu warning",
		},
		{
			name: "dedup_is_scoped_to_tenant",
			src:  tlCriticalConnections, tgt: tlWarningOtherTenant, want: false,
			why: "cross-tenant suppression would break tenant isolation",
		},
		{
			// THE #1132 REGRESSION GUARD. Both alerts lack metric_group, so AM
			// reads ""=="" as equal. The metric_group=~".+" gates are what keep
			// this pair out of the rule; drop BOTH and it flips to true —
			// silently, with amtool still green.
			//
			// Either gate alone still excludes the pair (a metric_group-less
			// alert fails whichever gate remains), so they are defence in depth
			// rather than two independently-load-bearing guards — verified by
			// mutating each gate out separately. Do not read this case as
			// pinning both.
			name: "metric_group_less_critical_does_not_eat_metric_group_less_warning",
			src:  tlCriticalNoGroup, tgt: tlWarningNoGroup, want: false,
			why: "#1132: missing-on-both-sides counts as equal; the =~\".+\" gates are what exclude these",
		},
		{
			// The other half of #1132: the sentinel says "dedup is ENABLED for
			// this tenant", never "a critical fired" — it cannot express dedup
			// and must not be a source. severity=none keeps it out now.
			name: "dedup_sentinel_is_not_an_inhibit_source",
			src:  tlDedupSentinel, tgt: tlWarningNoGroup, want: false,
			why: "#1132 root cause: TenantSeverityDedupEnabled as source suppressed metric_group-less warnings",
		},
		{
			name: "silent_warning_suppresses_tenant_warning",
			src:  tlSilentWarnSentinel, tgt: tlWarningConnections, want: true,
			why: "Silent Mode (warning) must suppress that tenant's warnings",
		},
		{
			name: "silent_warning_is_scoped_to_tenant",
			src:  tlSilentWarnSentinel, tgt: tlWarningOtherTenant, want: false,
			why: "one tenant's silent mode must not silence another's",
		},
		{
			name: "silent_warning_leaves_critical_alone",
			src:  tlSilentWarnSentinel, tgt: tlCriticalConnections, want: false,
			why: "tri-state: silencing warnings must not silence criticals",
		},
		{
			name: "silent_critical_suppresses_tenant_critical",
			src:  tlSilentCritSentinel, tgt: tlCriticalConnections, want: true,
			why: "Silent Mode (critical) must suppress that tenant's criticals",
		},
	})
}

// ---- k8s (the checked-in ConfigMap Alertmanager actually loads) ----

// TestK8sPerTenantDedupSemantics covers the GENERATED per-tenant dedup rules,
// which pin a literal tenant in their matchers. The fixtures must therefore use
// ids read back out of the config: a synthetic tenant would match no rule and
// every `want: false` case would pass vacuously.
func TestK8sPerTenantDedupSemantics(t *testing.T) {
	rules := loadConfigMapAM(t, "k8s/03-monitoring/configmap-alertmanager.yaml", "alertmanager.yml")
	tenants := dedupTenants(t, rules)
	tA := tenants[0]

	critA := lset{
		"alertname": "MariaDBHighConnectionsCritical",
		"severity":  "critical", "tenant": tA, "metric_group": "connections",
	}
	warnA := lset{
		"alertname": "MariaDBHighConnections",
		"severity":  "warning", "tenant": tA, "metric_group": "connections",
	}
	critNoGroupA := lset{
		"alertname": "PostgreSQLDown",
		"severity":  "critical", "tenant": tA,
	}
	warnNoGroupA := lset{
		"alertname": "TenantConfigEvent",
		"severity":  "warning", "tenant": tA,
	}

	run(t, rules, []check{
		{
			name: "per_tenant_dedup_suppresses_paired_warning",
			src:  critA, tgt: warnA, want: true,
			why: "the generated per-tenant dedup rule must actually dedup",
		},
		{
			// Same class as #1132, checked on the production config.
			name: "metric_group_less_critical_does_not_eat_metric_group_less_warning",
			src:  critNoGroupA, tgt: warnNoGroupA, want: false,
			why: "both lack metric_group; the =~\".+\" gates keep them out of the dedup rule",
		},
	})

	// Cross-tenant isolation needs a second deployed tenant. Scoped to a subtest
	// so a single-tenant config cannot silently drop the assertions above.
	t.Run("cross_tenant_isolation", func(t *testing.T) {
		if len(tenants) < 2 {
			t.Skipf("config pins %d tenant(s); need 2 to prove cross-tenant isolation", len(tenants))
		}
		tB := tenants[1]
		warnB := lset{
			"alertname": "MariaDBHighConnections",
			"severity":  "warning", "tenant": tB, "metric_group": "connections",
		}
		critB := lset{
			"alertname": "MariaDBHighConnectionsCritical",
			"severity":  "critical", "tenant": tB, "metric_group": "connections",
		}

		// BOTH directions. The generated rule's `equal:` is ["metric_group"]
		// ONLY — tenant isolation rests entirely on the tenant pins in the
		// source AND target matchers, so a single direction leaves the source-
		// side pin untested (mutating it out survives a one-direction test).
		run(t, rules, []check{
			{
				name: "tenant_a_critical_does_not_suppress_tenant_b_warning",
				src:  critA, tgt: warnB, want: false,
				why: "target-side tenant pin: equal:[metric_group] alone would match across tenants",
			},
			{
				name: "tenant_b_critical_does_not_suppress_tenant_a_warning",
				src:  critB, tgt: warnA, want: false,
				why: "source-side tenant pin: the mirror direction, otherwise untested",
			},
		})
	})
}

// TestK8sTenantAgnosticInhibitSemantics covers the rules that pin NO tenant —
// Silent Mode and per-recipe Custom Alert silence. They are tenant-agnostic by
// construction, so synthetic ids exercise them fully (dev-rules #2).
func TestK8sTenantAgnosticInhibitSemantics(t *testing.T) {
	rules := loadConfigMapAM(t, "k8s/03-monitoring/configmap-alertmanager.yaml", "alertmanager.yml")

	// `name` and `mode` reach the Custom_* alerts via group_left — see header.
	customSilentSentinelX := lset{
		"alertname": "CustomRecipeSilent",
		"severity":  "none", "component": "sentinel", "tenant": tenantX, "name": "slow-queries",
	}
	customAlertSameRecipeX := lset{
		"alertname": "Custom_rate__mysql_global_status_slow_queries__gt__w5m__for1m",
		"severity":  "warning", "component": "custom", "tenant": tenantX,
		"name": "slow-queries", "mode": "silent", "recipe": "rate",
	}
	customAlertOtherRecipeX := lset{
		"alertname": "Custom_threshold__mysql_global_status_threads_connected__gt__w5m__for1m",
		"severity":  "warning", "component": "custom", "tenant": tenantX,
		"name": "conn-ceiling", "mode": "page", "recipe": "threshold",
	}
	// SAME recipe name, DIFFERENT tenant — the case that pins `tenant` in
	// equal:[tenant,name]. Without it, dropping tenant from equal survives.
	customAlertSameRecipeY := lset{
		"alertname": "Custom_rate__mysql_global_status_slow_queries__gt__w5m__for1m",
		"severity":  "warning", "component": "custom", "tenant": tenantY,
		"name": "slow-queries", "mode": "silent", "recipe": "rate",
	}
	platformWarningX := lset{
		"alertname": "MariaDBHighConnections",
		"severity":  "warning", "tenant": tenantX, "metric_group": "connections",
	}
	silentWarnSentinelX := lset{
		"alertname": "TenantSilentWarning",
		"severity":  "none", "component": "sentinel", "tenant": tenantX,
	}

	run(t, rules, []check{
		{
			name: "custom_recipe_silent_suppresses_its_own_recipe",
			src:  customSilentSentinelX, tgt: customAlertSameRecipeX, want: true,
			why: "#741 S7/S8: a silent recipe's notification must be inhibited",
		},
		{
			// `name` is a group_left label, so this assertion is exactly the one
			// a labels-block-derived gate would get wrong.
			name: "custom_recipe_silent_is_scoped_to_one_recipe",
			src:  customSilentSentinelX, tgt: customAlertOtherRecipeX, want: false,
			why: "a silent recipe must not silence a page-mode recipe of the same tenant",
		},
		{
			// Tenant isolation for Custom Alerts. Recipe names are tenant-chosen
			// and WILL collide across tenants, so `tenant` in equal: is the only
			// thing preventing one tenant's silence from muting another's alert.
			name: "custom_recipe_silent_does_not_cross_tenants",
			src:  customSilentSentinelX, tgt: customAlertSameRecipeY, want: false,
			why: "same recipe name, different tenant: equal:[tenant,name] must keep these apart",
		},
		{
			// NOTE ON WHY THIS PASSES: platform alerts carry no `name`, so
			// equal:[tenant,name] already separates them ("" != "slow-queries").
			// The component="custom" target matcher is defence in depth on top of
			// that — mutating it out does NOT turn this red. Kept because the
			// property (a recipe silence must not mute platform alerts) is worth
			// stating; do not read it as pinning the component matcher.
			name: "custom_recipe_silent_does_not_touch_platform_alerts",
			src:  customSilentSentinelX, tgt: platformWarningX, want: false,
			why: "platform alerts carry no name, so equal:[tenant,name] excludes them",
		},
		{
			name: "silent_warning_suppresses_tenant_warning",
			src:  silentWarnSentinelX, tgt: platformWarningX, want: true,
			why: "Silent Mode (warning) must suppress that tenant's warnings",
		},
		{
			name: "silent_warning_is_scoped_to_tenant",
			src:  silentWarnSentinelX, tgt: lset{
				"alertname": "MariaDBHighConnections",
				"severity":  "warning", "tenant": tenantY, "metric_group": "connections",
			}, want: false,
			why: "one tenant's silent mode must not silence another's",
		},
	})
}

// TestEveryInhibitRuleIsTenantScoped asserts the platform's core multi-tenancy
// invariant structurally, over EVERY rule in both hand-written configs:
//
//	a rule may only suppress across alerts of the SAME tenant.
//
// An Alertmanager inhibit rule can achieve that two ways, and both are in use:
//   - `tenant` listed in equal:      — Silent Mode, Custom Alert silence, try-local dedup
//   - tenant pinned to the same literal on BOTH matcher sides — the k8s generated
//     per-tenant dedup rules, whose equal: is ["metric_group"] alone
//
// A rule doing NEITHER can suppress one tenant's alert with another tenant's —
// the worst failure this config can express (dev-rules #2). Fixture pairs cannot
// cover this: they only probe the tenant ids that happen to be deployed, and a
// rule added later gets no fixture at all. This check needs no fixtures and
// covers rules that do not exist yet.
func TestEveryInhibitRuleIsTenantScoped(t *testing.T) {
	for _, cfg := range []struct {
		label string
		rules []common.InhibitRule
	}{
		{"try-local/alertmanager.yml", loadPlainAM(t, "try-local/alertmanager.yml")},
		{"k8s configmap-alertmanager.yaml", loadConfigMapAM(t,
			"k8s/03-monitoring/configmap-alertmanager.yaml", "alertmanager.yml")},
	} {
		t.Run(cfg.label, func(t *testing.T) {
			for i, r := range cfg.rules {
				if slices.Contains(r.Equal, "tenant") {
					continue // scoped via equal:
				}
				src, srcOK := pinnedTenant(r.SourceMatchers)
				tgt, tgtOK := pinnedTenant(r.TargetMatchers)
				if srcOK && tgtOK && src == tgt {
					continue // scoped via matching literal pins on both sides
				}
				t.Errorf("rule[%d] is not provably tenant-scoped: `tenant` is not in equal:%v, and "+
					"the matcher pins do not agree (source pinned=%v:%q, target pinned=%v:%q).\n"+
					"  source_matchers: %v\n  target_matchers: %v\n"+
					"Fix by adding `tenant` to equal:, or by pinning the SAME tenant on both sides.\n"+
					"NOTE: only an exact `tenant=\"x\"` matcher counts as a pin here. A regex pin "+
					"(tenant=~\"x\") may well be safe, but this check does not reason about regex "+
					"equivalence and reports it conservatively rather than risk a false green.",
					i, r.Equal, srcOK, src, tgtOK, tgt, r.SourceMatchers, r.TargetMatchers)
			}
		})
	}
}

// TestTwoSidedMatchExclusionMirrorsAlertmanager pins the excludeTwoSidedMatch
// clause in inhibits().
//
// Every rule in the repo today has disjoint sides, so NO assertion above covers
// that clause — deleting it leaves the whole suite green. This synthetic
// overlapping rule is the only thing standing between it and a future
// "simplification", and without it a later rule whose sides overlap would be
// mis-modelled (harness says suppressed, production does not).
//
// Alertmanager v0.33.1 inhibit.go: Mutes computes
// `excludeTwoSidedMatch := r.SourceMatchers.Matches(target)` and hasEqual then
// skips any candidate source whose labels match TargetMatchers — i.e. exclude
// when SourceMatchers(target) AND TargetMatchers(source).
func TestTwoSidedMatchExclusionMirrorsAlertmanager(t *testing.T) {
	// Both sides select the same alerts, so any two warnings of one tenant match
	// source AND target.
	rules := parseAM(t, "two-sided overlap (synthetic)", `
inhibit_rules:
  - source_matchers: ['severity = "warning"']
    target_matchers: ['severity = "warning"']
    equal: ['tenant']
`)
	a := lset{"alertname": "AlertA", "severity": "warning", "tenant": tenantX}
	b := lset{"alertname": "AlertB", "severity": "warning", "tenant": tenantX}

	if muted(rules, a, b) {
		t.Error("two alerts that each satisfy BOTH sides must not inhibit each other: " +
			"Alertmanager excludes the two-sided match, so inhibits() must too")
	}
	// Self-inhibition is the degenerate case of the same exclusion.
	if muted(rules, a, a) {
		t.Error("an alert must not inhibit itself")
	}
}

// TestHarnessReproducesTheHistoricalBug pins this gate's resolution against the
// one case where we have ground truth: the pre-#1132 try-local rule, whose real
// production behaviour is documented in that PR.
//
// If this test ever goes green in the "want: true" case, the harness has lost
// the ability to see the bug class it exists to catch, and the assertions above
// would be worthless.
func TestHarnessReproducesTheHistoricalBug(t *testing.T) {
	buggy := parseAM(t, "pre-#1132 try-local rule (historical)", `
inhibit_rules:
  - source_matchers: ['alertname = "TenantSeverityDedupEnabled"']
    target_matchers: ['severity = "warning"']
    equal: ['tenant', 'metric_group']
`)

	run(t, buggy, []check{
		{
			// Failure direction 1 — dedup DEAD. The sentinel carries no
			// metric_group ("") while the warning carries "connections", so they
			// are never equal and the rule could never fire as intended.
			name: "historical_dedup_was_dead",
			src:  tlDedupSentinel, tgt: tlWarningConnections, want: false,
			why: "sentinel has no metric_group, so it never matched a warning that has one",
		},
		{
			// Failure direction 2 — SILENT OVER-SUPPRESSION. Neither side has
			// metric_group, AM reads ""=="" as equal, and the warning is eaten
			// whenever dedup is on for the tenant. This is the one that lost
			// real notifications.
			name: "historical_rule_silently_ate_metric_group_less_warnings",
			src:  tlDedupSentinel, tgt: tlWarningNoGroup, want: true,
			why: "both sides lack metric_group -> AM treats as equal -> TenantConfigEvent suppressed",
		},
	})
}
