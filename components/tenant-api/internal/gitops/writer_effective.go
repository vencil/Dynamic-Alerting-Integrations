package gitops

import (
	"errors"
	"fmt"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// effectiveResolveAttempts bounds ResolveEffective's walks: the first on the
// prior, each retry cold. Two retries absorb a write landing between a walk
// and the resolve's reads (each retry walks after it); a tree rewritten
// faster than a cold walk plus a resolve keeps failing, closed.
const effectiveResolveAttempts = 3

// ResolveEffective is GET /tenants/{id}/effective's resolve (#1977): one
// bounded conf.d walk on this Writer's prior, then
// cfg.ResolveEffectiveFromScan over it, which reads only the tenant's own
// file, the root `_` files and the defaults carriers of its directories.
// Before it, every request walked cold and the exporter's whole-tree build
// re-read and re-parsed every tenant file in conf.d.
//
// ⛔ ITS OWN WALK, NEVER A JOINED ONE. scanTreeForRead hands a caller a read
// walk already in flight, which may have started before a write that has
// since returned — a GET that follows a PUT could answer the tree from
// before it. Each call here starts a walk of its own, after the call began,
// so a write that returned before the GET is in the tree it walks (the
// walker judges the written file's moved stat, as for every walk on the
// prior).
//
// The walk is walkTree's: bounded by treeScanTimeout and published as the
// next prior only when it completed in time. ⚠️ Only the walk is bounded:
// the resolve's reads of the tenant's files after it are not. Its breaker is the read
// path's (stuckReadTreeScans): a walk this endpoint left blocked fails later
// reads at once — previews and /effective — and never a write, whose walks
// count in stuckTreeScans. Sharing the read breaker rather than keeping a
// third: a file that blocks this walk blocks a preview's the same way, so a
// separate counter would only let one more goroutine block on it.
//
// cfg.ErrScanStale — a file the resolve needs no longer hashes to what the
// walk recorded — retries on a COLD walk, which also replaces the prior: the
// walker's known gap (a same-size rewrite inside one mtime tick of a file
// the prior recorded within TreeScanMtimeGuard) carries a stale hash on the
// fast-path walk after walk, and only a walk without the prior reads past
// it. A stale file the resolve does not read is not seen (see
// cfg.ResolveEffectiveFromScan).
//
// Errors: the resolve's own (cfg.ErrTenantNotFound, *cfg.DuplicateTenantError,
// *cfg.DecodeError, …) as cfg.ResolveEffective returns them; a walk that
// failed, timed out or met the breaker, and a resolve still stale after the
// last attempt, as other errors (the handler's 500).
func (w *Writer) ResolveEffective(tenantID string) (*cfg.EffectiveConfig, error) {
	for attempt := 1; ; attempt++ {
		scan, err := w.walkTreeFrom(&w.stuckReadTreeScans, attempt == 1)
		if err != nil {
			return nil, fmt.Errorf("conf.d walk for tenant %s: %w", tenantID, err)
		}
		if w.onEffectiveWalked != nil {
			w.onEffectiveWalked()
		}
		ec, err := cfg.ResolveEffectiveFromScan(scan, tenantID)
		if errors.Is(err, cfg.ErrScanStale) && attempt < effectiveResolveAttempts {
			continue
		}
		return ec, err
	}
}
