package main

// builtin_layer_test.go — the built-in layer (#3832): the platform's
// default-armed thresholds are the bottom of every reader's merge, below the
// conf.d root `defaults:`. Judged end to end through da-guard's own
// subcommands, so /metrics (served-values), /effective (effective), the key
// verdict and the main gate answer from the same tree.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// builtinTree writes files under <tmp>/conf.d and returns that directory.
func builtinTree(t *testing.T, files map[string]string) string {
	t.Helper()
	tmp := t.TempDir()
	tree := make(map[string]string, len(files))
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	return filepath.Join(tmp, "conf.d")
}

// builtinEffTenant decodes one tenant of `da-guard effective`.
type builtinEffTenant struct {
	MergedHash      string                      `json:"merged_hash"`
	EffectiveConfig map[string]any              `json:"effective_config"`
	KeySources      map[string]config.KeySource `json:"key_sources"`
}

func builtinEffectiveOf(t *testing.T, dir, tenant string) builtinEffTenant {
	t.Helper()
	code, doc, stderr := runEffectiveOn(t, dir)
	if code != exitOK {
		t.Fatalf("effective: exit %d; stderr=%q", code, stderr)
	}
	var et builtinEffTenant
	if err := json.Unmarshal(doc.Tenants[tenant], &et); err != nil {
		t.Fatalf("effective: tenant %q: %v", tenant, err)
	}
	return et
}

func sortedBuiltinKeys() []string {
	keys := keysOf(config.BuiltinDefaults())
	sort.Strings(keys)
	return keys
}

func TestBuiltinLayer_ARootWithNoValueServesEveryBuiltinKey(t *testing.T) {
	t.Parallel()
	if len(config.BuiltinDefaults()) == 0 {
		t.Fatal("the built-in table is empty — this test would pass vacuously")
	}
	for name, files := range map[string]map[string]string{
		"no root carrier":       {"t1.yaml": "tenants:\n  t1: {}\n"},
		"empty defaults":        {"_defaults.yaml": "defaults: {}\n", "t1.yaml": "tenants:\n  t1: {}\n"},
		"defaults left blank":   {"_defaults.yaml": "defaults:\n", "t1.yaml": "tenants:\n  t1: {}\n"},
		"only another root key": {"_defaults.yaml": "defaults:\n  redis_connected_clients: 1000\n", "t1.yaml": "tenants:\n  t1: {}\n"},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := builtinTree(t, files)
			code, doc, _, stderr := servedWith(t, files, "")
			mustOK(t, code, stderr)
			et := builtinEffectiveOf(t, dir, "t1")
			for _, k := range sortedBuiltinKeys() {
				want, _ := config.BuiltinDefault(k)
				wantValue(t, doc, "t1", k, want)
				if sev := doc.Tenants["t1"].Severities[k]; sev != "warning" {
					t.Errorf("served %q: severity %q, want warning", k, sev)
				}
				if got := et.EffectiveConfig[k]; got != want {
					t.Errorf("effective %q = %v, want the built-in %v", k, got, want)
				}
				if src := et.KeySources[k]; src.Layer != config.KeyLayerBuiltin || src.File != "" || src.Level != nil {
					t.Errorf("key_sources[%q] = %+v, want layer builtin with no file", k, src)
				}
			}
		})
	}
}

func TestBuiltinLayer_TheTreeDecidesOverTheBuiltinValue(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  container_memory: 70\n  pg_replication_lag: null\n",
		"t1.yaml": "tenants:\n  t1:\n    pg_connections: \"60\"\n    container_cpu: disable\n" +
			"    mysql_replication_lag_critical: \"90\"\n",
	}
	code, doc, dir, stderr := servedWith(t, files, "")
	mustOK(t, code, stderr)
	tv := doc.Tenants["t1"]
	wantValue(t, doc, "t1", "container_memory", 70) // the root's number beats the built-in 85
	wantValue(t, doc, "t1", "pg_connections", 60)   // the tenant's beats the built-in 80
	wantBuiltin, _ := config.BuiltinDefault("pg_replication_lag")
	wantValue(t, doc, "t1", "pg_replication_lag", wantBuiltin) // a root null falls to the built-in value
	if _, served := tv.Values["container_cpu"]; served {
		t.Errorf("container_cpu: the tenant's disable served a row: %v", tv.Values["container_cpu"])
	}
	// The base has a value (the built-in one), so the tenant's critical is
	// served as a critical row.
	if sev := tv.Severities["mysql_replication_lag_critical"]; sev != "critical" {
		t.Errorf("mysql_replication_lag_critical: severity %q, want critical; severities=%v", sev, tv.Severities)
	}
	et := builtinEffectiveOf(t, dir, "t1")
	if src := et.KeySources["container_memory"]; src.Layer != config.KeyLayerDefaults || src.File != "_defaults.yaml" {
		t.Errorf("key_sources[container_memory] = %+v, want the root file", src)
	}
	if src := et.KeySources["pg_replication_lag"]; src.Layer != config.KeyLayerBuiltin {
		t.Errorf("key_sources[pg_replication_lag] = %+v, want builtin (the root writes null)", src)
	}
	if got := et.EffectiveConfig["container_cpu"]; got != "disable" {
		t.Errorf("effective container_cpu = %v, want disable", got)
	}
}

