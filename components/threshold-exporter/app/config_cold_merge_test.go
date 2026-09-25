package main

// #1978: a hierarchical cold load (populateHierarchyStateFrom) merges from
// the scan's own bytes and parses each defaults file once, instead of
// recomputeMergedHash's per-tenant re-read and re-parse of the tenant file
// and its whole chain. These tests pin that the change is invisible: the
// same merged_hash for every tenant, the same parsedDefaults cache, and the
// same parse-failure signal (metric + ERROR log + per-tenant skip) as the
// recomputeMergedHash path it replaced — which stays the debounced path's
// and is used here as the oracle.

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	promtest "github.com/prometheus/client_golang/prometheus/testutil"
)

// coldMergeFixtures are trees that exercise every input shape the cold merge
// reads. Each is a map rel-path → content.
func coldMergeFixtures() map[string]map[string]string {
	return map[string]map[string]string{
		// Four-level chain, a subtree override of a scheduled value, an
		// empty-body tenant, `_metadata` (never inherited) and a reserved
		// key deleted by an explicit null.
		"multi-level": {
			"_defaults.yaml":       "defaults:\n  cpu: 80\n  mem: 85\n  _routing:\n    receiver: root\n",
			"a/_defaults.yaml":     "defaults:\n  cpu: 70\n  _metadata:\n    owner: team-a\n",
			"a/b/_defaults.yaml":   "defaults:\n  mem:\n    default: \"90\"\n    overrides:\n      - window: \"22:00-06:00\"\n        value: \"95\"\n",
			"a/b/c/_defaults.yaml": "defaults:\n  disk: 60\n",
			"a/b/c/t-deep.yaml":    "tenants:\n  t-deep:\n    cpu: \"65\"\n    _routing: null\n",
			"a/b/t-mid.yaml":       "tenants:\n  t-mid:\n",
			"t-root.yaml":          "tenants:\n  t-root:\n    cpu: \"50\"\n",
		},
		// One tenant file declaring several tenants; a `.yml` tenant file; a
		// case-variant carrier; a `.yaml`/`.yml` carrier pair in one
		// directory (one is selected, both are cached in parsedDefaults).
		"shapes": {
			"_defaults.yaml":       "defaults:\n  cpu: 80\n",
			"x/_Defaults.YAML":     "defaults:\n  cpu: 75\n",
			"x/multi.yaml":         "tenants:\n  m-1:\n    mem: \"10\"\n  m-2:\n    mem: \"20\"\n  m-3:\n",
			"y/_defaults.yaml":     "defaults:\n  cpu: 71\n",
			"y/_defaults.yml":      "defaults:\n  cpu: 72\n",
			"y/t-yml.yml":          "tenants:\n  t-yml:\n    disk: \"5\"\n",
			"z/_defaults.yaml":     "",
			"z/t-empty-chain.yaml": "tenants:\n  t-empty-chain:\n    cpu: \"1\"\n",
			// Whitespace with a tab: YAML rejects it, so t-ws's merge fails,
			// while parseDefaultsBytes caches it as an empty map without
			// parsing (its TrimSpace short-cut) — both halves must survive.
			"w/_defaults.yaml": "   \n\t\n",
			"w/t-ws.yaml":      "tenants:\n  t-ws:\n    cpu: \"2\"\n",
			"v/_defaults.yaml": "# comment only\n",
			"v/t-comment.yaml": "tenants:\n  t-comment:\n    cpu: \"3\"\n",
			"u/_defaults.yaml": "cpu: 33\nmem: 44\n", // no `defaults:` wrapper
			"u/t-bare.yaml":    "tenants:\n  t-bare:\n    cpu: \"4\"\n",
		},
		// A broken subtree defaults file (its tenants' merges fail), a
		// broken tenant file (the walker drops it), and a healthy sibling.
		"broken": {
			"_defaults.yaml":         "defaults:\n  cpu: 80\n",
			"bad/_defaults.yaml":     "defaults:\n  cpu: {unclosed-brace\n",
			"bad/t-under-bad.yaml":   "tenants:\n  t-under-bad:\n    cpu: \"1\"\n  t-under-bad-2:\n",
			"bad/deep/t-deeper.yaml": "tenants:\n  t-deeper:\n    cpu: \"2\"\n",
			"good/t-good.yaml":       "tenants:\n  t-good:\n    cpu: \"3\"\n",
			"good/t-broken.yaml":     "tenants:\n  t-broken: [unclosed\n",
		},
	}
}

func writeColdMergeFixture(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for rel, content := range files {
		writeFile(t, filepath.Join(root, filepath.FromSlash(rel)), content)
	}
	return root
}

