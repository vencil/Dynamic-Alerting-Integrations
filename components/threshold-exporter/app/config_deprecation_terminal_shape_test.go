package main

// Where the exporter's own loader meets `deprecate_rule --execute` (#1787).
//
// `ThresholdConfig.Defaults` is `map[string]float64` (pkg/config/types.go:208):
// a string under a root `defaults:` does not disable one metric — `yaml.Unmarshal`
// fails and `parsePartialConfig` drops the WHOLE carrier, `state_filters:` and
// all. The tool therefore deletes keys, and two fixtures under
// tests/golden/fixtures link the two sides byte for byte:
//
//   - deprecate-rule/{before,after}/_defaults.yaml — the Python side runs the
//     tool on a copy of before/ and asserts the bytes equal after/
//     (tests/ops/test_deprecate_rule_carriers.py::
//     test_the_tool_output_is_the_golden_after_file); this file loads after/
//     through the real loader.
//   - defaults-carrier-oracle.json — the truth table for the tool's carrier
//     health check. TestDefaultsCarrierOracle is the AUTHORITY on the
//     `exporter` column; the Python side (…::test_carrier_health_matches_the_exporter)
//     must reach the same verdict from the same bytes. A verdict in that file
//     changes only when this test proves it.
//
// ⚠️ The `disable-sentinel` arm of each terminal-shape test is a CONTROL and is
// expected to FAIL to load. If it ever starts loading, the loader was widened to
// accept string sentinels under `defaults:` — ADR-017's defaults/tenants type
// boundary, not this test, is the thing to revisit.

import (
	"bytes"
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// goldenFixture reads one file under tests/golden/fixtures. Missing is a
// failure, not a skip: the fixture IS the link between the two sides.
func goldenFixture(t *testing.T, parts ...string) []byte {
	t.Helper()
	path := filepath.Join(append([]string{"..", "..", "..", "tests", "golden",
		"fixtures"}, parts...)...)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("golden fixture unreadable: %v", err)
	}
	return data
}

// The carrier `deprecate_rule --execute` used to leave behind (pre-#1787).
const deprecatedDisableCarrier = `# _defaults.yaml — Platform global defaults
defaults:
  cpu_usage: disable
  mem_usage: 90
  disk_usage: 85
state_filters:
  container_crashloop:
    reasons: ["CrashLoopBackOff"]
    severity: "critical"
`

// What the platform still owes every tenant AFTER cpu_usage is deprecated —
// the values in deprecate-rule/before/_defaults.yaml that are not cpu_usage's.
var deprecationSurvivors = map[string]float64{
	"mem_usage":  90,
	"disk_usage": 85,
}

