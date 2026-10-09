package main

// ============================================================
// Config values /metrics does not serve as written (#2065)
// ============================================================
//
// A threshold value the exporter cannot use is not an error at load time:
// /metrics serves the platform default instead (value_unparsed), serves no
// series for it (value_unparsed_dropped), never applies a schedule window it
// cannot read (window_invalid), or keeps a shallower level's value where a
// subtree `_defaults.yaml` wrote one that is not threshold-shaped
// (value_rejected). Before this the only trace was the resolver's WARN
// printed on every scrape, and nothing for value_rejected.
//
// ⛔ NO VERDICT OF ITS OWN. The tenant half is ThresholdConfig.ValuesNotServed
// — the resolver's own record, the same table `da-guard effective` reports as
// not_served — and the subtree half is config.RejectedShownCache.Shown: the
// effective resolver's own value_rejected verdict, for the tenants under a
// value the build refused (FlatBuild.RejectedChainValues), and its
// spelling_duplicate verdict (#2031), for the tenants reading a file that
// wrote a threshold under two spellings in one map. Nothing here
// reads YAML or attributes a key to a layer itself.
//
// ⚠️ It names tenant and key, never a file: which layer and file a value
// comes from is the effective resolver's attribution (keySources), which the
// commit does not run — a file named here was measured to point at the
// tenant's file for a value written in `_platform.yaml`, `_profiles.yaml` or
// a subtree schedule. `da-guard effective` names it.
//
// What /metrics serves does not change: this is the gauge
// da_config_values_not_served{reason} and one WARN per change of the set,
// at commit time (Load and every reload), never on the scrape path.

import (
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// valuesNotServedReasons is the closed label set of
// da_config_values_not_served{reason}: the not_served reasons whose cause is
// a value written in the config (pkg/config's NotServed* constants).
var valuesNotServedReasons = config.ValueNotServedReasons()

// valuesNotServedLogSampleLimit caps how many values the WARN names inline.
const valuesNotServedLogSampleLimit = 20

// valueNotServed is one (tenant, key) of the committed config whose value
// /metrics does not serve as written. Key is spelled as the tenant's
// effective config spells it (the written spelling).
type valueNotServed struct {
	Tenant string
	Key    string
	Reason string
}

// collectValuesNotServed lists the (tenant, key) pairs of cfg /metrics does
// not serve as written, sorted, one reason each: the build's refused subtree
// values a tenant is shown and its spelling duplicates (rejected:
// config.RejectedShownCache.Shown, key → reason), then
// the resolver's record (verdicts: cfg.ValuesNotServed at the commit's now,
// or valuesNotServedCache's copy of it) — in the order pkg/config's
// notServed asks them, so a pair carries the reason `da-guard effective`
// gives it. Only config.ValueNotServedAsWritten pairs.
func collectValuesNotServed(cfg *ThresholdConfig, verdicts, rejected map[string]map[string]string) []valueNotServed {
	byPair := map[[2]string]string{}
	for tenant, keys := range rejected {
		for key, reason := range keys {
			if config.ValueNotServedAsWritten(key, reason) {
				byPair[[2]string{tenant, key}] = reason
			}
		}
	}
	for tenant, keys := range verdicts {
		for canon, reason := range keys {
			key := writtenSpelling(cfg.Tenants[tenant], canon)
			if !config.ValueNotServedAsWritten(key, reason) {
				continue
			}
			if _, taken := byPair[[2]string{tenant, key}]; !taken {
				byPair[[2]string{tenant, key}] = reason
			}
		}
	}
	out := make([]valueNotServed, 0, len(byPair))
	for k, r := range byPair {
		out = append(out, valueNotServed{Tenant: k[0], Key: k[1], Reason: r})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Tenant != out[j].Tenant {
			return out[i].Tenant < out[j].Tenant
		}
		return out[i].Key < out[j].Key
	})
	return out
}

// writtenSpelling is the spelling of canonical key canon the tenant's map
// holds — the resolver records the canonical spelling (#1231), the
// effective config keys the written one. canon when the map holds it (the
// canonical spelling wins when both are written, as in the resolver).
func writtenSpelling(overrides map[string]ScheduledValue, canon string) string {
	if _, ok := overrides[canon]; ok {
		return canon
	}
	if legacy, ok := config.LegacySpellingFor(canon); ok {
		if _, ok := overrides[legacy]; ok {
			return legacy
		}
	}
	return canon
}

