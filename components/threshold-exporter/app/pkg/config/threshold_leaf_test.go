package config

import (
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// thresholdLeafSpellings are threshold values as a YAML file writes them,
// unquoted unless the quote is the point: every shape on which YAML's typing
// and the exporter's reading of the text part (#3250, #3361), and the
// shapes on which they agree.
var thresholdLeafSpellings = []string{
	"70", "70.0", "1.5", "1e6", "7e1", "1_000", "+5", "-0", "-3",
	"017", "0x1F", "0o17", "0b101", "0x1p4", "5_",
	`"70"`, `'017'`, `" 70 "`,
	".nan", "NaN", ".inf", "-.inf", "Inf", "1e999",
	"true", "abc", "70:critical",
}

// thresholdLeafLayers write the value v of key mysql_connections at one layer
// of a conf.d tree whose root declares the key at 5 (the root layer itself
// excepted); tenant t1 lives in the tree.
var thresholdLeafLayers = map[string]func(v string) map[string]string{
	"root": func(v string) map[string]string {
		return map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: " + v + "\n",
			"t1.yaml":        "tenants:\n  t1:\n    _metadata:\n      owner: x\n",
		}
	},
	"subtree": func(v string) map[string]string {
		return map[string]string{
			"_defaults.yaml":      "defaults:\n  mysql_connections: 5\n",
			"team/_defaults.yaml": "defaults:\n  mysql_connections: " + v + "\n",
			"team/t1.yaml":        "tenants:\n  t1:\n    _metadata:\n      owner: x\n",
		}
	},
	// #3250 review: `defaults:` that comes only through a top-level `<<`
	// merge is read the same way.
	"root-merged": func(v string) map[string]string {
		return map[string]string{
			"_defaults.yaml": "x-base: &d {defaults: {mysql_connections: " + v + "}}\n<<: *d\n",
			"t1.yaml":        "tenants:\n  t1:\n    _metadata:\n      owner: x\n",
		}
	},
	"subtree-merged": func(v string) map[string]string {
		return map[string]string{
			"_defaults.yaml":      "defaults:\n  mysql_connections: 5\n",
			"team/_defaults.yaml": "x-base: &d {defaults: {mysql_connections: " + v + "}}\n<<: *d\n",
			"team/t1.yaml":        "tenants:\n  t1:\n    _metadata:\n      owner: x\n",
		}
	},
	"tenant": func(v string) map[string]string {
		return map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 5\n",
			"t1.yaml":        "tenants:\n  t1:\n    mysql_connections: " + v + "\n",
		}
	},
	"tenant-schedule": func(v string) map[string]string {
		return map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 5\n",
			"t1.yaml":        "tenants:\n  t1:\n    mysql_connections:\n      default: " + v + "\n",
		}
	},
	"platform": func(v string) map[string]string {
		return map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 5\n",
			"_platform.yaml": "tenants:\n  t1:\n    mysql_connections: " + v + "\n",
			"t1.yaml":        "tenants:\n  t1:\n    _metadata:\n      owner: x\n",
		}
	},
	"profile": func(v string) map[string]string {
		return map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 5\n",
			"_profiles.yaml": "profiles:\n  p:\n    mysql_connections: " + v + "\n",
			"t1.yaml":        "tenants:\n  t1:\n    _profile: p\n",
		}
	},
}

