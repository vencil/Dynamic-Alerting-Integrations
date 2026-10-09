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
// not_served — and the subtree half is the build's
// FlatBuild.RejectedChainWinners (the tenants whose shown value is the
// refused one). Nothing here reads YAML.
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
var valuesNotServedReasons = []string{
	config.NotServedValueUnparsed,
	config.NotServedValueUnparsedDropped,
	config.NotServedWindowInvalid,
	config.NotServedValueRejected,
}

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
// values a tenant is shown (rejected: FlatBuild.RejectedChainWinners), then
// the resolver's record at now (cfg.ValuesNotServed) — in the order
// pkg/config's notServed asks them, so a pair carries the reason
// `da-guard effective` gives it. Only config.ValueNotServedAsWritten pairs.
func collectValuesNotServed(cfg *ThresholdConfig, now time.Time, rejected map[string]map[string]string) []valueNotServed {
	byPair := map[[2]string]string{}
	for tenant, keys := range rejected {
		for key := range keys {
			if config.ValueNotServedAsWritten(key, config.NotServedValueRejected) {
				byPair[[2]string{tenant, key}] = config.NotServedValueRejected
			}
		}
	}
	for tenant, keys := range cfg.ValuesNotServed(now) {
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
			"window_invalid never applies that schedule window, value_rejected keeps a shallower level's value. "+
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
	vs := collectValuesNotServed(cfg, m.now(), rejected)
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
