package config

// InheritanceGraph + the defaults-chain walker that builds it.
//
// v2.8.0 PR-8 promoted these from `app/config_hierarchy.go` so that
// `pkg/config/source.go` (also new in PR-8) and any future cmd/da-guard
// or tenant-api consumer can construct the graph without depending on
// `package main`. The exporter's disk scanner (`scanDirHierarchical`)
// still lives in app/ because it threads `fileStat` (mtime cache) which
// is exporter-specific.
//
// Semantic rules unchanged from the original definition (parity-pinned
// against describe_tenant.py + golden fixtures):
//   - chain is L0..Ln (root first, leaf last) after CollectDefaultsChain
//     reverses its accumulator
//   - .yaml wins over .yml when both exist at the same level
//   - filepath.Clean'd paths so equality compares stable across calls

import "path/filepath"

// InheritanceGraph tracks the defaults↔tenants dependency for a
// hierarchical conf.d layout (ADR-017).
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

// CollectDefaultsChain walks from leafDir up to (and including) root,
// picking the `_defaults.yaml` (or `.yml`) at each level and reversing
// the accumulator so chain[0] is the top-most (L0) defaults.
//
// Both the exporter's disk scanner (scanDirHierarchical in app/) and
// the in-memory scanFromConfigSource in this package call this helper.
// `defaults` is the populated set of known _defaults.yaml paths
// (basename match, set membership only — values are unused).
func CollectDefaultsChain(leafDir, root string, defaults map[string]bool) []string {
	var chain []string
	current := filepath.Clean(leafDir)
	rootClean := filepath.Clean(root)

	for {
		// Prefer .yaml over .yml when both exist at the same level
		// (same precedence rule as describe_tenant.py).
		yamlPath := filepath.Join(current, "_defaults.yaml")
		ymlPath := filepath.Join(current, "_defaults.yml")
		if defaults[yamlPath] {
			chain = append(chain, yamlPath)
		} else if defaults[ymlPath] {
			chain = append(chain, ymlPath)
		}

		if current == rootClean {
			break
		}
		parent := filepath.Dir(current)
		if parent == current {
			// Reached filesystem root without hitting rootClean —
			// shouldn't happen given the precondition, but don't loop.
			break
		}
		current = parent
	}

	// Reverse to make chain[0] the top-most (L0) defaults and the last
	// entry the nearest-to-tenant (Ln). Matches describe_tenant.py
	// line 152.
	for i, j := 0, len(chain)-1; i < j; i, j = i+1, j-1 {
		chain[i], chain[j] = chain[j], chain[i]
	}
	return chain
}
