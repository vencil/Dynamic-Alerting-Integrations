package config

// ============================================================
// The ONE conf.d tree walker (#1568, the #1911 family's end state)
// ============================================================
//
// Before this walker the same conf.d tree was walked by two independent
// functions with two hand-written skip rules in the exporter's package main:
// a FLAT scanner (bytes, hashes, mtimes) and a HIERARCHICAL scanner
// (tenants, defaults, graph). Both are gone from production; their names
// survive only as test projections onto this walk in
// app/scan_wrappers_test.go.
//
// Two enumerators over one tree is the defect CLASS (#1911): every cell of
// the skip rule — hidden dir, hidden file, extension case, `_` prefix,
// symlinked root, walk error, empty tree — had to be kept equal by hand, and
// the divergence audit (app/config_divergence.go) exists because it was not.
//
// ScanDirTree is the single walk. It produces BOTH products in one pass.
// ⛔ It is the only RECURSIVE conf.d walker in the exporter module's
// production paths — pinned by app/confd_walker_population_test.go, which
// fails on any other production function that references a stdlib DIRECT
// directory-listing entry point (see that file's NOT GUARDED list;
// cmd/da-batchpr's walkFilesDir is excluded by name: it walks a PR payload,
// not conf.d). (Not the only reader of conf.d in the repo: tenant-api
// lists it with its own non-recursive os.ReadDir — internal/handler/
// tenant_list.go, internal/confd/resolve.go, internal/federation/orphan/
// detector.go — and describe_tenant.py walks it in Python.) The
// exporter's manager (package main) reaches it through the one-line adapter
// `scanDirTree` (app/config_tree_scan.go), and since W2 (#1677) the two
// library readers in THIS package consume its product instead of walking on
// their own — ResolveEffective (hierarchy.go; tenant-api's /effective) and
// ScopeEffective (scope.go; cmd/da-guard) each run exactly one ScanDirTree
// and read tenants through TreeScan.Locate, defaults through
// TreeScan.Defaults, and bytes through TreeFile.Data.
// Where the two historical walkers disagreed, the cell takes the
// HIERARCHICAL walker's answer: that one is the oracle of the cross-language
// name-classification matrix (app/confd_name_classification_parity_test.go),
// so /metrics and /effective agree by construction rather than by audit.
//
// The defaults CHAIN is one rule too since #1674 (B8): the walker classifies
// carriers case-folded (confdname.IsDefaults, so `_DEFAULTS.YML` is one), and
// every chain — the exporter's graph, ResolveEffective, ScopeEffective, the
// flat plane's root Defaults — picks one carrier per directory from that set
// through TreeScan.DefaultsCarriers (SelectDefaultsCarriers). Before, the
// graph matched only the two exact lower-case names while ResolveEffective
// folded case, and the flat plane merged every root `_` file's defaults.
// tree_scan_parity_test.go pins, row by row, where the three planes agree
// and where they still diverge, so neither side can move silently.
//
// The hierarchy products are the Go port of the Python reference
// implementation (ADR-016; scripts/tools/dx/describe_tenant.py):
//
//	ConfDScanner._scan()          → ScanDirTree (this file)
//	ConfDScanner.effective_config → computeMergedHash (app/config_inheritance.go)
//
// The port is a *semantic translation*: any divergence from
// describe_tenant.py that changes the 16-char merged_hash is a bug, and
// fixtures in tests/golden/ pin the expected hashes
// (app/config_golden_parity_test.go). Rules carried over from that
// reference: directories starting with '_' are NOT pruned (they may hold
// nested defaults), several tenants may live in one file, and the defaults
// chain is root-first with ONE carrier per level (SelectDefaultsCarriers:
// `.yaml` beats `.yml`, case-folded). DuplicateTenantError (errors.go) is the typed
// cross-file duplicate-tenant error recorded on the scan; package main
// aliases it, so callers' errors.As unwrap it identically.
//
// ⛔ THE MTIME FAST-PATH CARRIES TENANT DECLARATIONS. Skipping the read of an
// unchanged file must not forget the tenants it declares: a scan that
// reused the hash but dropped `TenantIDs` would report the tenant as gone
// from the hierarchy while the flat plane still served it — the exact
// divergence shape this file exists to close, reintroduced by an
// optimisation. `prior` is therefore a *TreeScan, not a map of stats, and
// the carry is pinned by app/config_tree_scan_test.go.
//
// ⛔ EXPORTED FIELDS ARE READ-ONLY TO CALLERS. The fields are exported only
// because the exporter's package main reads them (and its tests build
// priors); a TreeScan is a value produced by the walk, and the next scan
// reuses it as its prior. Mutating one after the walk returns — above all
// appending to a TreeFile's TenantIDs, which is shared across scans — is a
// bug even though the compiler allows it.

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

	"github.com/vencil/threshold-exporter/internal/confdname"
)

