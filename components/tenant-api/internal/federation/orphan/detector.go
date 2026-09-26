package orphan

import (
	"log/slog"
	"os"
	"sort"
	"sync/atomic"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/federation/token"
)

// federationSubsetDir is the conf.d subdirectory holding per-tenant
// federation metric-subset files (ADR-020 IV-2e): the subset for tenant
// X lives at conf.d/_federation/X.yaml (or X.yml — any spelling
// confd.TenantIDFromFile accepts, #1698).
const federationSubsetDir = confd.FederationSubsetDirName

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
	Subsets []string // tenant id of each stale conf.d/_federation/<id>.{yaml,yml} (any extension case)
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

// scanSubsetTenants returns the tenant ids that have a federation subset
// file under conf.d/_federation/. A missing _federation directory is
// not an error — it just means no tenant has configured a subset yet.
//
// Each id appears ONCE however many spellings claim it (#1698): the
// orphan gauge counts tenants, and `<id>.yaml` beside `<id>.yml` is one
// tenant, not two orphans. Such an id is also logged — the read and
// write handlers refuse it with 409 (confd.ErrAmbiguousTenantFile), and
// this periodic scan is the only place an operator hears about it
// without first tripping over the 409. Observe-only, like the rest of
// the detector: nothing is renamed or deleted.
func scanSubsetTenants(configDir string) ([]string, error) {
	// Same single enumeration loop as conf.d itself (confd.ListTenantFiles,
	// #1680), and content-blind for the same reason as scanKnownTenants.
	entries, err := confd.ListTenantFiles(confd.FederationSubsetDir(configDir))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []string
	files := make(map[string][]string)
	for _, f := range entries {
		if len(files[f.ID]) == 0 {
			out = append(out, f.ID)
		}
		files[f.ID] = append(files[f.ID], f.Name)
	}
	for _, id := range out {
		if len(files[id]) > 1 {
			slog.Warn("federation subset: more than one file claims one tenant; GET/PUT on its subset return 409 until one is removed",
				"tenant", id, "files", files[id])
		}
	}
	return out, nil
}

// Detector periodically reports federation artifacts left behind
// by an incomplete tenant offboarding — live token Records and
// conf.d/_federation/<tenant>.{yaml,yml} subset files (any extension
// case) whose owning tenant is
// no longer in conf.d (ADR-020 #521).
//
// It only OBSERVES: it emits a WARN log and updates the orphan gauges
// (OrphanCounts). It never revokes a token or deletes a file — cleanup
// is the offboarding runbook's job. An auto-revoking reconciler would
// risk misfiring on a transient conf.d glitch (a GitOps sync in flight,
// a broken file); a warn-only detector gives the same safety net with
// zero misfire risk. See docs/internal/tenant-offboarding-runbook.md.
type Detector struct {
	configDir string
	records   func() ([]token.Record, error)
}

// NewDetector builds a detector. records lists every live token
// Record across all tenants (Manager.ListAllRecords).
func NewDetector(configDir string, records func() ([]token.Record, error)) *Detector {
	return &Detector{configDir: configDir, records: records}
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
	subsets, err := scanSubsetTenants(d.configDir)
	if err != nil {
		slog.Warn("federation orphan detector: cannot scan _federation/, skipping pass", "error", err)
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
			"orphaned_subset_files", rep.Subsets)
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
