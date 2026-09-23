package main

// config_defaults_carrier_test.go — the FLAT plane's half of B8 (#1674 +
// #1676): what reaches the one global `Defaults` map served on /metrics must
// be exactly what the inheritance chain (/effective, describe_tenant) reads
// at the root — one carrier, chosen by the shared selection, and nothing
// from any other `_` file.
//
// Measured on main 98d2184e before this change (the fixtures below are
// those measurements):
//
//	A  _defaults-multidb.yaml / _profiles.yaml `defaults:` blocks were merged
//	   into GetConfig().Defaults (…999, …777) while the chain was
//	   [_defaults.yaml] — /metrics served keys /effective never showed.
//	B  _defaults.yaml + _defaults.yml at the root: flat Defaults
//	   {cpu_pct:90 disk_pct:4 mem_pct:70} (the .yml overwrote), exporter
//	   chain [_defaults.yaml] merged {cpu_pct:50 disk_pct:4}.
//
// Seams: logger via SetLogger (per-test buffer); no globals.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func chainBases(chain []string) []string {
	out := make([]string, 0, len(chain))
	for _, p := range chain {
		out = append(out, filepath.Base(p))
	}
	return out
}

// A (#1676): a root `_` file that is not the defaults carrier cannot
// contribute defaults / state_filters / optional_overrides, and says so.
func TestNonCarrierRootUnderscoreFileCannotSmuggleDefaults(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  cpu_pct: 50\n")
	writeFile(t, filepath.Join(dir, "_defaults-multidb.yaml"), `defaults:
  multidb_replica_lag_pct: 999
state_filters:
  smuggled_filter:
    reasons: ["CrashLoopBackOff"]
    severity: "warning"
optional_overrides:
  - smuggled_optional
`)
	writeFile(t, filepath.Join(dir, "_profiles.yaml"), `defaults:
  profiles_smuggled_pct: 777
profiles:
  gold:
    cpu_pct: "70"
`)
	writeFile(t, filepath.Join(dir, "tenant-a.yaml"), "tenants:\n  t-a:\n    mem_pct: \"60\"\n")

	var logBuf bytes.Buffer
	m := NewConfigManager(dir)
	defer m.Close()
	m.SetLogger(log.New(&logBuf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	cfg := m.GetConfig()

	// Control: the carrier's own key IS served — so an empty map below is not
	// the explanation for the smuggled keys' absence.
	if want := map[string]float64{"cpu_pct": 50}; !reflect.DeepEqual(cfg.Defaults, want) {
		t.Errorf("GetConfig().Defaults = %v, want %v — only the defaults carrier may feed the global map", cfg.Defaults, want)
	}
	if _, ok := cfg.StateFilters["smuggled_filter"]; ok {
		t.Errorf("state_filters from a non-carrier `_` file reached the config: %v", cfg.StateFilters)
	}
	for _, k := range cfg.OptionalOverrides {
		if k == "smuggled_optional" {
			t.Errorf("optional_overrides from a non-carrier `_` file reached the config: %v", cfg.OptionalOverrides)
		}
	}
	// Profiles keep their allowance from any platform file (unchanged).
	if _, ok := cfg.Profiles["gold"]; !ok {
		t.Errorf("profiles from _profiles.yaml were dropped; #1676 must not touch them: %v", cfg.Profiles)
	}

	logs := logBuf.String()
	for _, want := range []string{
		"WARN: defaults found in _defaults-multidb.yaml — not a defaults carrier",
		"WARN: state_filters found in _defaults-multidb.yaml — not a defaults carrier",
		"WARN: optional_overrides found in _defaults-multidb.yaml — platform-scoped, not a defaults carrier",
		"WARN: defaults found in _profiles.yaml — not a defaults carrier",
	} {
		if !strings.Contains(logs, want) {
			t.Errorf("missing WARN %q — an operator must learn the block is ignored.\nlogs:\n%s", want, logs)
		}
	}

	ec, ok := m.Resolve("t-a")
	if !ok {
		t.Fatalf("Resolve(t-a): unknown tenant")
	}
	if got := chainBases(ec.DefaultsChain); !reflect.DeepEqual(got, []string{"_defaults.yaml"}) {
		t.Errorf("exporter chain = %v, want [_defaults.yaml]", got)
	}
}

// B (#1674): a root `.yaml` + `.yml` pair is ONE file on every plane.
func TestRootCarrierPairIsOneFileOnEveryPlane(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  cpu_pct: 50\n  disk_pct: 4\n")
	writeFile(t, filepath.Join(dir, "_defaults.yml"), "defaults:\n  cpu_pct: 90\n  mem_pct: 70\n")
	writeFile(t, filepath.Join(dir, "tenant-b.yaml"), "tenants:\n  t-b: {}\n")

	var logBuf bytes.Buffer
	m := NewConfigManager(dir)
	defer m.Close()
	m.SetLogger(log.New(&logBuf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	if want := map[string]float64{"cpu_pct": 50, "disk_pct": 4}; !reflect.DeepEqual(m.GetConfig().Defaults, want) {
		t.Errorf("flat Defaults = %v, want %v (the chain's carrier only)", m.GetConfig().Defaults, want)
	}
	if n := strings.Count(logBuf.String(), "defaults carriers"); n != 1 {
		t.Errorf("want exactly one multi-carrier WARN on Load, got %d:\n%s", n, logBuf.String())
	}
	if !strings.Contains(logBuf.String(), "only _defaults.yaml is read, _defaults.yml is ignored") {
		t.Errorf("multi-carrier WARN does not name the chosen and the ignored file:\n%s", logBuf.String())
	}

	// Exporter hierarchy plane and pkg/config's /effective plane: one chain,
	// one merged hash.
	ec, ok := m.Resolve("t-b")
	if !ok {
		t.Fatalf("Resolve(t-b): unknown tenant")
	}
	if got := chainBases(ec.DefaultsChain); !reflect.DeepEqual(got, []string{"_defaults.yaml"}) {
		t.Errorf("exporter chain = %v, want [_defaults.yaml]", got)
	}
	pe, err := config.ResolveEffective(dir, "t-b")
	if err != nil {
		t.Fatalf("ResolveEffective: %v", err)
	}
	if !reflect.DeepEqual(pe.DefaultsChain, []string{"_defaults.yaml"}) || pe.MergedHash != ec.MergedHash {
		t.Errorf("/effective chain=%v hash=%s, exporter chain=%v hash=%s — the planes disagree",
			pe.DefaultsChain, pe.MergedHash, chainBases(ec.DefaultsChain), ec.MergedHash)
	}

	// ⛔ Not on the quiet tick: a misconfigured tree must not WARN every
	// WatchInterval.
	logBuf.Reset()
	if changed, _, err := m.detectChange(); err != nil || changed {
		t.Fatalf("quiet detectChange: changed=%v err=%v", changed, err)
	}
	if strings.Contains(logBuf.String(), "defaults carriers") {
		t.Errorf("multi-carrier WARN fired on a quiet watch tick:\n%s", logBuf.String())
	}

	// The selection moves on reload: with the `.yaml` gone the `.yml` is the
	// carrier, and its cached partial must not have been stripped.
	if err := os.Remove(filepath.Join(dir, "_defaults.yaml")); err != nil {
		t.Fatal(err)
	}
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	if want := map[string]float64{"cpu_pct": 90, "mem_pct": 70}; !reflect.DeepEqual(m.GetConfig().Defaults, want) {
		t.Errorf("after removing _defaults.yaml, flat Defaults = %v, want %v", m.GetConfig().Defaults, want)
	}
}

// The incremental flat path re-merges from CACHED partials; the selection
// must be applied at merge time, not baked into the cache.
func TestRootCarrierSelectionMovesOnIncrementalLoad(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  cpu_pct: 50\n")
	writeFile(t, filepath.Join(dir, "_defaults.yml"), "defaults:\n  cpu_pct: 90\n")
	writeFile(t, filepath.Join(dir, "tenant-c.yaml"), "tenants:\n  t-c: {}\n")

	m := NewConfigManager(dir)
	defer m.Close()
	m.SetLogger(log.New(&bytes.Buffer{}, "", 0))
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad (cold): %v", err)
	}
	if got := m.GetConfig().Defaults["cpu_pct"]; got != 50 {
		t.Fatalf("cold: cpu_pct = %v, want 50", got)
	}
	if err := os.Remove(filepath.Join(dir, "_defaults.yaml")); err != nil {
		t.Fatal(err)
	}
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad (after removal): %v", err)
	}
	if got := m.GetConfig().Defaults["cpu_pct"]; got != 90 {
		t.Errorf("after removing _defaults.yaml: cpu_pct = %v, want 90 from _defaults.yml", got)
	}
}

// A case-variant carrier alone is THE carrier on both planes: before #1674
// the flat plane served it (any `_` file fed Defaults) and the chain left it
// out (exact lower-case names only).
func TestUpperCaseRootCarrierFeedsFlatAndChain(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_DEFAULTS.YAML"), "defaults:\n  cpu_pct: 50\n")
	writeFile(t, filepath.Join(dir, "tenant-d.yaml"), "tenants:\n  t-d: {}\n")

	m := NewConfigManager(dir)
	defer m.Close()
	m.SetLogger(log.New(&bytes.Buffer{}, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := m.GetConfig().Defaults["cpu_pct"]; got != 50 {
		t.Errorf("flat cpu_pct = %v, want 50", got)
	}
	ec, ok := m.Resolve("t-d")
	if !ok {
		t.Fatalf("Resolve(t-d): unknown tenant")
	}
	if got := chainBases(ec.DefaultsChain); !reflect.DeepEqual(got, []string{"_DEFAULTS.YAML"}) {
		t.Errorf("exporter chain = %v, want [_DEFAULTS.YAML]", got)
	}
}
