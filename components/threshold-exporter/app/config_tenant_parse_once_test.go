package main

// #2153: one merge pass — a cold load, or one reload tick — reads and parses
// each tenant file once (tenantFilesOnce) and merges every tenant it
// declares from that one parse, where it used to parse the whole file once
// per tenant. These pin that the sharing is invisible (each tenant's
// merged_hash, error, log lines and parse-failure counts are those of a
// parse of its own — recomputeMergedHash with a fresh tenantFilesOnce per
// call is that oracle) and that the parse really happens once per file per
// pass.

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	promtest "github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// parseOnceTrees are trees whose tenant files each declare several tenants,
// over every input the merge reads: a defaults chain, root platform blocks
// (#2019), profiles (#2117), a broken subtree defaults file (its tenants'
// merges fail) and a broken tenant file (the walker drops it).
func parseOnceTrees() map[string]map[string]string {
	return map[string]map[string]string{
		"one-file-many-tenants": {
			"_defaults.yaml": "defaults:\n  cpu: 80\n  _routing:\n    receiver: root\n",
			"team/all.yaml": "tenants:\n" +
				"  t-1:\n    cpu: \"10\"\n    _routing:\n      receiver: own\n" +
				"  t-2:\n" +
				"  t-3:\n    mem:\n      default: \"90\"\n      overrides:\n        - window: \"22:00-06:00\"\n          value: \"95\"\n" +
				"  t-4:\n    _routing: null\n    _metadata:\n      owner: x\n" +
				"  t-5:\n    cpu: \"50\"\n",
		},
		"chain-many-files": {
			"_defaults.yaml":        "defaults:\n  cpu: 80\n  mem: 85\n",
			"a/_defaults.yaml":      "defaults:\n  cpu: 70\n  _metadata:\n    owner: a\n",
			"a/b/_defaults.yaml":    "defaults:\n  disk: 60\n",
			"a/b/pair.yaml":         "tenants:\n  t-1:\n    cpu: \"1\"\n  t-2:\n    disk: \"2\"\n",
			"a/trio.yaml":           "tenants:\n  t-3:\n  t-4:\n    mem: \"4\"\n  t-5:\n    cpu: \"5\"\n",
			"root-tenants.yaml":     "tenants:\n  t-6:\n    cpu: \"6\"\n  t-7:\n",
			"a/b/c/_defaults.yaml":  "defaults:\n  cpu: 11\n",
			"a/b/c/one-tenant.yaml": "tenants:\n  t-8:\n    mem: \"8\"\n",
		},
		"platform-and-profiles": {
			"_defaults.yaml": "defaults:\n  cpu: 80\n  mem: 85\n" +
				"tenants:\n  t-1:\n    cpu: \"61\"\n    _silent_mode: warning\n  t-3:\n    mem: \"63\"\n",
			"_profiles.yaml": "profiles:\n  gold:\n    cpu: 40\n    disk: 41\n  bronze:\n    mem: 99\n",
			"all.yaml": "tenants:\n" +
				"  t-1:\n    _profile: gold\n    mem: \"1\"\n" +
				"  t-2:\n    _profile: bronze\n" +
				"  t-3:\n    _profile: gold\n    disk: \"3\"\n" +
				"  t-4:\n",
		},
		"broken-inputs": {
			"_defaults.yaml":     "defaults:\n  cpu: 80\n",
			"bad/_defaults.yaml": "defaults:\n  cpu: {unclosed-brace\n",
			"bad/pair.yaml":      "tenants:\n  t-1:\n    cpu: \"1\"\n  t-2:\n",
			"good/trio.yaml":     "tenants:\n  t-3:\n    cpu: \"3\"\n  t-4:\n  t-5:\n    cpu: \"5\"\n",
			"good/broken.yaml":   "tenants:\n  t-6: [unclosed\n  t-7:\n",
		},
	}
}

// assertHashesMatchPerTenantParse compares mgr's installed merged hashes with
// recomputeMergedHash per tenant — a fresh read and parse per call — under
// the platform layers mgr installed. Returns how many tenants hash.
func assertHashesMatchPerTenantParse(t *testing.T, mgr *ConfigManager) int {
	t.Helper()
	mgr.mu.RLock()
	sources := mgr.hierarchy.tenantSources
	got := mgr.hierarchy.mergedHashes
	graph := mgr.hierarchy.graph
	platform := mgr.hierarchy.platform
	profiles := mgr.hierarchy.profiles
	mgr.mu.RUnlock()

	oracle := NewConfigManager(mgr.path)
	oracleMetrics, _ := freshMetrics(t)
	oracleLog, _ := newTestLogger()
	oracle.SetMetrics(oracleMetrics)
	oracle.SetLogger(oracleLog)
	for tid, src := range sources {
		layers := config.TenantLayers{Overlay: config.PlatformOverlayFor(platform, tid), Profiles: profiles}
		want, werr := oracle.recomputeMergedHash(tid, src, graph.TenantDefaults[tid], layers)
		// A reload tick records a tenant whose merge fails with no prior
		// hash as "" (classifyAndCount), a cold load leaves it out: both
		// mean "no merged_hash".
		have, ok := got[tid]
		ok = ok && have != ""
		switch {
		case werr != nil && ok:
			t.Errorf("tenant %s: installed %s, own parse fails: %v", tid, have, werr)
		case werr == nil && !ok:
			t.Errorf("tenant %s: nothing installed, own parse gives %s", tid, want)
		case werr == nil && have != want:
			t.Errorf("tenant %s: installed %s != own parse %s", tid, have, want)
		}
	}
	n := 0
	for _, h := range got {
		if h != "" {
			n++
		}
	}
	return n
}

