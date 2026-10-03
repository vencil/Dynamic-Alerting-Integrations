package config

// subtree_reserved_test.go — #2388: ScopedTenants.SubtreeReserved lists, per
// in-scope tenant, the reserved keys (subtreeDefaultsRefusedKey) a SUBTREE
// `_defaults.yaml` of its chain writes, whatever the value. It and #1976's
// ScopedTenants.Undeliverable split one key space with one predicate: a key a
// subtree writes and the build cannot deliver is in exactly one of the two.
//
// Seams: none — t.TempDir() trees.

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

func TestScopeEffective_SubtreeReservedKeys(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableTree(t, map[string]string{
		// The root's own reserved key is not a subtree level: not listed.
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _state_maintenance: 0\n" +
			"state_filters:\n  maintenance:\n    reasons: []\n    severity: warning\n    default_state: enable\n",
		// enable (dropped), a severity (dropped), disable (applied), an
		// undeclared filter (undeliverable), a null, an unrecognised `_` key
		// and a plain threshold (the last two are #1976's, not #2388's).
		"finance/_defaults.yaml": "defaults:\n  _state_maintenance: enable\n  _silent_mode: warning\n" +
			"  _severity_dedup: disable\n  _state_bogus: 5\n  _metadata: null\n  _myth: 5\n  redis_x: 5\n" +
			// `_routing*` is the routing checks' (routing_in_unread_location).
			"  _routing_profile: p1\n",
		"finance/us/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
		"finance/us/tenant-a.yaml":  "tenants:\n  tenant-a:\n    _silent_mode: critical\n",
		"ops/tenant-b.yaml":         "tenants:\n  tenant-b: {}\n",
	})

	scoped, err := ScopeEffective(dir, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(scoped.ParseFailed) != 0 {
		t.Fatalf("ParseFailed = %v; the tree must load whole", scoped.ParseFailed)
	}
	wantReserved := map[string]map[string][]string{
		"tenant-a": {
			// Listed per level, root-first; listed although the tenant sets
			// `_silent_mode` itself.
			"_state_maintenance": {"finance/_defaults.yaml", "finance/us/_defaults.yaml"},
			"_silent_mode":       {"finance/_defaults.yaml"},
			"_severity_dedup":    {"finance/_defaults.yaml"},
			"_state_bogus":       {"finance/_defaults.yaml"},
			// `_metadata: null` is not listed: a null writes nothing (#2388 r1b).
		},
	}
	if !reflect.DeepEqual(scoped.SubtreeReserved, wantReserved) {
		t.Errorf("SubtreeReserved = %v, want %v", scoped.SubtreeReserved, wantReserved)
	}
	// The other half of the partition (#1976): the undeliverable keys the
	// predicate does not refuse, and none it does.
	wantUndeliverable := map[string][]string{"tenant-a": {"_myth", "redis_x"}}
	if !reflect.DeepEqual(scoped.Undeliverable, wantUndeliverable) {
		t.Errorf("Undeliverable = %v, want %v", scoped.Undeliverable, wantUndeliverable)
	}

	// Scope: tenant-a is outside ops/.
	ops, err := ScopeEffective(dir, "ops")
	if err != nil {
		t.Fatal(err)
	}
	if ops.SubtreeReserved != nil {
		t.Errorf("--scope ops: SubtreeReserved = %v, want nil", ops.SubtreeReserved)
	}
}

// #2388 r1b: a null reserved key in a subtree level is treated as not written
// (the #2518 ruling: null deletes that level's value, the next level's
// applies). The shape of the golden fixture mixed-mode/conf.d/reserved-null:
// the parent writes `_severity_dedup` and `_silent_mode`, the child nulls
// `_severity_dedup` — only the parent is listed. A tree whose only subtree
// mention of the key is the null lists nothing.
func TestScopeEffective_SubtreeReservedNullIsNotWritten(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  map[string]map[string][]string
	}{
		{
			name: "reserved-null",
			files: map[string]string{
				"_defaults.yaml":                     "defaults:\n  mysql_connections: 80\n",
				"reserved-null/_defaults.yaml":       "defaults:\n  _severity_dedup: \"disable\"\n  _silent_mode: \"warning\"\n",
				"reserved-null/child/_defaults.yaml": "defaults:\n  _severity_dedup: ~\n",
				"reserved-null/child/tenants.yaml":   "tenants:\n  tenant-reserved: {}\n",
			},
			want: map[string]map[string][]string{"tenant-reserved": {
				"_severity_dedup": {"reserved-null/_defaults.yaml"},
				"_silent_mode":    {"reserved-null/_defaults.yaml"},
			}},
		},
		{
			name: "null-only",
			files: map[string]string{
				"_defaults.yaml":         "defaults:\n  mysql_connections: 80\n",
				"finance/_defaults.yaml": "defaults:\n  _severity_dedup: ~\n  _silent_mode: null\n",
				"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			scoped, err := ScopeEffective(writeUndeliverableTree(t, tc.files), "")
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(scoped.SubtreeReserved, tc.want) {
				t.Errorf("SubtreeReserved = %v, want %v", scoped.SubtreeReserved, tc.want)
			}
		})
	}
}

