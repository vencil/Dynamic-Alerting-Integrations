package main

// #2031: two spellings of one dimensional key — labels in another order, or
// quoted differently — are one threshold. In two layers the deeper one wins
// as for any key; in one layer one spelling is served and the other is
// refused by the gate (value_not_served, spelling_duplicate). Every output
// names a key as the layer that supplied its value wrote it.

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

const spellingRoot = "defaults:\n  pg_connections: 100\n  redis_queue_length: 10\n"

// effectiveTenantOf runs `da-guard effective` over files and returns the
// entry of tenant tx.
func effectiveTenantOf(t *testing.T, files map[string]string) (cfg map[string]any, ks map[string]config.KeySource, ns map[string]config.NotServedKey) {
	t.Helper()
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	code, doc, stderr := runEffectiveOn(t, filepath.Join(tmp, "conf.d"))
	if code != exitOK {
		t.Fatalf("effective: exit %d: %s", code, stderr)
	}
	var tv struct {
		EffectiveConfig map[string]any                 `json:"effective_config"`
		KeySources      map[string]config.KeySource    `json:"key_sources"`
		NotServed       map[string]config.NotServedKey `json:"not_served"`
	}
	if err := json.Unmarshal(doc.Tenants["tx"], &tv); err != nil {
		t.Fatal(err)
	}
	return tv.EffectiveConfig, tv.KeySources, tv.NotServed
}

// thresholdValues is a served-values or effective map without the reserved keys.
func thresholdValues(m map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range m {
		if !strings.HasPrefix(k, "_") {
			out[k] = v
		}
	}
	return out
}

// Across layers: the tenant's key replaces the other layer's under the
// other spelling — one value, the tenant's, named as the tenant wrote it in
// effective (and its key_sources) and in served-values alike; the gate has no
// error.
func TestSpelling_AcrossLayers_OneThresholdAsWritten(t *testing.T) {
	t.Parallel()
	const tenantKey = `pg_connections{env="prod",r="x"}`
	const otherKey = `pg_connections{r="x",env="prod"}`
	tenant := "tenants:\n  tx:\n    '" + tenantKey + "': 31\n"
	for name, files := range map[string]map[string]string{
		"subtree _defaults": {"_defaults.yaml": spellingRoot,
			"sub/_defaults.yaml": "defaults:\n  '" + otherKey + "': 30\n", "sub/tx.yaml": tenant},
		"platform tenants:": {"_defaults.yaml": spellingRoot + "tenants:\n  tx:\n    '" + otherKey + "': 30\n",
			"tx.yaml": tenant},
		"profile": {"_defaults.yaml": spellingRoot, "_profiles.yaml": "profiles:\n  std:\n    '" + otherKey + "': 30\n",
			"tx.yaml": strings.Replace(tenant, "    '", "    _profile: std\n    '", 1)},
		"root defaults:": {"_defaults.yaml": spellingRoot + "  '" + otherKey + "': 30\n", "tx.yaml": tenant},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			cfg, ks, ns := effectiveTenantOf(t, files)
			if got := cfg[tenantKey]; got != float64(31) {
				t.Errorf("effective_config[%s] = %v, want 31; config %v", tenantKey, got, cfg)
			}
			if _, both := cfg[otherKey]; both {
				t.Errorf("effective_config holds both spellings: %v", cfg)
			}
			if ks[tenantKey].Layer != config.KeyLayerTenant {
				t.Errorf("key_sources[%s] = %+v, want the tenant layer", tenantKey, ks[tenantKey])
			}
			if len(ns) != 0 {
				t.Errorf("not_served = %v, want none", ns)
			}
			code, doc, _, stderr := served(t, files, "")
			mustOK(t, code, stderr)
			got := thresholdValues(doc.Tenants["tx"].Values)
			want := thresholdValues(cfg)
			delete(want, "_profile")
			if len(got) != len(want) || got[tenantKey] != float64(31) {
				t.Errorf("served-values %v, want effective's keys and values %v", got, want)
			}
			gcode, fs := guardFindingsOf(t, files)
			for _, f := range fs {
				if f.Severity == guard.SeverityError {
					t.Errorf("gate error: %+v", f)
				}
			}
			if gcode != exitOK {
				t.Errorf("gate exit = %d, want %d", gcode, exitOK)
			}
		})
	}
}