// TreeScanMtimeGuard is the safety window for coarse-mtime filesystems: a
// file modified within it is always re-read, even when its stat matches the
// prior scan, because the mtime alone cannot prove the bytes are unchanged.
const TreeScanMtimeGuard = 2 * time.Second

// FileStat is the mtime fast-path's identity of one file: ModTime+Size of
// the file the entry NAMES — the directory entry's own stat for a regular
// file, the TARGET's stat for a symlink (#1969; see walkDirTree, and
// TreeFile.LinkStat for the link's own half).
// Zero-value-comparable on purpose (the fast-path compares with ==).
type FileStat struct {
	ModTime int64 // UnixNano
	Size    int64
}

// sameLinkStat reports whether two TreeFile.LinkStat values agree: both nil
// (two regular files), or both set and equal. A regular file replaced by a
// symlink (or back) is therefore a change.
func sameLinkStat(a, b *FileStat) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

// guardMtime is the mtime the fast-path's TreeScanMtimeGuard ages: the
// file's own for a regular file, the NEWER of target and link for a symlink
// (both must be older than the guard; see walkDirTree).
func guardMtime(st FileStat, link *FileStat) int64 {
	if link == nil {
		return st.ModTime
	}
	return max(st.ModTime, link.ModTime)
}

// ScanObserver receives the walker's metric events. The exporter's
// *configMetrics implements it.
//
// ⛔ A nil ScanObserver means "no metrics": parse failures are logged but not
// counted and no scan metric is touched. Go's typed-nil rule makes this
// fragile at the call site — a nil *T stored in the interface is NOT == nil,
// so ScanDirTree would call methods on a nil receiver. Callers holding a
// possibly-nil pointer must convert it to a true nil interface first
// (package main does, in its scanDirTree adapter), or make its methods
// nil-receiver safe (*configMetrics does both; pinned by
// app's TestScanDirTree_NilConfigMetrics).
type ScanObserver interface {
	// ObserveScanElapsed records one scan's wall-clock duration.
	//
	// ⚠️ Deliberately NOT the `ObserveScanDuration() func()` shape main's
	// *configMetrics used to have (removed in #1941): a closure returned
	// through an interface
	// call cannot be inlined, so it escaped to the heap — measured +1
	// allocs/op on every scan, the warm fast-path included
	// (ScanDirTree_100_Warm 871→872, IncrementalLoad_1000_NoChange_MtimeGuard
	// 8096→8097). ScanDirTree takes t0 itself and hands over the elapsed
	// time, which keeps the fast path at the pre-#1941 allocation count.
	ObserveScanElapsed(d time.Duration)
	// SetLastScanComplete stamps the last-clean-scan gauge.
	SetLastScanComplete(t time.Time)
	// IncParseFailure counts one tenant-declaration parse failure, labelled
	// by file basename.
	IncParseFailure(fileBasename string)
}

