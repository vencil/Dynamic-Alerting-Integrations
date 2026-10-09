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
//     and in its committed hierarchy (tenantSources), while the stateless readers here
//     (ResolveEffective → tenant-api, ScopeEffective → da-guard) have no
//     prior and answer not-found — #1980, pinned by package main's
//     TestOneTenantSet_KnownException_IncrementalKeepsLastGood;
//   - a `_`-prefixed platform file declaring `tenants:` is never parsed for
//     tenants by the walker, but the flat plane merges its `tenants:` block,
//     so /metrics serves a tenant /effective does not know — #1982.
//
// Semantics are a plain yaml.Unmarshal into ThresholdConfig — the flat
// plane's historical decode — pinned against that oracle over a variant
// corpus by config_file_test.go, with TWO post-decode steps: a value written
// as null is no write (dropNullThresholds, #2518 — a `defaults:` key, and a
// threshold key of a `tenants:` / `profiles:` body), and inside `defaults:`
// a spelling that writes nothing beside one that does is dropped
// (dropNullShadowingSpellings, #2418). The tenant set of a file is the key
// set of the returned Tenants.
//
// ⚠️ A TENANT FILE ASKS ParseTenantFile, NOT THIS. This accepts a tenant id
// that is not valid UTF-8; that is right for a `_`-prefixed platform file
// (see ParseTenantFile) and wrong for a file that DECLARES tenants.
func ParseConfigFile(data []byte) (ThresholdConfig, error) {
	var cfg ThresholdConfig
	err := yaml.Unmarshal(data, &cfg)
	if err == nil {
		dropNullThresholds(&cfg, data)
		dropNullShadowingSpellings(cfg.Defaults, data)
		normalizeConfigKeys(&cfg)
	}
	return cfg, err
}

// normalizeConfigKeys re-keys every threshold map of one file's decode to the
// canonical dimensional spelling (normalizeKeys, #2031) — after the null
// drops above, which look keys up by the text the file wrote. The maps keep
// their identity when nothing is re-spelled.
func normalizeConfigKeys(cfg *ThresholdConfig) {
	var s *keySpellings
	cfg.Defaults, s = normalizeKeys(cfg.Defaults)
	tenants, profiles := normalizeBodies(cfg.Tenants), normalizeBodies(cfg.Profiles)
	cfg.spelled = s != nil || tenants || profiles
}

// normalizeBodies is normalizeKeys over each body of m, in place; it reports
// whether any body was re-spelled.
func normalizeBodies(m map[string]map[string]ScheduledValue) bool {
	spelled := false
	for name, body := range m {
		nb, s := normalizeKeys(body)
		if s != nil {
			m[name], spelled = nb, true
		}
	}
	return spelled
}

// dropNullShadowingSpellings removes from a decoded `defaults:` map every
// spelling of a threshold that the file does not write (null, ±Inf, NaN —
// levelWritesSpelling) while it writes another spelling of the same
// threshold (#2418).
//
// ⛔ WHY. `Defaults` is map[string]float64, so a null decodes to a PRESENT 0
// (since #2518 dropNullThresholds takes every null out first; the ±Inf /
// NaN half below is what still reaches here). With the canonical spelling
// written as null beside the retired spelling
// at 30 in the root `_defaults.yaml`, resolve's canonical-wins dedup
// (canonicalizeDefaults) then served the null's 0 over the 30, while the
// walker's defaults fold — which da-guard and /effective read — drops the
// null and keeps the 30. The guard therefore judged a tenant writing the
// retired spelling at 30 redundant, and deleting it moved /metrics from 30
// to 0.
//
// "Does this level write spelling s" is levelWritesSpelling, the predicate
// the subtree overlay (applySubtreeDefaults) and the walker's fold
// (noteSpellingWriters) already share: the root level answers it the same
// way, so the canonical-wins dedup inside one file is among the spellings
// that file WRITES.
//
// What this changes, all of it:
//   - a null beside the other spelling's value: served that value, not 0;
//   - `.inf` / `-.inf` / `.nan` beside the other spelling's value: served
//     that value, not ±Inf / NaN (a non-finite number is not threshold-
//     shaped, so it writes nothing either);
//   - every caller of this decode gets it — the conf.d root carrier AND
//     file mode's single config file (loadFile → ParseTenantFile).
//
// What stays: a non-finite spelling with NO written twin decodes as before,
// and a file writing both spellings with values is untouched (canonical
// wins). A null never reaches here: dropNullThresholds runs first and takes
// every null out of `defaults:` (#2518), twin or not.
//
// Fast path: the raw re-decode happens only when the map holds two
// spellings of one threshold — never for a file without aliased keys.
func dropNullShadowingSpellings(defaults map[string]float64, data []byte) {
	var pairs []string
	var buf [2]string
	for k := range defaults {
		for _, s := range otherSpellings(k, &buf) {
			if _, both := defaults[s]; both {
				pairs = append(pairs, k)
				break
			}
		}
	}
	if len(pairs) == 0 {
		return
	}
	var raw struct {
		Defaults map[string]any `yaml:"defaults"`
	}
	if yaml.Unmarshal(data, &raw) != nil {
		return // cannot happen: the typed decode of the same bytes succeeded
	}
	var drop []string
	for _, k := range pairs {
		if levelWritesSpelling(raw.Defaults, k) {
			continue
		}
		for _, s := range otherSpellings(k, &buf) {
			if _, in := defaults[s]; in && levelWritesSpelling(raw.Defaults, s) {
				drop = append(drop, k)
				break
			}
		}
	}
	for _, k := range drop {
		delete(defaults, k)
	}
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