func TestBuiltinLayer_ARefusedRootValueFallsToTheBuiltinValue(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  pg_connections: abc\n",
		"t1.yaml":        "tenants:\n  t1: {}\n",
	}
	code, doc, dir, stderr := servedWith(t, files, "")
	mustOK(t, code, stderr)
	want, _ := config.BuiltinDefault("pg_connections")
	wantValue(t, doc, "t1", "pg_connections", want)
	if len(doc.RootDefaultsRefused) != 1 || doc.RootDefaultsRefused[0].Key != "pg_connections" {
		t.Errorf("root_defaults_refused = %+v, want pg_connections still named", doc.RootDefaultsRefused)
	}
	et := builtinEffectiveOf(t, dir, "t1")
	if got := et.EffectiveConfig["pg_connections"]; got != want {
		t.Errorf("effective pg_connections = %v, want the built-in %v (what /metrics serves)", got, want)
	}
	if src := et.KeySources["pg_connections"]; src.Layer != config.KeyLayerBuiltin {
		t.Errorf("key_sources[pg_connections] = %+v, want builtin", src)
	}
	gcode, gout, _ := runOnce(t, "--config-dir", dir)
	if gcode != exitFindings || !strings.Contains(gout, "root_default_refused") || !strings.Contains(gout, "built-in value") {
		t.Errorf("guard: exit %d, want %d naming root_default_refused and the built-in value:\n%s", gcode, exitFindings, gout)
	}
}

func TestBuiltinLayer_ARootThatCannotBeReadWithholdsIt(t *testing.T) {
	t.Parallel()
	tenant := "tenants:\n  t1:\n    pg_connections: \"60\"\n"
	cases := map[string]func(t *testing.T, dir string){
		"syntax error": func(t *testing.T, dir string) {
			if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte("defaults: [x\n"), 0o644); err != nil {
				t.Fatal(err)
			}
		},
		"a value the typed decode refuses": func(t *testing.T, dir string) {
			// YAML parses; the exporter's decode drops the file whole.
			if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte("defaults:\n  pg_connections: [1, 2]\n"), 0o644); err != nil {
				t.Fatal(err)
			}
		},
		"dangling link": func(t *testing.T, dir string) {
			if err := os.Symlink("nowhere.yaml", filepath.Join(dir, "_defaults.yaml")); err != nil {
				t.Skipf("symlink: %v", err)
			}
		},
	}
	for name, mk := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := builtinTree(t, map[string]string{"t1.yaml": tenant})
			mk(t, dir)
			code, doc, stderr := servedDirDoc(t, dir)
			if code == exitOK {
				t.Fatalf("served-values: exit 0 over a root that cannot be read; stderr=%q", stderr)
			}
			for k := range config.BuiltinDefaults() {
				if v, ok := doc.Tenants["t1"].Values[k]; ok {
					t.Errorf("served %q = %v from the built-in layer, over a root that cannot be read", k, v)
				}
			}
			_, eff, _ := runEffectiveOn(t, dir)
			if raw, ok := eff.Tenants["t1"]; ok {
				var et builtinEffTenant
				if err := json.Unmarshal(raw, &et); err != nil {
					t.Fatal(err)
				}
				for k := range config.BuiltinDefaults() {
					if k == "pg_connections" {
						continue // the tenant's own
					}
					if v, ok := et.EffectiveConfig[k]; ok {
						t.Errorf("effective %q = %v from the built-in layer, over a root that cannot be read", k, v)
					}
				}
			}
		})
	}
}

// servedDirDoc is servedDir without the raw stdout.
func servedDirDoc(t *testing.T, dir string) (int, servedOut, string) {
	t.Helper()
	code, doc, _, stderr := servedDir(t, dir)
	return code, doc, stderr
}

// formatBuiltin is a built-in value as the generated table writes it.
func formatBuiltin(v float64) string { return strconv.FormatFloat(v, 'f', -1, 64) }

