package main

// ============================================================
// The ONE conf.d tree walker (#1568, the #1911 family's end state)
// ============================================================
//
// Before this file the same conf.d tree was walked by two independent
// functions with two hand-written skip rules:
//
//	flat_scanner.go     scanDirFileHashes            → bytes, hashes, mtimes
//	config_hierarchy.go scanDirHierarchicalWithMetrics → tenants, defaults, graph
//
// Two enumerators over one tree is the defect CLASS (#1911): every cell of
// the skip rule — hidden dir, hidden file, extension case, `_` prefix,
// symlinked root, walk error, empty tree — had to be kept equal by hand, and
// the divergence audit (config_divergence.go) exists because it was not.
//
// scanDirTree is the single walk. It produces BOTH products in one pass.
// ⛔ It is also the ONLY walker production can call: the two historical
// functions survive as projections in scan_wrappers_test.go, so a second
// enumerator cannot be reintroduced on a manager path without moving a
// symbol out of a test file. Where the two walkers disagreed, the cell
// takes the HIERARCHICAL walker's answer: that one is the oracle of the
// cross-language name-classification matrix
// (confd_name_classification_parity_test.go), so /metrics and /effective
// now agree by construction rather than by audit.
//
// The hierarchy products are the Go port of the Python reference
// implementation (ADR-016; scripts/tools/dx/describe_tenant.py):
//
//	ConfDScanner._scan()          → scanDirTree (this file)
//	ConfDScanner.effective_config → computeMergedHash (config_inheritance.go)
//
// The port is a *semantic translation*: any divergence from
// describe_tenant.py that changes the 16-char merged_hash is a bug, and
// fixtures in tests/golden/ pin the expected hashes
// (config_golden_parity_test.go). Rules carried over from that reference:
// directories starting with '_' are NOT pruned (they may hold nested
// defaults), several tenants may live in one file, and the defaults chain
// is root-first with `.yaml` beating `.yml` at one level
// (config.CollectDefaultsChain). DuplicateTenantError, the typed
// cross-file duplicate-tenant error recorded on the scan, lives in
// pkg/config and is aliased into this package by config_types.go, so
// callers' errors.As unwrap it identically to a pkg/config one.
//
// ⛔ THE MTIME FAST-PATH CARRIES TENANT DECLARATIONS. Skipping the read of an
// unchanged file must not forget the tenants it declares: a scan that
// reused the hash but dropped `tenantIDs` would report the tenant as gone
// from the hierarchy while the flat plane still served it — the exact
// divergence shape this file exists to close, reintroduced by an
// optimisation. `prior` is therefore a *treeScan, not a map of stats, and
// the carry is pinned by config_tree_scan_test.go.

