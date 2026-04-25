package main

import (
	"path/filepath"
	"reflect"
	"sort"
	"testing"
)

// ============================================================
// defaultsPathLevel
// ============================================================

func TestDefaultsPathLevel_RootIsGlobal(t *testing.T) {
	root := filepath.FromSlash("/conf.d")
	got := defaultsPathLevel(filepath.FromSlash("/conf.d/_defaults.yaml"), root)
	if got != "global" {
		t.Errorf("expected global, got %q", got)
	}
}

func TestDefaultsPathLevel_OneLevelDeepIsDomain(t *testing.T) {
	root := filepath.FromSlash("/conf.d")
	got := defaultsPathLevel(filepath.FromSlash("/conf.d/finance/_defaults.yaml"), root)
	if got != "domain" {
		t.Errorf("expected domain, got %q", got)
	}
}

func TestDefaultsPathLevel_TwoLevelsDeepIsRegion(t *testing.T) {
	root := filepath.FromSlash("/conf.d")
	got := defaultsPathLevel(filepath.FromSlash("/conf.d/finance/us-east/_defaults.yaml"), root)
	if got != "region" {
		t.Errorf("expected region, got %q", got)
	}
}

func TestDefaultsPathLevel_ThreeLevelsDeepIsEnv(t *testing.T) {
	root := filepath.FromSlash("/conf.d")
	got := defaultsPathLevel(filepath.FromSlash("/conf.d/finance/us-east/prod/_defaults.yaml"), root)
	if got != "env" {
		t.Errorf("expected env, got %q", got)
	}
}

func TestDefaultsPathLevel_DeeperThanFourIsUnknown(t *testing.T) {
	root := filepath.FromSlash("/conf.d")
	got := defaultsPathLevel(filepath.FromSlash("/conf.d/a/b/c/d/e/_defaults.yaml"), root)
	if got != "unknown" {
		t.Errorf("expected unknown, got %q", got)
	}
}

func TestDefaultsPathLevel_NonDescendantIsUnknown(t *testing.T) {
	root := filepath.FromSlash("/conf.d")
	got := defaultsPathLevel(filepath.FromSlash("/elsewhere/_defaults.yaml"), root)
	if got != "unknown" {
		t.Errorf("expected unknown, got %q", got)
	}
}

// ============================================================
// widestChangedScope
// ============================================================

func TestWidestChangedScope_PicksShallowestChanged(t *testing.T) {
	root := filepath.FromSlash("/conf.d")
	chain := []string{
		filepath.FromSlash("/conf.d/_defaults.yaml"),
		filepath.FromSlash("/conf.d/finance/_defaults.yaml"),
		filepath.FromSlash("/conf.d/finance/us-east/prod/_defaults.yaml"),
	}
	prior := map[string]string{
		chain[0]: "h-global-old",
		chain[1]: "h-domain-stable",
		chain[2]: "h-env-old",
	}
	now := map[string]string{
		chain[0]: "h-global-NEW", // changed
		chain[1]: "h-domain-stable",
		chain[2]: "h-env-NEW", // changed
	}
	if got := widestChangedScope(chain, now, prior, root); got != "global" {
		t.Errorf("expected global (widest changed), got %q", got)
	}
}

func TestWidestChangedScope_NoneChangedReturnsEmpty(t *testing.T) {
	root := filepath.FromSlash("/conf.d")
	chain := []string{filepath.FromSlash("/conf.d/_defaults.yaml")}
	hashes := map[string]string{chain[0]: "h-stable"}
	if got := widestChangedScope(chain, hashes, hashes, root); got != "" {
		t.Errorf("expected empty (no changes), got %q", got)
	}
}

// ============================================================
// parseDefaultsBytes
// ============================================================

func TestParseDefaultsBytes_WrappedDefaultsKey(t *testing.T) {
	b := []byte("defaults:\n  mysql_connections: 80\n")
	got, err := parseDefaultsBytes(b)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got["mysql_connections"] != 80 {
		t.Errorf("expected mysql_connections=80, got %v", got["mysql_connections"])
	}
}

func TestParseDefaultsBytes_NakedDict(t *testing.T) {
	b := []byte("mysql_connections: 80\n")
	got, err := parseDefaultsBytes(b)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got["mysql_connections"] != 80 {
		t.Errorf("expected mysql_connections=80 (naked), got %v", got["mysql_connections"])
	}
}

func TestParseDefaultsBytes_EmptyReturnsEmptyMap(t *testing.T) {
	got, err := parseDefaultsBytes([]byte("   \n  \n"))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("expected empty map, got %v", got)
	}
}

func TestParseDefaultsBytes_InvalidYAMLReturnsError(t *testing.T) {
	if _, err := parseDefaultsBytes([]byte("not: : valid: yaml: :\n")); err == nil {
		t.Errorf("expected error for malformed YAML, got nil")
	}
}

// ============================================================
// changedDefaultsKeys
// ============================================================

func sortedKeys(s []string) []string {
	out := append([]string(nil), s...)
	sort.Strings(out)
	return out
}