// TreeFile is one YAML file the walk kept.
type TreeFile struct {
	AbsPath string // Clean absolute path under the RESOLVED root (hierarchy key)
	RelKey  string // root-relative slash path (flat key)
	Hash    string // full SHA-256 hex
	Stat    FileStat
	// LinkStat is the link's OWN lstat (ModTime+Size) when the entry is a
	// symlink, nil for a regular file (#1969): Stat is then the target's,
	// and the fast-path needs BOTH unchanged. A pointer, not a second
	// FileStat, so a regular file's TreeFile stays in its allocation size
	// class (the warm-path benches' B/op).
	LinkStat *FileStat
	// Data holds the file's bytes only when this scan READ the file AND the
	// caller has no prior hash for it or the hash moved. A file read only
	// because it was too young for the mtime guard, whose hash then matched
	// the prior, is NOT cached — the flat plane's cache has always held "what
	// needs re-parsing", not "what was read".
	Data []byte
	// TenantIDs is sorted; nil for `_`-prefixed, unparseable or tenant-less
	// files. ⛔ IMMUTABLE once parseTenantDecls returns it: a carried file
	// shares the prior's slice (no copy), so nothing may append to or
	// reorder it. Readers range over it or hand it to a sorting copy.
	TenantIDs  []string
	IsDefaults bool // confdname.IsDefaults(basename): lower-cases to `_defaults.yaml` / `.yml`
	Reused     bool // Hash + TenantIDs came from prior (mtime fast-path)
	// Parsed records that THIS scan ran parseTenantDecls on the file's
	// bytes. False when the declarations were carried from the prior — by
	// the mtime fast-path, or because the file was read (too young for the
	// guard, or stat mismatch) and hashed IDENTICAL to the prior. That second
	// carry is load-bearing for cost: a tree whose files are younger than
	// TreeScanMtimeGuard is read on every tick, and re-parsing 1000 unchanged
	// files each time is the parse cost the bench gate flagged. Declarations
	// are a function of the bytes, and the hash is the bytes.
	Parsed bool
	// ParseFailed records that the tenant-declaration parse of this file's
	// bytes failed. ⛔ A file in this state NEVER takes the mtime fast-path:
	// reusing its prior would silence da_config_parse_failure_total after the
	// one tick that read it, while the last-scan-complete gauge kept
	// stamping — the ConfigParseFailure rule (`increase(...[1h]) > 5`) would
	// then never fire for a file that is broken on every tick. Re-reading a
	// broken file each tick keeps the counter's meaning identical to the
	// walkers this one replaced; broken files are rare, so the cost is nil.
	ParseFailed bool
}

// TreeScan is everything one walk of the conf.d tree yields.
type TreeScan struct {
	AbsRoot   string
	Files     map[string]*TreeFile // RelKey → file
	Keys      []string             // RelKeys sorted; the composite-hash order
	Composite string               // SHA-256 over the per-file hashes in `Keys` order

	// Hierarchy products. When Conflict is non-nil the Tenants map and the
	// graph are nil: a duplicate tenant across files is a rejected
	// configuration, not a graph with one edge fewer. That is the EXPORTER's
	// whole-tree verdict; the per-tenant view that survives a conflict (what
	// /effective and da-guard answer) is Locate.
	Tenants  map[string]string // tenantID → AbsPath
	Defaults map[string]bool   // AbsPath → true
	// Conflict is the FIRST cross-file duplicate in walk order (PathA/PathB
	// are the first two declaring files for that tenant, in walk order).
	Conflict *DuplicateTenantError

	// attrib is every tenant's FIRST declaring file in walk order, recorded
	// even when the tree has a conflict. When Conflict is nil it is the very
	// map exported as Tenants (no second allocation on the clean path).
	// dups holds, per tenant declared in more than one file, that tenant's
	// own first two declaring files; nil unless the tree has a conflict.
	// Unexported and read only through Locate, so the exported contract
	// ("Tenants is nil under a conflict") is unchanged. Immutable once the
	// walk returns.
	attrib map[string]string
	dups   map[string]*DuplicateTenantError

	// graph is built on first use (InheritanceGraph), not by the walk: the
	// flat plane's paths (IncrementalLoad, the flat branch of detectChange)
	// never read it, and building it on every quiet tick was ~1 ms and ~2k
	// allocs the bench gate charged to IncrementalLoad_1000_NoChange_MtimeGuard.
	// Tenants and Defaults are immutable once the walk returns, so the lazy
	// build is a pure function of the scan and sync.Once makes it safe for
	// concurrent readers of a retained scan.
	graphOnce sync.Once
	graph     *InheritanceGraph

	// carriers is the per-directory defaults selection, built on first use
	// for the same reason and under the same immutability as graph.
	carriersOnce sync.Once
	carriers     DefaultsCarriers
}

// DefaultsCarriers returns SelectDefaultsCarriers over the scan's Defaults,
// computed once per scan. It is the chain rule every plane reads — the
// graph below, ResolveEffective / ScopeEffective, and the exporter's flat
// root Defaults — so they cannot pick different files for one directory.
// Answered under a conflict too (the per-tenant readers need it there).
func (s *TreeScan) DefaultsCarriers() DefaultsCarriers {
	s.carriersOnce.Do(func() {
		s.carriers = SelectDefaultsCarriers(s.Defaults)
	})
	return s.carriers
}

