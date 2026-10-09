package main

// config_broken_tenant_file_reload_test.go — #1980 (owner ruling D4 P1) and
// #2022: a tenant file that breaks (a syntax error, a tenant body of the
// wrong type), becomes unreadable, or breaks and is then deleted drops every
// tenant it declared, on EVERY reload path — the flat tree's incremental
// reload (incrementalLoadFrom → patchTenants), the hierarchical reload, and a
// fresh Load (a restart). The fresh Load is the oracle.
//
// ⛔ WHY THE FLAT TREE NEEDED THIS. Until #1980 the flat tree's tenant-only
// incremental reload kept a broken file's tenants on their last good values
// (a "fail-safe"), while the same edit on a tree with a root `_defaults.yaml`
// — or a restart — dropped them. Measured on main with the exporter running
// (-reload-interval 1s): a.yaml rewritten to `tenants:\n  acme: [1` or
// `tenants:\n  acme: 5` left acme's user_silent_mode series up on the flat
// tree, and a.yaml broken then deleted 3 s later STILL served acme (#2022:
// the broken file's partial had already left the cache, so the removal pass
// never saw the deletion). The owner ruled the fail-safe out.
//
// ⛔ THE MUST-FIRE CONTROL. Every case also flips another tenant's
// `_silent_mode` (zeta, warning → critical) in the reload under test, and
// asserts that change landed — so "acme is gone" cannot be satisfied by a
// reload that silently committed nothing.
//
// Injection discipline (test-map.md): metrics via SetMetrics + freshMetrics,
// logger via SetLogger (newAuditedManager). No global swaps.

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

const (
	brokenFileAcme      = "tenants:\n  acme:\n    _silent_mode: warning\n"
	brokenFileZetaWarn  = "tenants:\n  zeta:\n    _silent_mode: warning\n"
	brokenFileZetaCrit  = "tenants:\n  zeta:\n    _silent_mode: critical\n"
	brokenFileHierarchy = "defaults: {}\n"
)

// brokenFileStep is one reload's worth of edits to a.yaml. The control
// (zeta → critical) is applied with the LAST step only.
type brokenFileStep func(t *testing.T, dir string)

func writeA(body string) brokenFileStep {
	return func(t *testing.T, dir string) {
		t.Helper()
		writeTestYAML(t, filepath.Join(dir, "a.yaml"), body)
	}
}

func replaceAWithDir(t *testing.T, dir string) {
	t.Helper()
	p := filepath.Join(dir, "a.yaml")
	if err := os.Remove(p); err != nil {
		t.Fatalf("remove a.yaml: %v", err)
	}
	if err := os.Mkdir(p, 0o755); err != nil {
		t.Fatalf("mkdir a.yaml: %v", err)
	}
}

func removeA(t *testing.T, dir string) {
	t.Helper()
	if err := os.Remove(filepath.Join(dir, "a.yaml")); err != nil {
		t.Fatalf("remove a.yaml: %v", err)
	}
}

// brokenFileCases are the four shapes measured on main. wantParseFailed
// says whether the FINAL state still has a.yaml on disk as a file that does
// not parse — the cases where the loud signals (WARN + counter + identity
// parse_failed) must be present after the last reload.
var brokenFileCases = []struct {
	name            string
	steps           []brokenFileStep
	wantParseFailed bool
}{
	{"syntax error", []brokenFileStep{writeA("tenants:\n  acme: [1\n")}, true},
	{"tenant body is a scalar", []brokenFileStep{writeA("tenants:\n  acme: 5\n")}, true},
	{"replaced by a directory (unreadable as a file)", []brokenFileStep{replaceAWithDir}, false},
	// #2022: broken on one reload, deleted on the next.
	{"broken then deleted (#2022)", []brokenFileStep{writeA("tenants:\n  acme: [1\n"), removeA}, false},
}

func buildBrokenFileTree(t *testing.T, dir string, hierarchical bool) {
	t.Helper()
	if hierarchical {
		writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), brokenFileHierarchy)
	}
	writeTestYAML(t, filepath.Join(dir, "a.yaml"), brokenFileAcme)
	writeTestYAML(t, filepath.Join(dir, "b.yaml"), brokenFileZetaWarn)
}

// silentModeSeries renders the user_silent_mode series the collector emits
// for this manager as sorted "tenant=severity" rows — what /metrics shows.
func silentModeSeries(t *testing.T, m *ConfigManager) []string {
	t.Helper()
	reg := prometheus.NewPedanticRegistry()
	reg.MustRegister(NewThresholdCollector(m))
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	var rows []string
	for _, mf := range mfs {
		if mf.GetName() != "user_silent_mode" {
			continue
		}
		for _, mt := range mf.GetMetric() {
			var tenant, sev string
			for _, lp := range mt.GetLabel() {
				switch lp.GetName() {
				case "tenant":
					tenant = lp.GetValue()
				case "target_severity":
					sev = lp.GetValue()
				}
			}
			rows = append(rows, tenant+"="+sev)
		}
	}
	sort.Strings(rows)
	return rows
}

