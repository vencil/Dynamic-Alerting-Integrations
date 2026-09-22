package main

// config_tree_scan_test.go — pins for the ONE tree walker (#1568).
//
// Three things are pinned here and nowhere else:
//
//  1. The mtime fast-path CARRIES tenant declarations. A scan that reuses a
//     prior hash but forgets the tenants the file declares would drop the
//     tenant from the hierarchy on every quiet tick while the flat plane
//     still served it — the #1911 divergence shape, reintroduced by an
//     optimisation. Proven without trusting uid: the file's bytes are
//     swapped for a same-size document naming a DIFFERENT tenant, the
//     mtime is restored, and the second scan must still report the OLD
//     tenant. Only a scan that did not read can answer that way.
//  2. The two wrappers are projections of one scan: same file set, same
//     hashes, keyed rel-vs-abs.
//  3. Where the two walkers used to disagree, the cell now has one answer
//     (a root that is a file is an error on BOTH planes).
//
// Seams: metrics via freshMetrics(t), logger caller-supplied, no package
// global touched — every test here is t.Parallel().

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// treeScanFixtureAge is how far into the past fixture mtimes are pushed so
// the treeScanMtimeGuard window can never be the reason a file was read.
const treeScanFixtureAge = time.Hour

// writeAgedFile writes content and back-dates the mtime past the guard.
func writeAgedFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
	old := time.Now().Add(-treeScanFixtureAge)
	if err := os.Chtimes(path, old, old); err != nil {
		t.Fatalf("chtimes %s: %v", path, err)
	}
}

// twoTenantTree builds root/_defaults.yaml, root/t-alpha.yaml and
// root/nested/{_defaults.yaml,t-beta.yaml} with aged mtimes.
func twoTenantTree(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	writeAgedFile(t, filepath.Join(root, "_defaults.yaml"), "defaults:\n  mysql_connections: 50\n")
	writeAgedFile(t, filepath.Join(root, "t-alpha.yaml"), "tenants:\n  t-alpha: {}\n")
	writeAgedFile(t, filepath.Join(root, "nested", "_defaults.yaml"), "defaults:\n  mysql_connections: 90\n")
	writeAgedFile(t, filepath.Join(root, "nested", "t-beta.yaml"), "tenants:\n  t-beta: {}\n")
	return root
}

// treeScanTenantIDs reuses sortedTenantIDs (config_source_oracle_parity_test.go)
// on the scan's tenants product.
func treeScanTenantIDs(scan *treeScan) []string { return sortedTenantIDs(scan.tenants) }

