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

// #3046: a `<base>_critical` / `<base>{…}` key served no row because its
// base is switched off (ReasonBaseDisabled) is read by the exporter — the
// phase that switches it off read it — so it is no finding, wherever it is
// written; any other no-row key of the same shape stays unread.
func TestJudge_AKeySwitchedOffWithItsBaseIsRead(t *testing.T) {
	t.Parallel()
	for _, layer := range []string{config.KeyLayerTenant, config.KeyLayerProfile, config.KeyLayerPlatform} {
		for _, reason := range []string{ReasonBaseDisabled, "no_row"} {
			tree := Judge(Input{
				Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{
					"mysql_connection_usage_percent":          {Reason: ReasonDisabled, Disable: DisableEffective},
					"mysql_connection_usage_percent_critical": {Reason: reason}}}},
				Effective: map[string]EffectiveTenant{"t1": {
					Config: map[string]any{"mysql_connection_usage_percent": "disable", "mysql_connection_usage_percent_critical": "90"},
					Sources: map[string]config.KeySource{
						"mysql_connection_usage_percent":          {Layer: config.KeyLayerTenant, File: "t1.yaml"},
						"mysql_connection_usage_percent_critical": {Layer: layer, File: "x.yaml"}}}},
			})
			v := one(t, tree, "t1", "mysql_connection_usage_percent_critical")
			if reason == ReasonBaseDisabled {
				if v.Verdict != Read || !reflect.DeepEqual(v.ReadBy, []string{"exporter"}) || v.Reason != nil ||
					len(tree.Findings) != 0 {
					t.Errorf("%s %s: %+v findings %+v", layer, reason, v, tree.Findings)
				}
			} else if v.Verdict != Unread || deref(v.Severity) != SeverityError {
				t.Errorf("%s %s: %+v", layer, reason, v)
			}
		}
	}
}

// A Policy-as-Code rule names a key in either #1231 spelling
// (canonicalSpelling, the exporter's own rule) — a dimensional `_critical`
// included (#3556): the rule reads it, so it is read.
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

// A defaults file with no `defaults:` wrapper: effective shows the whole
// document as inherited, its own top-level keys too. `_routing_defaults`
// (the route generator reads it there) and `optional_overrides` (a field of
// the file) are no tenant's keys and are not judged — once the gates block,
// a valid routing-only `_defaults.yaml` must not fail them (#3016). A
// misspelt `_` key there is still a tenant's key no one reads.
func TestJudge_AFileLevelKeyOfADefaultsFileIsNotATenantsKey(t *testing.T) {
	t.Parallel()
	src := func(lvl int) config.KeySource {
		return config.KeySource{Layer: config.KeyLayerDefaults, File: "_defaults.yaml", Level: level(lvl)}
	}
	tree := Judge(Input{
		Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{}, Values: map[string]any{}}},
		Effective: map[string]EffectiveTenant{"t1": {
			Config: map[string]any{"_routing_defaults": map[string]any{}, "optional_overrides": []any{"k"},
				"_routng": "x"},
			Sources: map[string]config.KeySource{"_routing_defaults": src(0), "optional_overrides": src(0),
				"_routng": src(0)},
			DefaultsChain: []string{"_defaults.yaml"}}},
	})
	var got []string
	for _, v := range tree.Keys["t1"] {
		got = append(got, v.Key+" "+v.Verdict)
	}
	if !reflect.DeepEqual(got, []string{"_routng unread"}) {
		t.Fatalf("judged %v", got)
	}
}