// coldLoaded runs a cold load with its own metrics and a discarded-to-buffer
// logger, and returns the manager.
func coldLoaded(t *testing.T, root string) *ConfigManager {
	t.Helper()
	fresh, _ := freshMetrics(t)
	logger, _ := newTestLogger()
	mgr := NewConfigManager(root)
	mgr.SetMetrics(fresh)
	mgr.SetLogger(logger)
	if err := mgr.fullDirLoad(); err != nil {
		t.Fatalf("fullDirLoad: %v", err)
	}
	return mgr
}

// assertColdStateMatchesRecompute checks mgr's installed hierarchy state
// against the recomputeMergedHash / parseDefaultsBytes oracle, which re-reads
// every file from disk. It returns how many tenants carry a hash, so callers
// can refuse a vacuous comparison.
func assertColdStateMatchesRecompute(t *testing.T, mgr *ConfigManager) int {
	t.Helper()
	mgr.mu.RLock()
	sources := mgr.hierarchy.tenantSources
	got := mgr.hierarchy.mergedHashes
	graph := mgr.hierarchy.graph
	gotParsed := mgr.hierarchy.parsedDefaults
	mgr.mu.RUnlock()
	if graph == nil {
		t.Fatal("no inheritance graph installed")
	}

	oracle := NewConfigManager(mgr.path)
	oracleMetrics, _ := freshMetrics(t)
	oracleLog, _ := newTestLogger()
	oracle.SetMetrics(oracleMetrics)
	oracle.SetLogger(oracleLog)

	tids := make([]string, 0, len(sources))
	for tid := range sources {
		tids = append(tids, tid)
	}
	sort.Strings(tids)
	for _, tid := range tids {
		want, werr := oracle.recomputeMergedHash(tid, sources[tid], graph.TenantDefaults[tid])
		have, ok := got[tid]
		switch {
		case werr != nil && ok:
			t.Errorf("tenant %s: cold load installed merged_hash %s, recompute fails: %v", tid, have, werr)
		case werr == nil && !ok:
			t.Errorf("tenant %s: cold load installed no merged_hash, recompute gives %s", tid, want)
		case werr == nil && have != want:
			t.Errorf("tenant %s: cold merged_hash %s != recompute %s", tid, have, want)
		}
	}
	for tid := range got {
		if _, ok := sources[tid]; !ok {
			t.Errorf("merged_hash installed for %s, which has no source", tid)
		}
	}

	// parsedDefaults: every defaults file the tree holds, in
	// parseDefaultsBytes' shape; a file that fails to parse is absent.
	wantParsed := map[string]map[string]any{}
	_ = filepath.WalkDir(mgr.path, func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasPrefix(d.Name(), "_") {
			return nil
		}
		b, rerr := os.ReadFile(p)
		if rerr != nil {
			t.Fatalf("read %s: %v", p, rerr)
		}
		if parsed, perr := parseDefaultsBytes(b); perr == nil {
			wantParsed[p] = parsed
		}
		return nil
	})
	// The manager keys by the scan's absolute path under the resolved root.
	gotByPath := map[string]map[string]any{}
	for p, v := range gotParsed {
		gotByPath[p] = v
	}
	if len(gotByPath) != len(wantParsed) {
		t.Errorf("parsedDefaults has %d files, want %d: got %v", len(gotByPath), len(wantParsed), parsedDefaultsKeys(gotByPath))
	}
	for p, want := range wantParsed {
		resolved := p
		if r, err := filepath.EvalSymlinks(p); err == nil {
			resolved = r
		}
		have, ok := gotByPath[resolved]
		if !ok {
			t.Errorf("parsedDefaults lacks %s", resolved)
			continue
		}
		if !reflect.DeepEqual(have, want) {
			t.Errorf("parsedDefaults[%s] = %#v, want %#v", resolved, have, want)
		}
	}
	return len(got)
}

