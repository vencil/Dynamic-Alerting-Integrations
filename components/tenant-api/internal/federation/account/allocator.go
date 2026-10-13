package account

// Allocator turns "give tenant X an AccountID" into a compare-and-swap
// read-modify-write of the AccountID registry record in tenant-api's state
// store (#2856; it used to be conf.d/_account_registry.yaml committed on the
// pod's local branch and never pushed, so an emptyDir restart lost it).

import (
	"context"
	"errors"
	"fmt"
	"sort"

	"github.com/vencil/tenant-api/internal/statestore"
)

// RegistryFileName is the registry's record key. It keeps the old file name:
// the Vector projection gate mounts the registry under this key.
const RegistryFileName = statestore.KeyAccountRegistry

// commitEntity labels the registry write's audit event and change notice.
const commitEntity = "account-registry"

// RegistryStore is the slice of statestore.Store the allocator needs.
type RegistryStore interface {
	Mutate(ctx context.Context, key string, m statestore.Meta, t statestore.Transform) (bool, error)
}

// FleetSize reports how many tenants conf.d holds.
type FleetSize func() (int, error)

// ErrRegistryReset refuses an allocation against a blank or missing registry
// while tenants exist: their ids were allocated from a ledger that is now
// gone, and allocating from the floor would re-issue them (ADR-021
// cross-tenant log leak). The startup check (VerifyRegistryNotResetWithFleet)
// catches this at boot; this catches the record vanishing while running.
var ErrRegistryReset = errors.New("account: AccountID registry is blank or missing while tenants exist — refusing to allocate (restore the registry)")

// Allocator hands out monotonic AccountIDs. It holds no state of its own:
// the registry record is the single source of truth, and every allocation is
// computed inside the store's compare-and-swap against the freshly-read
// record, so concurrent allocations — in one process or several — neither
// duplicate an id nor lose one (the loser of a race re-runs on fresh data).
type Allocator struct {
	s     RegistryStore
	fleet FleetSize
	hw    HighWater
}

// NewAllocator builds an Allocator over the state store. fleet may be nil
// (no runtime reset check — tests of the pure allocation logic).
func NewAllocator(s RegistryStore, fleet FleetSize) *Allocator {
	return &Allocator{s: s, fleet: fleet}
}

// WithHighWater makes every allocation check the registry against hw first
// and record the new id in hw before the registry write (#2928; see
// highwater.go). Without it the allocator trusts the registry alone.
func (a *Allocator) WithHighWater(hw HighWater) *Allocator {
	a.hw = hw
	return a
}

// parseGuarded parses the current record, refusing a blank one while the
// fleet is non-empty.
func (a *Allocator) parseGuarded(current []byte) (*Registry, error) {
	if isBlank(current) && a.fleet != nil {
		n, err := a.fleet()
		if err != nil {
			return nil, fmt.Errorf("account: cannot count tenants for the registry reset check: %w", err)
		}
		if n > 0 {
			return nil, ErrRegistryReset
		}
	}
	return Parse(current)
}

// EnsureAccountID returns tenantID's AccountID, allocating one if the
// tenant has none yet (idempotent allocate-if-missing). An already-allocated
// tenant returns its id and writes nothing.
//
// authorEmail is recorded in the write's audit event.
func (a *Allocator) EnsureAccountID(ctx context.Context, tenantID, authorEmail string) (uint32, error) {
	if tenantID == "" {
		return 0, fmt.Errorf("account: empty tenant id")
	}
	var allocated uint32
	transform := func(current []byte) ([]byte, error) {
		allocated = 0 // re-derived on every attempt
		reg, err := a.parseGuarded(current)
		if err != nil {
			return nil, err
		}
		if id, ok := reg.Lookup(tenantID); ok {
			allocated = id
			// Idempotent: no registry write. A lagging mark is still
			// brought up to what the registry accounts for.
			return nil, raiseHighWater(ctx, a.hw, reg.NextAccountID)
		}
		if err := guardAgainstHighWater(ctx, a.hw, reg); err != nil {
			return nil, err
		}
		id, _, err := reg.ensure(tenantID)
		if err != nil {
			return nil, err
		}
		// The mark first: a failure after this leaves it ahead of the
		// registry, which the next allocation skips past.
		if err := raiseHighWater(ctx, a.hw, reg.NextAccountID); err != nil {
			return nil, err
		}
		allocated = id
		return reg.Marshal()
	}
	if _, err := a.s.Mutate(ctx, RegistryFileName, statestore.Meta{Entity: commitEntity, Author: authorEmail}, transform); err != nil {
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
// already have one, in a SINGLE registry write. It is the
// one-shot companion to lazy per-issuance allocation: run it once to give
// the whole existing fleet ids without waiting for each tenant to mint its
// first logs token.
//
// Idempotent: tenants that already hold an id are left untouched, and a
// pass in which nobody needs an id writes nothing (returns an empty
// Allocated set). Allocation order is the sorted tenant-id order, so a
// re-run on a superset of tenants is deterministic and monotonic.
//
// authorEmail is recorded in the write's audit event.
func (a *Allocator) Backfill(ctx context.Context, tenantIDs []string, authorEmail string) (BackfillResult, error) {
	// Sort so allocation order — and therefore the id each backfilled
	// tenant receives — is deterministic regardless of caller ordering.
	ids := append([]string(nil), tenantIDs...)
	sort.Strings(ids)

	var res BackfillResult
	transform := func(current []byte) ([]byte, error) {
		res = BackfillResult{} // re-derived on every attempt: the store re-runs this on a lost race
		reg, err := a.parseGuarded(current)
		if err != nil {
			return nil, err
		}
		if err := guardAgainstHighWater(ctx, a.hw, reg); err != nil {
			return nil, err
		}
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
			// nobody needed an id → no write; bring a lagging mark up
			return nil, raiseHighWater(ctx, a.hw, reg.NextAccountID)
		}
		// The mark first, at the last id of the batch.
		if err := raiseHighWater(ctx, a.hw, reg.NextAccountID); err != nil {
			return nil, err
		}
		return reg.Marshal()
	}

	if _, err := a.s.Mutate(ctx, RegistryFileName, statestore.Meta{Entity: commitEntity, Author: authorEmail}, transform); err != nil {
		return BackfillResult{}, err
	}
	return res, nil
}