// In one layer: the canonical spelling (else the smallest text) is served,
// the other is a spelling_duplicate the gate refuses — for a dimensional key
// and for the two #1231 spellings alike.
func TestSpelling_OneLayer_DuplicateIsRefused(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		tenant         string
		served, loser  string
		value          float64
		file, otherDir string
	}{
		"label order": {
			tenant: "    'redis_queue_length{queue=\"a\", priority=\"high\"}': 5\n" +
				"    'redis_queue_length{priority=\"high\", queue=\"a\"}': 6\n",
			served: `redis_queue_length{priority="high", queue="a"}`, loser: `redis_queue_length{queue="a", priority="high"}`,
			value: 6, file: "tx.yaml"},
		"quotes, neither canonical": {
			tenant: "    \"redis_queue_length{queue='a'}\": 5\n    'redis_queue_length{ queue=\"a\" }': 6\n",
			served: `redis_queue_length{ queue="a" }`, loser: `redis_queue_length{queue='a'}`,
			value: 6, file: "tx.yaml"},
		"#1231 spellings": {
			tenant: "    " + "mysql_" + "cpu: 5\n    mysql_threads_running: 6\n",
			served: "mysql_threads_running", loser: "mysql_" + "cpu",
			value: 6, file: "tx.yaml"},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{
				"_defaults.yaml": spellingRoot + "  mysql_threads_running: 30\n",
				"tx.yaml":        "tenants:\n  tx:\n" + tc.tenant,
			}
			cfg, _, ns := effectiveTenantOf(t, files)
			if cfg[tc.served] != tc.value {
				t.Errorf("effective_config[%s] = %v, want %v; config %v", tc.served, cfg[tc.served], tc.value, cfg)
			}
			if want := (config.NotServedKey{Reason: config.NotServedSpellingDuplicate, File: tc.file}); ns[tc.loser] != want {
				t.Errorf("not_served[%s] = %+v, want %+v (all: %v)", tc.loser, ns[tc.loser], want, ns)
			}
			code, doc, _, stderr := served(t, files, "")
			mustOK(t, code, stderr)
			if got := doc.Tenants["tx"].Values[tc.served]; got != tc.value {
				t.Errorf("served-values[%s] = %v, want %v", tc.served, got, tc.value)
			}
			gcode, fs := guardFindingsOf(t, files)
			if gcode != exitFindings {
				t.Errorf("gate exit = %d, want %d", gcode, exitFindings)
			}
			found := false
			for _, f := range fs {
				if f.Kind == guard.FindingValueNotServed && f.Field == tc.loser &&
					strings.HasPrefix(f.Message, config.NotServedSpellingDuplicate+": ") {
					found = true
				}
			}
			if !found {
				t.Errorf("no value_not_served spelling_duplicate on %s: %+v", tc.loser, fs)
			}
		})
	}
}

// Keys whose labels the parser cuts at a comma are not re-spelled, so two
// texts that mean different thresholds are never merged into one.
func TestSpelling_KeysTheParserCutsAreNotMerged(t *testing.T) {
	t.Parallel()
	cfg, _, _ := effectiveTenantOf(t, map[string]string{
		"_defaults.yaml": spellingRoot,
		"tx.yaml":        "tenants:\n  tx:\n    'redis_queue_length{q=~\"A,B\"}': 1\n    'redis_queue_length{q=~\"A\"}': 2\n",
	})
	if cfg[`redis_queue_length{q=~"A,B"}`] != float64(1) || cfg[`redis_queue_length{q=~"A"}`] != float64(2) {
		t.Errorf("effective_config = %v, want both keys as written", cfg)
	}
}

// A finding on a re-spelled key names it as written: the gate reads the
// canonical spelling, the report does not.
func TestSpelling_FindingNamesTheKeyAsWritten(t *testing.T) {
	t.Parallel()
	const written = `pg_connections{env="prod",r="x"}`
	_, fs := guardFindingsOf(t, map[string]string{
		"_defaults.yaml":     spellingRoot,
		"sub/_defaults.yaml": "defaults:\n  'pg_connections{r=\"x\",env=\"prod\"}': 30\n",
		"sub/tx.yaml":        "tenants:\n  tx:\n    '" + written + "': 30\n",
	})
	var got *guard.Finding
	for i, f := range fs {
		if f.Kind == guard.FindingRedundantOverride {
			got = &fs[i]
		}
	}
	if got == nil || got.Field != written || !strings.Contains(got.Message, strings.ReplaceAll(written, `"`, `\"`)) {
		t.Errorf("redundant_override = %+v, want Field and message naming %s", got, written)
	}
}