// #2388 A: SubtreeReservedApplied is the overlay's own verdict on each refused
// key a subtree writes — applied when the key ends up in the tenant's map,
// dropped otherwise — and with many levels, what the tenant ends up with.
func TestScopeEffective_SubtreeReservedApplied(t *testing.T) {
	t.Parallel()
	root := "defaults:\n  mysql_connections: 80\n" +
		"state_filters:\n  maintenance:\n    reasons: []\n    severity: warning\n    default_state: enable\n"
	dir := writeUndeliverableTree(t, map[string]string{
		"_defaults.yaml": root,
		// applied: a plain disable for a declared filter, and _severity_dedup
		// disable; dropped: enable for _silent_mode (not threshold-shaped), an
		// undeclared filter (no reader), and a key the tenant sets itself.
		"a/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n  _severity_dedup: disable\n" +
			"  _silent_mode: warning\n  _state_nope: disable\n",
		"a/t1.yaml": "tenants:\n  t1: {}\n",
		"a/t2.yaml": "tenants:\n  t2:\n    _severity_dedup: enable\n",
		// many levels: the shallower disable is written, the deeper enable is
		// dropped — the tenant keeps the disable, so applied. And the reverse:
		// shallower enable dropped, deeper disable written → applied.
		"m/_defaults.yaml":    "defaults:\n  _state_maintenance: disable\n",
		"m/us/_defaults.yaml": "defaults:\n  _state_maintenance: enable\n",
		"m/us/t3.yaml":        "tenants:\n  t3: {}\n",
		"n/_defaults.yaml":    "defaults:\n  _state_maintenance: enable\n",
		"n/us/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
		"n/us/t4.yaml":        "tenants:\n  t4: {}\n",
		// only an ignored value at every level → not applied.
		"o/_defaults.yaml":    "defaults:\n  _state_maintenance: enable\n",
		"o/us/_defaults.yaml": "defaults:\n  _state_maintenance: enable\n",
		"o/us/t5.yaml":        "tenants:\n  t5: {}\n",
	})
	scoped, err := ScopeEffective(dir, "")
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]map[string]bool{
		"t1": {"_state_maintenance": true, "_severity_dedup": true, "_silent_mode": false, "_state_nope": false},
		"t2": {"_state_maintenance": true, "_severity_dedup": false, "_silent_mode": false, "_state_nope": false},
		"t3": {"_state_maintenance": true},
		"t4": {"_state_maintenance": true},
		"t5": {"_state_maintenance": false},
	}
	got := map[string]map[string]bool{}
	for id, byKey := range scoped.SubtreeRefusedVerdicts {
		got[id] = map[string]bool{}
		for k, v := range byKey {
			got[id][k] = v.Applied
		}
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("applied = %v, want %v", got, want)
	}
	// #2388 A r2: the verdict names the file and value the tenant gets, and
	// whether the tenant sets the key itself.
	for _, tc := range []struct {
		id, key string
		want    SubtreeRefusedVerdict
	}{
		{"t3", "_state_maintenance", SubtreeRefusedVerdict{Applied: true, Source: "m/_defaults.yaml", Value: "disable"}},
		{"t4", "_state_maintenance", SubtreeRefusedVerdict{Applied: true, Source: "n/us/_defaults.yaml", Value: "disable"}},
		{"t5", "_state_maintenance", SubtreeRefusedVerdict{}},
		{"t2", "_severity_dedup", SubtreeRefusedVerdict{TenantSets: true, EntryFiles: []string{"a/t2.yaml"}}},
	} {
		if v := scoped.SubtreeRefusedVerdicts[tc.id][tc.key]; !reflect.DeepEqual(v, tc.want) {
			t.Errorf("%s %s: verdict = %+v, want %+v", tc.id, tc.key, v, tc.want)
		}
	}
}