// TestTenantFileParsedOncePerPassMatchesPerTenantParse: the cold load and a
// reload tick that re-merges every tenant give each tenant the merged_hash
// (or the skip) a parse of its own gives.
func TestTenantFileParsedOncePerPassMatchesPerTenantParse(t *testing.T) {
	t.Parallel()
	minHashed := map[string]int{
		"one-file-many-tenants": 5,
		"chain-many-files":      8,
		"platform-and-profiles": 4,
		"broken-inputs":         3, // t-3..t-5; t-1/t-2 fail on bad/_defaults.yaml, broken.yaml is dropped
	}
	for name, files := range parseOnceTrees() {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			root := writeColdMergeFixture(t, files)
			mgr := coldLoaded(t, root)
			if n := assertHashesMatchPerTenantParse(t, mgr); n < minHashed[name] {
				t.Fatalf("cold: %d tenants hashed, want >= %d", n, minHashed[name])
			}

			// Change every tenant file (a key appended to its last tenant):
			// every tenant is re-merged on the tick, from shared parses.
			mgr.mu.RLock()
			sources := mgr.hierarchy.tenantSources
			mgr.mu.RUnlock()
			for _, src := range uniqueFiles(sources) {
				b, err := os.ReadFile(src)
				if err != nil {
					t.Fatal(err)
				}
				writeAgedFile(t, src, string(b)+"    zz_changed: \"1\"\n")
			}
			reloaded, _, err := mgr.diffAndReload()
			if err != nil {
				t.Fatalf("diffAndReload: %v", err)
			}
			if n := assertHashesMatchPerTenantParse(t, mgr); n < minHashed[name] {
				t.Fatalf("reload: %d tenants hashed, want >= %d", n, minHashed[name])
			}
			if reloaded < minHashed[name] {
				t.Errorf("reload re-merged %d tenants, want >= %d — the shared parse is not exercised", reloaded, minHashed[name])
			}
		})
	}
}

func uniqueFiles(sources map[string]string) []string {
	seen := map[string]struct{}{}
	out := []string{}
	for _, p := range sources {
		if _, ok := seen[p]; !ok {
			seen[p] = struct{}{}
			out = append(out, p)
		}
	}
	sort.Strings(out)
	return out
}

// TestTenantFileParsedOncePerPass is the optimisation itself, which the
// equality test cannot see: a cold load and a reload tick each parse every
// tenant file exactly once, however many tenants it declares.
func TestTenantFileParsedOncePerPass(t *testing.T) {
	t.Parallel()
	var b strings.Builder
	b.WriteString("tenants:\n")
	const many = 40
	for i := 0; i < many; i++ {
		fmt.Fprintf(&b, "  t-%02d:\n    cpu: \"%d\"\n", i, i)
	}
	root := writeColdMergeFixture(t, map[string]string{
		"_defaults.yaml":   "defaults:\n  cpu: 80\n",
		"team/many.yaml":   b.String(),
		"other/pair.yaml":  "tenants:\n  t-x:\n  t-y:\n    cpu: \"1\"\n",
		"other/alone.yaml": "tenants:\n  t-z:\n",
	})
	logger, _ := newTestLogger()
	scan, err := scanDirTree(root, nil, nil, logger)
	if err != nil {
		t.Fatal(err)
	}
	files := uniqueFiles(scan.Tenants)
	if len(files) != 3 || len(scan.Tenants) != many+3 {
		t.Fatalf("fixture: %d tenants over %d files", len(scan.Tenants), len(files))
	}
	assertOncePerFile := func(pass string, parses map[string]int) {
		t.Helper()
		if len(parses) != len(files) {
			t.Errorf("%s: parsed %d tenant files, want each of the %d", pass, len(parses), len(files))
		}
		for p, n := range parses {
			if n != 1 {
				t.Errorf("%s: %s parsed %d times, want once", pass, p, n)
			}
		}
	}

	coldParses := map[string]int{}
	in := newColdMergeInputs(scan)
	in.tenants.onParse = func(p string) { coldParses[p]++ }
	mgr := NewConfigManager(root)
	mgr.SetLogger(logger)
	mgr.populateHierarchyStateWith(scan, in)
	assertOncePerFile("cold", coldParses)
	mgr.mu.RLock()
	hashed := len(mgr.hierarchy.mergedHashes)
	mgr.mu.RUnlock()
	if hashed != len(scan.Tenants) {
		t.Fatalf("cold: %d of %d tenants hashed", hashed, len(scan.Tenants))
	}

	// Reload from a manager whose state the full cold path installed, then
	// change one value in each file: every tenant of every file re-merges.
	mgr = coldLoaded(t, root)
	reloadParses := map[string]int{}
	mgr.onReloadTenantParse = func(p string) { reloadParses[p]++ }
	for _, src := range files {
		body, err := os.ReadFile(src)
		if err != nil {
			t.Fatal(err)
		}
		writeAgedFile(t, src, string(body)+"    zz_changed: \"1\"\n")
	}
	reloaded, _, err := mgr.diffAndReload()
	if err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	if reloaded != len(scan.Tenants) {
		t.Fatalf("reload re-merged %d of %d tenants", reloaded, len(scan.Tenants))
	}
	assertOncePerFile("reload", reloadParses)
}

