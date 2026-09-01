package main

// config_source_oracle_parity_test.go — pins `pkg/config.ScanFromConfigSource`
// against the production walker (`scanDirHierarchical`, this package) on the
// axis #1589 measured: PATH vs BASENAME.
//
// ⛔ WHY THIS FILE EXISTS, AND WHY HERE. `ScanFromConfigSource` documents
// itself as "the in-memory cousin of scanDirHierarchical … using identical
// classification + dedup + chain rules", and `source.go` carried the comment
// `// Hidden files skipped — match scanDirHierarchical.` on a test that only
// looked at `path.Base(p)`. The oracle prunes hidden DIRECTORIES with
// `fs.SkipDir`, so `<root>/.git/inside.yaml` is never visited; the in-memory
// cousin walks a flat map, and `path.Base` of that path is `inside.yaml`,
// which is not dot-prefixed. The two answered differently and nothing said so
// — the #1339 family's shape (one tree, several enumerators, different
// selection conditions, silent divergence), this time between an implementation
// and the comment claiming it matched.
//
// It lives in `package main` because that is the only package that can call
// BOTH sides: the oracle is unexported here, and `pkg/config` cannot import
// `main`. Measured before writing this file: `pkg/config`'s three enumerators
// (`hierarchy.go`, `scope.go`, `source.go`) had NO parity test against the
// oracle at all — `grep -rn scanDirHierarchical pkg/` returned comments only,
// zero calls. `flat_scanner.go` has `config_scanner_pair_test.go`; these three
// had nothing. This closes that gap for `source.go` only; `hierarchy.go` and
// `scope.go` remain unpinned (that is "not measured", not "measured fine").
//
// ⛔ SCOPE HONESTY. `SimulateEffective` — the only production caller — builds
// its own synthetic corpus (`/sim`, `/sim/lvl1/…`, `tenant.yaml`) and can never
// hand a hidden path to the scanner. So this is NOT a live /simulate incident.
// What diverged is a public library API in `pkg/config` plus a comment stating
// the opposite of the truth. The family's whole claim is that such divergence
// is the defect, discovered later at a worse moment.

import (
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// hiddenAxisCorpus is one logical tree, expressed root-relative with POSIX
// separators. Bodies are distinct so a misattributed tenant is visible by name
// rather than by count.
//
// The three hidden-path shapes are deliberately different from each other:
// a dot FILE at the root, a dot DIRECTORY one level down, and a dot DIRECTORY
// with a further plain level beneath it. The middle one is the cell #1589
// reported; the last one is there because the oracle's `fs.SkipDir` prunes the
// whole subtree, and a fix that only tested the file's immediate parent would
// pass the middle cell and still miss this one.
// The `_defaultſ.yaml` entry (U+017F LATIN SMALL LETTER LONG S) is not part of
// the hidden axis. It pins the OTHER trap found while fixing this one: the
// walker classifies defaults with `strings.ToLower(name) == "_defaults.yaml"`
// while `internal/confdname.IsDefaults` uses `strings.EqualFold`, and those two
// disagree on exactly this name (measured in Go: false vs true). Swapping the
// scanner onto the shared predicate — the obvious "collapse the copies" move —
// therefore turns this cell red here. That is the point of keeping it.
var hiddenAxisCorpus = map[string]string{
	"_defaults.yaml":     "defaults:\n  cpu_usage: 80\n",
	"_defaultſ.yaml":     "defaults:\n  cpu_usage: 99\n",
	"plain.yaml":         "tenants:\n  plain:\n    cpu_usage: 1\n",
	"sub/nested.YAML":    "tenants:\n  nested:\n    cpu_usage: 4\n",
	".hidden.yaml":       "tenants:\n  fromhiddenfile:\n    cpu_usage: 2\n",
	".git/inside.yaml":   "tenants:\n  fromgit:\n    cpu_usage: 3\n",
	".cache/deep/x.yaml": "tenants:\n  fromcache:\n    cpu_usage: 5\n",
}

// materializeOnDisk writes the corpus under a fresh temp dir and returns it.
func materializeOnDisk(t *testing.T, corpus map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for rel, body := range corpus {
		abs := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
			t.Fatalf("mkdir for %s: %v", rel, err)
		}
		if err := os.WriteFile(abs, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", rel, err)
		}
	}
	return root
}

