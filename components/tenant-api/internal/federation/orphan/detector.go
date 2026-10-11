package orphan

import (
	"context"
	"log/slog"
	"sort"
	"sync/atomic"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/statestore"
)

// orphanedTokens / orphanedSubsets hold the most recent Detector
// scan result, exposed to the /metrics handler via OrphanCounts. They
// are gauges — each scan overwrites them; both are 0 before the first
// scan and when no detector runs.
var (
	orphanedTokens  atomic.Int64
	orphanedSubsets atomic.Int64
)

// OrphanCounts returns the federation-artifact orphan counts from the
// most recent Detector scan: live token Records, and subset
// files, whose owning tenant is no longer present in conf.d. Consumed
// by the tenant-api /metrics endpoint (handler.MetricsHandler).
func OrphanCounts() (tokens, subsetFiles int64) {
	return orphanedTokens.Load(), orphanedSubsets.Load()
}

// OrphanReport is the result of one orphan scan.
type OrphanReport struct {
	Tokens  []string // token_id of each live token whose tenant left conf.d
	Subsets []string // tenant id of each stale federation subset record (state store, #2856)
}

// empty reports whether the scan found nothing.
func (r OrphanReport) empty() bool {
	return len(r.Tokens) == 0 && len(r.Subsets) == 0
}

// scanOrphans diffs federation artifacts against the set of tenants
// currently present in conf.d. Pure function — no I/O — so the diff
// logic is unit-testable without a filesystem or a ConfigMap.
func scanOrphans(known map[string]struct{}, records []token.Record, subsetTenants []string) OrphanReport {
	var rep OrphanReport
	for _, r := range records {
		if _, ok := known[r.TenantID]; !ok {
			rep.Tokens = append(rep.Tokens, r.TokenID)
		}
	}
	for _, t := range subsetTenants {
		if _, ok := known[t]; !ok {
			rep.Subsets = append(rep.Subsets, t)
		}
	}
	sort.Strings(rep.Tokens)
	sort.Strings(rep.Subsets)
	return rep
}

// scanKnownTenants returns the set of tenant ids present in configDir —
// every <id>.yaml / <id>.yml that is not an underscore-prefixed special
// file (_defaults.yaml, _rbac.yaml, _federation_policy.yaml, …) and not
// a subdirectory. On a read error it returns (nil, err); callers MUST
// treat the error as "skip this pass" and never as "no tenants exist",
// which would flag every artifact as orphaned.
//
// ⛔ Content-blind on purpose (#1680): enumeration is confd.ListTenantFiles,
// and confd.ReadTenantFile is deliberately NOT consulted. A tenant whose file
// is broken is not an offboarded tenant — skipping it would under-count the
// live set, which is exactly the direction that flags its artifacts as
// orphaned. This detector must only ever err toward "still live".
func scanKnownTenants(configDir string) (map[string]struct{}, error) {
	files, err := confd.ListTenantFiles(configDir)
	if err != nil {
		return nil, err
	}
	known := make(map[string]struct{}, len(files))
	for _, f := range files {
		known[f.ID] = struct{}{}
	}
	return known, nil
}

// SubsetTenants lists the tenants that have a federation subset record in
// the state store (#2856; they used to be conf.d/_federation/<id>.yaml).
func SubsetTenants(s statestore.Store) func() ([]string, error) {
	return func() ([]string, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		keys, err := s.Keys(ctx, statestore.SubsetPrefix)
		if err != nil {
			return nil, err
		}
		out := make([]string, 0, len(keys))
		for _, k := range keys {
			if id, ok := statestore.SubsetTenant(k); ok {
				out = append(out, id)
			}
		}
		return out, nil
	}
}

// Detector periodically reports federation artifacts left behind
// by an incomplete tenant offboarding — live token Records and
// federation subset records whose owning tenant is no longer in conf.d
// (ADR-020 #521).
//
// It only OBSERVES: it emits a WARN log and updates the orphan gauges
// (OrphanCounts). It never revokes a token or deletes a file — cleanup
// is the offboarding runbook's job. An auto-revoking reconciler would
// risk misfiring on a transient conf.d glitch (a GitOps sync in flight,
// a broken file); a warn-only detector gives the same safety net with
// zero misfire risk. See docs/internal/tenant-offboarding-runbook.md.
type Detector struct {
	configDir string
	subsets   func() ([]string, error)
	records   func() ([]token.Record, error)
}

// NewDetector builds a detector. subsets lists the tenants that have a
// federation subset (SubsetTenants); records lists every live token
// Record across all tenants (Manager.ListAllRecords).
func NewDetector(configDir string, subsets func() ([]string, error), records func() ([]token.Record, error)) *Detector {
	return &Detector{configDir: configDir, subsets: subsets, records: records}
}

// scanOnce runs one detection pass: it updates the orphan gauges and
// emits a WARN log if anything is orphaned. A read error on conf.d or
// on the token store aborts the pass WITHOUT touching the gauges — a
// transient failure must never be read as "everything is orphaned".
func (d *Detector) scanOnce() {
	known, err := scanKnownTenants(d.configDir)
	if err != nil {
		slog.Warn("federation orphan detector: cannot scan conf.d, skipping pass", "error", err)
		return
	}
	subsets, err := d.subsets()
	if err != nil {
		slog.Warn("federation orphan detector: cannot list federation subsets, skipping pass", "error", err)
		return
	}
	records, err := d.records()
	if err != nil {
		slog.Warn("federation orphan detector: cannot list token records, skipping pass", "error", err)
		return
	}
	rep := scanOrphans(known, records, subsets)
	orphanedTokens.Store(int64(len(rep.Tokens)))
	orphanedSubsets.Store(int64(len(rep.Subsets)))
	if !rep.empty() {
		slog.Warn("federation offboarding incomplete: orphaned artifacts found — see docs/internal/tenant-offboarding-runbook.md",
			"orphaned_tokens", rep.Tokens,
			"orphaned_subset_files", rep.Subsets) // key kept: log queries read it
	}
}

// Run scans once immediately, then every interval, until stopCh closes.
func (d *Detector) Run(interval time.Duration, stopCh <-chan struct{}) {
	d.scanOnce()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-stopCh:
			return
		case <-ticker.C:
			d.scanOnce()
		}
	}
}
