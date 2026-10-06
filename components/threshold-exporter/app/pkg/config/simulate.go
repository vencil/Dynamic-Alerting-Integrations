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
// to. Importantly a simulated hash is byte-identical to the merged_hash
// the exporter commits for the same bytes, and the simulated config equals
// what ResolveEffective reads for them from disk — that contract is
// asserted by TestSimulate_VsCommitted_ParityHash.
//
// ⚠️ "Same merged_hash" is NOT "same path as the exported metric". This
// merge is the DIAGNOSTIC path; the series
// `user_threshold` is produced by collector.go →
// ThresholdConfig.ResolveAtWithStats, a different resolver that decodes
// into typed ScheduledValue rather than merging `map[string]any`. The two
// agree on every shape the schema admits — deepMerge was aligned to the
// emitting path for threshold nulls in #1339 — but they are separate
// implementations, so read a simulate result as "what the config would
// merge to", not as "what /metrics will emit".
//
// ⚠️ NO PLATFORM PER-TENANT LAYER (#2019). /effective, da-guard and the
// exporter's merged_hash also apply the root platform files' `tenants:`
// entries for the tenant (PlatformOverlayFor). A /simulate request carries
// a tenant file and a defaults chain only — no platform files — so that
// layer is not applied here, and for a tenant a root platform file names
// the simulated effective config and merged_hash differ from /effective's
// by exactly that layer. Deliberate: the request shape is unchanged, and
// the result is "tenant file + this chain", nothing read from disk.
//
// PROFILES (#2117): a `_profile` the tenant elects IS expanded, from the
// `profiles:` block of the chain's ROOT entry (L0) only — see
// simulateProfiles. `_profiles.yaml` is not part of the request shape, so a
// profile defined only there expands on /effective and /metrics but not
// here.
//
// API shape mirrors describe_tenant.py JSON output and EffectiveConfig
// so HTTP consumers can compare the two responses field-for-field.

import (
	"errors"
	"fmt"
	"path"
	"strconv"
	"strings"
	"unicode/utf8"

	"gopkg.in/yaml.v3"
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
// the tenant ID, it returns the same effective config and hashes the
// disk-backed ResolveEffective would produce for an equivalent on-disk
// tree.
//
// Errors:
//   - SimulateRequest.TenantID empty                → fmt error
//   - len(TenantYAML) == 0                          → fmt error
//   - YAML parse failure (any defaults or tenant)   → fmt error
//   - tenant_yaml, or the chain's ROOT entry (L0), that the exporter's own
//     decode rejects                                → fmt error (#1981;
//     see rejectWhatTheExporterSkips)
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

	if err := rejectWhatTheExporterSkips(req); err != nil {
		return nil, err
	}

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

	parts, err := computeEffectiveConfigBytesDetailed(req.TenantYAML, req.TenantID, chainBytes, nil, simulateProfiles(chain, files))
	if err != nil {
		return nil, fmt.Errorf("simulate merge: %w", err)
	}
	merged := parts.merged
	mergedHash, err := mergedHashOf(merged)
	if err != nil {
		return nil, fmt.Errorf("simulate hash: %w", err)
	}

	// #2115: effective_config is the per-threshold view /effective
	// reports (effectiveView), merged_hash the merge's — as ResolveEffective
	// builds them, so a simulate response stays the /effective answer for
	// the same tree. The merge succeeded over these bytes, so every chain
	// entry parses here.
	blocks := make([]map[string]any, len(chainBytes))
	for i, b := range chainBytes {
		blocks[i] = ParseChainDefaults(b).block
	}

	return &SimulateResponse{
		TenantID:      req.TenantID,
		SourceHash:    ComputeSourceHash(req.TenantYAML),
		MergedHash:    mergedHash,
		DefaultsChain: append([]string(nil), chain...),
		Config:        effectiveView(blocks, parts.override),
	}, nil
}

// simulateProfiles is the profile set a /simulate request carries (#2117):
// the `profiles:` block (and `optional_overrides:`) of the chain's ROOT
// entry — L0, placed at SimRoot/_defaults.yaml — decoded exactly as the
// walker plane decodes a root platform file (newPlatformProfiles).
//
// ⛔ ONLY L0, because only a ROOT platform file's `profiles:` reaches
// /metrics: a nested `_defaults.yaml`'s block is read by no plane
// (flat_build.go drops nested platform files), so expanding an L1+ entry's
// would simulate a profile the exporter never serves.
//
// ⚠️ `_profiles.yaml` (or any root platform file other than the defaults
// carrier) is not part of the request shape — the request carries a tenant
// file and a defaults chain only. A profile defined there is unknown to
// /simulate: a tenant electing it simulates with no expansion, while
// /effective and /metrics expand it. Same stance as the platform per-tenant
// layer above: the result is "tenant file + this chain", nothing read from
// disk.
func simulateProfiles(chain []string, files map[string][]byte) *PlatformProfiles {
	root := path.Join(SimRoot, "_defaults.yaml")
	if len(chain) == 0 || chain[0] != root {
		return nil
	}
	return newPlatformProfiles([]profileSourceFile{{key: "_defaults.yaml", data: files[root]}})
}