// TestABrokenTenantFileDropsItsTenantsOnEveryReloadPath is the #1980 table:
// four cases × (flat tree, hierarchical tree), each comparing the watch
// path's reload against a fresh Load of the same final tree.
func TestABrokenTenantFileDropsItsTenantsOnEveryReloadPath(t *testing.T) {
	t.Parallel()
	for _, tree := range []struct {
		name         string
		hierarchical bool
	}{
		{"flat (incrementalLoadFrom)", false},
		{"hierarchical (root _defaults.yaml)", true},
	} {
		for _, tc := range brokenFileCases {
			tree, tc := tree, tc
			t.Run(tree.name+"/"+tc.name, func(t *testing.T) {
				t.Parallel()

				// Path A: load, then reload once per step as the watch path does.
				warm := t.TempDir()
				buildBrokenFileTree(t, warm, tree.hierarchical)
				mIncr, freshIncr, logIncr := newAuditedManager(t, warm)
				if err := mIncr.Load(); err != nil {
					t.Fatalf("warm Load: %v", err)
				}
				if tree.hierarchical {
					mIncr.mu.RLock()
					hier := mIncr.hierarchy.enabled
					mIncr.mu.RUnlock()
					if !hier {
						t.Fatal("fixture precondition: a root _defaults.yaml did not turn hierarchical mode on")
					}
				} else {
					requireFlatWatchPath(t, mIncr)
				}
				if got := silentModeSeries(t, mIncr); strings.Join(got, ",") != "acme=warning,zeta=warning" {
					t.Fatalf("fixture precondition: user_silent_mode before the edit = %v", got)
				}
				for i, step := range tc.steps {
					step(t, warm)
					if i == len(tc.steps)-1 {
						writeTestYAML(t, filepath.Join(warm, "b.yaml"), brokenFileZetaCrit)
					}
					logIncr.Reset()
					if err := watchReload(mIncr); err != nil {
						t.Fatalf("reload %d: %v", i+1, err)
					}
				}

				// Path B, the oracle: the same final tree, loaded from scratch.
				cold := t.TempDir()
				buildBrokenFileTree(t, cold, tree.hierarchical)
				for _, step := range tc.steps {
					step(t, cold)
				}
				writeTestYAML(t, filepath.Join(cold, "b.yaml"), brokenFileZetaCrit)
				mFull, _, _ := newAuditedManager(t, cold)
				if err := mFull.Load(); err != nil {
					t.Fatalf("cold Load: %v", err)
				}

				// The must-fire control, then the subject.
				got := silentModeSeries(t, mIncr)
				if strings.Join(got, ",") != "zeta=critical" {
					t.Errorf("user_silent_mode after the reload = %v, want [zeta=critical] "+
						"(zeta's flip must land; acme's file no longer declares it)", got)
				}
				if want := silentModeSeries(t, mFull); strings.Join(got, ",") != strings.Join(want, ",") {
					t.Errorf("user_silent_mode: reload %v, fresh Load %v", got, want)
				}
				if a, b := publishedStateFingerprint(t, mIncr, warm), publishedStateFingerprint(t, mFull, cold); a != b {
					t.Errorf("the reload disagrees with a fresh Load\n--- reload ---\n%s\n--- fresh Load ---\n%s", a, b)
				}
				if a, b := mIncr.Identity().ParseFailed, mFull.Identity().ParseFailed; strings.Join(a, ",") != strings.Join(b, ",") {
					t.Errorf("identity parse_failed: reload %v, fresh Load %v", a, b)
				}

				// The loud signals: the reload that left a.yaml broken on disk
				// says so in the log, on the counter and in the identity.
				if !tc.wantParseFailed {
					return
				}
				if !strings.Contains(logIncr.String(), "WARN: skip unparseable file") ||
					!strings.Contains(logIncr.String(), "a.yaml") {
					t.Errorf("the reload that dropped acme logged no `WARN: skip unparseable file` for a.yaml:\n%s", logIncr.String())
				}
				if n := parseFailureCount(freshIncr, "a.yaml"); n < 1 {
					t.Errorf("da_config_parse_failure_total{file_basename=\"a.yaml\"} = %v, want >= 1", n)
				}
				if pf := mIncr.Identity().ParseFailed; strings.Join(pf, ",") != "a.yaml" {
					t.Errorf("identity parse_failed = %v, want [a.yaml]", pf)
				}
			})
		}
	}
}