import (
	"crypto/sha256"
	"fmt"
	"io/fs"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// treeScanMtimeGuard is the safety window for coarse-mtime filesystems: a
// file modified within it is always re-read, even when its stat matches the
// prior scan, because the mtime alone cannot prove the bytes are unchanged.
const treeScanMtimeGuard = 2 * time.Second

// treeFile is one YAML file the walk kept.
type treeFile struct {
	absPath string // Clean absolute path under the RESOLVED root (hierarchy key)
	relKey  string // root-relative slash path (flat key)
	hash    string // full SHA-256 hex
	stat    fileStat
	// data holds the file's bytes only when this scan READ the file AND the
	// caller has no prior hash for it or the hash moved. A file read only
	// because it was too young for the mtime guard, whose hash then matched
	// the prior, is NOT cached — the flat plane's cache has always held "what
	// needs re-parsing", not "what was read".
	data []byte
	// tenantIDs is sorted; nil for `_`-prefixed, unparseable or tenant-less
	// files. ⛔ IMMUTABLE once parseTenantDecls returns it: a carried file
	// shares the prior's slice (no copy), so nothing may append to or
	// reorder it. Readers range over it or hand it to sortedTenantIDs.
	tenantIDs  []string
	isDefaults bool // basename folds to `_defaults.yaml` / `_defaults.yml`
	reused     bool // hash + tenantIDs came from prior (mtime fast-path)
	// parsed records that THIS scan ran parseTenantDecls on the file's
	// bytes. False when the declarations were carried from the prior — by
	// the mtime fast-path, or because the file was read (too young for the
	// guard, or stat mismatch) and hashed IDENTICAL to the prior. That second
	// carry is load-bearing for cost: a tree whose files are younger than
	// treeScanMtimeGuard is read on every tick, and re-parsing 1000 unchanged
	// files each time is the parse cost the bench gate flagged. Declarations
	// are a function of the bytes, and the hash is the bytes.
	parsed bool
	// parseFailed records that the tenant-declaration parse of this file's
	// bytes failed. ⛔ A file in this state NEVER takes the mtime fast-path:
	// reusing its prior would silence da_config_parse_failure_total after the
	// one tick that read it, while the last-scan-complete gauge kept
	// stamping — the ConfigParseFailure rule (`increase(...[1h]) > 5`) would
	// then never fire for a file that is broken on every tick. Re-reading a
	// broken file each tick keeps the counter's meaning identical to the
	// walkers this one replaced; broken files are rare, so the cost is nil.
	parseFailed bool
}

// treeScan is everything one walk of the conf.d tree yields.
type treeScan struct {
	absRoot   string
	files     map[string]*treeFile // relKey → file
	keys      []string             // relKeys sorted; the composite-hash order
	composite string               // SHA-256 over the per-file hashes in `keys` order

	// Hierarchy products. When conflict is non-nil the tenants map and the
	// graph are nil: a duplicate tenant across files is a rejected
	// configuration, not a graph with one edge fewer.
	tenants  map[string]string // tenantID → absPath
	defaults map[string]bool   // absPath → true
	conflict *DuplicateTenantError

	// graph is built on first use (inheritanceGraph), not by the walk: the
	// flat plane's paths (IncrementalLoad, the flat branch of detectChange)
	// never read it, and building it on every quiet tick was ~1 ms and ~2k
	// allocs the bench gate charged to IncrementalLoad_1000_NoChange_MtimeGuard.
	// tenants and defaults are immutable once the walk returns, so the lazy
	// build is a pure function of the scan and sync.Once makes it safe for
	// concurrent readers of a retained scan.
	graphOnce sync.Once
	graph     *InheritanceGraph
}

// inheritanceGraph returns the scan's inheritance graph, building it on the
// first call. Nil when the scan has a conflict (no tenants were attributed).
// Chains are cached per directory because tenants in one directory share a
// chain; iteration is sorted so the graph's slices are stable across scans
// (debounce batching relies on the order).
func (s *treeScan) inheritanceGraph() *InheritanceGraph {
	s.graphOnce.Do(func() {
		if s.conflict != nil {
			return
		}
		g := NewInheritanceGraph()
		chainCache := make(map[string][]string)
		tenantIDs := make([]string, 0, len(s.tenants))
		for tid := range s.tenants {
			tenantIDs = append(tenantIDs, tid)
		}
		sort.Strings(tenantIDs)
		for _, tid := range tenantIDs {
			dir := filepath.Dir(s.tenants[tid])
			chain, cached := chainCache[dir]
			if !cached {
				chain = config.CollectDefaultsChain(dir, s.absRoot, s.defaults)
				chainCache[dir] = chain
			}
			g.AddTenant(tid, chain)
		}
		s.graph = g
	})
	return s.graph
}

// scanDirTree walks root once and returns both the flat products (per-file
// hashes keyed by root-relative path, composite hash, mtimes, byte cache)
// and the hierarchy products (tenant sources, defaults files, inheritance
// graph).
//
// Rules (one answer per cell; the hierarchical walker's where they differed):
//   - root is absolutised, cleaned and symlink-resolved via absScanRoot; a
//     missing root or a root that is not a directory is an error.
//   - a walk error is logged and the walk continues (never SkipDir).
//   - directories whose name starts with '.' are pruned whole (never the
//     root); files whose name starts with '.' are skipped.
//   - only files whose lower-cased name ends in `.yaml` / `.yml` are kept.
//   - an entry whose stat or read fails is logged and dropped from every map.
//   - `_`-prefixed files are hashed but never parsed for tenants; the ones
//     folding to `_defaults.yaml`/`.yml` are entered in `defaults`.
//   - every other kept file is parsed for its top-level `tenants:` keys; a
//     parse failure is logged, counted on metrics (when non-nil) and drops
//     the file from `tenants` only — it stays hashed and watched.
//   - the same tenant declared in two files is a conflict (see treeScan).
//
// prior enables the mtime fast-path: a file whose ModTime+Size equal the
// prior's and that is older than treeScanMtimeGuard reuses the prior's hash
// AND tenantIDs without being read. prior may be nil (cold scan: every file
// is read and cached).
//
// metrics may be nil: parse failures are then logged but not counted and no
// scan metric is touched. This is the flat wrapper's historical contract;
// every manager path passes m.getMetrics(). When metrics is non-nil the
// scan carries the hierarchical scanner's metric contract, moved here so
// the paths that call the walker directly keep it: the scan duration is
// observed on EVERY call including error returns (a scan that errors out
// fast is itself useful signal), and the last-scan-complete gauge is
// stamped ONLY on a clean success — never on an error and never on a
// duplicate-tenant conflict, so a rejected tree cannot look like a
// completed scan. logger nil falls back to log.Default().
func scanDirTree(root string, prior *treeScan, metrics *configMetrics, logger *log.Logger) (*treeScan, error) {
	if logger == nil {
		logger = log.Default()
	}
	if metrics != nil {
		defer metrics.ObserveScanDuration()()
	}
	scan, err := walkDirTree(root, prior, metrics, logger)
	if err != nil {
		return nil, err
	}
	if scan.conflict == nil && metrics != nil {
		metrics.SetLastScanComplete(time.Now())
	}
	return scan, nil
}

// walkDirTree is scanDirTree without the metric contract: the walk, the
// hash, the classification and the hierarchy products.
func walkDirTree(root string, prior *treeScan, metrics *configMetrics, logger *log.Logger) (*treeScan, error) {
	absRoot := absScanRoot(root)

	info, serr := os.Stat(absRoot)
	if serr != nil {
		return nil, fmt.Errorf("stat %q: %w", absRoot, serr)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("%q is not a directory", absRoot)
	}

	// Phase 1: enumerate. Stats come from the DirEntry so no extra os.Stat
	// per file; the read is deferred to phase 2 so the fast-path can skip it.
	type entry struct {
		abs  string
		rel  string
		name string
		info os.FileInfo
	}
	var entries []entry
	walkErr := filepath.WalkDir(absRoot, func(path string, d fs.DirEntry, werr error) error {
		if werr != nil {
			logger.Printf("WARN: walk error at %s: %v", path, werr)
			return nil
		}
		name := d.Name()
		if d.IsDir() {
			if path != absRoot && strings.HasPrefix(name, ".") {
				return fs.SkipDir
			}
			return nil
		}
		if strings.HasPrefix(name, ".") {
			return nil
		}
		lower := strings.ToLower(name)
		if !strings.HasSuffix(lower, ".yaml") && !strings.HasSuffix(lower, ".yml") {
			return nil
		}
		entryInfo, ierr := d.Info()
		if ierr != nil {
			logger.Printf("WARN: cannot stat %s: %v", path, ierr)
			return nil
		}
		rel, rerr := filepath.Rel(absRoot, path)
		if rerr != nil {
			logger.Printf("WARN: cannot relativise %s: %v", path, rerr)
			return nil
		}
		entries = append(entries, entry{abs: filepath.Clean(path), rel: filepath.ToSlash(rel), name: name, info: entryInfo})
		return nil
	})
	if walkErr != nil {
		return nil, fmt.Errorf("walk %q: %w", absRoot, walkErr)
	}

	scan := &treeScan{
		absRoot:  absRoot,
		files:    make(map[string]*treeFile, len(entries)),
		keys:     make([]string, 0, len(entries)),
		defaults: make(map[string]bool),
	}

	// Phase 2: hash + classify, in WALK order (the duplicate-tenant error
	// names the first two files in that order, as it always has).
	var walkOrder []*treeFile
	for _, e := range entries {
		lower := strings.ToLower(e.name)
		f := &treeFile{
			absPath:    e.abs,
			relKey:     e.rel,
			stat:       fileStat{ModTime: e.info.ModTime().UnixNano(), Size: e.info.Size()},
			isDefaults: strings.HasPrefix(e.name, "_") && (lower == "_defaults.yaml" || lower == "_defaults.yml"),
		}

		var pf *treeFile
		if prior != nil {
			pf = prior.files[e.rel]
		}
		if pf != nil && pf.hash != "" && !pf.parseFailed && pf.stat == f.stat && time.Since(e.info.ModTime()) > treeScanMtimeGuard {
			f.hash = pf.hash
			// ⛔ THE CARRY. Reusing the hash without the declarations would
			// make the tenant vanish from the hierarchy on every quiet tick.
			// The slice is SHARED, not copied: tenantIDs is never mutated
			// after parseTenantDecls builds it (see the field's doc), and a
			// copy per file was 1000 allocs on every quiet tick of a
			// 1000-tenant tree.
			f.tenantIDs = pf.tenantIDs
			f.reused = true
		} else {
			data, rerr := os.ReadFile(e.abs)
			if rerr != nil {
				logger.Printf("WARN: cannot read %s: %v", e.abs, rerr)
				continue
			}
			f.hash = fmt.Sprintf("%x", sha256.Sum256(data))
			if pf == nil || pf.hash != f.hash {
				f.data = data
			}
			switch {
			case strings.HasPrefix(e.name, "_"):
				// Never parsed for tenants.
			case pf != nil && pf.hash == f.hash && !pf.parseFailed:
				// Same bytes as the prior: the declarations cannot differ, so
				// carry them instead of parsing again. A prior that failed to
				// parse is excluded on purpose — it must be re-parsed (and
				// re-counted) every scan, see parseFailed. Shared, not copied
				// (immutable once built).
				f.tenantIDs = pf.tenantIDs
			default:
				f.tenantIDs, f.parseFailed = parseTenantDecls(e.abs, data, metrics, logger)
				f.parsed = true
			}
		}

		scan.files[e.rel] = f
		scan.keys = append(scan.keys, e.rel)
		walkOrder = append(walkOrder, f)
		if f.isDefaults {
			scan.defaults[e.abs] = true
		}
	}

	// Composite: hash-of-hashes in sorted key order, the flat plane's
	// change-detection identity since v2.1.0.
	sort.Strings(scan.keys)
	compositeHasher := sha256.New()
	for _, k := range scan.keys {
		compositeHasher.Write([]byte(scan.files[k].hash))
	}
	scan.composite = fmt.Sprintf("%x", compositeHasher.Sum(nil))

	// Tenant attribution + cross-file duplicate detection (#127 guardrail:
	// silently preferring one file would mask config drift).
	tenants := make(map[string]string)
	for _, f := range walkOrder {
		for _, tid := range f.tenantIDs {
			if prev, exists := tenants[tid]; exists && prev != f.absPath {
				scan.conflict = &DuplicateTenantError{TenantID: tid, PathA: prev, PathB: f.absPath}
				return scan, nil
			}
			tenants[tid] = f.absPath
		}
	}
	scan.tenants = tenants
	// The inheritance graph is NOT built here — see inheritanceGraph.
	return scan, nil
}

// parseTenantDecls extracts the top-level `tenants:` keys of one tenant
// file. It deliberately decodes only the shape it needs — the full config is
// parsed by the plane that consumes it — so a large tree stays cheap to scan.
// Returns (nil, true) for an unparseable file (logged, counted when
// metrics is non-nil) and (nil, false) for a file without a `tenants:`
// mapping (a commented-out placeholder is not an error).
func parseTenantDecls(absPath string, data []byte, metrics *configMetrics, logger *log.Logger) (ids []string, failed bool) {
	var doc struct {
		Tenants map[string]yaml.Node `yaml:"tenants"`
	}
	if perr := yaml.Unmarshal(data, &doc); perr != nil {
		logger.Printf("WARN: cannot parse %s: %v", absPath, perr)
		if metrics != nil {
			// Basename, not full path, to cap label cardinality (A-8d).
			metrics.IncParseFailure(filepath.Base(absPath))
		}
		return nil, true
	}
	if len(doc.Tenants) == 0 {
		return nil, false
	}
	ids = make([]string, 0, len(doc.Tenants))
	for tid := range doc.Tenants {
		ids = append(ids, tid)
	}
	sort.Strings(ids)
	return ids, false
}

// relHashes / relMtimes / dataCache are the flat plane's projections (keys
// are root-relative slash paths). Fresh maps every call: the manager keeps
// the previous scan's maps as the next prior and must not see them mutate.
func (s *treeScan) relHashes() map[string]string {
	out := make(map[string]string, len(s.files))
	for k, f := range s.files {
		out[k] = f.hash
	}
	return out
}

func (s *treeScan) relMtimes() map[string]fileStat {
	out := make(map[string]fileStat, len(s.files))
	for k, f := range s.files {
		out[k] = f.stat
	}
	return out
}

func (s *treeScan) dataCache() map[string][]byte {
	out := make(map[string][]byte)
	for k, f := range s.files {
		if f.data != nil {
			out[k] = f.data
		}
	}
	return out
}

// releaseData drops the cached bytes once the plane that needed them has
// parsed. The manager retains the scan as the next prior, and the prior
// reads only hash, stat and tenantIDs — keeping a cold load's bytes alive
// until the first reload would be a silent retention the cache never had.
func (s *treeScan) releaseData() {
	for _, f := range s.files {
		f.data = nil
	}
}

// absHashes is the hierarchy plane's projection (keys are Clean absolute
// paths under the resolved root).
func (s *treeScan) absHashes() map[string]string {
	out := make(map[string]string, len(s.files))
	for _, f := range s.files {
		out[f.absPath] = f.hash
	}
	return out
}
