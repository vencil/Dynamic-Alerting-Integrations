package federation

import (
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync/atomic"
	"time"
)

// federationSubsetDir is the conf.d subdirectory holding per-tenant
// federation metric-subset files (ADR-020 IV-2e): the subset for tenant
// X lives at conf.d/_federation/X.yaml.
const federationSubsetDir = "_federation"

// orphanedTokens / orphanedSubsets hold the most recent OrphanDetector
// scan result, exposed to the /metrics handler via OrphanCounts. They
// are gauges — each scan overwrites them; both are 0 before the first
// scan and when no detector runs.
var (
	orphanedTokens  atomic.Int64
	orphanedSubsets atomic.Int64
)

// OrphanCounts returns the federation-artifact orphan counts from the
// most recent OrphanDetector scan: live token Records, and subset
// files, whose owning tenant is no longer present in conf.d. Consumed
// by the tenant-api /metrics endpoint (handler.MetricsHandler).
func OrphanCounts() (tokens, subsetFiles int64) {
	return orphanedTokens.Load(), orphanedSubsets.Load()
}

// OrphanReport is the result of one orphan scan.
type OrphanReport struct {
	Tokens  []string // token_id of each live token whose tenant left conf.d
	Subsets []string // tenant id of each stale conf.d/_federation/<id>.yaml
}

// empty reports whether the scan found nothing.
func (r OrphanReport) empty() bool {
	return len(r.Tokens) == 0 && len(r.Subsets) == 0
}

// scanOrphans diffs federation artifacts against the set of tenants
// currently present in conf.d. Pure function — no I/O — so the diff
// logic is unit-testable without a filesystem or a ConfigMap.
func scanOrphans(known map[string]struct{}, records []Record, subsetTenants []string) OrphanReport {
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
func scanKnownTenants(configDir string) (map[string]struct{}, error) {
	entries, err := os.ReadDir(configDir)
	if err != nil {
		return nil, err
	}
	known := make(map[string]struct{})
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		id, ok := tenantIDFromFile(e.Name())
		if !ok || strings.HasPrefix(e.Name(), "_") {
			continue
		}
		known[id] = struct{}{}
	}
	return known, nil
}

// scanSubsetTenants returns the tenant ids that have a federation subset
// file conf.d/_federation/<id>.yaml. A missing _federation directory is
// not an error — it just means no tenant has configured a subset yet.
func scanSubsetTenants(configDir string) ([]string, error) {
	dir := filepath.Join(configDir, federationSubsetDir)
	entries, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if id, ok := tenantIDFromFile(e.Name()); ok {
			out = append(out, id)
		}
	}
	return out, nil
}

// tenantIDFromFile maps a YAML filename to its tenant id (filename minus
// the .yaml/.yml extension), reporting false for non-YAML files.
func tenantIDFromFile(name string) (string, bool) {
	switch {
	case strings.HasSuffix(name, ".yaml"):
		return strings.TrimSuffix(name, ".yaml"), true
	case strings.HasSuffix(name, ".yml"):
		return strings.TrimSuffix(name, ".yml"), true
	default:
		return "", false
	}
}

// OrphanDetector periodically reports federation artifacts left behind
// by an incomplete tenant offboarding — live token Records and
// conf.d/_federation/<tenant>.yaml subset files whose owning tenant is
// no longer in conf.d (ADR-020 #521).
//
// It only OBSERVES: it emits a WARN log and updates the orphan gauges
// (OrphanCounts). It never revokes a token or deletes a file — cleanup
// is the offboarding runbook's job. An auto-revoking reconciler would
// risk misfiring on a transient conf.d glitch (a GitOps sync in flight,
// a broken file); a warn-only detector gives the same safety net with
// zero misfire risk. See docs/internal/tenant-offboarding-runbook.md.
type OrphanDetector struct {
	configDir string
	records   func() ([]Record, error)
}

// NewOrphanDetector builds a detector. records lists every live token
// Record across all tenants (Manager.ListAllRecords).
func NewOrphanDetector(configDir string, records func() ([]Record, error)) *OrphanDetector {
	return &OrphanDetector{configDir: configDir, records: records}
}

// scanOnce runs one detection pass: it updates the orphan gauges and
// emits a WARN log if anything is orphaned. A read error on conf.d or
// on the token store aborts the pass WITHOUT touching the gauges — a
// transient failure must never be read as "everything is orphaned".
func (d *OrphanDetector) scanOnce() {
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
func (d *OrphanDetector) Run(interval time.Duration, stopCh <-chan struct{}) {
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
