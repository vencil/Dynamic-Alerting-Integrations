// Package account implements monotonic per-tenant AccountID allocation
// for tenant log federation (ADR-021 Phase 1 / #609).
//
// Each tenant is assigned a stable, immutable uint32 AccountID. The id is
// embedded in a tenant's *logs* federation JWT (audience
// "tenant-federation-logs") and used by VictoriaLogs as the tenant
// partition key, so every tenant's logs land in a distinct AccountID
// stream and no tenant can read another's.
//
// Two design invariants make the id safe to hand to a log store:
//
//   - Monotonic, never reused. Ids are drawn from an ever-increasing
//     high-water mark (next_account_id). Deleting a tenant does NOT free
//     its id: a recycled id given to a NEW tenant would let that tenant
//     read the OLD tenant's logs still inside the retention window — a
//     cross-tenant leak. So offboarding leaves the allocation in place
//     (a tombstone the registry simply never reissues).
//   - Explicit counter, never hashed. The mapping is an authoritative
//     committed table, not hash(tenant_id) into a number space: a hash
//     collides (two tenants → one AccountID → merged logs), and is
//     impossible to evolve. ADR-021 forbids hash-derived ids.
//
// This file is the PURE allocation core: an in-memory Registry plus the
// allocation calculation, with no git and no I/O so it unit-tests without
// a shell-out. The commit-on-write persistence (allocator.go) wraps it.
package account

import (
	"fmt"
	"sort"

	"gopkg.in/yaml.v3"
)

// schemaVersion tags the on-disk registry document. A reader that loads a
// document carrying an UNKNOWN (newer) version refuses to allocate against
// it rather than silently dropping fields a newer binary added — the same
// forward-compat guard the federation token store uses.
const schemaVersion = "v1"

// FirstTenantAccountID is the lowest id handed to a tenant. Everything
// below it is reserved:
//
//   - 0   — the platform default partition. VictoriaLogs routes a log line
//     whose tenant header is absent/unparsable to AccountID 0, so 0 must
//     never belong to a real tenant (its logs would mingle with the
//     unattributed default stream).
//   - 1..999 — reserved for future platform/system streams (audit,
//     billing, internal probes) so they can be carved out without
//     colliding with tenant ids already in the field.
//
// Tenant allocation therefore starts at 1000.
const FirstTenantAccountID uint32 = 1000

// Registry is the in-memory form of _account_registry.yaml.
//
//	schema_version: v1
//	next_account_id: 1000          # high-water mark; the NEXT id to hand out
//	allocations:
//	  tenant-alpha: 1000
//	  tenant-beta:  1001
//
// NextAccountID is the high-water mark — strictly greater than every value
// in Allocations once any tenant has been allocated. It only ever
// increases, even across tenant deletions (no-recycle).
type Registry struct {
	SchemaVersion string            `yaml:"schema_version"`
	NextAccountID uint32            `yaml:"next_account_id"`
	Allocations   map[string]uint32 `yaml:"allocations"`
}

// newRegistry returns an empty registry primed at the reserved floor.
func newRegistry() *Registry {
	return &Registry{
		SchemaVersion: schemaVersion,
		NextAccountID: FirstTenantAccountID,
		Allocations:   map[string]uint32{},
	}
}

// Parse decodes registry YAML. Empty/whitespace input (a brand-new file
// the GitOps layer has not written yet) yields a fresh registry at the
// reserved floor — NOT an error, so first-ever allocation just works.
//
// Fail-safe on a CORRUPT or out-of-spec document (FAIL CLOSED): a parse
// error, an unknown schema version, or a NextAccountID that sits at or
// below an id already handed out (which would re-issue a live id) all
// return an error. Allocation must never proceed from a registry it does
// not fully trust — a wrong id is a cross-tenant data leak, not a
// recoverable glitch.
func Parse(data []byte) (*Registry, error) {
	if isBlank(data) {
		return newRegistry(), nil
	}
	var reg Registry
	if err := yaml.Unmarshal(data, &reg); err != nil {
		return nil, fmt.Errorf("parse account registry: %w", err)
	}
	if reg.SchemaVersion != "" && reg.SchemaVersion != schemaVersion {
		return nil, fmt.Errorf("account registry schema %q is newer than this binary supports (%q); refusing to allocate",
			reg.SchemaVersion, schemaVersion)
	}
	if reg.Allocations == nil {
		reg.Allocations = map[string]uint32{}
	}
	// Repair an under-set high-water mark up to the reserved floor (a
	// hand-written / empty-but-present file). This RAISES, never lowers,
	// the counter — it cannot cause a reuse.
	if reg.NextAccountID < FirstTenantAccountID {
		reg.NextAccountID = FirstTenantAccountID
	}
	// Integrity (FAIL CLOSED on every shape that maps a tenant to the wrong
	// partition). Three checks, each guarding a distinct cross-tenant leak:
	//   - id >= next_account_id : the next allocation would re-issue a live id.
	//   - id <  FirstTenant...  : a tenant sits in the reserved/platform range.
	//   - two tenants share id  : their VictoriaLogs streams merge — the exact
	//     leak this package exists to prevent. The monotonic allocator never
	//     produces a duplicate, so this is only reachable via a hand-edit /
	//     external corruption of the committed file — precisely the threat
	//     model the other two checks already assume, so we close it too rather
	//     than leave a hole in an explicitly fail-closed boundary.
	seen := make(map[uint32]string, len(reg.Allocations))
	for tenant, id := range reg.Allocations {
		if id >= reg.NextAccountID {
			return nil, fmt.Errorf("account registry corrupt: tenant %q holds id %d at/above next_account_id %d (would re-issue a live id)",
				tenant, id, reg.NextAccountID)
		}
		if id < FirstTenantAccountID {
			return nil, fmt.Errorf("account registry corrupt: tenant %q holds reserved id %d (< %d)",
				tenant, id, FirstTenantAccountID)
		}
		if other, dup := seen[id]; dup {
			// Name the pair in a stable order so the error is deterministic
			// regardless of map iteration order.
			lo, hi := tenant, other
			if hi < lo {
				lo, hi = hi, lo
			}
			return nil, fmt.Errorf("account registry corrupt: tenants %q and %q both hold id %d (cross-tenant log merge)",
				lo, hi, id)
		}
		seen[id] = tenant
	}
	if reg.SchemaVersion == "" {
		reg.SchemaVersion = schemaVersion
	}
	return &reg, nil
}