// #2388 A r2: SubtreeRefusedVerdicts covers the whole tree, also under a
// --scope that lists only some tenants — a subtree file is shared.
func TestScopeEffective_SubtreeRefusedVerdictsWholeTree(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" +
			"state_filters:\n  maintenance:\n    reasons: []\n    severity: warning\n    default_state: enable\n",
		"a/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
		"a/t1.yaml":        "tenants:\n  t1: {}\n",
		"a/x/t2.yaml":      "tenants:\n  t2:\n    _state_maintenance: enable\n",
	})
	scoped, err := ScopeEffective(dir, "a/x")
	if err != nil {
		t.Fatal(err)
	}
	if _, listed := scoped.SubtreeReserved["t1"]; listed {
		t.Errorf("SubtreeReserved lists out-of-scope t1: %v", scoped.SubtreeReserved)
	}
	if v := scoped.SubtreeRefusedVerdicts["t1"]["_state_maintenance"]; !v.Applied || v.Source != "a/_defaults.yaml" {
		t.Errorf("t1 verdict = %+v, want applied from a/_defaults.yaml", v)
	}
}

func TestRenderYAMLFlow(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		in   any
		want string
	}{
		{"disable", "disable"},
		{1, "1"},
		{"70:critical", "70:critical"},
		{map[string]any{"target": "warning", "expires": "2026-12-31"}, "{expires: \"2026-12-31\", target: warning}"},
		{[]any{"a", "b"}, "[a, b]"},
	} {
		if got, ok := RenderYAMLFlow(tc.in); !ok || got != tc.want {
			t.Errorf("RenderYAMLFlow(%#v) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// #2388 A r3 (R2-1): RenderYAMLFlow is one line, and pasted as `k: <it>` it
// decodes back to the same value — a block scalar there broke the file.
func TestRenderYAMLFlowRoundTrips(t *testing.T) {
	t.Parallel()
	for _, v := range []any{
		"disable\n", "a\nb", "a\tb", "\tlead", " lead", "trail ", "yes", "on", "no", "off", "007",
		"5:critical", ":", "a: b", "#", "a #b", "", "~", "null", "true", "1e6", `"q"`, "'s'",
		1, 1.5, true, map[string]any{"target": "a\nb", "k": "yes"}, []any{"x\n", 2},
		strings.Repeat("long words ", 40) + "end", map[string]any{"a": strings.Repeat("x", 300)},
	} {
		r, ok := RenderYAMLFlow(v)
		if !ok || strings.Contains(r, "\n") {
			t.Errorf("RenderYAMLFlow(%#v) = %q: more than one line", v, r)
			continue
		}
		var got map[string]any
		if err := yaml.Unmarshal([]byte("k: "+r+"\n"), &got); err != nil {
			t.Errorf("RenderYAMLFlow(%#v) = %q: `k: …` does not decode: %v", v, r, err)
			continue
		}
		if !reflect.DeepEqual(got["k"], v) {
			t.Errorf("RenderYAMLFlow(%#v) = %q decodes to %#v", v, r, got["k"])
		}
	}
}

// #2388 A r4 (R3-1): where a tenant-set key is set (EntryFiles / Profile) is
// filled on da-guard's path (ScopeEffective), NOT by BuildFlatConfig — the
// exporter's reload, which has no reader for it and where the first version
// scanned every file per verdict.
func TestTenantSetSourcesOnlyOnTheGuardPath(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_profiles.yaml": "profiles:\n  quiet:\n    _silent_mode: warning\n",
		"_platform.yaml": "tenants:\n  t2:\n    _severity_dedup: disable\n",
		"a/_defaults.yaml": "defaults:\n  _silent_mode: warning\n  _severity_dedup: disable\n" +
			"  _state_maintenance: disable\n",
		"a/t1.yaml": "tenants:\n  t1:\n    _profile: quiet\n",
		"a/t2.yaml": "tenants:\n  t2: {}\n",
		"a/t3.yaml": "tenants:\n  t3:\n    _silent_mode: critical\n",
	})
	scan, err := ScanDirTree(dir, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	built, err := loadDirBuild(scan, scan.AbsRoot, discardLogger, nil)
	if err != nil {
		t.Fatal(err)
	}
	if v := built.SubtreeRefusedVerdicts["t1"]["_silent_mode"]; !v.TenantSets || v.Profile != "" || v.EntryFiles != nil {
		t.Errorf("BuildFlatConfig filled the source: %+v", v)
	}
	scoped, err := ScopeEffective(dir, "")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		id, key string
		want    SubtreeRefusedVerdict
	}{
		{"t1", "_silent_mode", SubtreeRefusedVerdict{TenantSets: true, Profile: "quiet"}},
		{"t2", "_severity_dedup", SubtreeRefusedVerdict{TenantSets: true, EntryFiles: []string{"_platform.yaml"}}},
		{"t3", "_silent_mode", SubtreeRefusedVerdict{TenantSets: true, EntryFiles: []string{"a/t3.yaml"}}},
	} {
		if v := scoped.SubtreeRefusedVerdicts[tc.id][tc.key]; !reflect.DeepEqual(v, tc.want) {
			t.Errorf("%s %s: verdict = %+v, want %+v", tc.id, tc.key, v, tc.want)
		}
	}
}

// #2388 A r4 (R3-1): markTenantSetSources is linear in the tree — one pass
// over the `tenants:` entries, not one per verdict. 4000 tenants sharing a
// profile must finish well inside a second (the quadratic first version took
// seconds at this size).
func TestTenantSetSourcesScales(t *testing.T) {
	t.Parallel()
	const n = 4000
	verdicts := map[string]map[string]SubtreeRefusedVerdict{}
	files := map[string]ThresholdConfig{}
	cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{},
		Profiles: map[string]map[string]ScheduledValue{"quiet": {"_silent_mode": {Default: "warning"}}}}
	for i := 0; i < n; i++ {
		id := fmt.Sprintf("t%d", i)
		verdicts[id] = map[string]SubtreeRefusedVerdict{"_silent_mode": {TenantSets: true}}
		files["fin/"+id+".yaml"] = ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{id: {"_profile": {Default: "quiet"}}}}
		cfg.Tenants[id] = map[string]ScheduledValue{"_profile": {Default: "quiet"}, "_silent_mode": {Default: "warning"}}
	}
	start := time.Now()
	markTenantSetSources(verdicts, files, cfg)
	if d := time.Since(start); d > time.Second {
		t.Errorf("markTenantSetSources over %d tenants took %v", n, d)
	}
	if v := verdicts["t17"]["_silent_mode"]; v.Profile != "quiet" {
		t.Errorf("t17 = %+v, want Profile quiet", v)
	}
}
