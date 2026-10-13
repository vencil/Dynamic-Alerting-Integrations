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
//   - Written FIRST. An allocation raises the mark to the new id before the
//     registry write. A failure between the two leaves the mark one id ahead,
//     never behind, so there is no moment at which an allocated id is
//     unrecorded. (Writing the registry first would let a failed mark write
//     followed by a registry rollback pass as "mark lagging" and be healed.)
//   - Compared before every allocation (judgeHighWater):
//     next-1 >= mark  the registry holds every id the mark knows of; a mark
//     below next-1 is lagging (first run after upgrade, or
//     its ConfigMap restored older) and is raised;
//     next == mark    exactly the last allocation is missing — its registry
//     write failed after the mark was written, or one write
//     was rolled back. Skipping that id is safe either way:
//     nothing is re-issued;
//     next <  mark    the registry went back more than one allocation —
//     refuse until `tenant-api state restore` merges it.
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

// HighWater stores the largest AccountID ever allocated somewhere a restore
// of the state store does not roll back. tenant-api keeps it in an
// annotation on the federation token-store ConfigMap (token.NewHighWater).
type HighWater interface {
	// Get returns the recorded mark, or 0 when none has been recorded.
	Get(ctx context.Context) (uint32, error)
	// Raise records max(current mark, v) and returns the result. It never
	// lowers the mark, so concurrent raises cannot undo each other.
	Raise(ctx context.Context, v uint32) (uint32, error)
}

// ErrRegistryBehindHighWater refuses an allocation against a registry whose
// counter is more than one allocation below the high-water mark: an older
// copy of the registry was put back, and allocating from it would re-issue
// ids that may still hold logs (ADR-021 cross-tenant log leak).
var ErrRegistryBehindHighWater = errors.New("account: the AccountID registry is behind the high-water mark — an older copy was put back; refusing to allocate (merge it back with `tenant-api state restore`)")

// hwVerdict is judgeHighWater's answer.
type hwVerdict int

const (
	hwOK     hwVerdict = iota // the registry holds every id the mark knows of
	hwSkip                    // exactly the marked id is missing: skip it
	hwBehind                  // more than one allocation missing: refuse
)

// judgeHighWater compares the registry counter with the mark. next is the
// NEXT id to hand out, so the largest id the registry accounts for is
// next-1 (which may itself be unallocated after a restore raised next).
func judgeHighWater(next, mark uint32) hwVerdict {
	switch {
	case next > mark:
		return hwOK
	case next == mark:
		return hwSkip
	default:
		return hwBehind
	}
}

// guardAgainstHighWater reads the mark and applies judgeHighWater to reg,
// skipping a missing last id in place. A nil hw disables the check.
func guardAgainstHighWater(ctx context.Context, hw HighWater, reg *Registry) error {
	if hw == nil {
		return nil
	}
	mark, err := hw.Get(ctx)
	if err != nil {
		return fmt.Errorf("account: read the AccountID high-water mark: %w", err)
	}
	switch judgeHighWater(reg.NextAccountID, mark) {
	case hwSkip:
		if mark == ^uint32(0) {
			return fmt.Errorf("account: id space exhausted at %d", mark)
		}
		slog.Warn("account: skipping an AccountID the high-water mark records but the registry does not",
			"event", "account_id_high_water_skip",
			"skipped_account_id", mark, "next_account_id", reg.NextAccountID)
		reg.NextAccountID = mark + 1
	case hwBehind:
		return fmt.Errorf("%w (next_account_id %d, high-water mark %d)", ErrRegistryBehindHighWater, reg.NextAccountID, mark)
	}
	return nil
}

// raiseHighWater records next-1 — every id the registry accounts for — as
// the mark. A registry that has allocated nothing records nothing.
func raiseHighWater(ctx context.Context, hw HighWater, next uint32) error {
	if hw == nil || next <= FirstTenantAccountID {
		return nil
	}
	if _, err := hw.Raise(ctx, next-1); err != nil {
		return fmt.Errorf("account: record the AccountID high-water mark: %w", err)
	}
	return nil
}
