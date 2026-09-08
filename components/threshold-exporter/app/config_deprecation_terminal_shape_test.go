package main

// The shape `deprecate_rule --execute` leaves a root `_defaults.yaml` in, read
// back by the loader that actually consumes it (#1787).
//
// `scripts/tools/ops/deprecate_rule.py` used to deprecate a metric by writing
// `<metric>: "disable"` under `defaults:`. `ThresholdConfig.Defaults` is
// `map[string]float64` (pkg/config/types.go:208), so that string does not
// disable one metric — `yaml.Unmarshal` fails on the document and
// `parsePartialConfig` returns ok=false, which makes the caller drop the ENTIRE
// carrier: every surviving threshold in it, plus its `state_filters:`. The tool
// now removes the metric's keys instead, and this file is the Go-side half of
// that contract.
//
// ⛔ The fixtures are hand-written YAML on purpose — nothing here shells out to
// Python. These tests pin the SHAPE the tool must produce, so they stay
// meaningful in a Go-only CI run and cannot go green because a Python
// dependency was unavailable. The Python side pins that the tool produces this
// shape (`tests/ops/test_deprecate_rule_carriers.py`,
// `test_the_written_root_defaults_still_decodes_as_map_string_float64`); the two
// halves meet here, on the bytes.
//
// ⚠️ The `disable-sentinel` arm of each test is a CONTROL and is expected to
// FAIL to load. If it ever starts loading, the property asserted here has
// stopped being a property (the loader would have been widened to accept string
// sentinels under `defaults:`) — at which point ADR-017's defaults/tenants type
// boundary, not this test, is the thing to revisit.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The carrier, in the two terminal shapes, for one deprecated metric
// (`cpu_usage`, base + `_critical`).
const (
	deprecatedDeleteKeyCarrier = `# _defaults.yaml — Platform global defaults
defaults:
  mem_usage: 90
  mem_usage_critical: 97
  disk_usage: 85
state_filters:
  container_crashloop:
    reasons: ["CrashLoopBackOff"]
    severity: "critical"
`

	deprecatedDisableCarrier = `# _defaults.yaml — Platform global defaults
defaults:
  cpu_usage: disable
  cpu_usage_critical: disable
  mem_usage: 90
  mem_usage_critical: 97
  disk_usage: 85
state_filters:
  container_crashloop:
    reasons: ["CrashLoopBackOff"]
    severity: "critical"
`
)

// What the platform still owes every tenant AFTER cpu_usage is deprecated.
var deprecationSurvivors = map[string]float64{
	"mem_usage":          90,
	"mem_usage_critical": 97,
	"disk_usage":         85,
}

func TestDeprecatedMetricTerminalShape_CarrierStillParses(t *testing.T) {
	arms := []struct {
		name    string
		doc     string
		wantOK  bool
		wantLog string // substring the loader must log when it rejects the file
	}{
		{
			name:   "delete-key (post-#1787 deprecate_rule output)",
			doc:    deprecatedDeleteKeyCarrier,
			wantOK: true,
		},
		{
			name:    "disable-sentinel (pre-#1787 output — control)",
			doc:     deprecatedDisableCarrier,
			wantOK:  false,
			wantLog: "entire block dropped",
		},
	}

	for _, a := range arms {
		a := a
		t.Run(a.name, func(t *testing.T) {
			var logBuf bytes.Buffer
			cfg, ok := parsePartialConfig(
				"_defaults.yaml", "/conf.d/_defaults.yaml", []byte(a.doc),
				newConfigMetrics(), log.New(&logBuf, "", 0))

			if ok != a.wantOK {
				t.Fatalf("parsePartialConfig ok = %v, want %v (log: %s)",
					ok, a.wantOK, logBuf.String())
			}

			if !a.wantOK {
				// ⚠️ Do NOT assert on the returned struct here: `yaml.Unmarshal`
				// leaves it PARTIALLY populated (it fills what it decoded before
				// the type error), which reads like "only cpu_usage was lost"
				// and is the opposite of what happens. The `ok=false` IS the
				// loss — every caller discards the value on it, and
				// `..._WholeTreeReload` below measures that at the level where
				// it is observable. What must be pinned here is that the
				// rejection is not silent.
				if !strings.Contains(logBuf.String(), a.wantLog) {
					t.Errorf("expected the drop to be logged with %q, got: %s",
						a.wantLog, logBuf.String())
				}
				return
			}

			// The deprecated metric is gone …
			for _, k := range []string{"cpu_usage", "cpu_usage_critical",
				"custom_cpu_usage", "custom_cpu_usage_critical"} {
				if _, present := cfg.Defaults[k]; present {
					t.Errorf("deprecated key %q survived in defaults: %v",
						k, cfg.Defaults)
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
// a whole conf.d. This is the codified miniature of the #1787 counterfactual —
// deleting the key costs exactly the deprecated metric's rows, while the
// `disable` sentinel costs the entire tree that carrier fed (and takes
// `state_filters` with it, which no ticket about a threshold would predict).
func TestDeprecatedMetricTerminalShape_WholeTreeReload(t *testing.T) {
	arms := []struct {
		name             string
		carrier          string
		wantMetrics      []string // metric names that must resolve for db-a
		wantStateFilters int
	}{
		{
			name:    "delete-key (post-#1787 deprecate_rule output)",
			carrier: deprecatedDeleteKeyCarrier,
			// cpu_usage is gone; everything else the platform owes survives.
			wantMetrics:      []string{"mem_usage", "mem_usage_critical", "disk_usage"},
			wantStateFilters: 1,
		},
		{
			name:    "disable-sentinel (pre-#1787 output — control)",
			carrier: deprecatedDisableCarrier,
			// The carrier is rejected whole: no defaults, so no base rows at
			// all — and the state filter goes with it.
			wantMetrics:      nil,
			wantStateFilters: 0,
		},
	}

	for _, a := range arms {
		a := a
		t.Run(a.name, func(t *testing.T) {
			dir := t.TempDir()
			write := func(name, body string) {
				t.Helper()
				if err := os.WriteFile(filepath.Join(dir, name),
					[]byte(body), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			write("_defaults.yaml", a.carrier)
			write("db-a.yaml", "tenants:\n  db-a: {}\n")

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