// A threshold key of a profile no tenant binds is not judged — whether the
// exporter reads it is measured on a tenant — and says so with a notice
// that blocks nothing (#3395); only over the whole tree, where "no tenant
// binds it" is known. A bound profile's keys are judged through its
// tenants; an unbound profile's `_` key by the registry.
func TestJudge_AnUnboundProfilesThresholdKeyIsNotJudged(t *testing.T) {
	t.Parallel()
	in := func(whole bool) Input {
		return Input{
			Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{}, Values: map[string]any{}}},
			Effective: map[string]EffectiveTenant{"t1": {Config: map[string]any{}, Profile: "gold",
				Sources: map[string]config.KeySource{}}},
			RootProfiles: map[string]map[string]any{"_profiles.yaml": {
				"gold":   map[string]any{"redis_memory": "1"},
				"silver": map[string]any{"redis_evictd": "5", "_routng": "x"},
			}},
			Whole: whole,
		}
	}
	var got []string
	for _, f := range Judge(in(true)).Findings {
		if f.Kind == KindProfileNotJudged {
			got = append(got, f.Severity+" "+f.Key+" "+deref(f.File)+" "+deref(f.Reason)+" "+strings.Join(f.Tenants, ","))
			if f.Blocks {
				t.Errorf("a not-judged notice blocks: %+v", f)
			}
		}
	}
	if !reflect.DeepEqual(got, []string{"notice redis_evictd _profiles.yaml profile_unbound "}) {
		t.Fatalf("whole tree: %q", got)
	}
	for _, f := range Judge(in(false)).Findings {
		if f.Kind == KindProfileNotJudged {
			t.Fatalf("a tenant subset knows no profile to be unbound: %+v", f)
		}
	}
}

// A key a bound profile writes that the exporter does not fill in (the
// root declares it in optional_overrides without a value, #1189) is in no
// layer of the tenant's config; effective names it profile_not_filled, and
// it is unread — an error on the profile file listing the tenants (#3395).
// A key the tenant's config holds otherwise keeps its own row.
func TestJudge_AProfileKeyNotFilledInIsUnread(t *testing.T) {
	t.Parallel()
	ns := map[string]config.NotServedKey{
		"redis_memory": {Reason: config.NotServedProfileNotFilled, File: "_profiles.yaml"},
		"redis_x":      {Reason: "no_row", File: "a.yaml"},
	}
	tree := Judge(Input{
		Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{}, Values: map[string]any{}},
			"t2": {Keys: map[string]ServedKey{}, Values: map[string]any{}}},
		Effective: map[string]EffectiveTenant{
			"t1": {Config: map[string]any{}, Sources: map[string]config.KeySource{}, Profile: "gold", NotServed: ns},
			"t2": {Config: map[string]any{}, Sources: map[string]config.KeySource{}, Profile: "gold", NotServed: ns},
		},
	})
	v := one(t, tree, "t1", "redis_memory")
	if v.Verdict != Unread || deref(v.Severity) != SeverityError || deref(v.File) != "_profiles.yaml" ||
		deref(v.Layer) != config.KeyLayerProfile || deref(v.Reason) != config.NotServedProfileNotFilled {
		t.Fatalf("row %+v", v)
	}
	if len(tree.Findings) != 1 || !tree.Findings[0].Blocks ||
		!reflect.DeepEqual(tree.Findings[0].Tenants, []string{"t1", "t2"}) {
		t.Fatalf("findings %+v", tree.Findings)
	}
	for _, kv := range tree.Keys["t1"] {
		if kv.Key == "redis_x" {
			t.Fatalf("a not_served entry of another reason became a row: %+v", kv)
		}
	}
}