// materializeInMemory returns the same corpus keyed by POSIX paths under
// simParityRoot, which is what InMemoryConfigSource contracts for.
func materializeInMemory(corpus map[string]string) map[string][]byte {
	files := make(map[string][]byte, len(corpus))
	for rel, body := range corpus {
		files[path.Join(simParityRoot, rel)] = []byte(body)
	}
	return files
}

const simParityRoot = "/sim"

// relKeys normalizes a scanner's path-keyed map to sorted root-relative POSIX
// paths so the two scanners' answers are comparable at all.
func relKeys[V any](t *testing.T, m map[string]V, root string) []string {
	t.Helper()
	out := make([]string, 0, len(m))
	for k := range m {
		rel, err := filepath.Rel(root, filepath.FromSlash(k))
		if err != nil {
			t.Fatalf("Rel(%q, %q): %v", root, k, err)
		}
		out = append(out, filepath.ToSlash(rel))
	}
	sort.Strings(out)
	return out
}

func sortedTenantIDs(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for id := range m {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// TestScanFromConfigSourceMatchesOracleOnHiddenPaths is the parity assertion.
// It deliberately states no expected answer of its own: whatever the production
// walker does with these six names IS the contract, and the in-memory cousin
// has to reproduce it. Hard-coding the expectation here would let both sides
// drift together and still pass.
func TestScanFromConfigSourceMatchesOracleOnHiddenPaths(t *testing.T) {
	t.Parallel()

	fresh, _ := freshMetrics(t)
	diskRoot := materializeOnDisk(t, hiddenAxisCorpus)

	oracleTenants, oracleDefaults, oracleHashes, _, _, err := scanDirHierarchicalWithMetrics(diskRoot, nil, fresh, nil)
	if err != nil {
		t.Fatalf("scanDirHierarchical: %v", err)
	}

	src := config.NewInMemoryConfigSource(materializeInMemory(hiddenAxisCorpus))
	memTenants, memDefaults, memHashes, _, err := config.ScanFromConfigSource(src, simParityRoot)
	if err != nil {
		t.Fatalf("ScanFromConfigSource: %v", err)
	}

	// Fixture-drift guard. If the corpus ever stops containing a hidden path,
	// every assertion below still passes while testing nothing — the exact
	// shape this repo has paid for four times (a guard that is never reached
	// reports green). Fatal, not Skip.
	var hiddenInCorpus int
	for rel := range hiddenAxisCorpus {
		for _, seg := range strings.Split(rel, "/") {
			if strings.HasPrefix(seg, ".") {
				hiddenInCorpus++
				break
			}
		}
	}
	if hiddenInCorpus < 3 {
		t.Fatalf("fixture drift: corpus carries %d hidden-path entries, want >= 3 "+
			"(a dot file, a dot dir, and a dot dir with a plain level beneath) — "+
			"this test asserts nothing without them", hiddenInCorpus)
	}
	// And the oracle must actually be dropping them, or "parity" is vacuous.
	if len(oracleTenants) >= len(hiddenAxisCorpus) {
		t.Fatalf("fixture drift: oracle registered %d tenants from a %d-entry corpus; "+
			"it is not pruning anything, so parity proves nothing",
			len(oracleTenants), len(hiddenAxisCorpus))
	}

	if got, want := sortedTenantIDs(memTenants), sortedTenantIDs(oracleTenants); !equalStrings(got, want) {
		t.Errorf("tenant IDs diverge from the production walker:\n"+
			"  oracle (scanDirHierarchical): %v\n"+
			"  cousin (ScanFromConfigSource): %v", want, got)
	}
	if got, want := relKeys(t, memDefaults, simParityRoot), relKeys(t, oracleDefaults, diskRoot); !equalStrings(got, want) {
		t.Errorf("defaults carriers diverge:\n  oracle: %v\n  cousin: %v", want, got)
	}
	if got, want := relKeys(t, memHashes, simParityRoot), relKeys(t, oracleHashes, diskRoot); !equalStrings(got, want) {
		t.Errorf("hashed file set diverges:\n  oracle: %v\n  cousin: %v", want, got)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
