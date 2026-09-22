package main

// scan_wrappers_test.go — the two historical conf.d walkers, reduced to
// projections of scanDirTree and moved OUT of the production build (#1568
// round 2). They are kept only because the tests and benchmarks written
// against their signatures (~15 call sites for each) still describe
// behaviour worth pinning:
//
//	scanDirFileHashes              flat plane:      root-relative keys, composite, byte cache
//	scanDirHierarchicalWithMetrics hierarchy plane: absolute keys, typed duplicate error
//	scanDirHierarchical            the legacy singleton-metrics form of the above
//
// ⛔ Living in a _test.go file is the guard, not a tidiness choice: the
// manager's walk count (TestManagerWalksTheTreeOncePerPath) is measured
// through the metrics scanDirTree observes, and these wrappers are the
// callers that passed nil metrics or the package singleton — invisible to
// that count. With their symbols absent from the production package, a
// manager path cannot call them at all. (scanDirTree itself still accepts
// nil metrics; that shape has no production caller and is noted at
// scanWalks.)

import (
	"log"
)

// scanDirFileHashes scans a directory and returns per-file SHA-256 hashes,
// the composite hash, per-file mtime+size stats, and a byte cache of files
// that were actually read (for reuse by callers that need file contents,
// avoiding double disk reads in fullDirLoad/IncrementalLoad).
//
// Uses DirEntry.Info() to get mtime+size from the directory listing itself,
// avoiding separate os.Stat calls per file.
//
// When oldHashes and oldMtimes are provided (non-nil), the mtime guard kicks in:
// files whose ModTime and Size match the previous scan reuse the cached SHA-256
// without re-reading file contents. This reduces NoChange cost from O(N×read)
// to O(N×stat) — typically 4-5× faster at 1000 tenants.
//
// ⛔ THIS IS A WRAPPER OVER scanDirTree (#1568). The walk, the skip rules and
// the mtime guard live in config_tree_scan.go, shared with the hierarchical
// scanner, so the two planes can no longer disagree about which files exist.
// What this function keeps is its projection: root-relative slash-path keys
// (bare names collide across directories — #1521), a hash-of-hashes
// composite in sorted-key order, and a byte cache holding only the files
// that need re-parsing (everything on a cold scan, changed/added otherwise).
// A missing root — or, since the merge, a root that is not a directory —
// stays a hard error, returned exactly as the walker words it: the caller
// treats it as "config dir unreadable", never as an empty config.
func scanDirFileHashes(dir string, oldHashes map[string]string, oldMtimes map[string]fileStat, logger *log.Logger) (map[string]string, string, map[string]fileStat, map[string][]byte, error) {
	scan, err := scanDirTree(dir, treeScanPriorFromFlat(oldHashes, oldMtimes), nil, logger)
	if err != nil {
		return nil, "", nil, nil, err
	}
	return scan.relHashes(), scan.composite, scan.relMtimes(), scan.dataCache(), nil
}

// treeScanPriorFromFlat rebuilds a prior from the flat plane's two cache
// maps so scanDirFileHashes keeps its historical fast-path semantics
// unchanged: nil when there is no hash cache (cold load: read + cache
// everything); otherwise one entry per cached hash. A key with a hash but
// no stat gets a stat no real file can have, so it can never take the
// fast-path but still counts as "known" for the cache-only-changed rule.
//
// ⚠️ A prior built this way carries NO tenant declarations, so the tenants
// product of the scan it feeds is lossy under the fast-path. The flat
// wrapper discards that product, and it is the ONLY caller: the manager
// feeds its retained *treeScan (flatScanState.tree), never a rebuilt one.
func treeScanPriorFromFlat(oldHashes map[string]string, oldMtimes map[string]fileStat) *treeScan {
	if oldHashes == nil {
		return nil
	}
	prior := &treeScan{files: make(map[string]*treeFile, len(oldHashes))}
	for k, h := range oldHashes {
		st, ok := oldMtimes[k]
		if !ok {
			st = fileStat{ModTime: -1, Size: -1}
		}
		prior.files[k] = &treeFile{relKey: k, hash: h, stat: st}
	}
	return prior
}

func (s *treeScan) absMtimes() map[string]fileStat {
	out := make(map[string]fileStat, len(s.files))
	for _, f := range s.files {
		out[f.absPath] = f.stat
	}
	return out
}

// scanDirHierarchical is the legacy form of the hierarchical walker: the
// package-level metrics + logger singletons. Kept for the tests that never
// cared about isolation; new tests use scanDirHierarchicalWithMetrics with
// freshMetrics(t).
func scanDirHierarchical(rootPath string, priorMtimes map[string]fileStat) (
	tenants map[string]string,
	defaults map[string]bool,
	hashes map[string]string,
	mtimes map[string]fileStat,
	graph *InheritanceGraph,
	err error,
) {
	return scanDirHierarchicalWithMetrics(rootPath, priorMtimes, getConfigMetrics(), log.Default())
}

// scanDirHierarchicalWithMetrics is the hierarchy plane's projection of
// one scanDirTree call: absolute Clean-path keys, and the duplicate-tenant
// conflict surfaced as the typed error its callers unwrap. The metric
// contract (scan duration observed on every call, parse failures counted,
// last-scan-complete stamped only on success) lives in scanDirTree.
// metrics / logger nil fall back to the package singletons, as the walker
// this projects always did.
//
// priorMtimes is ignored: a prior rebuilt from stats cannot carry tenant
// declarations, so the projection is always a cold scan.
func scanDirHierarchicalWithMetrics(rootPath string, priorMtimes map[string]fileStat, metrics *configMetrics, logger *log.Logger) (
	tenants map[string]string,
	defaults map[string]bool,
	hashes map[string]string,
	mtimes map[string]fileStat,
	graph *InheritanceGraph,
	err error,
) {
	if metrics == nil {
		metrics = getConfigMetrics()
	}
	if logger == nil {
		logger = log.Default()
	}
	_ = priorMtimes

	scan, err := scanDirTree(rootPath, nil, metrics, logger)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	if scan.conflict != nil {
		return nil, nil, nil, nil, nil, scan.conflict
	}
	return scan.tenants, scan.defaults, scan.absHashes(), scan.absMtimes(), scan.inheritanceGraph(), nil
}