// InheritanceGraph returns the scan's inheritance graph, building it on the
// first call. Nil when the scan has a conflict (no tenants were attributed).
// Chains are cached per directory because tenants in one directory share a
// chain; iteration is sorted so the graph's slices are stable across scans
// (debounce batching relies on the order).
func (s *TreeScan) InheritanceGraph() *InheritanceGraph {
	s.graphOnce.Do(func() {
		if s.Conflict != nil {
			return
		}
		g := NewInheritanceGraph()
		byDir := s.DefaultsCarriers().ByDir
		chainCache := make(map[string][]string)
		tenantIDs := make([]string, 0, len(s.Tenants))
		for tid := range s.Tenants {
			tenantIDs = append(tenantIDs, tid)
		}
		sort.Strings(tenantIDs)
		for _, tid := range tenantIDs {
			dir := filepath.Dir(s.Tenants[tid])
			chain, cached := chainCache[dir]
			if !cached {
				chain = chainFromCarriers(dir, s.AbsRoot, byDir, nativePathOps)
				chainCache[dir] = chain
			}
			g.AddTenant(tid, chain)
		}
		s.graph = g
	})
	return s.graph
}

// ScanDirTree walks root once and returns both the flat products (per-file
// hashes keyed by root-relative path, composite hash, mtimes, byte cache)
// and the hierarchy products (tenant sources, defaults files, inheritance
// graph).
//
// Rules (one answer per cell; the hierarchical walker's where they differed):
//   - root is absolutised, cleaned and symlink-resolved via AbsScanRoot; a
//     missing root or a root that is not a directory is an error.
//   - a walk error is logged and the walk continues (never SkipDir).
//   - directories whose name starts with '.' are pruned whole (never the
//     root); files whose name starts with '.' are skipped.
//   - only files whose lower-cased name ends in `.yaml` / `.yml` are kept.
//   - an entry whose stat or read fails is logged and dropped from every map;
//     a symlinked file is statted through its target too, so a dangling
//     link is dropped here (#1969).
//   - `_`-prefixed files are hashed but never parsed for tenants; the ones
//     confdname.IsDefaults accepts are entered in `Defaults` (every spelling;
//     which ONE a directory's chain reads is DefaultsCarriers' question).
//   - every other kept file is parsed for its top-level `tenants:` keys; a
//     parse failure is logged, counted on obs (when non-nil) and drops
//     the file from `Tenants` only — it stays hashed and watched.
//   - the same tenant declared in two files is a conflict (see TreeScan):
//     the whole-tree verdict (Conflict, nil Tenants) is the exporter's, the
//     per-tenant verdict (Locate) is the read-only diagnostics'.
//
// prior enables the mtime fast-path: a file whose FileStat equals the
// prior's (for a symlink: the link's lstat AND the target's stat) and that
// is older than TreeScanMtimeGuard reuses the prior's hash
// AND TenantIDs without being read. prior may be nil (cold scan: every file
// is read and cached).
//
// obs may be nil (a TRUE nil interface — see ScanObserver for the typed-nil
// trap): parse failures are then logged but not counted and no scan metric
// is touched. This is the flat wrapper's historical contract; every manager
// path passes m.getMetrics(). When obs is non-nil the scan carries the
// hierarchical scanner's metric contract, moved here so the paths that call
// the walker directly keep it: the scan duration is observed on EVERY call
// including error returns (a scan that errors out fast is itself useful
// signal), and the last-scan-complete gauge is stamped ONLY on a clean
// success — never on an error and never on a duplicate-tenant conflict, so
// a rejected tree cannot look like a completed scan. logger nil falls back
// to log.Default().
func ScanDirTree(root string, prior *TreeScan, obs ScanObserver, logger *log.Logger) (*TreeScan, error) {
	if logger == nil {
		logger = log.Default()
	}
	if obs != nil {
		t0 := time.Now()
		defer func() { obs.ObserveScanElapsed(time.Since(t0)) }()
	}
	scan, err := walkDirTree(root, prior, obs, logger, walkFull)
	if err != nil {
		return nil, err
	}
	if scan.Conflict == nil && obs != nil {
		obs.SetLastScanComplete(time.Now())
	}
	return scan, nil
}

// walkMode selects how much of the tree walkDirTree visits. Unexported on
// purpose: the only non-full caller is scanRootDefaults in this package.
type walkMode int

const (
	// walkFull is ScanDirTree: the whole tree, every kept file hashed,
	// tenant files parsed for their declarations.
	walkFull walkMode = iota
	// walkRootCarriers visits ONLY the root directory and keeps ONLY the
	// entries the full walk would classify as defaults carriers there.
	walkRootCarriers
)

