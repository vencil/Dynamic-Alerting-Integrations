package account

// Allocator is the commit-on-write face of the pure Registry core. It
// turns "give tenant X an AccountID" into a serialised, Git-tracked
// read-modify-write on conf.d/_account_registry.yaml.
//
// Persistence lives on the existing gitops single-writer trail (ADR-009 /
// ADR-021): no external stateful DB. The registry file is `_`-prefixed so
// the threshold-exporter config loader skips it (it is not a tenant), yet
// it is committed and versioned like every other conf.d state file, giving
// the AccountID ledger a full audit history for free.

import (
	"context"
	"fmt"
	"sort"
)

// RegistryFileName is the conf.d file holding the AccountID ledger. The
// leading underscore makes the threshold-exporter loader skip it (it is
// platform state, not a tenant), exactly like _groups.yaml / _views.yaml.
const RegistryFileName = "_account_registry.yaml"

// commitEntity labels the registry commit + onWrite event.
const commitEntity = "account-registry"

// RegistryWriter is the slice of gitops.Writer the allocator needs: a
// serialised read-modify-write-commit of a single conf.d file. Defined
// here (consumer side) so the account package does not import gitops and
// stays unit-testable with a fake — *gitops.Writer satisfies it via its
// MutateConfigFile method.
type RegistryWriter interface {
	MutateConfigFile(ctx context.Context, filename, entityType, authorEmail string, transform func(current []byte) (next []byte, err error)) error
}

// Allocator hands out monotonic AccountIDs, persisting each new allocation
// through the GitOps writer. It holds no mutable state of its own — the
// registry file under the writer's mutex is the single source of truth, so
// the Allocator is safe for concurrent use WITHIN a process. The deployment
// is pinned single-writer (helm tenant-api: replicaCount=1 + Recreate); were
// two writer processes ever to race, the writer's HEAD-conflict detection
// aborts the loser with ErrConflict rather than double-allocating — it does
// not silently corrupt. Multi-writer coordination (an ADR-023 Lease) is not
// yet built.
type Allocator struct {
	w RegistryWriter
}

// NewAllocator builds an Allocator over the given GitOps writer.
func NewAllocator(w RegistryWriter) *Allocator {
	return &Allocator{w: w}
}

// EnsureAccountID returns tenantID's AccountID, allocating one if the
// tenant has none yet (idempotent allocate-if-missing).
//
// The allocation is computed INSIDE the writer mutex against the
// freshly-read registry (the transform closure below), so two concurrent
// onboardings of different tenants serialise and can never read the same
// high-water mark — neither a duplicate id nor a lost allocation is
// possible. An already-allocated tenant returns its id and commits
// nothing (the transform reports no change).
//
// authorEmail is the operator identity recorded as the git commit author.
func (a *Allocator) EnsureAccountID(ctx context.Context, tenantID, authorEmail string) (uint32, error) {
	if tenantID == "" {
		return 0, fmt.Errorf("account: empty tenant id")
	}

	var allocated uint32
	transform := func(current []byte) ([]byte, error) {
		reg, err := Parse(current)
		if err != nil {
			return nil, err
		}
		id, changed, err := reg.ensure(tenantID)
		if err != nil {
			return nil, err
		}
		allocated = id
		if !changed {
			return nil, nil // idempotent: already allocated → no commit
		}
		return reg.Marshal()
	}

	if err := a.w.MutateConfigFile(ctx, RegistryFileName, commitEntity, authorEmail, transform); err != nil {
		return 0, err
	}
	return allocated, nil
}

// BackfillResult reports what a Backfill pass did.
type BackfillResult struct {
	// Allocated lists tenants that received a NEW id in this pass, in the
	// order they were allocated (id-ascending).
	Allocated []string `json:"allocated"`
	// AlreadyPresent is the count of input tenants that already held an id.
	AlreadyPresent int `json:"already_present"`
}

// Backfill assigns an AccountID to every tenant in tenantIDs that does not
// already have one, in a SINGLE committed registry write. It is the
// one-shot companion to lazy per-issuance allocation: run it once to give
// the whole existing fleet ids without waiting for each tenant to mint its
// first logs token.
//
// Idempotent: tenants that already hold an id are left untouched, and a
// pass in which nobody needs an id commits nothing (returns an empty
// Allocated set). Allocation order is the sorted tenant-id order, so a
// re-run on a superset of tenants is deterministic and monotonic.
//
// authorEmail is recorded as the git commit author.
func (a *Allocator) Backfill(ctx context.Context, tenantIDs []string, authorEmail string) (BackfillResult, error) {
	// Sort so allocation order — and therefore the id each backfilled
	// tenant receives — is deterministic regardless of caller ordering.
	ids := append([]string(nil), tenantIDs...)
	sort.Strings(ids)

	var res BackfillResult
	transform := func(current []byte) ([]byte, error) {
		reg, err := Parse(current)
		if err != nil {
			return nil, err
		}
		res = BackfillResult{} // derive res solely from this single transform run (MutateConfigFile invokes the transform exactly once; this also keeps it correct if a retry-on-conflict is ever added)
		anyChange := false
		for _, t := range ids {
			if t == "" {
				continue
			}
			if _, ok := reg.Lookup(t); ok {
				res.AlreadyPresent++
				continue
			}
			if _, _, err := reg.ensure(t); err != nil {
				return nil, err
			}
			res.Allocated = append(res.Allocated, t)
			anyChange = true
		}
		if !anyChange {
			return nil, nil // nobody needed an id → no commit
		}
		return reg.Marshal()
	}

	if err := a.w.MutateConfigFile(ctx, RegistryFileName, commitEntity, authorEmail, transform); err != nil {
		return BackfillResult{}, err
	}
	return res, nil
}
