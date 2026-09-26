package config

// #2019 — the walker plane's platform per-tenant layer, below what the
// shared matrix (tests/shared/platform_tenant_overlay_matrix.json, asserted
// by app/config_platform_tenant_overlay_test.go and the Python half) pins:
// ScopeEffective (da-guard), the guard's inherited view, and the edges of
// overlayTenant's attribution.

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func writePlatformTree(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// ScopeEffective resolves through the same resolver as ResolveEffective, so
// da-guard sees the platform's per-tenant values; and MergedDefaults — the
// guard's "what the tenant inherits" — includes them, because deleting a key
// from the tenant file falls back to the platform value, not the chain's.
// Measured before #2019: ScopeEffective gave tx 80 and no `_silent_mode`.
func TestScopeEffective_AppliesPlatformOverlay(t *testing.T) {
	t.Parallel()
	dir := writePlatformTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_x: 5\ntenants:\n  tx:\n    mysql_connections: \"60\"\n    _silent_mode: warning\n",
		"tx.yaml":        "tenants:\n  tx:\n    redis_x: \"7\"\n",
		"ty.yaml":        "tenants:\n  ty: {}\n",
	})
	scoped, err := ScopeEffective(dir, dir)
	if err != nil {
		t.Fatal(err)
	}
	byID := map[string]*EffectiveConfig{}
	for _, ec := range scoped.Tenants {
		byID[ec.TenantID] = ec
	}
	tx := byID["tx"]
	if tx == nil {
		t.Fatalf("tx missing from scope: %+v", scoped.Tenants)
	}
	want := map[string]any{"mysql_connections": "60", "_silent_mode": "warning", "redis_x": "7"}
	if !reflect.DeepEqual(tx.EffectiveConfig, want) {
		t.Errorf("tx effective = %v, want %v", tx.EffectiveConfig, want)
	}
	if got := tx.MergedDefaults["mysql_connections"]; got != "60" {
		t.Errorf("tx MergedDefaults.mysql_connections = %v, want the platform's \"60\"", got)
	}
	if got := tx.MergedDefaults["redis_x"]; got != 5 {
		t.Errorf("tx MergedDefaults.redis_x = %v, want the chain's 5", got)
	}
	if !reflect.DeepEqual(tx.TenantOverridesRaw, map[string]any{"redis_x": "7"}) {
		t.Errorf("tx TenantOverridesRaw = %v, want the tenant file's block only", tx.TenantOverridesRaw)
	}
	one, err := ResolveEffective(dir, "tx")
	if err != nil {
		t.Fatal(err)
	}
	if one.MergedHash != tx.MergedHash || !reflect.DeepEqual(one.PlatformOverlay, tx.PlatformOverlay) {
		t.Errorf("ScopeEffective and ResolveEffective disagree: %s %+v vs %s %+v",
			tx.MergedHash, tx.PlatformOverlay, one.MergedHash, one.PlatformOverlay)
	}
	// Control: a tenant no platform file names is exactly the chain + file.
	if ty := byID["ty"]; ty == nil || ty.PlatformOverlay != nil || ty.EffectiveConfig["mysql_connections"] != 80 {
		t.Errorf("ty = %+v, want the chain's 80 and no platform_overlay", ty)
	}
}

func TestOverlayTenant_Attribution(t *testing.T) {
	t.Parallel()
	overlay := []PlatformBlock{
		{File: "_a.yaml", Block: map[string]any{"k1": "a", "k2": "a", "_metadata": map[string]any{"o": 1}, "_x": nil}},
		{File: "_b.yaml", Block: map[string]any{"k2": "b", "k3": "b"}},
	}
	tenant := map[string]any{"k3": "t", "_silent_mode": nil}
	combined, sources := overlayTenant(tenant, overlay, nil)
	wantCombined := map[string]any{"k1": "a", "k2": "b", "k3": "t", "_metadata": map[string]any{"o": 1}, "_x": nil, "_silent_mode": nil}
	if !reflect.DeepEqual(combined, wantCombined) {
		t.Errorf("combined = %v, want %v", combined, wantCombined)
	}
	// k2: the later file; k3: the tenant's; _metadata never inherited; a
	// null supplies nothing.
	wantSources := []PlatformOverlaySource{{File: "_a.yaml", Keys: []string{"k1"}}, {File: "_b.yaml", Keys: []string{"k2"}}}
	if !reflect.DeepEqual(sources, wantSources) {
		t.Errorf("sources = %+v, want %+v", sources, wantSources)
	}
	// A null on a RESERVED key deletes what the chain has for it, so it is
	// supplied when the chain has one; a null on a threshold key never is.
	nulls := []PlatformBlock{{File: "_a.yaml", Block: map[string]any{"_x": nil, "k9": nil}}}
	_, sources = overlayTenant(map[string]any{}, nulls, map[string]any{"_x": "warning", "k9": 5})
	if want := []PlatformOverlaySource{{File: "_a.yaml", Keys: []string{"_x"}}}; !reflect.DeepEqual(sources, want) {
		t.Errorf("null attribution = %+v, want %+v", sources, want)
	}
	// No overlay: the tenant block itself, no attribution.
	if got, src := overlayTenant(tenant, nil, nil); !reflect.DeepEqual(got, tenant) || src != nil {
		t.Errorf("no overlay: %v %v", got, src)
	}
}

// A root platform file the flat plane's decode rejects contributes nothing
// on /metrics (parsePartialConfig drops it whole), so it contributes nothing
// here either — even its well-formed entries.
func TestPlatformOverlay_FileTheFlatDecodeRejectsSuppliesNothing(t *testing.T) {
	t.Parallel()
	dir := writePlatformTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_profiles.yaml": "tenants:\n  tx:\n    mysql_connections: \"60\"\n  ty: 5\n",
		"tx.yaml":        "tenants:\n  tx: {}\n",
	})
	if _, err := ParseConfigFile([]byte("tenants:\n  tx:\n    mysql_connections: \"60\"\n  ty: 5\n")); err == nil {
		t.Fatal("premise: the flat decode accepts the file")
	}
	ec, err := ResolveEffective(dir, "tx")
	if err != nil {
		t.Fatal(err)
	}
	if ec.EffectiveConfig["mysql_connections"] != 80 || ec.PlatformOverlay != nil {
		t.Errorf("tx = %v %+v, want the chain's 80 and no platform_overlay", ec.EffectiveConfig, ec.PlatformOverlay)
	}
}
