package config

// InheritanceGraph + the defaults-chain walker that builds it.
//
// v2.8.0 PR-8 promoted these from the exporter's package main so that
// `pkg/config/source.go` (also new in PR-8) and any future cmd/da-guard
// or tenant-api consumer can construct the graph without depending on
// `package main`. The exporter's disk walker, ScanDirTree (tree_scan.go),
// has since moved into this package too (#1941), mtime cache included,
// and builds its graph with CollectDefaultsChain below.
//
// Semantic rules (parity-pinned against describe_tenant.py + golden
// fixtures):
//   - chain is L0..Ln (root first, leaf last)
//   - ONE carrier per level, chosen by SelectDefaultsCarriers from the
//     walker's case-folded defaults set (#1674): a `.yaml` spelling beats a
//     `.yml` one, and a directory with more than one carrier is WARNed about
//   - filepath.Clean'd paths so equality compares stable across calls

import (
	"fmt"
	"path"
	"path/filepath"
	"sort"
	"strings"
)

// InheritanceGraph tracks the defaults↔tenants dependency for a
// hierarchical conf.d layout (ADR-016).
//
//   - TenantDefaults[tenantID]   → L0..Ln defaults paths (root first).
//     Used by ComputeMergedHash and the /effective handler.
//   - DefaultsToTenants[path]    → tenant IDs whose merged_hash depends
//     on this defaults file. Used by the debounced reload path: when
//     a _defaults.yaml changes we look up exactly which tenants need
//     re-hash.
//
// All paths are absolute + filepath.Clean'd. The struct is treated as
// immutable once built — reload constructs a fresh graph and atomically
// swaps the pointer on ConfigManager.
type InheritanceGraph struct {
	DefaultsToTenants map[string][]string
	TenantDefaults    map[string][]string
}

// NewInheritanceGraph returns an empty graph with both directions
// initialized.
func NewInheritanceGraph() *InheritanceGraph {
	return &InheritanceGraph{
		DefaultsToTenants: make(map[string][]string),
		TenantDefaults:    make(map[string][]string),
	}
}

// AddTenant records a tenant's inheritance chain. defaultsChain MUST
// be ordered root-first, leaf-last (matching describe_tenant.py after
// its internal reverse). A defensive copy is made so the caller may
// reuse the slice.
func (g *InheritanceGraph) AddTenant(tenantID string, defaultsChain []string) {
	if g.TenantDefaults == nil {
		g.TenantDefaults = make(map[string][]string)
	}
	if g.DefaultsToTenants == nil {
		g.DefaultsToTenants = make(map[string][]string)
	}
	chain := make([]string, len(defaultsChain))
	copy(chain, defaultsChain)
	g.TenantDefaults[tenantID] = chain

	for _, dp := range chain {
		g.DefaultsToTenants[dp] = append(g.DefaultsToTenants[dp], tenantID)
	}
}

// TenantsAffectedBy returns the tenant IDs whose effective config
// depends on the given _defaults.yaml path. Returns nil when the path
// is unknown — the caller can distinguish "no tenants inherit this
// file" vs "unrelated file" via the defaults map returned by the
// scanner.
func (g *InheritanceGraph) TenantsAffectedBy(defaultsPath string) []string {
	if g == nil {
		return nil
	}
	return g.DefaultsToTenants[defaultsPath]
}

// DefaultsCarriers is the ONE answer to "which defaults file does this
// directory's chain read" (#1674, B8). Every plane reads it: the exporter's
// inheritance graph (TreeScan.InheritanceGraph), /effective and da-guard
// (ResolveEffective / ScopeEffective), the in-memory simulate source
// (ScanFromConfigSource), and the flat plane's root `Defaults` on /metrics
// (package main, the ROOT directory's entry). describe_tenant.py mirrors it
// in `_lib_confd.select_defaults_carrier`, and the golden fixtures pin the
// two languages to one merged_hash.
type DefaultsCarriers struct {
	// ByDir maps a directory (Clean'd, in the path flavour of the input
	// set) to the one carrier its chain level reads.
	ByDir map[string]string
	// Ambiguous maps each directory holding MORE THAN ONE carrier to all of
	// them, in walk order. A second spelling is a misconfiguration: every
	// plane reads only ByDir's pick, and the exporter WARNs from this map on
	// each load/reload (never on the quiet watch tick).
	Ambiguous map[string][]string
}

// SelectDefaultsCarriers applies the selection rule to a defaults SET (the
// walker's case-folded classification, TreeScan.Defaults: `_DEFAULTS.YML`
// is a member). Per directory:
//
//   - a spelling that lower-cases to `_defaults.yaml` beats any spelling that
//     lower-cases to `_defaults.yml`;
//   - among `.yaml` case variants the LAST in walk order wins, among `.yml`
//     variants the FIRST.
//
// ⚠️ The asymmetry in the second bullet is deliberate, not a bug to tidy. It
// is exactly what /effective did before #1674 — its own walk overwrote on
// `.yaml` and kept the first `.yml` — and the owner ruling on #1674 kept that
// rule when the chain was made to follow the case-folded classification.
// Only a directory with two case variants of ONE extension can observe it,
// and such a directory is WARNed about anyway.
//
// Walk order within one directory is lexical by name (filepath.WalkDir), and
// two paths in one directory compare by their names, so sorting the paths
// reproduces it — which is the only order this rule reads.
func SelectDefaultsCarriers(defaults map[string]bool) DefaultsCarriers {
	return selectDefaultsCarriers(defaults, nativePathOps)
}