func parsedDefaultsKeys(m map[string]map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// TestColdLoadMergedHashesMatchRecompute is the byte-identity pin of #1978:
// every tenant's cold-load merged_hash equals the one recomputeMergedHash
// (the pre-#1978 cold path, still the debounced path) derives from disk —
// over the hand-written shape fixtures and the 1000-tenant-shaped bench
// fixture at a smaller size — and a merge that fails on one path fails on
// the other. It also runs a second, WARM fullDirLoad on each manager: that
// scan carries every file (same hash as the prior), so the cold merge has no
// scan bytes and reads the files itself — the fallback branch.
func TestColdLoadMergedHashesMatchRecompute(t *testing.T) {
	t.Parallel()
	cases := coldMergeFixtures()
	names := make([]string, 0, len(cases)+1)
	for n := range cases {
		names = append(names, n)
	}
	sort.Strings(names)
	names = append(names, "bench-fixture-150")

	minHashed := map[string]int{
		"multi-level":       3,
		"shapes":            7, // 8 tenants; t-ws fails (see w/_defaults.yaml)
		"broken":            1, // t-good only
		"bench-fixture-150": 150,
	}
	for _, name := range names {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			var root string
			if name == "bench-fixture-150" {
				root = t.TempDir()
				if err := writeHierarchicalBenchFixtureContent(root, 150); err != nil {
					t.Fatal(err)
				}
			} else {
				root = writeColdMergeFixture(t, cases[name])
			}
			mgr := coldLoaded(t, root)
			n := assertColdStateMatchesRecompute(t, mgr)
			if n < minHashed[name] {
				t.Fatalf("only %d tenants hashed, want >= %d — the comparison would be near-vacuous", n, minHashed[name])
			}

			mgr.mu.RLock()
			first := mgr.hierarchy.mergedHashes
			mgr.mu.RUnlock()

			// Warm: the prior makes every file's bytes unavailable from
			// the scan (same hash → TreeFile.Data nil).
			if err := mgr.fullDirLoad(); err != nil {
				t.Fatalf("warm fullDirLoad: %v", err)
			}
			mgr.mu.RLock()
			tree := mgr.flat.tree
			second := mgr.hierarchy.mergedHashes
			mgr.mu.RUnlock()
			for rel, f := range tree.Files {
				if f.Data != nil {
					t.Fatalf("warm scan still holds bytes for %s — the fallback branch is not exercised", rel)
				}
			}
			if !reflect.DeepEqual(first, second) {
				t.Errorf("warm fullDirLoad changed merged hashes:\n cold %v\n warm %v", first, second)
			}
			assertColdStateMatchesRecompute(t, mgr)
		})
	}
}

// TestColdMergedHashMatchesRecomputeOnEveryError drives coldMergedHash and
// recomputeMergedHash directly with inputs the walker would never let reach
// the cold load (a tenant file that does not parse, a tenant absent from its
// file, an unreadable chain entry behind a broken one) and requires the same
// hash-or-error text and the same parse-failure counts from both.
func TestColdMergedHashMatchesRecomputeOnEveryError(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	good := filepath.Join(root, "_defaults.yaml")
	bad := filepath.Join(root, "bad", "_defaults.yaml")
	missing := filepath.Join(root, "gone", "_defaults.yaml")
	writeFile(t, good, "defaults:\n  cpu: 80\n")
	writeFile(t, bad, "defaults:\n  cpu: {unclosed\n")
	okTenant := filepath.Join(root, "t.yaml")
	brokenTenant := filepath.Join(root, "broken.yaml")
	writeFile(t, okTenant, "tenants:\n  t1:\n    cpu: \"5\"\n")
	writeFile(t, brokenTenant, "tenants:\n  t1: [unclosed\n")

	cases := []struct {
		name   string
		tid    string
		tenant string
		chain  []string
	}{
		{"ok", "t1", okTenant, []string{good}},
		{"ok-no-chain", "t1", okTenant, nil},
		{"broken-defaults-first", "t1", okTenant, []string{bad, good}},
		{"broken-defaults-second", "t1", okTenant, []string{good, bad}},
		{"broken-tenant", "t1", brokenTenant, []string{good}},
		{"broken-tenant-and-defaults", "t1", brokenTenant, []string{bad}},
		{"tenant-not-in-file", "t-nope", okTenant, []string{good}},
		{"unreadable-after-broken", "t1", okTenant, []string{bad, missing}},
		{"unreadable-tenant", "t1", filepath.Join(root, "absent.yaml"), []string{good}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			oracleMetrics, _ := freshMetrics(t)
			oracleLog, oracleBuf := newTestLogger()
			oracle := NewConfigManager(root)
			oracle.SetMetrics(oracleMetrics)
			oracle.SetLogger(oracleLog)
			wantHash, wantErr := oracle.recomputeMergedHash(tc.tid, tc.tenant, tc.chain)

			coldMetrics, _ := freshMetrics(t)
			coldLog, coldBuf := newTestLogger()
			cold := NewConfigManager(root)
			cold.SetMetrics(coldMetrics)
			cold.SetLogger(coldLog)
			// No scan bytes → every input goes through the read fallback;
			// then the same again with the bytes pre-seeded, the cold
			// load's normal case.
			for _, seeded := range []bool{false, true} {
				in := &coldMergeInputs{data: map[string][]byte{}, defaults: map[string]*coldDefaultsEntry{}}
				if seeded {
					for _, p := range append([]string{tc.tenant}, tc.chain...) {
						if b, err := os.ReadFile(p); err == nil {
							in.data[p] = b
						}
					}
				}
				gotHash, gotErr := cold.coldMergedHash(tc.tid, tc.tenant, tc.chain, in)
				if gotHash != wantHash {
					t.Errorf("seeded=%v: hash %q, recompute %q", seeded, gotHash, wantHash)
				}
				if (gotErr == nil) != (wantErr == nil) || (gotErr != nil && gotErr.Error() != wantErr.Error()) {
					t.Errorf("seeded=%v: err %v, recompute %v", seeded, gotErr, wantErr)
				}
				if wantErr != nil && errors.Is(wantErr, os.ErrNotExist) != errors.Is(gotErr, os.ErrNotExist) {
					t.Errorf("seeded=%v: errors.Is(ErrNotExist) differs: %v vs %v", seeded, gotErr, wantErr)
				}
			}
			// Both seeded and unseeded ran on `cold`, so its counters and
			// log hold exactly twice the oracle's.
			for _, base := range []string{"_defaults.yaml", "broken.yaml", "t.yaml"} {
				w := promtest.ToFloat64(oracleMetrics.parseFailures.WithLabelValues(base))
				g := promtest.ToFloat64(coldMetrics.parseFailures.WithLabelValues(base))
				if g != 2*w {
					t.Errorf("parse_failure{%s}: cold %v, want 2 × recompute %v", base, g, w)
				}
			}
			if want := oracleBuf.String() + oracleBuf.String(); coldBuf.String() != want {
				t.Errorf("log differs:\n cold:\n%s\n recompute (×2):\n%s", coldBuf.String(), want)
			}
		})
	}
}

