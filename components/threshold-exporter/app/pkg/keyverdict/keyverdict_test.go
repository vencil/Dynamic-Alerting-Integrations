package keyverdict

// The branches of Judge no tree today reaches through da-guard — the
// reserved-key registry declares no reader that would take them — over
// synthetic readings. The trees da-guard reads are pinned by
// tests/shared/test_key_verdict.py and cmd/da-guard/key_verdict_golden_test.go.
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func level(n int) *int { return &n }

func one(t *testing.T, tree Tree, tenant, key string) KeyVerdict {
	t.Helper()
	for _, v := range tree.Keys[tenant] {
		if v.Key == key {
			return v
		}
	}
	t.Fatalf("%s has no verdict on %s: %+v", tenant, key, tree.Keys[tenant])
	return KeyVerdict{}
}

// A reserved key the tenant inherits from a layer the exporter reads it in,
// which the exporter's resolver read, is read by the exporter — though the
// served reading has no entry for it (it is not in the merged config).
func TestJudge_AnInheritedReservedKeyTheResolverReadIsRead(t *testing.T) {
	t.Parallel()
	tree := Judge(Input{
		Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{},
			Values: map[string]any{"_silent_mode": []string{"warning"}}}},
		Effective: map[string]EffectiveTenant{"t1": {Config: map[string]any{"_silent_mode": "warning"},
			Sources: map[string]config.KeySource{"_silent_mode": {Layer: config.KeyLayerProfile, File: "_profiles.yaml"}}}},
	})
	v := one(t, tree, "t1", "_silent_mode")
	if v.Verdict != Read || !reflect.DeepEqual(v.ReadBy, []string{"exporter"}) {
		t.Fatalf("verdict %+v", v)
	}
	// With no reading, the same key is read by no one.
	tree = Judge(Input{
		Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{}, Values: map[string]any{}}},
		Effective: map[string]EffectiveTenant{"t1": {Config: map[string]any{"_silent_mode": "warning"},
			Sources: map[string]config.KeySource{"_silent_mode": {Layer: config.KeyLayerProfile, File: "_profiles.yaml"}}}},
	})
	if v := one(t, tree, "t1", "_silent_mode"); v.Verdict != Unread || deref(v.Severity) != SeverityError {
		t.Fatalf("no reading: %+v", v)
	}
}

// A key from a subtree `_defaults.yaml` (level > 0) is in the registry's
// subtree_defaults layer: a reader declared there reads it; the root
// `defaults:` (level 0) is no registry layer.
func TestJudge_ASubtreeDefaultsSourceIsTheSubtreeLayer(t *testing.T) {
	t.Parallel()
	in := func(lvl int) Input {
		yes := true
		return Input{
			Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{"_k": {Reason: "not_exporter_key",
				Registered: &yes, Declared: map[string][]string{"generator": {"subtree_defaults"}}}}}},
			Effective: map[string]EffectiveTenant{"t1": {Config: map[string]any{"_k": 1},
				Sources: map[string]config.KeySource{"_k": {Layer: config.KeyLayerDefaults, File: "d/_defaults.yaml",
					Level: level(lvl)}}}},
		}
	}
	if v := one(t, Judge(in(1)), "t1", "_k"); v.Verdict != Read || !reflect.DeepEqual(v.ReadBy, []string{"generator"}) {
		t.Fatalf("level 1: %+v", v)
	}
	if v := one(t, Judge(in(0)), "t1", "_k"); v.Verdict != Unread {
		t.Fatalf("level 0: %+v", v)
	}
}

