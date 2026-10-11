package account

// Restoring the AccountID registry from a backup (#2884).
//
// Overwriting the registry with a backup is the one routine operation that
// can re-issue an id: a tenant allocated after the backup was taken holds an
// id at or above the backup's next_account_id, so the next allocation after
// the overwrite hands that same id to someone else — a cross-tenant log leak
// the startup check cannot see (the restored registry is well-formed). A
// restore therefore never overwrites: it merges, and the counter only rises.

import (
	"fmt"
	"sort"
	"strings"
)

// TokenAllocation is the id a live logs-plane federation token carries for
// its tenant. The token store keeps a record until the token expires, so a
// tenant allocated after a backup is recoverable from it only while one of
// its logs tokens is still valid.
type TokenAllocation struct {
	TenantID  string
	AccountID uint32
	TokenID   string
}

// RestoreConflict is one tenant, or one id, the sources disagree on.
type RestoreConflict struct {
	// Subject is the tenant id, or "account_id <n>" when two tenants claim
	// one id.
	Subject string
	// Claims lists each source's claim, e.g. "current: 1001".
	Claims []string
}

// RestoreConflictError refuses a restore whose sources disagree. Nothing is
// written; the operator decides which claim is right.
type RestoreConflictError struct{ Conflicts []RestoreConflict }

func (e *RestoreConflictError) Error() string {
	var b strings.Builder
	fmt.Fprintf(&b, "account registry restore refused: %d conflict(s) between the current registry, the backup and live logs tokens; nothing written", len(e.Conflicts))
	for _, c := range e.Conflicts {
		fmt.Fprintf(&b, "\n  %s: %s", c.Subject, strings.Join(c.Claims, ", "))
	}
	return b.String()
}

// RestoreReport says what MergeForRestore changed.
type RestoreReport struct {
	// Added lists the tenants the merged registry holds that the current
	// one did not, with their ids and the source each came from.
	Added []string
	// NextBefore and NextAfter are the current and merged next_account_id.
	NextBefore, NextAfter uint32
}

// Changed reports whether the merge differs from the current registry.
func (r RestoreReport) Changed() bool {
	return len(r.Added) > 0 || r.NextAfter != r.NextBefore
}

// MergeForRestore merges the backup registry and the ids live logs tokens
// carry into the current registry:
//
//   - allocations are the union of the three; a tenant held at two
//     different ids, or an id held by two tenants, is a conflict and the
//     whole merge is refused (RestoreConflictError);
//   - next_account_id is the largest of the current one, the backup's, and
//     one above the largest id in the union — it never decreases.
//
// current may be blank (a lost registry), as may backup. Both must
// otherwise pass Parse. The result passes Parse.
func MergeForRestore(current, backup []byte, tokens []TokenAllocation) (*Registry, RestoreReport, error) {
	cur, err := Parse(current)
	if err != nil {
		return nil, RestoreReport{}, fmt.Errorf("current registry: %w", err)
	}
	bak, err := Parse(backup)
	if err != nil {
		return nil, RestoreReport{}, fmt.Errorf("backup registry: %w", err)
	}

	type claim struct {
		id     uint32
		source string
	}
	claims := map[string][]claim{}
	add := func(tenant string, id uint32, source string) {
		for _, c := range claims[tenant] {
			if c.id == id {
				return
			}
		}
		claims[tenant] = append(claims[tenant], claim{id, source})
	}
	for t, id := range cur.Allocations {
		add(t, id, "current")
	}
	for t, id := range bak.Allocations {
		add(t, id, "backup")
	}
	for _, tk := range tokens {
		if tk.TenantID == "" || tk.AccountID == 0 {
			continue
		}
		if tk.AccountID < FirstTenantAccountID {
			return nil, RestoreReport{}, fmt.Errorf("live logs token %s of tenant %q carries reserved account_id %d (< %d)",
				tk.TokenID, tk.TenantID, tk.AccountID, FirstTenantAccountID)
		}
		add(tk.TenantID, tk.AccountID, "token "+tk.TokenID)
	}

	var conflicts []RestoreConflict
	merged := newRegistry()
	holder := map[uint32]string{}
	tenants := make([]string, 0, len(claims))
	for t := range claims {
		tenants = append(tenants, t)
	}
	sort.Strings(tenants)
	for _, t := range tenants {
		cs := claims[t]
		if len(cs) > 1 {
			c := RestoreConflict{Subject: "tenant " + t}
			for _, cl := range cs {
				c.Claims = append(c.Claims, fmt.Sprintf("%s: %d", cl.source, cl.id))
			}
			conflicts = append(conflicts, c)
			continue
		}
		id := cs[0].id
		if other, dup := holder[id]; dup {
			conflicts = append(conflicts, RestoreConflict{
				Subject: fmt.Sprintf("account_id %d", id),
				Claims:  []string{"tenant " + other, "tenant " + t},
			})
			continue
		}
		holder[id] = t
		merged.Allocations[t] = id
	}
	if len(conflicts) > 0 {
		return nil, RestoreReport{}, &RestoreConflictError{Conflicts: conflicts}
	}

	next := cur.NextAccountID
	if bak.NextAccountID > next {
		next = bak.NextAccountID
	}
	for _, id := range merged.Allocations {
		if id >= next {
			if id == ^uint32(0) {
				return nil, RestoreReport{}, fmt.Errorf("account: id space exhausted at %d", id)
			}
			next = id + 1
		}
	}
	merged.NextAccountID = next

	rep := RestoreReport{NextBefore: cur.NextAccountID, NextAfter: next}
	for _, t := range merged.tenantsByID() {
		if _, had := cur.Allocations[t]; !had {
			rep.Added = append(rep.Added, fmt.Sprintf("%s=%d (%s)", t, merged.Allocations[t], claims[t][0].source))
		}
	}
	return merged, rep, nil
}
