package config

// ResolveEffectiveFromScan: one tenant's effective config from a scan the
// caller already has — typically a WARM one (#1977), made with a prior, whose
// files took the mtime fast-path and carry no bytes, and which the caller
// keeps as the next walk's prior (ReleaseData'd, so no Partials either).

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ErrScanStale is returned by ResolveEffectiveFromScan when a file the resolve
// needs no longer holds the bytes the scan hashed (rewritten, removed or
// unreadable since the walk). Nothing was answered from mixed snapshots: the
// caller walks again and retries (#1977).
var ErrScanStale = errors.New("conf.d changed since the scan")

// ResolveEffectiveFromScan is ResolveEffective over a scan the caller already
// has. scan is only read — never mutated — so a scan retained as some
// walker's prior may be passed while other goroutines read it.
//
// ⛔ BYTE-IDENTICAL TO A COLD RESOLVE OF THE SAME TREE. It reads exactly the
// files a tenant's answer depends on — the tenant's own file, every
// `_`-prefixed file at the conf.d root (the root carrier, the platform
// `tenants:` and `profiles:` blocks), and every defaults carrier in the
// directories from the root down to the tenant's — and the exporter's build
// that gives NotServed / ChainParseFailed runs over that subset
// (effectiveScanView) instead of the whole tree. Every verdict that build
// gives the tenant is a function of those files alone:
//
//   - the config-wide values (defaults, optional_overrides, state_filters,
//     max_metrics_per_tenant, profiles) come from root `_` files only —
//     applyBoundaryRules strips them from every tenant file, and nested `_`
//     files never reach the merge;
//   - the tenant's map comes from its one declaring file (Locate) and the
//     root platform `tenants:` entries;
//   - the subtree overlay (applySubtreeDefaults) walks each tenant's own
//     chain, and a chain file's refused values are a property of its bytes;
//   - the parse verdicts read are those of the tenant's winning files
//     (servedVerdicts.notServed) and its chain (chainFileUnread) — all in
//     the subset.
//
// ⚠️ servedVerdicts.candidates also asks "did ANY file of the build fail to
// parse", only to decide whether to attribute keys (keySources); NotServed
// names a key only for a parse failure of its winning file, so the body is
// the same either way. The one difference is keySources' internal-invariant
// error ("no layer writes effective key"), which a tree whose only broken
// file is elsewhere no longer reaches.
//
// A duplicate-tenant Conflict elsewhere in the tree is carried into the
// subset, so the subtree overlay is off there exactly as on the whole tree
// (the exporter installs no inheritance graph for a conflicted tree).
//
// Bytes: a file the scan cached (Data, a cold scan) is used as is; any other
// is read from disk and used only when its SHA-256 is the scan's, else
// ErrScanStale. So a warm scan is answered from the same snapshot the walk
// hashed, never from bytes newer than the tenant set it attributed.
//
// ⚠️ That is the walker's guarantee, not a stronger one: a file the scan
// carried on the fast-path keeps the prior's hash and tenant declarations,
// with ScanDirTree's known gap (a same-size rewrite within one mtime tick of
// a file the prior recorded inside TreeScanMtimeGuard). For a file this
// resolve reads, the gap surfaces as ErrScanStale (the bytes no longer hash
// to the carried hash); for one it does not read — another tenant file whose
// carried declarations are stale — it is not seen.
func ResolveEffectiveFromScan(scan *TreeScan, tenantID string) (*EffectiveConfig, error) {
	tenantFile, err := scan.Locate(tenantID)
	if err != nil {
		return nil, err
	}
	view, err := effectiveScanView(scan, tenantID, tenantFile)
	if err != nil {
		return nil, err
	}
	r := newEffectiveResolver(view)
	built, err := loadDirBuild(view, view.AbsRoot, discardLogger, discardLogger.Printf)
	if err != nil {
		return nil, err
	}
	r.served = newServedVerdicts(&built, false)
	ec, err := r.resolve(tenantID)
	// #2031: tenant-api's /effective shows every key as written.
	return ec.AsWritten(), err
}

// effectiveScanView is the subset of scan that tenantID's resolve and build
// read (see ResolveEffectiveFromScan), with every file's bytes present: the
// scan's own when it cached them, else read from disk and checked against
// the scan's hash. A fresh TreeScan whose TreeFiles are copies, so scan
// itself is not touched.
func effectiveScanView(scan *TreeScan, tenantID, tenantFile string) (*TreeScan, error) {
	// The directories from the tenant's up to the root: their defaults
	// carriers — every spelling, so DefaultsCarriers selects on the view as
	// it does on the whole tree — are the chain's candidates.
	chainDirs := map[string]bool{}
	for dir := filepath.Dir(tenantFile); ; {
		chainDirs[dir] = true
		if dir == scan.AbsRoot {
			break
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	view := &TreeScan{
		AbsRoot:  scan.AbsRoot,
		Files:    map[string]*TreeFile{},
		Defaults: map[string]bool{},
		Conflict: scan.Conflict,
		// Locate on the view answers tenantFile for tenantID, as on scan —
		// also under a Conflict, where Tenants stays nil.
		attrib: map[string]string{tenantID: tenantFile},
	}
	for abs := range scan.Defaults {
		if chainDirs[filepath.Dir(abs)] {
			view.Defaults[abs] = true
		}
	}
	tenantKey := ""
	for _, k := range scan.Keys {
		f := scan.Files[k]
		if f.AbsPath == tenantFile {
			tenantKey = k
		}
		keep := f.AbsPath == tenantFile || view.Defaults[f.AbsPath] ||
			(!strings.Contains(k, "/") && isPlatformKey(k))
		if !keep {
			continue
		}
		data, err := scanBytes(f)
		if err != nil {
			return nil, err
		}
		cp := *f
		cp.Data = data
		view.Files[k] = &cp
		view.Keys = append(view.Keys, k)
		if p, ok := scan.Partials[k]; ok {
			if view.Partials == nil {
				view.Partials = map[string]ThresholdConfig{}
			}
			view.Partials[k] = p
		}
	}
	sort.Strings(view.Keys) // already sorted on scan; kept explicit
	if scan.Conflict == nil {
		view.Tenants = map[string]string{}
		if f := view.Files[tenantKey]; f != nil {
			for _, id := range f.TenantIDs {
				view.Tenants[id] = tenantFile
			}
		}
		view.Tenants[tenantID] = tenantFile
	}
	return view, nil
}

// scanBytes is f's bytes as the scan hashed them: its cached Data, else a
// read whose SHA-256 must equal f.Hash (ErrScanStale otherwise).
func scanBytes(f *TreeFile) ([]byte, error) {
	if f.Data != nil {
		return f.Data, nil
	}
	b, err := os.ReadFile(f.AbsPath)
	if err != nil {
		return nil, fmt.Errorf("%w: read %s: %w", ErrScanStale, f.RelKey, err)
	}
	sum := sha256.Sum256(b)
	if hex.EncodeToString(sum[:]) != f.Hash {
		return nil, fmt.Errorf("%w: %s", ErrScanStale, f.RelKey)
	}
	return b, nil
}