// Marshal renders the registry back to YAML. Allocations are emitted in
// id order (deterministic) so the committed file diffs cleanly and a
// reviewer reads it as an append-only ledger.
func (r *Registry) Marshal() ([]byte, error) {
	r.SchemaVersion = schemaVersion
	if r.Allocations == nil {
		r.Allocations = map[string]uint32{}
	}
	// yaml.v3 emits a Go map in key order already, but to make the ledger
	// read append-only (by id, the order tenants were onboarded) we render
	// via an explicit ordered node.
	doc := yaml.Node{Kind: yaml.MappingNode}
	addScalar(&doc, "schema_version", r.SchemaVersion)
	addUint(&doc, "next_account_id", r.NextAccountID)

	allocNode := yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
	for _, t := range r.tenantsByID() {
		addUint(&allocNode, t, r.Allocations[t])
	}
	doc.Content = append(doc.Content, strNode("allocations"), &allocNode)

	return yaml.Marshal(&doc)
}

// Lookup returns the id allocated to tenantID, or (0, false) if none.
func (r *Registry) Lookup(tenantID string) (uint32, bool) {
	id, ok := r.Allocations[tenantID]
	return id, ok
}

// ensure is the pure allocation calculation: it returns tenantID's id and
// reports whether the registry was MUTATED (a fresh allocation).
//
// Idempotent: an already-allocated tenant returns its existing id with
// changed=false (no mutation, so the caller commits nothing). A new tenant
// takes NextAccountID, the counter advances by one, and changed=true.
//
// This is the only place ids are minted, so the monotonic / no-recycle /
// explicit-counter invariants live in one ~5-line function the tests pin.
func (r *Registry) ensure(tenantID string) (id uint32, changed bool, err error) {
	if tenantID == "" {
		return 0, false, fmt.Errorf("account: empty tenant id")
	}
	if existing, ok := r.Allocations[tenantID]; ok {
		return existing, false, nil
	}
	if r.NextAccountID < FirstTenantAccountID { // defensive; Parse already floors
		r.NextAccountID = FirstTenantAccountID
	}
	if r.NextAccountID == ^uint32(0) {
		return 0, false, fmt.Errorf("account: id space exhausted at %d", r.NextAccountID)
	}
	id = r.NextAccountID
	r.Allocations[tenantID] = id
	r.NextAccountID++
	return id, true, nil
}

// tenantsByID returns tenant ids sorted by their allocated AccountID
// (ascending) — onboarding order, the natural ledger order.
func (r *Registry) tenantsByID() []string {
	ts := make([]string, 0, len(r.Allocations))
	for t := range r.Allocations {
		ts = append(ts, t)
	}
	sort.Slice(ts, func(i, j int) bool {
		return r.Allocations[ts[i]] < r.Allocations[ts[j]]
	})
	return ts
}

// isBlank reports whether data is empty or all whitespace.
func isBlank(data []byte) bool {
	for _, b := range data {
		switch b {
		case ' ', '\t', '\r', '\n':
		default:
			return false
		}
	}
	return true
}

// strNode / intNode build explicitly-tagged scalar nodes. Tagging keys as
// !!str matters: a tenant id that LOOKS numeric or boolean (e.g. "123" or
// "true") would otherwise be emitted untagged and re-parse as an int/bool
// key, corrupting the ledger. !!int on the values keeps the counts bare
// (unquoted) so the file reads naturally.
func strNode(v string) *yaml.Node {
	return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: v}
}

func intNode(v uint32) *yaml.Node {
	return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!int", Value: fmt.Sprintf("%d", v)}
}

func addScalar(m *yaml.Node, key, val string) {
	m.Content = append(m.Content, strNode(key), strNode(val))
}

func addUint(m *yaml.Node, key string, val uint32) {
	m.Content = append(m.Content, strNode(key), intNode(val))
}