// rejectWhatTheExporterSkips refuses a request whose tenant file, or whose
// chain ROOT entry (L0), the exporter would drop on load (#1981): a dry run
// must fail where the real path fails, or it answers "fine" for a commit
// that never takes effect.
//
//   - tenant_yaml is judged with ParseTenantFile — the walker's decode of a
//     tenant file (parseTenantDecls). A file it rejects declares no tenant on
//     any plane: the exporter skips it whole (WARN, parse_failure).
//   - L0 is judged with ParseConfigFile — the decode the flat plane gives the
//     ROOT `_defaults.yaml` (parsePartialConfig for a `_` file). A file it
//     rejects is dropped whole on /metrics, the keys that WERE valid included
//     (ERROR, parse_failure, LoadDir's parseFailed).
//
// L0 is DefaultsChainYAML[0] by construction: SimulateEffective places it at
// SimRoot/_defaults.yaml, the only chain entry at the scan root — the same
// convention simulateProfiles relies on. The request carries no paths, so
// the root-first order IS the level.
//
// ⛔ L1 AND BELOW ARE NOT JUDGED HERE, ON PURPOSE. The exporter never decodes
// a nested `_defaults.yaml` into ThresholdConfig: the flat plane only
// syntax-probes it (reportUnparseableNestedPlatformFile) and the subtree
// plane reads it through ParseChainDefaults into `any` — so `"70"` there
// takes effect, and even `abc` is not a load failure. Judging those levels
// with ParseConfigFile would make simulate stricter than the real path. A
// syntax error in any level still fails, in the merge (ParseChainDefaults).
//
// ⚠️ /effective (ResolveEffective) still reads L0 through ParseChainDefaults
// only, so for an L0 this rejects, /simulate answers 400 while /effective
// renders the lenient merge — a known, temporary disagreement tracked in
// #2296.
//
// The error names the rejected input by its request field, since the
// request carries bytes, not file names.
//
// ⚠️ PRECEDENCE OVER 404. This runs before the tenant lookup, so a request
// whose tenant file (or L0) the exporter would drop is a 400 even when
// tenant_id is also absent from the file — the file is the first thing
// wrong with it, and a 404 would send the caller hunting for a typo in an
// id that no fix of the id can make resolve.
//
// ⛔ THE MESSAGE IS CAPPED (capDecodeError). yaml.v3 reports one line per
// type error, so a large payload of wrong-typed values turned into a
// multi-megabyte {error} (measured: 716 KB in → 3.26 MB of message out).
func rejectWhatTheExporterSkips(req SimulateRequest) error {
	if _, err := ParseTenantFile(req.TenantYAML); err != nil {
		return fmt.Errorf("simulate: tenant_yaml: the exporter would skip this tenant file: %s", capDecodeError(err))
	}
	if len(req.DefaultsChainYAML) > 0 {
		if _, err := ParseConfigFile(req.DefaultsChainYAML[0]); err != nil {
			return fmt.Errorf("simulate: defaults_chain_yaml[0] (L0, root _defaults.yaml): the exporter would skip this defaults file, dropping every key in it: %s", capDecodeError(err))
		}
	}
	return nil
}

// simulateErrorLines / simulateErrorBytes bound a decode error's rendering
// in a /simulate 400 (#1981).
const (
	simulateErrorLines = 10
	simulateErrorBytes = 4096
)

// capDecodeError renders err with at most simulateErrorLines of yaml.v3's
// per-error lines, followed by "… and M more errors (N total)" when some
// were cut, and never more than simulateErrorBytes before that suffix. The
// count is the decoder's own (yaml.TypeError.Errors) when err is one, else
// the lines of err's message.
func capDecodeError(err error) string {
	var lines []string
	head := ""
	var te *yaml.TypeError
	if errors.As(err, &te) {
		head = "yaml: unmarshal errors:"
		lines = te.Errors
	} else {
		lines = strings.Split(err.Error(), "\n")
	}
	total := len(lines)
	if total > simulateErrorLines {
		lines = lines[:simulateErrorLines]
	}
	var b strings.Builder
	b.WriteString(head)
	for i, l := range lines {
		if head != "" {
			b.WriteString("\n  ")
		} else if i > 0 {
			b.WriteString("\n")
		}
		b.WriteString(l)
	}
	out := b.String()
	if len(out) > simulateErrorBytes {
		out = out[:simulateErrorBytes]
		for !utf8.ValidString(out) { // never split a rune
			out = out[:len(out)-1]
		}
		out += " …"
	}
	if total > len(lines) {
		out += fmt.Sprintf("\n  … and %d more errors (%d total)", total-len(lines), total)
	}
	return out
}
