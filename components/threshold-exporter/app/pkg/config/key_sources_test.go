package config

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// A key of the effective config that no layer writes is an internal error,
// never a guessed source (#2564).
func TestKeySources_UnattributedKeyIsAnError(t *testing.T) {
	t.Parallel()
	_, err := keySources(map[string]any{"a": 1.0}, map[string]any{"a": nil}, "t.yaml", nil, nil, nil, nil, nil)
	if err == nil || !strings.Contains(err.Error(), `no layer writes effective key(s) ["a"]`) {
		t.Fatalf("err = %v, want the unattributed key named", err)
	}
}

// Only EffectiveTree pays for the attribution: ResolveEffective and
// ScopeEffective leave KeySources nil; all three bind the profile.
func TestKeySources_OnlyEffectiveTreeFillsThem(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  a: 1\n",
		"conf.d/_profiles.yaml": "profiles:\n  p:\n    b: 2\n",
		"conf.d/t.yaml":         "tenants:\n  t:\n    _profile: p\n",
	})
	dir := filepath.Join(tmp, "conf.d")

	ec, err := ResolveEffective(dir, "t")
	if err != nil {
		t.Fatal(err)
	}
	if ec.KeySources != nil || ec.BoundProfile != "p" {
		t.Errorf("ResolveEffective: KeySources = %v, BoundProfile = %q", ec.KeySources, ec.BoundProfile)
	}
	sc, err := ScopeEffective(dir, "")
	if err != nil {
		t.Fatal(err)
	}
	if sc.Tenants[0].KeySources != nil || sc.Tenants[0].BoundProfile != "p" {
		t.Errorf("ScopeEffective: KeySources = %v, BoundProfile = %q", sc.Tenants[0].KeySources, sc.Tenants[0].BoundProfile)
	}
	tr, err := EffectiveTree(dir)
	if err != nil {
		t.Fatal(err)
	}
	ks := tr.Tenants[0].KeySources
	if ks["a"].Layer != KeyLayerDefaults || ks["b"].Layer != KeyLayerProfile || ks["_profile"].Layer != KeyLayerTenant || len(ks) != 3 {
		t.Errorf("EffectiveTree: KeySources = %+v", ks)
	}
}