func TestScanDirTree_FastPathCarriesTenantDecls(t *testing.T) {
	t.Parallel()
	root := twoTenantTree(t)
	fresh, _ := freshMetrics(t)
	var buf bytes.Buffer
	logger := log.New(&buf, "", 0)

	first, err := scanDirTree(root, nil, fresh, logger)
	if err != nil {
		t.Fatalf("first scan: %v", err)
	}
	if got, want := treeScanTenantIDs(first), []string{"t-alpha", "t-beta"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("first scan tenants = %v, want %v", got, want)
	}
	for k, f := range first.files {
		if f.data == nil {
			t.Errorf("cold scan must cache every file's bytes; %s has none", k)
		}
		if f.reused {
			t.Errorf("cold scan cannot reuse anything; %s claims it did", k)
		}
	}

	// Swap t-beta's bytes for a SAME-SIZE document naming a different
	// tenant and restore the mtime. Stat is now identical to the prior; only
	// a read could notice the change.
	betaPath := filepath.Join(root, "nested", "t-beta.yaml")
	swapped := "tenants:\n  t-zeta: {}\n"
	if len(swapped) != int(first.files["nested/t-beta.yaml"].stat.Size) {
		t.Fatalf("fixture drift: swapped document is %d bytes, original %d — the "+
			"stat would differ and the fast-path could not be measured",
			len(swapped), first.files["nested/t-beta.yaml"].stat.Size)
	}
	if err := os.WriteFile(betaPath, []byte(swapped), 0o600); err != nil {
		t.Fatalf("swap bytes: %v", err)
	}
	old := time.Unix(0, first.files["nested/t-beta.yaml"].stat.ModTime)
	if err := os.Chtimes(betaPath, old, old); err != nil {
		t.Fatalf("restore mtime: %v", err)
	}

	second, err := scanDirTree(root, first, fresh, logger)
	if err != nil {
		t.Fatalf("second scan: %v", err)
	}
	if got, want := treeScanTenantIDs(second), []string{"t-alpha", "t-beta"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("fast-path forgot the declarations: tenants = %v, want %v "+
			"(t-zeta present means the file was READ; t-beta absent means the "+
			"carry is gone)", got, want)
	}
	if second.tenants["t-beta"] != first.tenants["t-beta"] {
		t.Errorf("t-beta source moved: %q → %q", first.tenants["t-beta"], second.tenants["t-beta"])
	}
	if second.composite != first.composite {
		t.Errorf("composite moved on an unchanged-by-stat tree: %s → %s", first.composite, second.composite)
	}
	for k, f := range second.files {
		if !f.reused {
			t.Errorf("%s was read on the warm scan (stat unchanged, aged past the guard)", k)
		}
		if f.data != nil {
			t.Errorf("%s carries bytes on the warm scan; the cache must hold only files that need re-parsing", k)
		}
		if f.hash != first.files[k].hash {
			t.Errorf("%s hash moved on the warm scan: %s → %s", k, first.files[k].hash, f.hash)
		}
	}
	if !reflect.DeepEqual(second.inheritanceGraph().TenantDefaults, first.inheritanceGraph().TenantDefaults) {
		t.Errorf("inheritance graph moved across the fast-path:\n first %v\nsecond %v",
			first.inheritanceGraph().TenantDefaults, second.inheritanceGraph().TenantDefaults)
	}
	if !reflect.DeepEqual(second.defaults, first.defaults) {
		t.Errorf("defaults set moved across the fast-path: %v → %v", first.defaults, second.defaults)
	}
	if strings.Contains(buf.String(), "WARN") {
		t.Errorf("unexpected WARN on a healthy tree:\n%s", buf.String())
	}
}

// TestScanDirTree_YoungFileIsReadDespiteMatchingStat pins the other half
// of the guard: a file inside the coarse-mtime window is re-read even when
// its stat matches, because the stat cannot vouch for the bytes yet.
//
// ⛔ The young mtime is set BEFORE the first scan on purpose: the prior
// then records exactly the stat the second scan sees, so a stat mismatch
// cannot be what forces the read — only the guard term can. Touching the
// file between the scans instead would make the test pass with the guard
// deleted (measured: the mutant stayed green).
func TestScanDirTree_YoungFileIsReadDespiteMatchingStat(t *testing.T) {
	t.Parallel()
	root := twoTenantTree(t)
	fresh, _ := freshMetrics(t)
	alphaPath := filepath.Join(root, "t-alpha.yaml")
	now := time.Now()
	if err := os.Chtimes(alphaPath, now, now); err != nil {
		t.Fatalf("chtimes: %v", err)
	}
	first, err := scanDirTree(root, nil, fresh, nil)
	if err != nil {
		t.Fatalf("first scan: %v", err)
	}
	// Same bytes, same (young) stat → inside the guard window.
	second, err := scanDirTree(root, first, fresh, nil)
	if err != nil {
		t.Fatalf("second scan: %v", err)
	}
	if second.files["t-alpha.yaml"].reused {
		t.Errorf("a file younger than the guard must be read, not reused")
	}
	if second.files["nested/t-beta.yaml"].reused != true {
		t.Errorf("an aged, unchanged sibling must still take the fast-path")
	}
	// Read, hash unchanged → not cached (the cache is "needs re-parse").
	if second.files["t-alpha.yaml"].data != nil {
		t.Errorf("a re-read file whose hash did not move must not be cached")
	}
}

