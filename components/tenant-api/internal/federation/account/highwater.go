package account

// The AccountID high-water mark (#2928): the largest id ever allocated, kept
// OUTSIDE the state store so that putting an older registry back does not
// roll it back too.
//
// The registry's own counter cannot defend itself: a backup applied over
// `tenant-api-state` brings back an older next_account_id together with an
// older allocation table, and the two agree with each other. The mark is the
// independent witness. Ordering and the three cases below are what make it
// one:
//
//   - Reserved FIRST. Before the registry write, an allocation raises the
//     mark to the last id it is about to hand out, recording alongside it the
//     registry's next_account_id it planned from (the base). A failure between
//     the two writes leaves the mark ahead, never behind, so there is no
//     moment at which an id in the registry is unrecorded. (Writing the
//     registry first would let a failed mark write followed by a registry
//     rollback pass as "mark lagging" and be healed.)
//   - Compared before every allocation (judgeHighWater):
//     next > mark          the registry holds every id the mark knows of; a
//     mark below next-1 is lagging (first run after
//     upgrade, or its ConfigMap restored older) and is raised;
//     base <= next <= mark the last reservation never reached the registry —
//     its write failed, or exactly that write was rolled
//     back. Skipping past the mark is safe either way:
//     nothing is re-issued. A Backfill batch is one
//     reservation, so a failed batch is skipped whole;
//     next <  base         the registry went back past a state it had
//     already reached — refuse until `tenant-api state
//     restore` merges it.
//
// It guards against accidents (an old backup applied, a GitOps sync writing
// an exported copy back), not against someone who can write the ConfigMap
// holding it — they can lower the mark too; cluster RBAC owns that, as it
// owns direct edits of the registry (ADR-021).

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
)

// Mark is the high-water mark: Max is the largest AccountID ever reserved;
// Base is the registry's next_account_id when Max was reserved, so a
// registry still at or above Base has merely not received that reservation.
// Base 0 means unknown and is read as Max (only the last id may be missing).
type Mark struct {
	Max  uint32
	Base uint32
}

// HighWater stores the mark somewhere a restore of the state store does not
// roll back. tenant-api keeps it in annotations on the federation
// token-store ConfigMap (token.NewHighWater).
type HighWater interface {
	// Get returns the recorded mark, or the zero Mark when none is recorded.
	Get(ctx context.Context) (Mark, error)
	// Raise records m when m.Max is above the current Max, Base included, and
	// returns the mark now recorded. It never lowers the mark, so concurrent
	// raises cannot undo each other.
	Raise(ctx context.Context, m Mark) (Mark, error)
}

// ErrRegistryBehindHighWater refuses an allocation against a registry whose
// counter went back past a state it had already reached: an older copy of
// the registry was put back, and allocating from it would re-issue ids that
// may still hold logs (ADR-021 cross-tenant log leak).
var ErrRegistryBehindHighWater = errors.New("account: the AccountID registry is behind the high-water mark — an older copy was put back; refusing to allocate (merge it back with `tenant-api state restore`)")

// hwVerdict is judgeHighWater's answer.
type hwVerdict int

const (
	hwOK     hwVerdict = iota // the registry holds every id the mark knows of
	hwSkip                    // the last reservation never reached the registry: skip it
	hwBehind                  // the registry went back: refuse
)

// judgeHighWater compares the registry counter with the mark. next is the
// NEXT id to hand out, so the largest id the registry accounts for is
// next-1 (which may itself be unallocated after a restore raised next).
func judgeHighWater(next uint32, m Mark) hwVerdict {
	if next > m.Max {
		return hwOK
	}
	base := m.Base
	if base == 0 || base > m.Max {
		base = m.Max
	}
	if next >= base {
		return hwSkip
	}
	return hwBehind
}

// guardAgainstHighWater reads the mark and applies judgeHighWater to reg,
// skipping a reservation that never reached the registry in place. ours is
// the reservation this call already made on an earlier attempt of the same
// transform (the zero Mark if none): finding the registry where that attempt
// left it, the plan is still good and nothing is skipped. A nil hw disables
// the check.
func guardAgainstHighWater(ctx context.Context, hw HighWater, reg *Registry, ours Mark) error {
	if hw == nil {
		return nil
	}
	m, err := hw.Get(ctx)
	if err != nil {
		return fmt.Errorf("account: read the AccountID high-water mark: %w", err)
	}
	if ours.Max != 0 && m == ours && reg.NextAccountID == ours.Base {
		return nil
	}
	switch judgeHighWater(reg.NextAccountID, m) {
	case hwSkip:
		if m.Max == ^uint32(0) {
			return fmt.Errorf("account: id space exhausted at %d", m.Max)
		}
		slog.Warn("account: skipping AccountIDs the high-water mark reserved but the registry never received",
			"event", "account_id_high_water_skip",
			"next_account_id", reg.NextAccountID, "high_water", m.Max, "skipped_through", m.Max)
		reg.NextAccountID = m.Max + 1
	case hwBehind:
		return fmt.Errorf("%w (next_account_id %d, high-water mark %d reserved from next_account_id %d)",
			ErrRegistryBehindHighWater, reg.NextAccountID, m.Max, m.Base)
	}
	return nil
}

// reserveHighWater records, before the registry write, every id up to
// next-1 as reserved from base — the registry's next_account_id as read.
// It returns the reservation for the caller's retry (see
// guardAgainstHighWater). A registry that has allocated nothing records
// nothing.
func reserveHighWater(ctx context.Context, hw HighWater, base, next uint32) (Mark, error) {
	if hw == nil || next <= FirstTenantAccountID {
		return Mark{}, nil
	}
	m := Mark{Max: next - 1, Base: base}
	if _, err := hw.Raise(ctx, m); err != nil {
		return Mark{}, fmt.Errorf("account: record the AccountID high-water mark: %w", err)
	}
	return m, nil
}

// raiseHighWater brings a lagging mark up to next-1, the ids the registry
// already holds.
func raiseHighWater(ctx context.Context, hw HighWater, next uint32) error {
	_, err := reserveHighWater(ctx, hw, next-1, next)
	return err
}