// A partial disable's inherited sibling the effective config has no source
// for is served from "?" (unknown), never from "".
func TestJudge_APartialSiblingWithNoSourceIsServedFromUnknown(t *testing.T) {
	t.Parallel()
	tree := Judge(Input{
		Served: map[string]ServedTenant{"t1": {
			Keys: map[string]ServedKey{"mysql_connections": {Reason: ReasonDisabled, Disable: DisablePartial,
				ServedFamily: []string{"mysql_connections_critical"}}},
			Values: map[string]any{"mysql_connections_critical": 90.0},
			Rows:   map[string]bool{"mysql_connections_critical": true}}},
		Effective: map[string]EffectiveTenant{"t1": {Config: map[string]any{"mysql_connections": "disable"},
			Sources: map[string]config.KeySource{"mysql_connections": {Layer: config.KeyLayerPlatform, File: "_p.yaml"}}}},
	})
	if len(tree.Findings) != 1 {
		t.Fatalf("findings %+v", tree.Findings)
	}
	f := tree.Findings[0]
	if f.Kind != KindDisablePartial || f.Severity != SeverityWarning ||
		!reflect.DeepEqual(f.ServedFamily, []string{"mysql_connections_critical"}) ||
		!reflect.DeepEqual(f.ServedFrom, []string{"?"}) {
		t.Fatalf("finding %+v", f)
	}
}

// A Policy-as-Code rule names a key in either #1231 spelling, as
// Policy-as-Code compares them (policyCanonical) — a dimensional `_critical`
// included, though the exporter serves `mysql_cpu_critical{…}` as written:
// the rule reads it, so it is read.
func TestJudge_APolicyRuleNamesAKeyAsPolicyAsCodeSpellsIt(t *testing.T) {
	t.Parallel()
	judged := func(key, head string) string {
		tree := Judge(Input{
			Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{key: {Reason: "no_row"}}}},
			Effective: map[string]EffectiveTenant{"t1": {Config: map[string]any{key: 1},
				Sources: map[string]config.KeySource{key: {Layer: config.KeyLayerTenant, File: "a.yaml"}}}},
			Heads: []PolicyHead{{Head: head}},
		})
		v := one(t, tree, "t1", key)
		return v.Verdict + " " + strings.Join(v.ReadBy, ",")
	}
	cases := []struct{ key, head, want string }{
		{"mysql_cpu", "mysql_threads_running", "read policy_engine"},
		{"mysql_threads_running", "mysql_cpu", "read policy_engine"},
		{`mysql_cpu{db="x"}`, `mysql_threads_running{db="x"}`, "read policy_engine"},
		{"mysql_cpu_critical", "mysql_threads_running_critical", "read policy_engine"},
		{`mysql_cpu_critical{db="x"}`, `mysql_threads_running_critical{db="x"}`, "read policy_engine"},
		{`mysql_threads_running_critical{db="x"}`, `mysql_cpu_critical{db="x"}`, "read policy_engine"},
		{`mysql_cpu_critical{db="x"}`, `mysql_threads_running{db="x"}`, "unread "},
		{"mysql_cpu", "mysql_*", "unread "},
	}
	for _, tc := range cases {
		if got := judged(tc.key, tc.head); got != tc.want {
			t.Errorf("key %s, rule %s: %q, want %q", tc.key, tc.head, got, tc.want)
		}
	}
}

// A reserved key the effective config shows in a layer the exporter does not
// read it in (the root's top level, level 0: no registry layer) is read by no
// one — the served reading's values list `_silent_mode` for every tenant,
// which is no evidence it was read. An error, though the file is the
// platform's (#3394: the severity describes the defect).
func TestJudge_AReservedKeyInALayerTheExporterDoesNotReadIsUnread(t *testing.T) {
	t.Parallel()
	yes := true
	tree := Judge(Input{
		Served: map[string]ServedTenant{"t1": {
			Keys: map[string]ServedKey{"_silent_mode": {Reason: "reserved", Registered: &yes,
				Declared: map[string][]string{"exporter": {"tenant", "platform_tenants", "profile"}}}},
			Values: map[string]any{"_silent_mode": []string{}}}},
		Effective: map[string]EffectiveTenant{"t1": {Config: map[string]any{"_silent_mode": "warning"},
			Sources: map[string]config.KeySource{"_silent_mode": {Layer: config.KeyLayerDefaults,
				File: "_defaults.yaml", Level: level(0)}},
			DefaultsChain: []string{"_defaults.yaml"}}},
	})
	v := one(t, tree, "t1", "_silent_mode")
	if len(v.ReadBy) != 0 || v.Verdict != Unread || deref(v.Severity) != SeverityError || deref(v.File) != "_defaults.yaml" {
		t.Fatalf("verdict %+v", v)
	}
}