// TestBothWrappersAreProjectionsOfOneScan is the in-package half of the
// D-02 parity measurement: the flat and hierarchical wrappers must describe
// the same file set with the same hashes, differing only in key shape.
func TestBothWrappersAreProjectionsOfOneScan(t *testing.T) {
	t.Parallel()
	root := twoTenantTree(t)
	writeAgedFile(t, filepath.Join(root, "UPPER.YML"), "tenants:\n  t-upper: {}\n")
	writeAgedFile(t, filepath.Join(root, "_profiles.yaml"), "profiles:\n  gold: {}\n")
	writeAgedFile(t, filepath.Join(root, ".hidden.yaml"), "tenants:\n  t-hidden: {}\n")
	writeAgedFile(t, filepath.Join(root, ".hiddendir", "shadow.yaml"), "tenants:\n  t-shadow: {}\n")
	writeAgedFile(t, filepath.Join(root, "README.md"), "# not yaml\n")

	fresh, _ := freshMetrics(t)
	flat, composite, flatM, cache, err := scanDirFileHashes(root, nil, nil, nil)
	if err != nil {
		t.Fatalf("scanDirFileHashes: %v", err)
	}
	tenants, defaults, hier, hierM, graph, err := scanDirHierarchicalWithMetrics(root, nil, fresh, nil)
	if err != nil {
		t.Fatalf("scanDirHierarchicalWithMetrics: %v", err)
	}

	absRoot := absScanRoot(root)
	want := []string{"UPPER.YML", "_defaults.yaml", "_profiles.yaml", "nested/_defaults.yaml", "nested/t-beta.yaml", "t-alpha.yaml"}
	if got := scanKeysOf(flat); !reflect.DeepEqual(got, want) {
		t.Errorf("flat keys = %v, want %v", got, want)
	}
	if len(hier) != len(flat) {
		t.Fatalf("hierarchy hashed %d files, flat %d", len(hier), len(flat))
	}
	for rel, h := range flat {
		abs := filepath.Join(absRoot, filepath.FromSlash(rel))
		if hier[abs] != h {
			t.Errorf("%s: flat hash %s, hierarchy hash %s", rel, h, hier[abs])
		}
		if hierM[abs] != flatM[rel] {
			t.Errorf("%s: stats differ across projections", rel)
		}
		if _, ok := cache[rel]; !ok {
			t.Errorf("%s: cold scan must cache bytes", rel)
		}
	}
	if composite == "" {
		t.Error("composite must be non-empty")
	}
	if got, want := sortedTenantIDs(tenants), []string{"t-alpha", "t-beta", "t-upper"}; !reflect.DeepEqual(got, want) {
		t.Errorf("tenants = %v, want %v", got, want)
	}
	if len(defaults) != 2 {
		t.Errorf("defaults = %v, want two levels", defaults)
	}
	if chain := graph.TenantDefaults["t-beta"]; len(chain) != 2 {
		t.Errorf("t-beta chain = %v, want root + nested", chain)
	}
}

// TestFlatWrapperKeepsItsCacheRule pins the projection detail the flat
// plane's IncrementalLoad depends on: with a prior, only changed/added files
// carry bytes, and a stale prior hash on an unchanged-by-stat file is
// trusted (that is what "fast-path" has always meant here).
func TestFlatWrapperKeepsItsCacheRule(t *testing.T) {
	t.Parallel()
	root := twoTenantTree(t)
	h1, _, m1, _, err := scanDirFileHashes(root, nil, nil, nil)
	if err != nil {
		t.Fatalf("cold: %v", err)
	}
	// Unchanged → nothing cached.
	_, _, _, cache, err := scanDirFileHashes(root, h1, m1, nil)
	if err != nil {
		t.Fatalf("warm: %v", err)
	}
	if len(cache) != 0 {
		t.Errorf("warm unchanged scan cached %v, want nothing", scanKeysOfBytes(cache))
	}
	// Prior missing one key → exactly that file cached.
	delete(h1, "nested/t-beta.yaml")
	delete(m1, "nested/t-beta.yaml")
	_, _, _, cache, err = scanDirFileHashes(root, h1, m1, nil)
	if err != nil {
		t.Fatalf("warm with a missing key: %v", err)
	}
	if got := scanKeysOfBytes(cache); !reflect.DeepEqual(got, []string{"nested/t-beta.yaml"}) {
		t.Errorf("cached %v, want only the file the prior did not know", got)
	}
	// Stale prior hash, matching stat → trusted verbatim.
	h1["t-alpha.yaml"] = "stale"
	h2, _, _, _, err := scanDirFileHashes(root, h1, m1, nil)
	if err != nil {
		t.Fatalf("warm with a stale hash: %v", err)
	}
	if h2["t-alpha.yaml"] != "stale" {
		t.Errorf("fast-path must reuse the prior hash without reading; got %s", h2["t-alpha.yaml"])
	}
}

