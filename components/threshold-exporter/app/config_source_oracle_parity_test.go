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

	"github.com/vencil/threshold-exporter/internal/confdname"
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

	// Chain depth. Without a nested carrier every tenant's chain is one entry
	// long and the chain assertion is nearly vacuous — it would agree even if
	// one side collected the chain in the wrong ORDER, since a single-element
	// list has no order to get wrong. `sub/_defaults.yaml` gives `nested` an
	// L0..L1 chain; the one under `.cache/` must appear in NOBODY's chain,
	// because the walker never descends there at all.
	"sub/_defaults.yaml":    "defaults:\n  cpu_usage: 81\n",
	".cache/_defaults.yaml": "defaults:\n  cpu_usage: 82\n",

	// ⛔ A case-variant chain carrier, covering a cell where the walker is
	// KNOWN TO BE WRONG (issue #1674). The walker's classifier folds case and
	// files this into its `defaults` set under its original-case path, while
	// `collectDefaultsChain` probes only the two lowercase literals — so it is
	// a carrier that reaches nobody's chain. `ResolveEffective`,
	// `describe_tenant.py` and `flat_scanner.go` all include it; the walker is
	// the only excluder of four readers.
	//
	// It is here BECAUSE the cousin reproduces that fault faithfully, which is
	// what parity means and is exactly what this file must keep true: when
	// #1674 is fixed, a fix applied to only one of the two turns this test red.
	// ⛔ Its presence is not an endorsement — the corpus comment above says
	// plainly that this cell is a known defect on both sides.
	"sub/_DEFAULTS.YML": "defaults:\n  mem_usage: 70\n",
}

