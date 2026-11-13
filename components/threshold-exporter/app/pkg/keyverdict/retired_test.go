package keyverdict

// #3829: a threshold key the threshold registry retired (`retired_keys:`) is
// unread at error, reason retired, whatever reads it and wherever it is
// written. The committed registry retires no key yet, so the table is the
// test's own (Input.Retired) — no global is swapped. Key names are fixture
// names, not shipped keys.

import (
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

const (
	retiredKey     = "fixture_old_conns"
	replacementKey = "fixture_new_conns"
)

// retiredFixture retires retiredKey in favour of replacementKey, by head as
// config.RetiredThresholdHint does.
func retiredFixture(key string) string {
	if head, _, _ := strings.Cut(key, "{"); head == retiredKey {
		return "retired threshold key (#1): write `" + replacementKey + "` instead; fixture"
	}
	return ""
}

// servedAndRead is a tenant whose key /metrics serves (the exporter reads
// it) from the given layer.
func servedAndRead(key string, src config.KeySource) Input {
	return Input{
		Served: map[string]ServedTenant{"t1": {Keys: map[string]ServedKey{key: {Exporter: true}},
			Values: map[string]any{key: 80.0}, Rows: map[string]bool{key: true}}},
		Effective: map[string]EffectiveTenant{"t1": {Config: map[string]any{key: 80},
			Sources: map[string]config.KeySource{key: src}, DefaultsChain: []string{"_defaults.yaml"}}},
		Retired: retiredFixture,
	}
}

func TestJudge_ARetiredThresholdKeyIsAnErrorInEveryLayerThoughTheExporterReadsIt(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		key  string
		src  config.KeySource
	}{
		// The deployment's own root `_defaults.yaml` (level 0): the case
		// the advisory `no_alert` row alone reported (#3829).
		{"root defaults", retiredKey, config.KeySource{Layer: config.KeyLayerDefaults, File: "_defaults.yaml", Level: level(0)}},
		{"subtree defaults", retiredKey, config.KeySource{Layer: config.KeyLayerDefaults, File: "a/_defaults.yaml", Level: level(1)}},
		{"tenant", retiredKey, config.KeySource{Layer: config.KeyLayerTenant, File: "t1.yaml"}},
		{"profile", retiredKey, config.KeySource{Layer: config.KeyLayerProfile, File: "_profiles.yaml"}},
		{"platform tenants", retiredKey, config.KeySource{Layer: config.KeyLayerPlatform, File: "_defaults.yaml"}},
		{"dimensional", retiredKey + `{db="a"}`, config.KeySource{Layer: config.KeyLayerTenant, File: "t1.yaml"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tree := Judge(servedAndRead(tc.key, tc.src))
			v := one(t, tree, "t1", tc.key)
			if v.Verdict != Unread || deref(v.Severity) != SeverityError || deref(v.Reason) != ReasonRetired {
				t.Fatalf("verdict %+v", v)
			}
			if !strings.Contains(deref(v.Hint), "`"+replacementKey+"`") {
				t.Fatalf("hint %q does not name the replacement %q", deref(v.Hint), replacementKey)
			}
			if len(tree.Findings) != 1 {
				t.Fatalf("findings %+v", tree.Findings)
			}
			f := tree.Findings[0]
			if f.Kind != KindUnread || f.Severity != SeverityError || deref(f.Reason) != ReasonRetired ||
				deref(f.File) != tc.src.File || !f.Blocks || !tree.Blocks ||
				!strings.Contains(deref(f.Hint), replacementKey) {
				t.Fatalf("finding %+v (hint %q)", f, deref(f.Hint))
			}
		})
	}
}

// Decidable without a root platform file: the registry alone says so.
func TestJudge_ARetiredThresholdKeyIsJudgedInAnUndecidableTree(t *testing.T) {
	t.Parallel()
	in := servedAndRead(retiredKey, config.KeySource{Layer: config.KeyLayerTenant, File: "t1.yaml"})
	in.Undecidable = true
	if v := one(t, Judge(in), "t1", retiredKey); v.Verdict != Unread || deref(v.Reason) != ReasonRetired {
		t.Fatalf("verdict %+v", v)
	}
}

// Every other key is judged as before: read, no hint, no finding — a
// replacement key included, and a key that merely shares the retired
// key's prefix.
func TestJudge_ANonRetiredThresholdKeyIsUnchanged(t *testing.T) {
	t.Parallel()
	for _, key := range []string{replacementKey, retiredKey + "_x", "_silent_mode"} {
		in := servedAndRead(key, config.KeySource{Layer: config.KeyLayerDefaults, File: "_defaults.yaml", Level: level(0)})
		withTable := Judge(in)
		in.Retired = func(string) string { return "" }
		without := Judge(in)
		if !reflect.DeepEqual(withTable, without) {
			t.Fatalf("%s: the retired table changed the verdict:\n with %+v\n without %+v", key, withTable, without)
		}
		if v := one(t, withTable, "t1", key); v.Hint != nil {
			t.Fatalf("%s: hint %q", key, *v.Hint)
		}
	}
}

// A nil Input.Retired reads the generated table (config.RetiredThresholdHint):
// with the committed registry retiring nothing, the root-defaults case is read.
func TestJudge_NilRetiredReadsTheRegistryTable(t *testing.T) {
	t.Parallel()
	in := servedAndRead(retiredKey, config.KeySource{Layer: config.KeyLayerDefaults, File: "_defaults.yaml", Level: level(0)})
	in.Retired = nil
	want := Read
	if config.RetiredThresholdHint(retiredKey) != "" {
		want = Unread
	}
	if v := one(t, Judge(in), "t1", retiredKey); v.Verdict != want {
		t.Fatalf("verdict %+v, want %s", v, want)
	}
}