// TestARootThatIsAFileIsAnErrorOnBothPlanes pins one of the cells where the
// two walkers used to disagree: the flat scanner walked a file root as a
// one-entry tree keyed ".", the hierarchical one refused it. One walker,
// one answer — the hierarchical one's.
func TestARootThatIsAFileIsAnErrorOnBothPlanes(t *testing.T) {
	t.Parallel()
	file := filepath.Join(t.TempDir(), "afile.yaml")
	writeAgedFile(t, file, "tenants:\n  t-file: {}\n")
	fresh, _ := freshMetrics(t)

	if _, _, _, _, err := scanDirFileHashes(file, nil, nil, nil); err == nil || !strings.Contains(err.Error(), "not a directory") {
		t.Errorf("flat wrapper on a file root: err = %v, want 'not a directory'", err)
	}
	if _, _, _, _, _, err := scanDirHierarchicalWithMetrics(file, nil, fresh, nil); err == nil || !strings.Contains(err.Error(), "not a directory") {
		t.Errorf("hierarchical wrapper on a file root: err = %v, want 'not a directory'", err)
	}
}

func scanKeysOfBytes(m map[string][]byte) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// scanWalks reads how many times scanDirTree ran with this metrics
// instance: every such walk observes the scan-duration histogram exactly
// once, so the sample count is the number of walks the manager made.
//
// ⚠️ What this does and does not guard. It counts walks that carry the
// manager's metrics; a walker called with nil metrics (or with the package
// singleton) would not show up here. The historical wrappers — the only
// callers that ever passed nil or the singleton — live in
// scan_wrappers_test.go, so their symbols are absent from the production
// package. What is NOT closed: scanDirTree itself still accepts a nil
// metrics argument, so a manager path written as
// scanDirTree(path, tree, nil, …) would compile and stay invisible here.
// That call shape has no production caller today; if one appears, this
// count is the wrong place to notice it.
func scanWalks(t *testing.T, fresh *configMetrics) uint64 {
	t.Helper()
	reg := prometheus.NewRegistry()
	if err := reg.Register(fresh.scanDuration); err != nil {
		t.Fatalf("register scanDuration: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, mf := range families {
		for _, metric := range mf.Metric {
			if h := metric.Histogram; h != nil {
				return h.GetSampleCount()
			}
		}
	}
	return 0
}

// TestManagerWalksTheTreeOncePerPath pins the #1568 end state on the
// manager: Load walks once, a quiet tick walks once, a tick that finds a
// change walks twice (detectChange, then the debounced reload), and the
// divergence gauge stays at 0 through a tenant edit AND a tenant deletion —
// the deletion being the case that would read 1 if the flat commit ran
// before the hierarchy install (the audit would pair the new config with
// the old tenantSources). Both the hierarchical and the flat layout are
// pinned because they take different branches of every function involved.
func TestManagerWalksTheTreeOncePerPath(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name         string
		hierarchical bool
	}{
		{"hierarchical", true},
		{"flat", false},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			if tc.hierarchical {
				root = twoTenantTree(t)
			} else {
				writeAgedFile(t, filepath.Join(root, "t-alpha.yaml"), "tenants:\n  t-alpha: {}\n")
				writeAgedFile(t, filepath.Join(root, "t-beta.yaml"), "tenants:\n  t-beta: {}\n")
			}
			betaKey := "t-beta.yaml"
			if tc.hierarchical {
				betaKey = "nested/t-beta.yaml"
			}

			fresh, _ := freshMetrics(t)
			var buf bytes.Buffer
			m := NewConfigManagerWithDebounce(root, 0) // 0 → the tick reloads synchronously
			m.SetMetrics(fresh)
			m.SetLogger(log.New(&buf, "", 0))
			t.Cleanup(m.Close)

			gauge := func() float64 { return testutil.ToFloat64(fresh.hierarchyDivergentTenants) }
			expectWalks := func(step string, want uint64, run func()) {
				t.Helper()
				before := scanWalks(t, fresh)
				run()
				if got := scanWalks(t, fresh) - before; got != want {
					t.Errorf("%s: walked the tree %d time(s), want %d", step, got, want)
				}
				if g := gauge(); g != 0 {
					t.Errorf("%s: da_config_hierarchy_divergent_tenants = %v, want 0", step, g)
				}
			}

			expectWalks("Load", 1, func() {
				if err := m.Load(); err != nil {
					t.Fatalf("Load: %v", err)
				}
			})
			m.mu.RLock()
			enabled, tree, hash0 := m.hierarchy.enabled, m.flat.tree, m.lastHash
			m.mu.RUnlock()
			if enabled != tc.hierarchical {
				t.Fatalf("hierarchy.enabled = %v, want %v", enabled, tc.hierarchical)
			}
			if tree == nil {
				t.Fatal("Load retained no *treeScan as the next prior")
			}

			expectWalks("quiet tick", 1, m.tickOnce)
			m.mu.RLock()
			hash1 := m.lastHash
			m.mu.RUnlock()
			if hash1 != hash0 {
				t.Errorf("a quiet tick moved the composite: %s → %s", hash0, hash1)
			}

			// Edit one tenant file. The committed scan must carry the untouched
			// aged sibling across the mtime fast-path (reused) and have READ the
			// edited one.
			writeAgedFile(t, filepath.Join(root, "t-alpha.yaml"), "tenants:\n  t-alpha:\n    mysql_connections: \"7\"\n")
			expectWalks("tick with an edit", 2, m.tickOnce)
			m.mu.RLock()
			hash2, tree2 := m.lastHash, m.flat.tree
			_, alphaLive := m.config.Tenants["t-alpha"]
			m.mu.RUnlock()
			if hash2 == hash1 {
				t.Errorf("the edit was not committed: composite still %s", hash1)
			}
			if !alphaLive {
				t.Errorf("t-alpha missing from the merged config after its edit")
			}
			if tree2 == tree {
				t.Errorf("the reload did not retain its own scan as the next prior")
			}
			if f := tree2.files[betaKey]; f == nil || !f.reused {
				t.Errorf("%s was read on a tick that did not touch it (fast-path lost)", betaKey)
			}
			if f := tree2.files["t-alpha.yaml"]; f == nil || f.reused {
				t.Errorf("the edited t-alpha.yaml took the fast-path; its bytes were never read")
			}

			// Delete one tenant. The gauge must read 0 at the commit — see the
			// order note on installNewHierarchyState — and both planes must
			// have let the tenant go.
			if err := os.Remove(filepath.Join(root, filepath.FromSlash(betaKey))); err != nil {
				t.Fatalf("remove: %v", err)
			}
			expectWalks("tick with a deletion", 2, m.tickOnce)
			m.mu.RLock()
			_, betaLive := m.config.Tenants["t-beta"]
			_, betaSource := m.hierarchy.tenantSources["t-beta"]
			m.mu.RUnlock()
			if betaLive {
				t.Errorf("t-beta still in the merged config after its file was removed")
			}
			if betaSource {
				t.Errorf("t-beta still in hierarchy.tenantSources after its file was removed")
			}

			expectWalks("quiet tick after the churn", 1, m.tickOnce)
			if strings.Contains(buf.String(), "scanner divergence") {
				t.Errorf("a divergence ERROR was logged on a healthy tree:\n%s", buf.String())
			}
		})
	}
}