// countValuesNotServed is the gauge's value per reason — (tenant, key) pairs
// — every reason of valuesNotServedReasons present, 0 when none.
func countValuesNotServed(vs []valueNotServed) map[string]int {
	out := make(map[string]int, len(valuesNotServedReasons))
	for _, r := range valuesNotServedReasons {
		out[r] = 0
	}
	for _, v := range vs {
		out[v.Reason]++
	}
	return out
}

// formatValuesNotServedLog is the operator-facing WARN line: one line, the
// first valuesNotServedLogSampleLimit pairs, then "+N more".
func formatValuesNotServedLog(vs []valueNotServed, root, context string) string {
	var b strings.Builder
	fmt.Fprintf(&b,
		"WARN: config values not served as written (%s): %d tenant value(s) under %s are not served as written "+
			"by /metrics: value_unparsed serves the platform default, value_unparsed_dropped serves no series, "+
			"window_invalid never applies that schedule window, value_rejected keeps a shallower level's value, "+
			"spelling_duplicate is a second spelling of a threshold the same mapping writes and only the other one is served. "+
			"Alerts on them do not fire at the written threshold. "+
			"Run `da-guard effective --config-dir %s` (not_served) for the file of each one. Values:",
		context, len(vs), root, root)
	shown := vs
	if len(shown) > valuesNotServedLogSampleLimit {
		shown = shown[:valuesNotServedLogSampleLimit]
	}
	for i, v := range shown {
		if i > 0 {
			b.WriteString(";")
		}
		fmt.Fprintf(&b, " tenant=%s key=%s reason=%s", v.Tenant, v.Key, v.Reason)
	}
	if len(vs) > len(shown) {
		fmt.Fprintf(&b, "; +%d more", len(vs)-len(shown))
	}
	return b.String()
}

// auditValuesNotServed publishes the committed config's values-not-served
// set: re-Sets da_config_values_not_served{reason} on every call and writes
// the WARN when the set changed. Called from commitConfig after the swap,
// outside m.mu. Returns the set for tests.
//
// rejected is the commit's flatScanState.rejected (nil in single-file mode
// and on the flat incremental path, which holds no `_defaults` file).
func (m *ConfigManager) auditValuesNotServed(cfg *ThresholdConfig, flatScan *flatScanState, context string) []valueNotServed {
	var rejected map[string]map[string]string
	if flatScan != nil {
		rejected = flatScan.rejected
	}
	vs := collectValuesNotServed(cfg, m.valuesNotServedCache.verdicts(cfg, m.now()), rejected)
	metrics := m.getMetrics()
	if m.valuesNotServed.recordAndDecide(vs, metrics.SetValuesNotServed) {
		m.getLogger().Print(formatValuesNotServedLog(vs, m.path, context))
	}
	return vs
}

// now is the manager's clock (a test's fake one when set), else time.Now.
func (m *ConfigManager) now() time.Time {
	m.mu.RLock()
	c := m.clock
	m.mu.RUnlock()
	if c == nil {
		return time.Now()
	}
	return c.Now()
}

// valuesNotServedLogState is undeliverableLogState's de-duplication for this
// WARN: the gauge is a level re-Set on every commit, the line is written once
// per change of the set (a commit driven by an unrelated tenant's edit does
// not repeat it), and a healthy commit or a new process starts over.
type valuesNotServedLogState struct {
	mu   sync.Mutex
	last string
}

// recordAndDecide sets the gauge, updates the memory and answers whether to
// write the WARN, under one lock (see undeliverableLogState.recordAndDecide
// for why the gauge write is inside it). setGauge must stay a plain value
// setter.
func (d *valuesNotServedLogState) recordAndDecide(vs []valueNotServed, setGauge func(map[string]int)) bool {
	var b strings.Builder
	for _, v := range vs {
		b.WriteString(v.Tenant)
		b.WriteByte(0)
		b.WriteString(v.Key)
		b.WriteByte(0)
		b.WriteString(v.Reason)
		b.WriteByte(1)
	}
	key := b.String()

	d.mu.Lock()
	defer d.mu.Unlock()
	setGauge(countValuesNotServed(vs))
	if len(vs) == 0 {
		d.last = ""
		return false
	}
	if d.last == key {
		return false
	}
	d.last = key
	return true
}