// The written view (/effective, ResolveEffective) and /metrics (LoadDir and
// the resolver) read a threshold's value one way at every layer (#3250,
// #3361): where /effective shows a number, or text thresholdTextNumber
// reads, /metrics serves that number and /effective names no reason it does
// not; where it shows text the exporter refuses, /effective names why
// /metrics does not serve it as shown. Before #3250 the root layer and the subtree layer were typed
// by YAML on the written side — `017` shown as 15 and served as 17, `0x1F`
// shown as 31 and refused — and redundant_override, merged_hash and
// key_sources all compared those numbers.
func TestThresholdLeaf_EffectiveShowsWhatMetricsServes(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC)
	for layer, files := range thresholdLeafLayers {
		for _, v := range thresholdLeafSpellings {
			if layer == "tenant-schedule" && strings.Contains(v, ":") {
				continue // a schedule's default takes no severity suffix of its own here
			}
			t.Run(layer+"/"+v, func(t *testing.T) {
				t.Parallel()
				dir := writeLeafTree(t, files(v))
				cfg, _, err := LoadDir(dir, nil)
				if err != nil {
					t.Fatalf("LoadDir: %v", err)
				}
				served, isServed := servedWarningRow(cfg, "t1", "mysql", "connections", now)
				ec, err := ResolveEffective(dir, "t1")
				if err != nil {
					t.Fatalf("ResolveEffective: %v", err)
				}
				shown := ec.EffectiveConfig["mysql_connections"]
				if m, ok := shown.(map[string]any); ok {
					shown = m["default"]
				}
				_, named := ec.NotServed["mysql_connections"]
				n, number := genericNumber(shown)
				if text, isText := shown.(string); isText {
					n, number = thresholdTextNumber(text)
				}
				switch {
				case strings.HasSuffix(v, ":critical"):
					// Served with severity critical: the warning row is the
					// root's fallback-free reading — not this test's question.
				case number && named:
					t.Errorf("effective shows %v and names a reason it is not served (%v)", shown,
						ec.NotServed["mysql_connections"])
				case number && (!isServed || served != n):
					t.Errorf("effective shows %v (%T); /metrics serves %v (row: %v)", shown, shown, served, isServed)
				case !number && !named:
					t.Errorf("effective shows %#v and names no reason; /metrics serves %v (row: %v)",
						shown, served, isServed)
				}
			})
		}
	}
}

// thresholdLeaf leaves a string, and a number whose reading did not change,
// exactly as the generic decode gave them, so merged_hash moves only where
// the meaning did; a number YAML misstates becomes the text.
func TestThresholdLeaf_KeepsAnAgreeingValue(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		g    any
		text string
		want any
	}{
		{70, "70", 70}, {1e6, "1e6", 1e6}, {1.5, "1.5", 1.5}, {1000, "1_000", 1000}, {"abc", "abc", "abc"},
		{"70", "70", "70"}, {"70:critical", "70:critical", "70:critical"}, {nil, "", nil},
		{math.Copysign(0, -1), "-0", math.Copysign(0, -1)},
		{15, "017", "017"}, {31, "0x1F", "0x1F"}, {15, "0o17", "0o17"}, {true, "true", "true"},
		{math.Inf(1), "1e999", "1e999"}, {math.NaN(), ".nan", ".nan"}, {0, "-0", 0},
	} {
		got := thresholdLeaf(c.g, c.text)
		if fmt.Sprintf("%T %v", got, got) != fmt.Sprintf("%T %v", c.want, c.want) ||
			(isFloat(got) && math.Signbit(got.(float64)) != math.Signbit(c.want.(float64))) {
			t.Errorf("thresholdLeaf(%#v, %q) = %#v (%T), want %#v (%T)", c.g, c.text, got, got, c.want, c.want)
		}
	}
}

func isFloat(v any) bool { _, ok := v.(float64); return ok }

func writeLeafTree(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// servedWarningRow is the value of tenant's severity=warning row for
// component's metric at now, with no dimensional labels.
func servedWarningRow(cfg *ThresholdConfig, tenant, component, metric string, now time.Time) (float64, bool) {
	for _, r := range cfg.ResolveAt(now) {
		if r.Tenant == tenant && r.Component == component && r.Metric == metric && r.Severity == "warning" &&
			len(r.CustomLabels) == 0 &&
			len(r.RegexLabels) == 0 && r.legacyTwinOf == "" {
			return r.Value, true
		}
	}
	return 0, false
}