func TestChangedDefaultsKeys_IdenticalReturnsEmpty(t *testing.T) {
	a := map[string]any{"x": 1, "y": "z"}
	b := map[string]any{"x": 1, "y": "z"}
	if got := changedDefaultsKeys(a, b); len(got) != 0 {
		t.Errorf("expected empty (identical), got %v", got)
	}
}

func TestChangedDefaultsKeys_AddedKey(t *testing.T) {
	a := map[string]any{}
	b := map[string]any{"new_key": 1}
	got := sortedKeys(changedDefaultsKeys(a, b))
	if !reflect.DeepEqual(got, []string{"new_key"}) {
		t.Errorf("expected [new_key], got %v", got)
	}
}

func TestChangedDefaultsKeys_RemovedKey(t *testing.T) {
	a := map[string]any{"old_key": 1}
	b := map[string]any{}
	got := sortedKeys(changedDefaultsKeys(a, b))
	if !reflect.DeepEqual(got, []string{"old_key"}) {
		t.Errorf("expected [old_key], got %v", got)
	}
}

func TestChangedDefaultsKeys_ScalarValueChange(t *testing.T) {
	a := map[string]any{"mysql_connections": 80}
	b := map[string]any{"mysql_connections": 100}
	got := sortedKeys(changedDefaultsKeys(a, b))
	if !reflect.DeepEqual(got, []string{"mysql_connections"}) {
		t.Errorf("expected [mysql_connections], got %v", got)
	}
}

func TestChangedDefaultsKeys_NestedSubkeyChangeDoesNotReportParent(t *testing.T) {
	a := map[string]any{"thresholds": map[string]any{"cpu": 80, "memory": 70}}
	b := map[string]any{"thresholds": map[string]any{"cpu": 90, "memory": 70}}
	got := sortedKeys(changedDefaultsKeys(a, b))
	if !reflect.DeepEqual(got, []string{"thresholds.cpu"}) {
		t.Errorf("expected [thresholds.cpu], got %v", got)
	}
}

func TestChangedDefaultsKeys_ArrayReplaceIsLeafChange(t *testing.T) {
	a := map[string]any{"x": []any{1, 2}}
	b := map[string]any{"x": []any{1, 2, 3}}
	got := sortedKeys(changedDefaultsKeys(a, b))
	if !reflect.DeepEqual(got, []string{"x"}) {
		t.Errorf("expected [x] (array replace is leaf), got %v", got)
	}
}

func TestChangedDefaultsKeys_BothNilReturnsEmpty(t *testing.T) {
	if got := changedDefaultsKeys(nil, nil); len(got) != 0 {
		t.Errorf("expected empty, got %v", got)
	}
}

func TestChangedDefaultsKeys_TypeChangeIsLeafChange(t *testing.T) {
	a := map[string]any{"x": 80}                      // scalar
	b := map[string]any{"x": map[string]any{"a": 1}}  // map
	got := sortedKeys(changedDefaultsKeys(a, b))
	if !reflect.DeepEqual(got, []string{"x"}) {
		t.Errorf("expected [x] (type change is leaf), got %v", got)
	}
}

// ============================================================
// tenantOverridesAll
// ============================================================

func TestTenantOverridesAll_AllTopLevelOverridden(t *testing.T) {
	tenant := map[string]any{"mysql_connections": 100, "redis_connections": 50}
	if !tenantOverridesAll(tenant, []string{"mysql_connections", "redis_connections"}) {
		t.Errorf("expected true (all overridden)")
	}
}

func TestTenantOverridesAll_PartialOverrideReturnsFalse(t *testing.T) {
	tenant := map[string]any{"mysql_connections": 100}
	if tenantOverridesAll(tenant, []string{"mysql_connections", "redis_connections"}) {
		t.Errorf("expected false (redis_connections missing)")
	}
}

func TestTenantOverridesAll_NestedExactPath(t *testing.T) {
	tenant := map[string]any{"thresholds": map[string]any{"cpu": 85}}
	if !tenantOverridesAll(tenant, []string{"thresholds.cpu"}) {
		t.Errorf("expected true (exact nested override)")
	}
}

func TestTenantOverridesAll_ParentScalarOverridesWholeSubtree(t *testing.T) {
	// Tenant replaces the whole `thresholds` block with a scalar →
	// any path beneath `thresholds` is considered overridden.
	tenant := map[string]any{"thresholds": "any-scalar"}
	if !tenantOverridesAll(tenant, []string{"thresholds.cpu", "thresholds.memory"}) {
		t.Errorf("expected true (parent scalar overrides subtree)")
	}
}

func TestTenantOverridesAll_NoOverlapReturnsFalse(t *testing.T) {
	tenant := map[string]any{"unrelated_key": 1}
	if tenantOverridesAll(tenant, []string{"thresholds.cpu"}) {
		t.Errorf("expected false (tenant doesn't touch thresholds)")
	}
}

func TestTenantOverridesAll_EmptyPathsReturnsTrue(t *testing.T) {
	if !tenantOverridesAll(map[string]any{}, nil) {
		t.Errorf("expected true (vacuous on empty paths)")
	}
}