// valuesNotServedCache keeps, between commits, each tenant's resolver
// verdicts (ThresholdConfig.ValuesNotServed's entry) with the inputs they
// were computed from, so a commit re-resolves only the tenants whose inputs
// moved (#2065: the whole-day reading costs about one full resolve per
// schedule segment, on every reload). A tenant is recomputed when it is new,
// its built map moved (config.TenantValuesInput), now has passed the
// earliest `expires:` it had not passed (config.ValuesNotServedExpiryEdge),
// or now is before the instant it was computed at (a clock set back can
// un-expire a value); every tenant when the config-wide input moved
// (ThresholdConfig.ValuesNotServedGlobalInput). A tenant gone from the
// config is dropped.
//
// ⛔ The fingerprints are of what the resolver reads, not of files — see
// pkg/config/not_served_inputs.go for why that is exact and a file hash or
// merged_hash is not. Only the commit path uses it; scrapes never do.
type valuesNotServedCache struct {
	mu      sync.Mutex
	valid   bool
	global  uint64
	tenants map[string]valuesNotServedEntry
	// fullPasses / tenantPasses count the recomputes, for tests to see
	// which path a commit took.
	fullPasses, tenantPasses int
}

type valuesNotServedEntry struct {
	input    uint64
	at       time.Time
	edge     time.Time
	hasEdge  bool
	verdicts map[string]string // nil: nothing recorded
}

// fresh reports whether e still holds the tenant's verdicts for input at now.
func (e valuesNotServedEntry) fresh(input uint64, now time.Time) bool {
	return e.input == input && !now.Before(e.at) && (!e.hasEdge || !now.After(e.edge))
}

func newValuesNotServedEntry(overrides map[string]ScheduledValue, input uint64, now time.Time, v map[string]string) valuesNotServedEntry {
	edge, hasEdge := config.ValuesNotServedExpiryEdge(overrides, now)
	return valuesNotServedEntry{input: input, at: now, edge: edge, hasEdge: hasEdge, verdicts: v}
}

// verdicts is cfg.ValuesNotServed(now), recomputed only where it may have
// changed since the previous call.
func (c *valuesNotServedCache) verdicts(cfg *ThresholdConfig, now time.Time) map[string]map[string]string {
	c.mu.Lock()
	defer c.mu.Unlock()
	global := cfg.ValuesNotServedGlobalInput()
	inputs := make(map[string]uint64, len(cfg.Tenants))
	var stale []string
	for tenant, overrides := range cfg.Tenants {
		in := config.TenantValuesInput(overrides)
		inputs[tenant] = in
		if !c.valid || c.global != global {
			continue
		}
		if e, ok := c.tenants[tenant]; !ok || !e.fresh(in, now) {
			stale = append(stale, tenant)
		}
	}
	if !c.valid || c.global != global {
		all := cfg.ValuesNotServed(now)
		c.tenants = make(map[string]valuesNotServedEntry, len(cfg.Tenants))
		for tenant, overrides := range cfg.Tenants {
			c.tenants[tenant] = newValuesNotServedEntry(overrides, inputs[tenant], now, all[tenant])
		}
		c.global, c.valid = global, true
		c.fullPasses++
		return all
	}
	for _, tenant := range stale {
		overrides := cfg.Tenants[tenant]
		c.tenants[tenant] = newValuesNotServedEntry(overrides, inputs[tenant], now, cfg.TenantValuesNotServed(tenant, now))
		c.tenantPasses++
	}
	out := map[string]map[string]string{}
	for tenant, e := range c.tenants {
		if _, live := inputs[tenant]; !live {
			delete(c.tenants, tenant)
			continue
		}
		if len(e.verdicts) > 0 {
			out[tenant] = e.verdicts
		}
	}
	return out
}
