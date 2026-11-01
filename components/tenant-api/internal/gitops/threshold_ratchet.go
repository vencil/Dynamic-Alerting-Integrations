package gitops

// #3267 (ADR-038 decision 12): a threshold value the exporter refuses —
// other than a non-finite number, which #3140 refuses strictly
// (nonFiniteWrites) — is judged by the exporter's own classifier
// (cfg.RefusedThresholdWrites), and one the file already holds, read alike
// by the exporter, does not block a write that leaves it so: it is told to
// the author as a notice instead.

import (
	"sort"

	"github.com/vencil/tenant-api/internal/confd"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// refusedThresholdValues is the write gate's #3267 check of yamlContent
// (tcfg: the gate's plain decode of it) against baseRaw, the file the write
// replaces (baseRaw, baseErr: readTenantFile's result). errs are the
// refusals this write makes; notices the values the file already held,
// which the exporter reads exactly as this write leaves them
// (cfg.ThresholdReadAlike — its reading, not the text: re-quoting, spacing,
// a suffix's case or `1_000` for `1000` is no change).
//
// ⛔ STRICT WHENEVER THE BASE IS NOT KNOWN, as ratchetValues: no file
// (a new tenant), a read error, a file parse-tree refuses or one that does
// not decode leave every refusal an error.
//
// Every tenant the body declares is judged and ratcheted (ADR-038 decision
// 12: a file may declare several), each against its own section of the base.
//
// ⚠️ A THRESHOLD WRITTEN AS NULL IS NO WRITE (#2518). The gate's plain
// yaml.Unmarshal holds `k: null` as "", which the classifier refuses like a
// written `k: ""`; the exporter decodes the body through cfg.ParseConfigFile,
// which takes the null out. So when anything is refused the body is judged
// again on that decode — only then, so the common write pays no second one.
func refusedThresholdValues(yamlContent string, tcfg cfg.ThresholdConfig, baseRaw []byte, baseErr error) (errs, notices []string) {
	tenants := tcfg.Tenants
	if !anyRefusedThreshold(tenants) {
		return nil, nil
	}
	if served, err := cfg.ParseConfigFile([]byte(yamlContent)); err == nil {
		tenants = served.Tenants
	}
	prior := thresholdBase(baseRaw, baseErr)
	for _, tid := range sortedTenantIDs(tenants) {
		for _, r := range cfg.RefusedThresholdWrites(tenants[tid]) {
			canon, _ := cfg.CanonicalKeyFor(r.Key)
			if prior != nil {
				if pv, ok := prior(tid, canon); ok && cfg.ThresholdReadAlike(canon, pv, tenants[tid][r.Key]) {
					notices = append(notices, r.Notice(tid))
					continue
				}
			}
			errs = append(errs, r.Message)
		}
	}
	return errs, notices
}

func anyRefusedThreshold(tenants map[string]map[string]cfg.ScheduledValue) bool {
	for _, keys := range tenants {
		if len(cfg.RefusedThresholdWrites(keys)) > 0 {
			return true
		}
	}
	return false
}

// thresholdBase is the ratchet's prev over baseRaw as the exporter decodes
// it (cfg.ParseConfigFile, which also asks parse-tree), or nil — strict —
// when nothing trustworthy is known of it.
func thresholdBase(baseRaw []byte, baseErr error) func(tenant, key string) (cfg.ScheduledValue, bool) {
	if baseErr != nil || len(baseRaw) == 0 || confd.Judge(baseRaw) != nil {
		return nil
	}
	base, err := cfg.ParseConfigFile(baseRaw)
	if err != nil {
		return nil
	}
	return cfg.PriorValues(base.Tenants)
}

func sortedTenantIDs(tenants map[string]map[string]cfg.ScheduledValue) []string {
	ids := make([]string, 0, len(tenants))
	for id := range tenants {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}