func selectDefaultsCarriers(defaults map[string]bool, ops pathOps) DefaultsCarriers {
	paths := make([]string, 0, len(defaults))
	for p := range defaults {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	sel := DefaultsCarriers{ByDir: make(map[string]string, len(paths))}
	var count map[string]int
	for _, p := range paths {
		dir := ops.dir(p)
		switch strings.ToLower(ops.base(p)) {
		case "_defaults.yaml":
			sel.ByDir[dir] = p
		case "_defaults.yml":
			if _, exists := sel.ByDir[dir]; !exists {
				sel.ByDir[dir] = p
			}
		default:
			continue // not a carrier: the set is the walker's, so unreachable
		}
		if count == nil {
			count = make(map[string]int, len(paths))
		}
		count[dir]++
	}
	for _, p := range paths {
		dir := ops.dir(p)
		if count[dir] > 1 {
			if sel.Ambiguous == nil {
				sel.Ambiguous = make(map[string][]string)
			}
			sel.Ambiguous[dir] = append(sel.Ambiguous[dir], p)
		}
	}
	return sel
}

// AmbiguityWarnings renders one WARN line per directory holding more than
// one carrier, sorted by directory. The wording is shared with
// describe_tenant.py (`_lib_confd.multi_carrier_warning`) so an operator
// grepping for one finds the other.
func (c DefaultsCarriers) AmbiguityWarnings() []string {
	if len(c.Ambiguous) == 0 {
		return nil
	}
	dirs := make([]string, 0, len(c.Ambiguous))
	for d := range c.Ambiguous {
		dirs = append(dirs, d)
	}
	sort.Strings(dirs)
	out := make([]string, 0, len(dirs))
	for _, d := range dirs {
		all := c.Ambiguous[d]
		chosen := c.ByDir[d]
		names := make([]string, 0, len(all))
		var ignored []string
		for _, p := range all {
			names = append(names, filepath.Base(p))
			if p != chosen {
				ignored = append(ignored, filepath.Base(p))
			}
		}
		out = append(out, fmt.Sprintf(
			"WARN: conf.d directory %s has %d defaults carriers (%s); only %s is read, "+
				"%s is ignored on every plane (#1674) — merge them into one file",
			d, len(all), strings.Join(names, ", "), filepath.Base(chosen), strings.Join(ignored, ", ")))
	}
	return out
}

// CollectDefaultsChain walks from leafDir up to (and including) root,
// picking at each level the carrier SelectDefaultsCarriers chooses, and
// returns the chain root-first (chain[0] is L0).
//
// `defaults` is the walker's defaults set (TreeScan.Defaults: case-folded
// membership, values unused). ⚠️ Before #1674 this function matched only the
// two exact lower-case names, so `_DEFAULTS.YAML` was classified a carrier
// and then left out of the chain; the set and the chain now read one rule.
//
// Convenience for one leaf: it selects over the whole set on every call.
// Callers that walk many directories (TreeScan.InheritanceGraph,
// ResolveEffective, ScanFromConfigSource) select once and call
// chainFromCarriers.
func CollectDefaultsChain(leafDir, root string, defaults map[string]bool) []string {
	return chainFromCarriers(leafDir, root, selectDefaultsCarriers(defaults, nativePathOps).ByDir, nativePathOps)
}

// CollectDefaultsChainPOSIX is the POSIX-only sibling of CollectDefaultsChain.
// Identical semantics, but uses path.* (always /) instead of filepath.* (OS-aware).
//
// Why a separate function: the disk scanner walks real filesystem paths where
// filepath.* is correct (handles \\ on Windows). The in-memory scanner
// (ScanFromConfigSource → simulate) walks synthetic POSIX paths under /sim,
// where filepath.* on Windows would convert / to \\ and break the prefix +
// map-key semantics — InMemoryConfigSource is documented as POSIX-only.
//
// History: discovered while triaging Simulate Windows-host flake (4 tests in
// config_simulate_test.go failed locally on zh-TW Windows but passed on
// Linux/CI). filepath.Dir("/sim/foo") on Windows returns "\\sim", which
// missed the POSIX-keyed defaults map → empty chain → inherited keys
// silently dropped from the merged config.
func CollectDefaultsChainPOSIX(leafDir, root string, defaults map[string]bool) []string {
	return chainFromCarriers(leafDir, root, selectDefaultsCarriers(defaults, posixPathOps).ByDir, posixPathOps)
}

// pathOps abstracts the path-manipulation functions the defaults-chain walk
// needs, so one implementation can run over either OS-native filesystem
// paths (filepath.*) or the synthetic POSIX paths (path.*) the in-memory
// config source uses. path.* and filepath.* share these signatures exactly.
type pathOps struct {
	clean func(string) string
	dir   func(string) string
	base  func(string) string
}

var (
	nativePathOps = pathOps{clean: filepath.Clean, dir: filepath.Dir, base: filepath.Base}
	posixPathOps  = pathOps{clean: path.Clean, dir: path.Dir, base: path.Base}
)

// chainFromCarriers walks from leafDir up to and including root, taking
// byDir's carrier at each level, then reverses the accumulator so chain[0]
// is the top-most (L0) defaults and the last entry the nearest-to-tenant
// (Ln) — the order describe_tenant.py produces.
func chainFromCarriers(leafDir, root string, byDir map[string]string, ops pathOps) []string {
	var chain []string
	current := ops.clean(leafDir)
	rootClean := ops.clean(root)

	for {
		if p, ok := byDir[current]; ok {
			chain = append(chain, p)
		}
		if current == rootClean {
			break
		}
		parent := ops.dir(current)
		if parent == current {
			// Reached filesystem root without hitting rootClean —
			// shouldn't happen given the precondition, but don't loop.
			break
		}
		current = parent
	}

	for i, j := 0, len(chain)-1; i < j; i, j = i+1, j-1 {
		chain[i], chain[j] = chain[j], chain[i]
	}
	return chain
}
