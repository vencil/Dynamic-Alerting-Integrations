package config

import (
	"fmt"
	"sort"
	"unicode/utf8"

	"gopkg.in/yaml.v3"
)

// ParseConfigFile is the ONE decode of a conf.d file's bytes into a
// ThresholdConfig (#1957). Every plane that asks "is this file valid, and
// which tenants does it declare" asks it here:
//
//   - the conf.d walker (ScanDirTree → parseTenantDecls): a file this
//     rejects declares NO tenants, so it is absent from Tenants / Locate and
//     therefore from ResolveEffective (tenant-api /effective), ScopeEffective
//     (da-guard) and the exporter's hierarchy plane;
//   - the exporter's flat plane (package main's parsePartialConfig, and the
//     decoded partials the walker hands over in TreeScan.Partials), which
//     builds /metrics.
//
// ⛔ WHY ONE FUNCTION. Before #1957 the walker decoded only the shape it
// needed (`tenants:` into map[string]yaml.Node) while the flat plane decoded
// the whole ThresholdConfig, so the two disagreed on every file the lenient
// decode accepted and the full one rejected: a tenant whose body is a scalar,
// or a sibling `defaults:` / `max_metrics_per_tenant:` block of the wrong
// shape. Such a tenant resolved through /effective while /metrics never
// served it, and the exporter's divergence gauge (cause "a", removed by
// #1957) existed to report it after the fact. With one decode the same file
// content gets the same verdict on every plane.
//
// ⚠️ THAT IS A STATEMENT ABOUT ONE FILE'S BYTES, NOT "THE PLANES ALWAYS HOLD
// THE SAME TENANTS". Two known exceptions, both pre-existing and tracked:
//   - the exporter's incremental tenant-only reload (package main's
//     patchTenants) KEEPS a now-rejected file's last good values on /metrics
//     and the exporter's own /effective, while the stateless readers here
//     (ResolveEffective → tenant-api, ScopeEffective → da-guard) have no
//     prior and answer not-found — #1980, pinned by package main's
//     TestOneTenantSet_KnownException_IncrementalKeepsLastGood;
//   - a `_`-prefixed platform file declaring `tenants:` is never parsed for
//     tenants by the walker, but the flat plane merges its `tenants:` block,
//     so /metrics serves a tenant /effective does not know — #1982.
//
// Semantics are exactly a plain yaml.Unmarshal into ThresholdConfig — the
// flat plane's historical decode — pinned against that oracle over a variant
// corpus by config_file_test.go. The tenant set of a file is the key set of
// the returned Tenants.
//
// ⚠️ A TENANT FILE ASKS ParseTenantFile, NOT THIS. This accepts a tenant id
// that is not valid UTF-8; that is right for a `_`-prefixed platform file
// (see ParseTenantFile) and wrong for a file that DECLARES tenants.
func ParseConfigFile(data []byte) (ThresholdConfig, error) {
	var cfg ThresholdConfig
	err := yaml.Unmarshal(data, &cfg)
	return cfg, err
}

// ParseTenantFile is ParseConfigFile for a file that declares tenants — a
// non-`_` conf.d file (the walker's parseTenantDecls) or the single config
// file of file mode — plus one rejection: a tenant id that is not valid
// UTF-8 (#2266; YAML can spell one with `!!binary`). The error names every
// such id, sorted, so the message is the same on every run.
//
// ⛔ WHY REJECT THE FILE, NOT THE TENANT. A declared tenant id becomes a
// Prometheus label value, and client_golang's WithLabelValues panics on
// non-UTF-8 — on the scrape goroutine, which has no recover, so the whole
// exporter died. Rejecting the file gives it the verdict every other bad
// tenant file gets (skipped whole, WARN, da_config_parse_failure_total), and
// since the walker's verdict is the flat plane's too, on every plane.
//
// ⛔ WHY NOT IN ParseConfigFile. A `_` platform file's `tenants:` entry names
// a tenant; it never declares one — the merge keeps an entry only for a
// tenant some tenant file declares (declaredTenantIDs), and the platform
// overlay is looked up by declared id. A non-UTF-8 key there therefore can
// never reach a label (every declared id passed this check) and is dropped
// and WARNed as an orphan (reportPlatformOrphans). Rejecting the whole
// platform file instead dropped every tenant's defaults with it (#2266
// blind review, F1).
func ParseTenantFile(data []byte) (ThresholdConfig, error) {
	cfg, err := ParseConfigFile(data)
	if err != nil {
		return cfg, err
	}
	var bad []string
	for tid := range cfg.Tenants {
		if !utf8.ValidString(tid) {
			bad = append(bad, tid)
		}
	}
	if len(bad) > 0 {
		sort.Strings(bad)
		if len(bad) > 1 {
			return ThresholdConfig{}, fmt.Errorf("tenant ids %q are not valid UTF-8", bad)
		}
		return ThresholdConfig{}, fmt.Errorf("tenant id %q is not valid UTF-8", bad[0])
	}
	return cfg, nil
}