func TestTenantOverridesAll_NilValueIsNotOverride(t *testing.T) {
	// YAML null → deepMerge "delete key" semantics → defaults still
	// visible → not an override.
	tenant := map[string]any{"mysql_connections": nil}
	if tenantOverridesAll(tenant, []string{"mysql_connections"}) {
		t.Errorf("expected false (nil = delete-key, not override)")
	}
}

// ============================================================
// classifyDefaultsNoOpEffect — Issue #61 shadowed/cosmetic split
// ============================================================

func TestClassifyDefaultsNoOpEffect_CosmeticWhenNoKeyChanged(t *testing.T) {
	dp := "/conf.d/_defaults.yaml"
	chain := []string{dp}
	prior := map[string]map[string]any{dp: {"mysql_connections": 80}}
	now := map[string]map[string]any{dp: {"mysql_connections": 80}} // identical content
	priorHashes := map[string]string{dp: "h-old"}
	hashes := map[string]string{dp: "h-NEW"} // file hash moved (e.g. comment-only)

	tenantYAML := []byte("tenants:\n  t1:\n    redis_connections: 50\n")
	got := classifyDefaultsNoOpEffect(tenantYAML, "t1", chain, prior, now, hashes, priorHashes)
	if got != "cosmetic" {
		t.Errorf("expected cosmetic (no key actually changed), got %q", got)
	}
}

func TestClassifyDefaultsNoOpEffect_ShadowedWhenTenantOverridesChangedKey(t *testing.T) {
	dp := "/conf.d/_defaults.yaml"
	chain := []string{dp}
	prior := map[string]map[string]any{dp: {"mysql_connections": 80}}
	now := map[string]map[string]any{dp: {"mysql_connections": 200}} // value changed
	priorHashes := map[string]string{dp: "h-old"}
	hashes := map[string]string{dp: "h-NEW"}

	// Tenant overrides mysql_connections → defaults change is shadowed.
	tenantYAML := []byte("tenants:\n  t1:\n    mysql_connections: 999\n")
	got := classifyDefaultsNoOpEffect(tenantYAML, "t1", chain, prior, now, hashes, priorHashes)
	if got != "shadowed" {
		t.Errorf("expected shadowed (tenant overrides mysql_connections), got %q", got)
	}
}

func TestClassifyDefaultsNoOpEffect_CosmeticWhenTenantSourceUnparseable(t *testing.T) {
	dp := "/conf.d/_defaults.yaml"
	chain := []string{dp}
	prior := map[string]map[string]any{dp: {"mysql_connections": 80}}
	now := map[string]map[string]any{dp: {"mysql_connections": 200}}
	priorHashes := map[string]string{dp: "h-old"}
	hashes := map[string]string{dp: "h-NEW"}

	tenantYAML := []byte("not: : valid: yaml: :\n")
	got := classifyDefaultsNoOpEffect(tenantYAML, "t1", chain, prior, now, hashes, priorHashes)
	if got != "cosmetic" {
		t.Errorf("expected cosmetic (parse fallback), got %q", got)
	}
}

func TestClassifyDefaultsNoOpEffect_CosmeticWhenPriorParseMissing(t *testing.T) {
	dp := "/conf.d/_defaults.yaml"
	chain := []string{dp}
	// Cache miss for prior — simulates first tick after restart with
	// no cold-start parse OR a parse failure earlier.
	prior := map[string]map[string]any{}
	now := map[string]map[string]any{dp: {"mysql_connections": 200}}
	priorHashes := map[string]string{dp: "h-old"}
	hashes := map[string]string{dp: "h-NEW"}

	tenantYAML := []byte("tenants:\n  t1:\n    redis_connections: 50\n")
	got := classifyDefaultsNoOpEffect(tenantYAML, "t1", chain, prior, now, hashes, priorHashes)
	if got != "cosmetic" {
		t.Errorf("expected cosmetic (cache miss fallback), got %q", got)
	}
}

func TestClassifyDefaultsNoOpEffect_ShadowedAcrossMultipleChainEntries(t *testing.T) {
	// Two defaults files in chain both moved; tenant overrides every
	// changed key across both → shadowed.
	dp1 := "/conf.d/_defaults.yaml"
	dp2 := "/conf.d/finance/_defaults.yaml"
	chain := []string{dp1, dp2}
	prior := map[string]map[string]any{
		dp1: {"mysql_connections": 80, "redis_connections": 50},
		dp2: {"kafka_lag": 1000},
	}
	now := map[string]map[string]any{
		dp1: {"mysql_connections": 100, "redis_connections": 50}, // mysql changed
		dp2: {"kafka_lag": 2000},                                  // kafka changed
	}
	priorHashes := map[string]string{dp1: "h-old", dp2: "h-old"}
	hashes := map[string]string{dp1: "h-NEW", dp2: "h-NEW"}

	tenantYAML := []byte("tenants:\n  t1:\n    mysql_connections: 999\n    kafka_lag: 5000\n")
	got := classifyDefaultsNoOpEffect(tenantYAML, "t1", chain, prior, now, hashes, priorHashes)
	if got != "shadowed" {
		t.Errorf("expected shadowed (tenant overrides both changed keys), got %q", got)
	}
}
