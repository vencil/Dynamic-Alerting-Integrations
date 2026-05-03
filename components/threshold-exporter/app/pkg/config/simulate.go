package config

// ============================================================
// Simulate primitive (v2.8.0 Phase .c C-7b)
// ============================================================
//
// SimulateEffective answers "if I committed this tenant.yaml under this
// defaults chain, what would the effective config + merged_hash be?"
// without writing to disk and without disturbing the WatchLoop.
//
// It is the pure function the /api/v1/tenants/simulate handler dispatches
// to. Importantly it goes through the same merge path
// (computeEffectiveConfig + computeMergedHash) as the production
// ConfigManager.Resolve, so a simulated hash is byte-identical to what
// you'd see after committing the same bytes — that contract is asserted
// by TestSimulate_VsResolve_ParityHash.
//
// API shape mirrors describe_tenant.py JSON output and EffectiveConfig
// so HTTP consumers can compare the two responses field-for-field.

import (
	"errors"
	"fmt"
	"path"
	"strconv"
)

// SimRoot is the synthetic root the simulator places its in-memory
// hierarchy under. POSIX style — chosen so filepath.Clean gives
// consistent results on Windows and Linux.
const SimRoot = "/sim"

// SimulateRequest is the input to a /simulate call.
//
//   - TenantID     — which tenant to compute the effective config for.
//     Must appear under the `tenants:` block of TenantYAML.
//   - TenantYAML   — raw bytes of the tenant file (with `tenants:` wrapper).
//   - DefaultsChainYAML — raw bytes of L0…Ln `_defaults.yaml` files,
//     ROOT-FIRST. Empty slice = no inherited defaults (flat tenant).
//
// The (TenantID, TenantYAML, DefaultsChainYAML) triple is the minimum
// the merge engine needs. We deliberately don't accept "domain/region"
// path hints because the chain order in the request fully determines
// merge precedence — keeping the API surface minimal also keeps the
// parity test simple (same inputs → same hash, no path-encoding game).
type SimulateRequest struct {
	TenantID          string   `json:"tenant_id"`
	TenantYAML        []byte   `json:"tenant_yaml"`
	DefaultsChainYAML [][]byte `json:"defaults_chain_yaml,omitempty"`
}

// SimulateResponse is the result of a /simulate call. Field names match
// EffectiveConfig (and describe_tenant.py source_info) so a reviewer can
// diff a simulate response against `GET /api/v1/tenants/{id}/effective`
// directly without remapping keys.
type SimulateResponse struct {
	TenantID      string         `json:"tenant_id"`
	SourceHash    string         `json:"source_hash"`
	MergedHash    string         `json:"merged_hash"`
	DefaultsChain []string       `json:"defaults_chain"`
	Config        map[string]any `json:"effective_config"`
}

// ErrSimulateTenantNotFound is returned when SimulateRequest.TenantID
// is missing from the tenant file's `tenants:` block. Surface this as
// HTTP 404 in the handler so the contract matches /effective.
var ErrSimulateTenantNotFound = errors.New("tenant id not present in tenant_yaml")

// SimulateEffective is the pure (no IO, no globals) computation behind
// the /simulate endpoint. Given a tenant file, its defaults chain, and
// the tenant ID, it returns the same EffectiveConfig data the disk-
// backed ConfigManager.Resolve would produce for an equivalent on-disk
// commit.
//
// Errors:
//   - SimulateRequest.TenantID empty                → fmt error
//   - len(TenantYAML) == 0                          → fmt error
//   - YAML parse failure (any defaults or tenant)   → fmt error
//   - TenantID not in tenant_yaml `tenants:` block  → ErrSimulateTenantNotFound
//
// On success the returned Config is freshly allocated and owned by
// the caller.
func SimulateEffective(req SimulateRequest) (*SimulateResponse, error) {
	if req.TenantID == "" {
		return nil, fmt.Errorf("simulate: tenant_id is required")
	}
	if len(req.TenantYAML) == 0 {
		return nil, fmt.Errorf("simulate: tenant_yaml is required")
	}

	// Build a synthetic in-memory hierarchy so the request's defaults
	// chain order (L0…Ln) maps to a real directory ancestry that the
	// shared scan engine can walk:
	//
	//   /sim/_defaults.yaml          ← L0
	//   /sim/lvl1/_defaults.yaml     ← L1
	//   /sim/lvl1/lvl2/_defaults.yaml ← L2
	//   ...
	//   /sim/lvl1/.../tenant.yaml    ← tenant file (deepest)
	//
	// This guarantees collectDefaultsChain (called by
	// ScanFromConfigSource) reproduces exactly the chain the caller
	// asked for. We could skip the scan and call computeEffectiveConfig
	// directly, but going through the scan exercises the same code
	// path the parity test needs — keeping one road keeps both roads
	// honest.
	files := make(map[string][]byte, len(req.DefaultsChainYAML)+1)
	tenantDir := SimRoot
	for i, defBytes := range req.DefaultsChainYAML {
		files[path.Join(tenantDir, "_defaults.yaml")] = defBytes
		if i < len(req.DefaultsChainYAML)-1 {
			tenantDir = path.Join(tenantDir, "lvl"+strconv.Itoa(i+1))
		}
	}
	tenantPath := path.Join(tenantDir, "tenant.yaml")
	files[tenantPath] = req.TenantYAML

	src := NewInMemoryConfigSource(files)
	tenants, _, _, graph, err := ScanFromConfigSource(src, SimRoot)
	if err != nil {
		return nil, fmt.Errorf("simulate scan: %w", err)
	}
	if _, ok := tenants[req.TenantID]; !ok {
		return nil, ErrSimulateTenantNotFound
	}

	chain := graph.TenantDefaults[req.TenantID]
	chainBytes := make([][]byte, 0, len(chain))
	for _, dp := range chain {
		chainBytes = append(chainBytes, files[dp])
	}

	merged, err := ComputeEffectiveConfig(req.TenantYAML, req.TenantID, chainBytes)
	if err != nil {
		return nil, fmt.Errorf("simulate merge: %w", err)
	}
	mergedHash, err := ComputeMergedHash(req.TenantYAML, req.TenantID, chainBytes)
	if err != nil {
		return nil, fmt.Errorf("simulate hash: %w", err)
	}

	return &SimulateResponse{
		TenantID:      req.TenantID,
		SourceHash:    ComputeSourceHash(req.TenantYAML),
		MergedHash:    mergedHash,
		DefaultsChain: append([]string(nil), chain...),
		Config:        merged,
	}, nil
}