// parseFailureCount reads da_config_parse_failure_total for one basename
// through the fresh-metrics seam.
func parseFailureCount(fresh *configMetrics, basename string) float64 {
	return testutil.ToFloat64(fresh.parseFailures.WithLabelValues(basename))
}

// TestBrokenFileIsReReadOnEveryTick pins the fast-path carve-out for a file
// whose tenant-declaration parse failed (#1568 round 2, G2): the counter
// must move once per tick for the broken file and not at all for a healthy
// aged sibling, on a manager whose ticks are otherwise all fast-path. A
// walker that reused the broken file's prior would leave the counter flat
// after the first read while the last-scan-complete gauge kept stamping.
func TestBrokenFileIsReReadOnEveryTick(t *testing.T) {
	t.Parallel()
	root := twoTenantTree(t)
	writeAgedFile(t, filepath.Join(root, "broken.yaml"), "tenants:\n  t-broken: [this is not a map\n")

	fresh, _ := freshMetrics(t)
	var buf bytes.Buffer
	m := NewConfigManagerWithDebounce(root, 0)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&buf, "", 0))
	t.Cleanup(m.Close)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := parseFailureCount(fresh, "broken.yaml"); got == 0 {
		t.Fatalf("Load did not count the broken file at all; the fixture is not broken")
	}

	brokenBefore := parseFailureCount(fresh, "broken.yaml")
	healthyBefore := parseFailureCount(fresh, "t-alpha.yaml")
	walksBefore := scanWalks(t, fresh)
	for i := 0; i < 3; i++ {
		m.tickOnce()
	}
	if got := scanWalks(t, fresh) - walksBefore; got != 3 {
		t.Fatalf("three quiet ticks walked %d times, want 3 — the ticks were not quiet", got)
	}
	if got := parseFailureCount(fresh, "broken.yaml") - brokenBefore; got != 3 {
		t.Errorf("parse_failure{broken.yaml} moved by %v over 3 quiet ticks, want 3 (one per re-read)", got)
	}
	if got := parseFailureCount(fresh, "t-alpha.yaml") - healthyBefore; got != 0 {
		t.Errorf("parse_failure{t-alpha.yaml} moved by %v over 3 quiet ticks, want 0 (healthy aged file takes the fast-path)", got)
	}
	m.mu.RLock()
	tree := m.flat.tree
	m.mu.RUnlock()
	if f := tree.files["broken.yaml"]; f == nil || !f.parseFailed {
		t.Errorf("the retained scan does not mark broken.yaml as parseFailed; the carve-out has nothing to key on")
	}
	if f := tree.files["t-alpha.yaml"]; f == nil || f.parseFailed {
		t.Errorf("the retained scan marks the healthy t-alpha.yaml as parseFailed")
	}
}