func TestDeprecatedMetricTerminalShape_CarrierStillParses(t *testing.T) {
	arms := []struct {
		name    string
		doc     []byte
		wantOK  bool
		wantLog string // substring the loader must log when it rejects the file
	}{
		{
			name:   "delete-key (deprecate-rule/after golden)",
			doc:    goldenFixture(t, "deprecate-rule", "after", "_defaults.yaml"),
			wantOK: true,
		},
		{
			name:    "disable-sentinel (pre-#1787 output — control)",
			doc:     []byte(deprecatedDisableCarrier),
			wantOK:  false,
			wantLog: "entire block dropped",
		},
	}

	for _, a := range arms {
		a := a
		t.Run(a.name, func(t *testing.T) {
			var logBuf bytes.Buffer
			cfg, ok := parsePartialConfig(
				"_defaults.yaml", "/conf.d/_defaults.yaml", a.doc,
				newConfigMetrics(), log.New(&logBuf, "", 0))

			if ok != a.wantOK {
				t.Fatalf("parsePartialConfig ok = %v, want %v (log: %s)",
					ok, a.wantOK, logBuf.String())
			}

			if !a.wantOK {
				// ⚠️ Do NOT assert on the returned struct here: `yaml.Unmarshal`
				// leaves it PARTIALLY populated, which reads like "only
				// cpu_usage was lost" and is the opposite of what happens. The
				// `ok=false` IS the loss; what must be pinned here is that the
				// rejection is not silent.
				if !strings.Contains(logBuf.String(), a.wantLog) {
					t.Errorf("expected the drop to be logged with %q, got: %s",
						a.wantLog, logBuf.String())
				}
				return
			}

			// The deprecated metric is gone from both planes …
			for _, k := range []string{"cpu_usage", "cpu_usage_critical",
				"custom_cpu_usage", "custom_cpu_usage_critical"} {
				if _, present := cfg.Defaults[k]; present {
					t.Errorf("deprecated key %q survived in defaults: %v",
						k, cfg.Defaults)
				}
				for _, o := range cfg.OptionalOverrides {
					if o == k {
						t.Errorf("deprecated key %q survived in optional_overrides: %v",
							k, cfg.OptionalOverrides)
					}
				}
			}
			// … and nothing else went with it.
			if len(cfg.Defaults) != len(deprecationSurvivors) {
				t.Errorf("defaults = %v, want exactly %v",
					cfg.Defaults, deprecationSurvivors)
			}
			for k, want := range deprecationSurvivors {
				got, present := cfg.Defaults[k]
				if !present {
					t.Errorf("defaults lost %q entirely: %v", k, cfg.Defaults)
					continue
				}
				if got != want {
					t.Errorf("defaults[%q] = %v, want %v", k, got, want)
				}
			}
			if len(cfg.OptionalOverrides) != 1 || cfg.OptionalOverrides[0] != "oracle_process_count" {
				t.Errorf("optional_overrides = %v, want the one surviving name",
					cfg.OptionalOverrides)
			}
			// The sibling block riding in the same file — the part of the blast
			// radius nobody expects a "metric deprecation" to touch.
			sf, present := cfg.StateFilters["container_crashloop"]
			if !present {
				t.Fatalf("state_filters lost the carrier's own block: %v",
					cfg.StateFilters)
			}
			if sf.Severity != "critical" {
				t.Errorf("state_filters severity = %q, want %q",
					sf.Severity, "critical")
			}
			if logBuf.Len() != 0 {
				t.Errorf("a clean carrier must load silently, got: %s",
					logBuf.String())
			}
		})
	}
}

// TestDeprecatedMetricTerminalShape_WholeTreeReload measures the same two
// shapes where an operator would notice: the series the exporter resolves for
// a whole conf.d. Deleting the key costs exactly the deprecated metric's rows;
// the `disable` sentinel costs the entire tree that carrier fed, state
// filters included.
func TestDeprecatedMetricTerminalShape_WholeTreeReload(t *testing.T) {
	arms := []struct {
		name             string
		carrier          []byte
		wantMetrics      []string // metric names that must resolve for the tenant
		wantStateFilters int
	}{
		{
			name:             "delete-key (deprecate-rule/after golden)",
			carrier:          goldenFixture(t, "deprecate-rule", "after", "_defaults.yaml"),
			wantMetrics:      []string{"mem_usage", "disk_usage"},
			wantStateFilters: 1,
		},
		{
			name:             "disable-sentinel (pre-#1787 output — control)",
			carrier:          []byte(deprecatedDisableCarrier),
			wantMetrics:      nil,
			wantStateFilters: 0,
		},
	}

	for _, a := range arms {
		a := a
		t.Run(a.name, func(t *testing.T) {
			dir := t.TempDir()
			write := func(name string, body []byte) {
				t.Helper()
				if err := os.WriteFile(filepath.Join(dir, name), body, 0o600); err != nil {
					t.Fatal(err)
				}
			}
			write("_defaults.yaml", a.carrier)
			write("alpha.yaml", []byte("tenants:\n  alpha: {}\n"))

			var logBuf bytes.Buffer
			mgr := NewConfigManager(dir)
			mgr.SetLogger(log.New(&logBuf, "", 0))
			if err := mgr.Load(); err != nil {
				t.Fatalf("Load: %v (log: %s)", err, logBuf.String())
			}
			cfg := mgr.GetConfig()

			got := map[string]bool{}
			for _, r := range cfg.ResolveAt(time.Now()) {
				got[r.Component+"_"+r.Metric] = true
			}
			if len(got) != len(a.wantMetrics) {
				t.Errorf("resolved %d series %v, want %d %v",
					len(got), got, len(a.wantMetrics), a.wantMetrics)
			}
			for _, m := range a.wantMetrics {
				if !got[m] {
					t.Errorf("series for %q did not resolve; got %v", m, got)
				}
			}
			if _, present := got["cpu_usage"]; present {
				t.Errorf("the deprecated metric still resolves: %v", got)
			}
			if n := len(cfg.ResolveStateFilters()); n != a.wantStateFilters {
				t.Errorf("state_filters resolved = %d, want %d",
					n, a.wantStateFilters)
			}
		})
	}
}

