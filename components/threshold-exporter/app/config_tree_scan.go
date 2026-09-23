package main

// ============================================================
// Adapter onto the ONE conf.d tree walker (pkg/config/tree_scan.go)
// ============================================================
//
// The walker itself — the skip rules, the mtime fast-path that carries
// tenant declarations, the flat and hierarchy products, the metric
// contract — moved to pkg/config (#1941, #1911 ①(a)) so that
// pkg/config.ResolveEffective / ScopeEffective can consume the same walk in
// a follow-up. That move changed nothing on this side: package main keeps
// its historical names through the aliases and the one-line adapter below,
// and every call site reads the same as before except for the (now
// exported) field and method names.
//
// ⛔ package main defines NO walker. scanDirTree below is the only entry into
// the walk from this package; the two historical functions survive as
// projections in scan_wrappers_test.go.

import (
	"log"

	"github.com/vencil/threshold-exporter/pkg/config"
)

type (
	// treeScan / treeFile / fileStat keep their package-main names so the
	// manager, debounce and test code did not have to be rewritten for the
	// move. Methods and fields are pkg/config's (exported).
	treeScan = config.TreeScan
	treeFile = config.TreeFile
	fileStat = config.FileStat
)

// absScanRoot / resolveScanRoot — see config.AbsScanRoot /
// config.ResolveScanRoot, the ONE derivation of the conf.d root.
func absScanRoot(dir string) string     { return config.AbsScanRoot(dir) }
func resolveScanRoot(dir string) string { return config.ResolveScanRoot(dir) }

// scanDirTree is config.ScanDirTree with this package's metrics type. See
// config.ScanDirTree for the rules and the metric contract (duration
// observed on every call including errors; last-scan-complete stamped only
// on a clean, conflict-free success). metrics may be nil (no metric
// touched); logger nil falls back to log.Default().
func scanDirTree(root string, prior *treeScan, metrics *configMetrics, logger *log.Logger) (*treeScan, error) {
	return config.ScanDirTree(root, prior, scanObserverFor(metrics), logger)
}

// scanObserverFor converts a possibly-nil *configMetrics into a
// config.ScanObserver.
//
// ⛔ THE TYPED-NIL TRAP. Passing `metrics` straight through would store a nil
// *configMetrics in a non-nil interface: config.ScanDirTree's `obs != nil`
// checks would then pass and call its methods on a nil receiver, which
// panic (configMetrics' methods dereference their collectors; measured:
// nil pointer dereference with this guard removed). The historical contract
// ("metrics may be nil: nothing is counted") therefore lives HERE, as an
// explicit true-nil return. Pinned by
// TestScanDirTree_NilConfigMetricsIsTrueNilObserver.
func scanObserverFor(metrics *configMetrics) config.ScanObserver {
	if metrics == nil {
		return nil
	}
	return metrics
}