// TestScanDirTree_UnchangedYoungFileIsNotReparsed pins the cost rule the
// bench gate flagged on PR #1935: a file read because it is younger than
// the mtime guard, whose bytes hash identical to the prior, carries the
// prior's declarations instead of parsing them again. Declarations are a
// function of the bytes; re-parsing 1000 unchanged files on every tick was
// the +945% allocs on IncrementalLoad_1000_NoChange.
//
// The production change that reddens the first arm: parsing whenever the
// file is read. Controls: a changed file IS parsed; a prior that failed to
// parse IS re-parsed (parseFailed carve-out, TestBrokenFileIsReReadOnEveryTick
// covers the counter side).
func TestScanDirTree_UnchangedYoungFileIsNotReparsed(t *testing.T) {
	t.Parallel()
	root := twoTenantTree(t)
	fresh, _ := freshMetrics(t)
	first, err := scanDirTree(root, nil, fresh, nil)
	if err != nil {
		t.Fatalf("first scan: %v", err)
	}
	for k, f := range first.files {
		if !strings.HasPrefix(filepath.Base(k), "_") && !f.parsed {
			t.Errorf("cold scan must parse every tenant carrier; %s was not", k)
		}
	}

	// t-alpha: same bytes, young mtime → read, hash equal → carried.
	alphaPath := filepath.Join(root, "t-alpha.yaml")
	now := time.Now()
	if err := os.Chtimes(alphaPath, now, now); err != nil {
		t.Fatalf("chtimes: %v", err)
	}
	// t-beta: new bytes → read, hash moved → parsed.
	betaPath := filepath.Join(root, "nested", "t-beta.yaml")
	if err := os.WriteFile(betaPath, []byte("tenants:\n  t-beta: {}\n  t-gamma: {}\n"), 0o600); err != nil {
		t.Fatalf("rewrite t-beta: %v", err)
	}

	second, err := scanDirTree(root, first, fresh, nil)
	if err != nil {
		t.Fatalf("second scan: %v", err)
	}
	alpha := second.files["t-alpha.yaml"]
	if alpha.reused {
		t.Fatalf("t-alpha is younger than the guard; it must have been read")
	}
	if alpha.parsed {
		t.Errorf("t-alpha was read but its hash did not move: declarations must be carried, not re-parsed")
	}
	if got, want := alpha.tenantIDs, []string{"t-alpha"}; !reflect.DeepEqual(got, want) {
		t.Errorf("carried declarations = %v, want %v", got, want)
	}
	beta := second.files["nested/t-beta.yaml"]
	if !beta.parsed {
		t.Errorf("t-beta's bytes changed; it must be parsed")
	}
	if got, want := beta.tenantIDs, []string{"t-beta", "t-gamma"}; !reflect.DeepEqual(got, want) {
		t.Errorf("parsed declarations = %v, want %v", got, want)
	}
	if got, want := treeScanTenantIDs(second), []string{"t-alpha", "t-beta", "t-gamma"}; !reflect.DeepEqual(got, want) {
		t.Errorf("tenants = %v, want %v", got, want)
	}
}
