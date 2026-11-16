package main

// builtin_defaults_doc_test.go — effective's `builtin_defaults` (#3832): the
// built-in table this binary was compiled with and, per key, what the
// exporter's build of the tree does with it. validate-config's
// platform_defaults row prints this answer and works out nothing itself, so
// every state is pinned here, against the same tree's key_sources and
// served values.

import (
	"encoding/json"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// builtinDefaultsOut decodes effective's `builtin_defaults`; nil when null.
func builtinDefaultsOut(t *testing.T, dir string) (int, *builtinDefaultsDoc) {
	t.Helper()
	code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir)
	var doc struct {
		BuiltinDefaults *builtinDefaultsDoc `json:"builtin_defaults"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON (exit %d, stderr=%q): %v", code, stderr, err)
	}
	return code, doc.BuiltinDefaults
}

// wantEveryKey fails unless got names exactly the keys of the built-in
// table, each with the platform's value, under this binary's digest.
func wantEveryKey(t *testing.T, got *builtinDefaultsDoc) {
	t.Helper()
	if got == nil {
		t.Fatal("builtin_defaults is null; want the table")
	}
	if got.Digest != config.BuiltinDefaultsDigest() {
		t.Errorf("digest = %q, want %q", got.Digest, config.BuiltinDefaultsDigest())
	}
	table := config.BuiltinDefaults()
	if len(table) == 0 {
		t.Fatal("the built-in table is empty — this test would pass vacuously")
	}
	if len(got.Keys) != len(table) {
		t.Errorf("keys = %v, want every key of %v", keysOf(got.Keys), keysOf(table))
	}
	for k, v := range table {
		if e, ok := got.Keys[k]; !ok || e.Value != v {
			t.Errorf("keys[%q] = %+v (present %v), want value %v", k, e, ok, v)
		}
	}
}

func TestBuiltinDefaultsDoc_ARootThatGivesNoValueIsBuiltin(t *testing.T) {
	t.Parallel()
	for name, files := range map[string]map[string]string{
		"no root carrier": {"t1.yaml": "tenants:\n  t1: {}\n"},
		"empty defaults":  {"_defaults.yaml": "defaults: {}\n", "t1.yaml": "tenants:\n  t1: {}\n"},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			code, got := builtinDefaultsOut(t, builtinTree(t, files))
			if code != exitOK {
				t.Fatalf("exit %d", code)
			}
			wantEveryKey(t, got)
			for k, e := range got.Keys {
				if e.State != config.BuiltinStateBuiltin || e.RootValue != nil {
					t.Errorf("keys[%q] = %+v, want state builtin and no root_value", k, e)
				}
			}
		})
	}
}

// TestBuiltinDefaultsDoc_TheRootDecides: a number under the root `defaults:`
// is `overridden`, with that number; null and a value the root decode
// refuses are `builtin` — what /metrics serves for each (served-values) and
// what key_sources says for a tenant that writes none of them.
func TestBuiltinDefaultsDoc_TheRootDecides(t *testing.T) {
	t.Parallel()
	keys := sortedBuiltinKeys()
	if len(keys) < 3 {
		t.Fatalf("need three built-in keys, have %v", keys)
	}
	valued, nulled, refused := keys[0], keys[1], keys[2]
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  " + valued + ": 7\n  " + nulled + ": null\n  " + refused + ": abc\n",
		"t1.yaml":        "tenants:\n  t1: {}\n",
	}
	dir := builtinTree(t, files)
	code, got := builtinDefaultsOut(t, dir)
	if code != exitOK {
		t.Fatalf("exit %d", code)
	}
	wantEveryKey(t, got)
	if e := got.Keys[valued]; e.State != config.BuiltinStateOverridden || e.RootValue == nil || *e.RootValue != 7 {
		t.Errorf("keys[%q] = %+v, want overridden with root_value 7", valued, e)
	}
	for _, k := range []string{nulled, refused} {
		if e := got.Keys[k]; e.State != config.BuiltinStateBuiltin || e.RootValue != nil ||
			e.Tenants == nil || *e.Tenants != 1 {
			t.Errorf("keys[%q] = %+v, want builtin served to 1 tenant", k, e)
		}
	}
	if e := got.Keys[valued]; e.Tenants != nil {
		t.Errorf("keys[%q].tenants = %v, want absent on overridden", valued, *e.Tenants)
	}
	// The same tree's own answers agree: the tenant's key_sources and its rows.
	et := builtinEffectiveOf(t, dir, "t1")
	for k, e := range got.Keys {
		wantLayer := "builtin"
		if e.State == config.BuiltinStateOverridden {
			wantLayer = "defaults"
		}
		if src := et.KeySources[k]; src.Layer != wantLayer {
			t.Errorf("key_sources[%q].layer = %q, but builtin_defaults says %q", k, src.Layer, e.State)
		}
	}
	sc, doc, _, stderr := servedWith(t, files, "")
	mustOK(t, sc, stderr)
	wantValue(t, doc, "t1", valued, 7)
	for _, k := range []string{nulled, refused} {
		want, _ := config.BuiltinDefault(k)
		wantValue(t, doc, "t1", k, want)
	}
}

// TestBuiltinDefaultsDoc_ARootThatCannotBeReadWithholdsEveryKey: exit 3, and
// the document still says why no default is served — every key `withheld` —
// so a reader tells it from "not judged".
func TestBuiltinDefaultsDoc_ARootThatCannotBeReadWithholdsEveryKey(t *testing.T) {
	t.Parallel()
	for name, root := range map[string]string{
		"syntax error":     "defaults: [oops\n",
		"dropped by build": "defaults:\n  " + sortedBuiltinKeys()[0] + ": [1, 2]\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			code, got := builtinDefaultsOut(t, builtinTree(t, map[string]string{
				"_defaults.yaml": root, "t1.yaml": "tenants:\n  t1: {}\n"}))
			if code != exitParseFailed {
				t.Fatalf("exit %d, want %d", code, exitParseFailed)
			}
			wantEveryKey(t, got)
			for k, e := range got.Keys {
				if e.State != config.BuiltinStateWithheld || e.RootValue != nil {
					t.Errorf("keys[%q] = %+v, want withheld", k, e)
				}
			}
		})
	}
}

// TestBuiltinDefaultsDoc_ATenantFileTheLoadDropsLeavesTheRootJudged: another
// file dropped (exit 3) does not withhold the layer.
func TestBuiltinDefaultsDoc_ATenantFileTheLoadDropsLeavesTheRootJudged(t *testing.T) {
	t.Parallel()
	code, got := builtinDefaultsOut(t, builtinTree(t, map[string]string{
		"t1.yaml": "tenants:\n  t1: {}\n",
		"t2.yaml": "tenants:\n  t2: [1]\n",
	}))
	if code != exitParseFailed {
		t.Fatalf("exit %d, want %d", code, exitParseFailed)
	}
	wantEveryKey(t, got)
	for k, e := range got.Keys {
		if e.State != config.BuiltinStateBuiltin {
			t.Errorf("keys[%q] = %+v, want builtin", k, e)
		}
	}
}

// TestBuiltinDefaultsDoc_TheTenantCountIsWhatMetricsServes: `tenants` counts
// the tenants /metrics serves the platform's value — none written, null, or
// a value that falls back (in the tenant's file or a subtree) — and not those
// that set a number or `disable` (#3832 review: key_sources alone said 2).
func TestBuiltinDefaultsDoc_TheTenantCountIsWhatMetricsServes(t *testing.T) {
	t.Parallel()
	k := sortedBuiltinKeys()[0]
	files := map[string]string{
		"plain.yaml":         "tenants:\n  plain: {}\n",
		"tnull.yaml":         "tenants:\n  tnull:\n    " + k + ": null\n",
		"tabc.yaml":          "tenants:\n  tabc:\n    " + k + ": abc\n",
		"own.yaml":           "tenants:\n  own:\n    " + k + ": \"5\"\n",
		"sched.yaml":         "tenants:\n  sched:\n    " + k + ":\n      default: \"70\"\n",
		"off.yaml":           "tenants:\n  off:\n    " + k + ": disable\n",
		"bad/_defaults.yaml": "defaults:\n  " + k + ": abc\n",
		"bad/tb.yaml":        "tenants:\n  tb: {}\n",
		"sub/_defaults.yaml": "defaults:\n  " + k + ": 6\n",
		"sub/deep.yaml":      "tenants:\n  deep: {}\n",
	}
	code, got := builtinDefaultsOut(t, builtinTree(t, files))
	if code != exitOK {
		t.Fatalf("exit %d", code)
	}
	if e := got.Keys[k]; e.Tenants == nil || *e.Tenants != 4 {
		t.Errorf("keys[%q] = %+v, want tenants 4 (plain, tnull, tabc, tb)", k, e)
	}
	sc, doc, _, stderr := servedWith(t, files, "")
	mustOK(t, sc, stderr)
	want, _ := config.BuiltinDefault(k)
	served := 0
	for _, tv := range doc.Tenants {
		if v, ok := tv.Values[k].(float64); ok && v == want {
			served++
		}
	}
	if served != 4 {
		t.Errorf("served-values serves %d tenant(s) the platform value, want 4", served)
	}
}