// scanRootDefaults is the root-only, carriers-only mode of the ONE walker
// (#1674 round 2). It exists for callers that need exactly one answer —
// "which defaults carrier does the chain read at the root, and what are its
// bytes" (MergeTenantWithRootDefaults, i.e. tenant-api GET / validate / the
// gitops write gate) — and must not pay a full walk for it: measured on a
// 1000-file tree, a full ScanDirTree per call was ~100x main's cost, and the
// gitops writer validates inside its single-writer token.
//
// ⛔ It is walkDirTree with a mode, NOT a second lister: the root's entries
// go through the same hidden / extension / stat / read / classification code
// as a full scan, so readability and carrier selection
// (TreeScan.DefaultsCarriers) cannot drift from what the exporter reads.
// confd_walker_population_test pins walkDirTree as the only listing site.
// The returned scan has no tenants; only Files/Keys/Defaults and
// DefaultsCarriers are meaningful.
func scanRootDefaults(root string) (*TreeScan, error) {
	return walkDirTree(root, nil, nil, discardLogger, walkRootCarriers)
}

// walkDirTree is ScanDirTree without the metric contract: the walk, the
// hash, the classification and the hierarchy products. mode is walkFull for
// every production caller except scanRootDefaults.
func walkDirTree(root string, prior *TreeScan, obs ScanObserver, logger *log.Logger, mode walkMode) (*TreeScan, error) {
	absRoot := AbsScanRoot(root)

	info, serr := os.Stat(absRoot)
	if serr != nil {
		return nil, fmt.Errorf("stat %q: %w", absRoot, serr)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("%q is not a directory", absRoot)
	}

	// Phase 1: enumerate. Stats come from the DirEntry so no extra os.Stat
	// per regular file (a symlink is statted through its target, below);
	// the read is deferred to phase 2 so the fast-path can skip it.
	// name is not stored: it is rel's last element (phase 2 slices it), so
	// the struct stays at its pre-#1969 size and the warm path's B/op with it.
	type entry struct {
		abs  string
		rel  string
		stat FileStat
		link *FileStat // symlinks only; see TreeFile.LinkStat
	}
	var entries []entry
	walkErr := filepath.WalkDir(absRoot, func(path string, d fs.DirEntry, werr error) error {
		if werr != nil {
			logger.Printf("WARN: walk error at %s: %v", path, werr)
			return nil
		}
		name := d.Name()
		if d.IsDir() {
			if path != absRoot && (mode == walkRootCarriers || strings.HasPrefix(name, ".")) {
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
		if mode == walkRootCarriers && !confdname.IsDefaults(name) {
			return nil
		}
		// ⛔ A SYMLINKED FILE IS JUDGED BY ITS LINK *AND* ITS TARGET (#1969).
		// d.Info() is the entry's LSTAT: for a symlink that is the link's own
		// mtime and size, which never move when the target does — a K8s
		// ConfigMap volume swaps `..data` to a new payload directory and
		// leaves every `key -> ..data/key` link untouched, and an in-place
		// edit of a link's target touches no link either. The mtime
		// fast-path below compared those link stats, matched, and carried
		// the PRIOR hash on every tick: a ConfigMap `..data` swap never
		// reloaded. The target's stat alone is not enough either: retargeting
		// the link itself (`ln -sfn`, or an atomic rename of a new link over
		// it) to another, already-written file whose size and mtime equal
		// the old target's leaves the target stat unchanged — only the
		// link's lstat moves. So a symlinked entry carries BOTH — Stat is
		// the target's (os.Stat), LinkStat the link's (d.Info()) — and the
		// fast-path needs both unchanged. Only symlinks pay the extra
		// syscall; ordinary entries keep d.Info() alone (the warm-path
		// benches pin that).
		//
		// The guard's age is the NEWER of the two mtimes, i.e. both must be
		// older than TreeScanMtimeGuard. The guard exists because an mtime
		// inside the filesystem's resolution window cannot prove the bytes
		// settled; that is true of whichever side moved last, so the
		// conservative reading is the one that re-reads when either is young.
		//
		// A target that cannot be statted (dangling link, permission) drops
		// the entry with a WARN, exactly like a read failure in phase 2: the
		// file is in no map, so a dangling defaults carrier is not a
		// candidate for DefaultsCarriers and the next spelling in the
		// directory is selected instead of the unreadable one keeping its
		// prior hash. The WARN repeats on every tick while the link dangles,
		// the same cadence as the phase-2 read-failure WARN.
		//
		// Residual gap, left open on purpose: two ModTime+Size pairs are not
		// identity. A change is missed only when BOTH the link's lstat and
		// the target's stat come out equal to the prior's — e.g. a `..data`
		// swap (links untouched) to a payload file with the same size and the
		// same mtime (to the filesystem's resolution) as the old one. What
		// protects the kubelet path is that kubelet writes the new payload at
		// swap time, so the target's mtime differs from the old file's and
		// the stat comparison fails. (The guard does not help there: the
		// default -reload-interval is 30s against a 2s TreeScanMtimeGuard, so
		// the tick that sees a swap almost never falls inside it.) dev+inode
		// would close the gap, but they live in FileInfo.Sys() —
		// *syscall.Stat_t on unix, no inode on Windows — so it needs a
		// per-platform build-tagged file; not done.
		var st FileStat
		var link *FileStat
		if d.Type()&fs.ModeSymlink != 0 {
			linkInfo, lerr := d.Info()
			if lerr != nil {
				logger.Printf("WARN: cannot stat %s: %v", path, lerr)
				return nil
			}
			targetInfo, terr := os.Stat(path)
			if terr != nil {
				logger.Printf("WARN: cannot stat symlink target of %s (dropped): %v", path, terr)
				return nil
			}
			st = FileStat{ModTime: targetInfo.ModTime().UnixNano(), Size: targetInfo.Size()}
			link = &FileStat{ModTime: linkInfo.ModTime().UnixNano(), Size: linkInfo.Size()}
		} else {
			entryInfo, ierr := d.Info()
			if ierr != nil {
				logger.Printf("WARN: cannot stat %s: %v", path, ierr)
				return nil
			}
			st = FileStat{ModTime: entryInfo.ModTime().UnixNano(), Size: entryInfo.Size()}
		}
		rel, rerr := filepath.Rel(absRoot, path)
		if rerr != nil {
			logger.Printf("WARN: cannot relativise %s: %v", path, rerr)
			return nil
		}
		entries = append(entries, entry{abs: filepath.Clean(path), rel: filepath.ToSlash(rel), stat: st, link: link})
		return nil
	})
	if walkErr != nil {
		return nil, fmt.Errorf("walk %q: %w", absRoot, walkErr)
	}

	scan := &TreeScan{
		AbsRoot:  absRoot,
		Files:    make(map[string]*TreeFile, len(entries)),
		Keys:     make([]string, 0, len(entries)),
		Defaults: make(map[string]bool),
	}

	// Phase 2: hash + classify, in WALK order (the duplicate-tenant error
	// names the first two files in that order, as it always has).
	var walkOrder []*TreeFile
	for _, e := range entries {
		name := e.rel[strings.LastIndexByte(e.rel, '/')+1:]
		f := &TreeFile{
			AbsPath:    e.abs,
			RelKey:     e.rel,
			Stat:       e.stat,
			LinkStat:   e.link,
			IsDefaults: confdname.IsDefaults(name),
		}

		var pf *TreeFile
		if prior != nil {
			pf = prior.Files[e.rel]
		}
		if pf != nil && pf.Hash != "" && !pf.ParseFailed && pf.Stat == f.Stat && sameLinkStat(pf.LinkStat, f.LinkStat) &&
			time.Since(time.Unix(0, guardMtime(f.Stat, f.LinkStat))) > TreeScanMtimeGuard {
			f.Hash = pf.Hash
			// ⛔ THE CARRY. Reusing the hash without the declarations would
			// make the tenant vanish from the hierarchy on every quiet tick.
			// The slice is SHARED, not copied: TenantIDs is never mutated
			// after parseTenantDecls builds it (see the field's doc), and a
			// copy per file was 1000 allocs on every quiet tick of a
			// 1000-tenant tree.
			f.TenantIDs = pf.TenantIDs
			f.Reused = true
		} else {
			data, rerr := os.ReadFile(e.abs)
			if rerr != nil {
				logger.Printf("WARN: cannot read %s: %v", e.abs, rerr)
				continue
			}
			f.Hash = fmt.Sprintf("%x", sha256.Sum256(data))
			if pf == nil || pf.Hash != f.Hash {
				f.Data = data
			}
			switch {
			case strings.HasPrefix(name, "_"):
				// Never parsed for tenants.
			case pf != nil && pf.Hash == f.Hash && !pf.ParseFailed:
				// Same bytes as the prior: the declarations cannot differ, so
				// carry them instead of parsing again. A prior that failed to
				// parse is excluded on purpose — it must be re-parsed (and
				// re-counted) every scan, see ParseFailed. Shared, not copied
				// (immutable once built).
				f.TenantIDs = pf.TenantIDs
			default:
				f.TenantIDs, f.ParseFailed = parseTenantDecls(e.abs, data, obs, logger)
				f.Parsed = true
			}
		}

		scan.Files[e.rel] = f
		scan.Keys = append(scan.Keys, e.rel)
		walkOrder = append(walkOrder, f)
		if f.IsDefaults {
			scan.Defaults[e.abs] = true
		}
	}

	// Composite: hash-of-hashes in sorted key order, the flat plane's
	// change-detection identity since v2.1.0.
	sort.Strings(scan.Keys)
	compositeHasher := sha256.New()
	for _, k := range scan.Keys {
		compositeHasher.Write([]byte(scan.Files[k].Hash))
	}
	scan.Composite = fmt.Sprintf("%x", compositeHasher.Sum(nil))

	// Tenant attribution + cross-file duplicate detection (#127 guardrail:
	// silently preferring one file would mask config drift).
	//
	// ⛔ The loop does NOT stop at the first conflict (#1677 W2). Stopping
	// left every tenant unattributed, so a per-tenant reader (ResolveEffective,
	// ScopeEffective) could not tell "tx is a duplicate" from "innocent ty
	// lives in c.yaml". The whole-tree verdict is unchanged — Conflict is the
	// first duplicate in walk order and Tenants stays nil under it — and the
	// per-tenant view is kept unexported behind Locate.
	tenants := make(map[string]string)
	var dups map[string]*DuplicateTenantError
	for _, f := range walkOrder {
		for _, tid := range f.TenantIDs {
			prev, exists := tenants[tid]
			if !exists {
				tenants[tid] = f.AbsPath
				continue
			}
			if prev == f.AbsPath {
				continue
			}
			if _, seen := dups[tid]; seen {
				continue // a third declaring file: the first two are what we name
			}
			dup := &DuplicateTenantError{TenantID: tid, PathA: prev, PathB: f.AbsPath}
			if dups == nil {
				dups = make(map[string]*DuplicateTenantError)
			}
			dups[tid] = dup
			if scan.Conflict == nil {
				scan.Conflict = dup
			}
		}
	}
	scan.attrib = tenants
	scan.dups = dups
	if scan.Conflict == nil {
		scan.Tenants = tenants
	}
	// The inheritance graph is NOT built here — see InheritanceGraph.
	return scan, nil
}

// Locate is the per-tenant view of the walk, and the ONLY one that survives
// a conflict: it answers for one tenant regardless of whether some OTHER
// tenant is duplicated.
//
//   - tenant declared in two or more files → that tenant's own
//     *DuplicateTenantError (its first two declaring files, walk order);
//   - tenant declared nowhere (or only in `_`-prefixed / unparseable files)
//     → ErrTenantNotFound;
//   - otherwise → the absolute path (under AbsRoot) of its declaring file.
//
// ⚠️ This is deliberately NOT what the exporter does with a conflict: the
// exporter rejects the whole tree (Conflict / nil Tenants). Locate is for the
// read-only diagnostics (ResolveEffective, ScopeEffective), where refusing to
// describe an innocent tenant because a neighbour is broken would only hide
// the one answer the operator asked for.
func (s *TreeScan) Locate(tenantID string) (absPath string, err error) {
	if d, ok := s.dups[tenantID]; ok {
		return "", d
	}
	attrib := s.attrib
	if attrib == nil {
		// A TreeScan not produced by the walk (e.g. a test-built prior) has
		// no private attribution; Tenants is the same information there.
		attrib = s.Tenants
	}
	p, ok := attrib[tenantID]
	if !ok {
		return "", ErrTenantNotFound
	}
	return p, nil
}

// parseTenantDecls extracts the top-level `tenants:` keys of one tenant
// file. It deliberately decodes only the shape it needs — the full config is
// parsed by the plane that consumes it — so a large tree stays cheap to scan.
// Returns (nil, true) for an unparseable file (logged, counted when
// obs is non-nil) and (nil, false) for a file without a `tenants:`
// mapping (a commented-out placeholder is not an error).
func parseTenantDecls(absPath string, data []byte, obs ScanObserver, logger *log.Logger) (ids []string, failed bool) {
	var doc struct {
		Tenants map[string]yaml.Node `yaml:"tenants"`
	}
	if perr := yaml.Unmarshal(data, &doc); perr != nil {
		logger.Printf("WARN: cannot parse %s: %v", absPath, perr)
		if obs != nil {
			// Basename, not full path, to cap label cardinality (A-8d).
			obs.IncParseFailure(filepath.Base(absPath))
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

// RelHashes / RelMtimes / DataCache are the flat plane's projections (keys
// are root-relative slash paths). Fresh maps every call: the manager keeps
// the previous scan's maps as the next prior and must not see them mutate.
func (s *TreeScan) RelHashes() map[string]string {
	out := make(map[string]string, len(s.Files))
	for k, f := range s.Files {
		out[k] = f.Hash
	}
	return out
}

// RelMtimes — see RelHashes.
func (s *TreeScan) RelMtimes() map[string]FileStat {
	out := make(map[string]FileStat, len(s.Files))
	for k, f := range s.Files {
		out[k] = f.Stat
	}
	return out
}

// DataCache — see RelHashes. Only files whose Data is non-nil appear.
func (s *TreeScan) DataCache() map[string][]byte {
	out := make(map[string][]byte)
	for k, f := range s.Files {
		if f.Data != nil {
			out[k] = f.Data
		}
	}
	return out
}

// ReleaseData drops the cached bytes once the plane that needed them has
// parsed. The manager retains the scan as the next prior, and the prior
// reads only Hash, Stat and TenantIDs — keeping a cold load's bytes alive
// until the first reload would be a silent retention the cache never had.
func (s *TreeScan) ReleaseData() {
	for _, f := range s.Files {
		f.Data = nil
	}
}

// AbsHashes is the hierarchy plane's projection (keys are Clean absolute
// paths under the resolved root).
func (s *TreeScan) AbsHashes() map[string]string {
	out := make(map[string]string, len(s.Files))
	for _, f := range s.Files {
		out[f.AbsPath] = f.Hash
	}
	return out
}

// AbsScanRoot is ResolveScanRoot preceded by the absolutisation every caller
// needs and one of them once forgot.
//
// ⛔ The exporter's `-config-dir` is frequently relative, while the walker
// stores absolute paths. Comparing the two without this made EVERY defaults
// file look like a subtree file — measured on the repo's own flat golden
// fixtures, whose ROOT defaults keys were then copied into tenant maps. That
// was fixed in place; this hoists the three-line derivation out of the one
// caller that had it so a second caller cannot get it subtly different.
// (#1569 sweep B-2.)
func AbsScanRoot(dir string) string {
	clean := filepath.Clean(dir)
	if abs, err := filepath.Abs(dir); err == nil {
		clean = filepath.Clean(abs)
	}
	return ResolveScanRoot(clean)
}

// ResolveScanRoot is the ONE derivation of "which directory is the conf.d
// root" that every enumerator over that tree must use.
//
// ⛔ IT EXISTS BECAUSE HAVING TWO OF THEM IS THIS TICKET'S ENTIRE DEFECT
// CLASS. `filepath.WalkDir` lstats its root and never follows a symlink, so
// each scanner that starts from an unresolved `-config-dir` silently sees an
// EMPTY tree when that path is a link. Fixing only the flat scanner produced
// exactly the split this PR closes, one layer down: measured on a symlinked
// root, `GetConfig()` had the tenant while `hierarchy.enabled` was false and
// `tenantSources` was empty, so the tenant's series carried the ROOT default
// (50) instead of the subtree's (90) — and the divergence audit reports only
// the opposite direction, so the gauge stayed at 0. (#1569 blind review.)
// ResolveEffective / ScopeEffective reach it through ScanDirTree since W2
// (#1677 F1), and ScopeEffective also resolves its --scope with AbsScanRoot
// so a root and a scope spelled through different links still compare as
// one tree (pinned by tree_scan_parity_test.go).
//
// ⚠️ Falls back to the given path when resolution fails (dangling link,
// permission), so the caller's own error handling still decides — this
// function never turns a broken path into a different one.
func ResolveScanRoot(dir string) string {
	if resolved, err := filepath.EvalSymlinks(dir); err == nil {
		return resolved
	}
	return dir
}