// TestSharedTenantFileKeepsPerTenantErrors: the tenants of one file merged
// from one read + parse — the cold load's coldMergedHash over one
// coldMergeInputs, a reload tick's recomputeMergedHashWith over one
// tenantFilesOnce — each get the hash or error, the logMergeSkip line and
// the parse-failure counts a read + parse of their own gives, for a file
// that does not parse, one whose tenants partly do not extract, and one
// that cannot be read.
func TestSharedTenantFileKeepsPerTenantErrors(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	good := filepath.Join(root, "_defaults.yaml")
	bad := filepath.Join(root, "bad", "_defaults.yaml")
	writeFile(t, good, "defaults:\n  cpu: 80\n")
	writeFile(t, bad, "defaults:\n  cpu: {unclosed\n")
	broken := filepath.Join(root, "broken.yaml")
	mixed := filepath.Join(root, "mixed.yaml")
	writeFile(t, broken, "tenants:\n  t-1: [unclosed\n  t-2:\n")
	writeFile(t, mixed, "tenants:\n  t-1:\n    cpu: \"1\"\n  t-2: 5\n  t-3:\n")

	cases := []struct {
		name   string
		tenant string
		chain  []string
		tids   []string
	}{
		{"broken-file", broken, []string{good}, []string{"t-1", "t-2", "t-3"}},
		{"broken-file-under-broken-chain", broken, []string{good, bad}, []string{"t-1", "t-2"}},
		{"some-tenants-do-not-extract", mixed, []string{good}, []string{"t-1", "t-2", "t-3", "t-absent"}},
		{"unreadable-file", filepath.Join(root, "absent.yaml"), []string{good}, []string{"t-1", "t-2"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			type pass struct {
				name  string
				merge func(m *ConfigManager, tid string) (string, error)
			}
			in := newColdMergeInputs(&treeScan{})
			shared := &tenantFilesOnce{}
			passes := []pass{
				{"own-parse", func(m *ConfigManager, tid string) (string, error) {
					return m.recomputeMergedHash(tid, tc.tenant, tc.chain, config.TenantLayers{})
				}},
				{"cold-shared", func(m *ConfigManager, tid string) (string, error) {
					return m.coldMergedHash(tid, tc.tenant, tc.chain, in, config.TenantLayers{})
				}},
				{"reload-shared", func(m *ConfigManager, tid string) (string, error) {
					return m.recomputeMergedHashWith(tid, tc.tenant, tc.chain, config.TenantLayers{}, shared)
				}},
			}
			type outcome struct {
				results []string
				log     string
				metrics map[string]float64
			}
			outcomes := make([]outcome, len(passes))
			for i, p := range passes {
				metrics, _ := freshMetrics(t)
				logger, buf := newTestLogger()
				m := NewConfigManager(root)
				m.SetMetrics(metrics)
				m.SetLogger(logger)
				for _, tid := range tc.tids {
					h, err := p.merge(m, tid)
					if err != nil {
						logMergeSkip(m.getLogger(), tid, "pass", err)
					}
					outcomes[i].results = append(outcomes[i].results, fmt.Sprintf("%s=%q/%v", tid, h, err))
				}
				outcomes[i].log = buf.String()
				outcomes[i].metrics = map[string]float64{}
				for _, base := range []string{"_defaults.yaml", "broken.yaml", "mixed.yaml", "absent.yaml"} {
					outcomes[i].metrics[base] = promtest.ToFloat64(metrics.parseFailures.WithLabelValues(base))
				}
			}
			want := outcomes[0]
			if strings.Count(want.log, "skipping merged_hash") == 0 && !strings.Contains(tc.name, "extract") {
				t.Fatalf("own-parse logged no skip — the case does not fail:\n%s", want.log)
			}
			for i, got := range outcomes[1:] {
				name := passes[i+1].name
				if strings.Join(got.results, "\n") != strings.Join(want.results, "\n") {
					t.Errorf("%s results:\n%s\nown parse:\n%s", name, strings.Join(got.results, "\n"), strings.Join(want.results, "\n"))
				}
				if got.log != want.log {
					t.Errorf("%s log:\n%s\nown parse:\n%s", name, got.log, want.log)
				}
				for k, v := range want.metrics {
					if got.metrics[k] != v {
						t.Errorf("%s parse_failure{%s} = %v, own parse %v", name, k, got.metrics[k], v)
					}
				}
			}
		})
	}
}