// carrierOracleCase is one row of defaults-carrier-oracle.json.
type carrierOracleCase struct {
	Name     string   `json:"name"`
	Doc      string   `json:"doc"`
	Exporter string   `json:"exporter"` // accepted | dropped | zero
	Key      *string  `json:"key"`      // nil for document-level rows
	Value    *float64 `json:"value"`    // only for finite accepted values
}

func loadCarrierOracle(t *testing.T) []carrierOracleCase {
	t.Helper()
	var table struct {
		Cases []carrierOracleCase `json:"cases"`
	}
	if err := json.Unmarshal(goldenFixture(t, "defaults-carrier-oracle.json"), &table); err != nil {
		t.Fatalf("defaults-carrier-oracle.json: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("defaults-carrier-oracle.json holds no cases")
	}
	return table.Cases
}

// assertCarrierVerdict is the meaning of the `exporter` column.
func assertCarrierVerdict(t *testing.T, name string, doc []byte, want string, key *string, value *float64) {
	t.Helper()
	var logBuf bytes.Buffer
	cfg, ok := parsePartialConfig(
		"_defaults.yaml", "/conf.d/_defaults.yaml", doc,
		newConfigMetrics(), log.New(&logBuf, "", 0))
	switch want {
	case "dropped":
		if ok {
			t.Fatalf("%s: loaded (defaults=%v), want the carrier dropped", name, cfg.Defaults)
		}
	case "accepted", "zero":
		if !ok {
			t.Fatalf("%s: dropped, want %s (log: %s)", name, want, logBuf.String())
		}
		if key == nil {
			return
		}
		got, present := cfg.Defaults[*key]
		if !present {
			t.Fatalf("%s: loaded but %q is not in defaults: %v", name, *key, cfg.Defaults)
		}
		if want == "zero" && got != 0 {
			t.Fatalf("%s: defaults[%q] = %v, want 0", name, *key, got)
		}
		if value != nil && got != *value {
			t.Fatalf("%s: defaults[%q] = %v, want %v", name, *key, got, *value)
		}
	default:
		t.Fatalf("%s: unknown exporter verdict %q", name, want)
	}
}

func TestDefaultsCarrierOracle(t *testing.T) {
	seen := map[string]bool{}
	for _, tc := range loadCarrierOracle(t) {
		tc := tc
		if seen[tc.Name] {
			t.Fatalf("duplicate case name %q", tc.Name)
		}
		seen[tc.Name] = true
		t.Run(tc.Name, func(t *testing.T) {
			assertCarrierVerdict(t, tc.Name, []byte(tc.Doc), tc.Exporter, tc.Key, tc.Value)
		})
	}
}

// Bytes that are not UTF-8 cannot live in the JSON table, so this row is
// written out in both languages under the same name.
func TestDefaultsCarrierOracle_InvalidUTF8Bytes(t *testing.T) {
	doc := []byte("defaults:\n  k: 80\n  neighbour: \xe9\n")
	assertCarrierVerdict(t, "invalid-utf8-bytes", doc, "dropped", nil, nil)
}