// materializeOnDisk writes the corpus under a fresh temp dir and returns it.
//
// ⛔ It then counts what actually landed. `_defaultſ.yaml` and `_defaults.yaml`
// are distinct byte sequences and distinct files on a case-sensitive
// filesystem, but a case-FOLDING one (macOS APFS/HFS+ in its default
// configuration, Windows NTFS) may fold U+017F onto `s` and collapse the two
// fixture entries into a single file. That would not announce itself: the
// oracle would simply see one fewer file and the parity assertions below would
// fail with a mismatch that reads like a code defect. Counting turns that into
// a named cause. ⚠️ NOT MEASURED on either of those filesystems — this
// container is Linux-only, and the repo's supported path is a Linux dev
// container. The guard exists so the first person who runs this natively on a
// Mac gets a sentence instead of a puzzle.
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
	var landed int
	if err := filepath.WalkDir(root, func(_ string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			landed++
		}
		return nil
	}); err != nil {
		t.Fatalf("walk %s: %v", root, err)
	}
	if landed != len(corpus) {
		t.Fatalf("fixture did not materialize: wrote %d entries, %d files landed under %s. "+
			"On a case-folding filesystem (macOS APFS/HFS+, Windows NTFS) `_defaultſ.yaml` "+
			"(U+017F) can collapse onto `_defaults.yaml`. This test needs a case-sensitive "+
			"filesystem — the repo's dev container is one.", len(corpus), landed, root)
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
// It deliberately states no expected answer of its own: it asserts that the
// in-memory cousin reproduces whatever the production walker does with this
// corpus. Hard-coding an expectation here would let one side be edited to match
// a wrong idea of the answer and still pass.
//
// ⛔ "REPRODUCES THE WALKER" IS NOT "IS CORRECT", and an earlier version of this
// comment blurred the two by calling the walker's behaviour "the contract". It
// is the contract *for this cousin* — the cousin exists to be the walker's
// in-memory twin — but the walker is not thereby right. Measured since:
// `scanDirHierarchical` contradicts itself on a case-variant carrier
// (`sub/_DEFAULTS.YML` enters its `defaults` set and no tenant's chain, because
// `collectDefaultsChain` probes the two lowercase literals), and on that cell
// `ResolveEffective`, `describe_tenant.py` and `flat_scanner.go` all disagree
// with it — the walker is one of four readers and the only excluder. Filed as
// issue #1674.
//
// ⇒ This test's job is to stop the cousin from drifting away from the walker.
// It is NOT evidence that the walker is right, and it must not be cited as
// such. Where the walker is wrong, the fix is a joint one — both sides move
// together, and this test correctly stays green through that.
func TestScanFromConfigSourceMatchesOracleOnHiddenPaths(t *testing.T) {
	t.Parallel()

	fresh, _ := freshMetrics(t)
	diskRoot := materializeOnDisk(t, hiddenAxisCorpus)

	oracleTenants, oracleDefaults, oracleHashes, _, oracleGraph, err := scanDirHierarchicalWithMetrics(diskRoot, nil, fresh, nil)
	if err != nil {
		t.Fatalf("scanDirHierarchical: %v", err)
	}

	src := config.NewInMemoryConfigSource(materializeInMemory(hiddenAxisCorpus))
	memTenants, memDefaults, memHashes, memGraph, err := config.ScanFromConfigSource(src, simParityRoot)
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

	// ⛔ THE CHAIN, which this harness used to discard on both sides while the
	// function it pins advertises "identical classification + dedup + CHAIN
	// RULES". Comparing three of the four return values and dropping the fourth
	// left a quarter of the documented contract with nothing behind it — a
	// promise with no guard, which is the defect this whole file exists to
	// catch. Today both sides reach the chain through the same
	// `collectDefaultsChain` parameterised by `pathOps`, so this is expected to
	// agree; that is the reason to pin it, not a reason to skip it. It stops
	// agreeing the day `posixPathOps` and `nativePathOps` diverge at some
	// boundary, and nothing else would notice.
	for _, tid := range sortedTenantIDs(oracleTenants) {
		oracleChain := relPathList(t, oracleGraph.TenantDefaults[tid], diskRoot)
		memChain := relPathList(t, memGraph.TenantDefaults[tid], simParityRoot)
		if !equalStrings(oracleChain, memChain) {
			t.Errorf("defaults chain for tenant %q diverges (order is L0..Ln and matters):\n"+
				"  oracle: %v\n  cousin: %v", tid, oracleChain, memChain)
		}
	}
}

// relPathList normalizes an ORDERED chain to root-relative POSIX paths. Unlike
// relKeys it must not sort: the chain's order is the inheritance precedence.
func relPathList(t *testing.T, paths []string, root string) []string {
	t.Helper()
	out := make([]string, 0, len(paths))
	for _, p := range paths {
		rel, err := filepath.Rel(root, filepath.FromSlash(p))
		if err != nil {
			t.Fatalf("Rel(%q, %q): %v", root, p, err)
		}
		out = append(out, filepath.ToSlash(rel))
	}
	return out
}

// TestSharedDefaultsPredicateStillDisagreesWithTheWalker pins the divergence
// that keeps `ScanFromConfigSource` on its own copy of the defaults rule
// (issue #1670), because until now nothing in the repo asserted it — the fact
// existed only as prose in a comment, and prose does not go red.
//
// ⛔ IT ASSERTS THE DISAGREEMENT, NOT THE WRONG ANSWER. Pinning
// `IsDefaults("_defaultſ.yaml") == true` would read as an endorsement and
// would have to be deleted to fix the bug. Pinning "these two disagree" makes
// the fix itself the trigger: the day `internal/confdname` is reconciled with
// the walker, this test fails, and its message says what to do about it.
//
// ⛔ `internal/confdname`'s matrix parity test cannot cover this cell — the
// matrix's 23 rows carry exactly one non-ASCII name (`İ.yaml`) and it is on
// the extension axis, so ToLower and EqualFold agree on every row it has.
func TestSharedDefaultsPredicateStillDisagreesWithTheWalker(t *testing.T) {
	t.Parallel()

	const name = "_defaultſ.yaml" // U+017F LATIN SMALL LETTER LONG S

	// The walker's rule, restated here rather than called: scanDirHierarchical
	// lowercases and compares against the two literals.
	lower := strings.ToLower(name)
	walkerSaysDefaults := lower == "_defaults.yaml" || lower == "_defaults.yml"
	sharedSaysDefaults := confdname.IsDefaults(name)

	if walkerSaysDefaults == sharedSaysDefaults {
		t.Fatalf("confdname.IsDefaults(%q) = %v and the walker's rule = %v — they now AGREE.\n"+
			"If #1670 has been fixed, that is the good news and this test has done its job:\n"+
			"  1. delete this test,\n"+
			"  2. re-read the \"DELIBERATELY NOT internal/confdname.IsDefaults\" note in\n"+
			"     pkg/config/source.go — its reason no longer holds, and ScanFromConfigSource\n"+
			"     may finally be able to drop its private copy of the rule,\n"+
			"  3. remove the %q cell from hiddenAxisCorpus, or keep it and say why.\n"+
			"If instead the WALKER moved, stop: the walker is the authority the shared\n"+
			"predicate is defined against, not the other way round.",
			name, sharedSaysDefaults, walkerSaysDefaults, name)
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