// TestColdLoadBrokenDefaultsKeepsItsSignal pins the observability contract
// of a broken subtree `_defaults.yaml` on a COLD load exactly (the older
// TestRecomputeMergedHash_DefaultsParseFailureEmitsErrorAndMetric asserts
// >= 1): one ERROR line naming the file and chain index per dependent
// tenant, one logMergeSkip WARN per dependent tenant, a parse-failure
// increment per dependent tenant plus the flat plane's own one for the
// nested file, the parsedDefaults WARN, and the healthy tenant untouched.
// The numbers are the ones the pre-#1978 path produced on the same tree.
func TestColdLoadBrokenDefaultsKeepsItsSignal(t *testing.T) {
	t.Parallel()
	root := writeColdMergeFixture(t, map[string]string{
		"_defaults.yaml":       "defaults:\n  cpu: 80\n",
		"team/_defaults.yaml":  "defaults:\n  cpu: {unclosed-brace\n",
		"team/pair.yaml":       "tenants:\n  t-1:\n    cpu: \"1\"\n  t-2:\n    cpu: \"2\"\n",
		"other/t-healthy.yaml": "tenants:\n  t-healthy:\n    cpu: \"3\"\n",
	})
	fresh, _ := freshMetrics(t)
	logger, buf := newTestLogger()
	mgr := NewConfigManager(root)
	mgr.SetMetrics(fresh)
	mgr.SetLogger(logger)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	logs := buf.String()

	for _, tid := range []string{"t-1", "t-2"} {
		assertLogLineWith(t, logs, "for tenant="+tid+":",
			"ERROR: skip unparseable defaults/profiles file",
			filepath.Join("team", "_defaults.yaml"), "(chain index 1)")
		assertLogLineWith(t, logs, "skipping merged_hash for tenant="+tid+" ",
			"WARN:", "(initial-hierarchy-scan)", "parse defaults[1]:")
	}
	if n := len(logLinesWith(logs, "(chain index")); n != 2 {
		t.Errorf("%d per-tenant ERROR lines, want 2 (one per dependent tenant); log:\n%s", n, logs)
	}
	if n := len(logLinesWith(logs, "skipping merged_hash")); n != 2 {
		t.Errorf("%d logMergeSkip lines, want 2; log:\n%s", n, logs)
	}
	assertLogLineWith(t, logs, "parsedDefaults cache: parse", "WARN:", filepath.Join("team", "_defaults.yaml"))
	if got := promtest.ToFloat64(fresh.parseFailures.WithLabelValues("_defaults.yaml")); got != 3 {
		t.Errorf("parse_failure{_defaults.yaml} = %v, want 3 (2 dependent tenants + the flat plane's nested probe)", got)
	}

	mgr.mu.RLock()
	hashes := mgr.hierarchy.mergedHashes
	parsed := mgr.hierarchy.parsedDefaults
	mgr.mu.RUnlock()
	if _, ok := hashes["t-healthy"]; !ok || len(hashes) != 1 {
		t.Errorf("merged hashes = %v, want exactly t-healthy", hashes)
	}
	if len(parsed) != 1 {
		t.Errorf("parsedDefaults = %v, want only the root file", parsed)
	}
}