// #3046: a family key switched off with its base (SwitchedOffWith) is a
// disable_cascade warning, naming the base, only when the tenant's own file
// writes it; one it inherits is what the cascade asks — no finding. It stays
// read either way.
func TestJudge_DisableCascadeOnlyForTheTenantsOwnKey(t *testing.T) {
	t.Parallel()
	for layer, want := range map[string]bool{
		config.KeyLayerTenant: true, config.KeyLayerProfile: false,
		config.KeyLayerDefaults: false, config.KeyLayerPlatform: false,
	} {
		tree := Judge(Input{
			Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{
				"mysql_connection_usage_percent":          {Reason: ReasonDisabled, Disable: DisableEffective},
				"mysql_connection_usage_percent_critical": {Reason: ReasonBaseDisabled, SwitchedOffWith: "mysql_connection_usage_percent"}}}},
			Effective: map[string]EffectiveTenant{"t1": {
				Config: map[string]any{"mysql_connection_usage_percent": "disable", "mysql_connection_usage_percent_critical": "90"},
				Sources: map[string]config.KeySource{
					"mysql_connection_usage_percent":          {Layer: config.KeyLayerTenant, File: "t1.yaml"},
					"mysql_connection_usage_percent_critical": {Layer: layer, File: "x.yaml"}}}},
		})
		v := one(t, tree, "t1", "mysql_connection_usage_percent_critical")
		if v.Verdict != Read || deref(v.SwitchedOffWith) != "mysql_connection_usage_percent" {
			t.Errorf("%s: %+v", layer, v)
		}
		var got []Finding
		for _, f := range tree.Findings {
			if f.Kind == KindDisableCascade {
				got = append(got, f)
			}
		}
		if !want {
			if len(got) != 0 {
				t.Errorf("%s: findings %+v, want none", layer, got)
			}
			continue
		}
		if len(got) != 1 || got[0].Severity != SeverityWarning || got[0].Blocks ||
			deref(got[0].Base) != "mysql_connection_usage_percent" || deref(got[0].Reason) != ReasonBaseDisabled ||
			got[0].Key != "mysql_connection_usage_percent_critical" || !reflect.DeepEqual(got[0].Tenants, []string{"t1"}) {
			t.Errorf("%s: findings %+v", layer, got)
		}
	}
}

// #3671: a tenant file's top-level key other than `tenants` is no tenant's
// key and no reader reads it — one unread error on that file, naming no
// tenant, that blocks; `tenants` itself is no finding.
func TestJudge_ATenantFilesStrayTopLevelKeyIsOneErrorOnTheFile(t *testing.T) {
	t.Parallel()
	tree := Judge(Input{
		TenantFiles: map[string][]string{"a.yaml": {"defaults", "tenants"}, "b.yaml": {"tenants"},
			"c.yaml": {"mysql_connection_usage_percent"}},
	})
	var got []string
	for _, f := range tree.Findings {
		got = append(got, strings.Join([]string{f.Severity, f.Kind, f.Key, deref(f.File), deref(f.Reason),
			boolStr(f.Blocks)}, " ")+" "+strings.Join(f.Tenants, ","))
		if f.Tenants == nil {
			t.Errorf("%s: tenants is nil, want [] (the document writes [])", f.Key)
		}
	}
	want := []string{
		"error unread defaults a.yaml tenant_file_top_level true ",
		"error unread mysql_connection_usage_percent c.yaml tenant_file_top_level true ",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("findings %q, want %q", got, want)
	}
	if !tree.Blocks {
		t.Fatal("tree.Blocks false")
	}
	var blocks []string
	for _, b := range Blocks(tree) {
		blocks = append(blocks, b.File+" "+b.Key+" "+b.Tenant)
	}
	if !reflect.DeepEqual(blocks, []string{"a.yaml defaults ", "c.yaml mysql_connection_usage_percent "}) {
		t.Fatalf("blocks %q", blocks)
	}
	// A write that leaves the key where it was makes nothing new; one that
	// adds it to another file does.
	all := func(string) bool { return true }
	if nb := NewBlocks(tree, tree, all); len(nb) != 0 {
		t.Fatalf("an unchanged tree has new blocks %+v", nb)
	}
	before := Judge(Input{TenantFiles: map[string][]string{"a.yaml": {"defaults", "tenants"}}})
	if nb := NewBlocks(before, tree, all); len(nb) != 1 || nb[0].File != "c.yaml" {
		t.Fatalf("new blocks %+v, want c.yaml's alone", nb)
	}
}