func TestBuiltinLayer_ARootWritingEveryBuiltinValueKeepsItsMergedHash(t *testing.T) {
	t.Parallel()
	var b strings.Builder
	b.WriteString("defaults:\n  redis_connected_clients: 1000\n")
	for _, k := range sortedBuiltinKeys() {
		v, _ := config.BuiltinDefault(k)
		b.WriteString("  " + k + ": " + formatBuiltin(v) + "\n")
	}
	tenant := "tenants:\n  t1:\n    pg_connections: \"70\"\n    mysql_replication_lag_critical: \"60\"\n"
	dir := builtinTree(t, map[string]string{"_defaults.yaml": b.String(), "t1.yaml": tenant})
	et := builtinEffectiveOf(t, dir, "t1")

	// The answer before the built-in layer: the same merge with it withheld.
	root, err := os.ReadFile(filepath.Join(dir, "_defaults.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	before, err := config.ComputeMergedHash([]byte(tenant), "t1", [][]byte{root}, config.TenantLayers{WithholdBuiltin: true})
	if err != nil {
		t.Fatal(err)
	}
	if et.MergedHash != before {
		t.Errorf("merged_hash %s with the built-in layer, %s without: a root writing every built-in value "+
			"must see no change", et.MergedHash, before)
	}
	beforeCfg, err := config.ComputeEffectiveConfig([]byte(tenant), "t1", [][]byte{root}, config.TenantLayers{WithholdBuiltin: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(beforeCfg) != len(et.EffectiveConfig) {
		t.Errorf("effective config: %d keys with the built-in layer, %d without", len(et.EffectiveConfig), len(beforeCfg))
	}

	// Control: a root writing only some of them sees the rest arrive.
	partial := "defaults:\n  redis_connected_clients: 1000\n"
	pdir := builtinTree(t, map[string]string{"_defaults.yaml": partial, "t1.yaml": tenant})
	pet := builtinEffectiveOf(t, pdir, "t1")
	pbefore, err := config.ComputeMergedHash([]byte(tenant), "t1", [][]byte{[]byte(partial)}, config.TenantLayers{WithholdBuiltin: true})
	if err != nil {
		t.Fatal(err)
	}
	if pet.MergedHash == pbefore {
		t.Errorf("control: merged_hash %s unchanged by the built-in layer over a root that writes none of it", pbefore)
	}
}

func TestBuiltinLayer_AListedBuiltinKeyWithoutANumberIsAnError(t *testing.T) {
	t.Parallel()
	dir := builtinTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  redis_connected_clients: 1000\noptional_overrides:\n  - mysql_replication_lag\n  - redis_keyspace_hits\n",
		"t1.yaml":        "tenants:\n  t1: {}\n",
	})
	code, out, _ := runOnce(t, "--config-dir", dir)
	if code != exitFindings {
		t.Fatalf("exit %d, want %d:\n%s", code, exitFindings, out)
	}
	if !strings.Contains(out, "optional_overrides.mysql_replication_lag") || !strings.Contains(out, "root_optional_builtin") {
		t.Errorf("the listed default-armed key is not named:\n%s", out)
	}
	if strings.Contains(out, "optional_overrides.redis_keyspace_hits") {
		t.Errorf("a key that is not default-armed is named:\n%s", out)
	}
	// It is still served: the list switches nothing off.
	_, doc, stderr := servedDirDoc(t, dir)
	want, _ := config.BuiltinDefault("mysql_replication_lag")
	if got := doc.Tenants["t1"].Values["mysql_replication_lag"]; got != want {
		t.Errorf("served mysql_replication_lag = %v, want the built-in %v; stderr=%q", got, want, stderr)
	}

	// Control: with a number under defaults: the list entry is no error.
	ok := builtinTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_replication_lag: 40\noptional_overrides:\n  - mysql_replication_lag\n",
		"t1.yaml":        "tenants:\n  t1: {}\n",
	})
	if code, out, _ := runOnce(t, "--config-dir", ok); strings.Contains(out, "root_optional_builtin") {
		t.Errorf("control: exit %d, a valued key reported:\n%s", code, out)
	}
}

func TestBuiltinLayer_TheKeyVerdictJudgesATreeWithNoRootFile(t *testing.T) {
	t.Parallel()
	dir := builtinTree(t, map[string]string{
		"t1.yaml": "tenants:\n  t1:\n    pg_connections: \"70\"\n    redis_connected_clients: \"5\"\n",
	})
	_, stdout, stderr := runOnce(t, keyVerdictCmd, "--config-dir", dir)
	var doc struct {
		Tenants map[string][]struct {
			Key     string  `json:"key"`
			Verdict string  `json:"verdict"`
			Reason  *string `json:"reason"`
			Layer   *string `json:"layer"`
			File    *string `json:"file"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("key-verdict: %v\n%s\n%s", err, stdout, stderr)
	}
	got := map[string]string{}
	for _, v := range doc.Tenants["t1"] {
		got[v.Key] = v.Verdict
		if v.Layer != nil && *v.Layer == config.KeyLayerBuiltin && v.File != nil {
			t.Errorf("%s: file %q for a built-in value, want none", v.Key, *v.File)
		}
	}
	if got["pg_connections"] != "read" {
		t.Errorf("pg_connections: verdict %q, want read (the built-in layer gives it a base)", got["pg_connections"])
	}
	if got["redis_connected_clients"] != "unread" {
		t.Errorf("redis_connected_clients: verdict %q, want unread (no layer declares it)", got["redis_connected_clients"])
	}
}
