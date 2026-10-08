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
// printed on every scrape, which names the value but not the file, and
// nothing for value_rejected.
//
// ⛔ NO VERDICT OF ITS OWN. The tenant half is ThresholdConfig.ValuesNotServed
// — the resolver's own record, the same table `da-guard effective` reports as
// not_served — and the subtree half is the build's
// FlatBuild.RejectedChainValues. Nothing here reads YAML.
//
// What /metrics serves does not change: this is the gauge
// da_config_values_not_served{reason} and one WARN per change of the set,
// at commit time (Load and every reload), never on the scrape path.

import (
	"fmt"
	"path/filepath"
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

// valueNotServed is one value of the committed config /metrics does not
// serve as written. Tenant is empty for value_rejected, which the build
// records per subtree defaults file (it applies to the tenants under it
// that do not set the key themselves).
type valueNotServed struct {
	Tenant string
	File   string // root-relative slash path when known; "" when not
	Key    string
	Reason string
}

// collectValuesNotServed lists the values of cfg /metrics does not serve as
// written, sorted: the resolver's record at now (cfg.ValuesNotServed) with
// each tenant's source file (sources: tenant → path, made root-relative
// against root when root is set), and the subtree values the build refused
// (rejected: FlatBuild.RejectedChainValues, root-relative file → keys).
//
// ⚠️ File for a tenant's value is the tenant's own file. A value a subtree
// `_defaults.yaml` hands down with a window that never applies is that
// defaults file's; `da-guard effective` attributes each key to its layer.
func collectValuesNotServed(cfg *ThresholdConfig, now time.Time, sources map[string]string, root string,
	rejected map[string]map[string]bool,
) []valueNotServed {
	var out []valueNotServed
	for tenant, keys := range cfg.ValuesNotServed(now) {
		file := sources[tenant]
		if file != "" && root != "" {
			if rel, err := filepath.Rel(root, file); err == nil && !strings.HasPrefix(rel, "..") {
				file = filepath.ToSlash(rel)
			}
		}
		for key, reason := range keys {
			if config.ValueNotServedAsWritten(key, reason) {
				out = append(out, valueNotServed{Tenant: tenant, File: file, Key: key, Reason: reason})
			}
		}
	}
	for file, keys := range rejected {
		for key := range keys {
			// A reserved key the overlay refuses is da-guard's
			// subtree_default_reserved_key / routing checks' (one cause,
			// one report), as in value_not_served.
			if config.ValueNotServedAsWritten(key, config.NotServedValueRejected) {
				out = append(out, valueNotServed{File: file, Key: key, Reason: config.NotServedValueRejected})
			}
		}
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Tenant != b.Tenant {
			return a.Tenant < b.Tenant
		}
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Key != b.Key {
			return a.Key < b.Key
		}
		return a.Reason < b.Reason
	})
	return out
}

// countValuesNotServed is the gauge's value per reason: every reason of
// valuesNotServedReasons present, 0 when none.
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
// first valuesNotServedLogSampleLimit values, then "+N more".
func formatValuesNotServedLog(vs []valueNotServed, root, context string) string {
	var b strings.Builder
	fmt.Fprintf(&b,
		"WARN: config values not served as written (%s): %d value(s) under %s are not served as written "+
			"by /metrics: value_unparsed serves the platform default, value_unparsed_dropped serves no series, "+
			"window_invalid never applies that schedule window, value_rejected keeps a shallower level's value "+
			"for the tenants under that file. Alerts on them do not fire at the written threshold. "+
			"Run `da-guard effective --config-dir %s` (not_served) to see each one. Values:",
		context, len(vs), root, root)
	shown := vs
	if len(shown) > valuesNotServedLogSampleLimit {
		shown = shown[:valuesNotServedLogSampleLimit]
	}
	for i, v := range shown {
		if i > 0 {
			b.WriteString(";")
		}
		if v.Tenant != "" {
			fmt.Fprintf(&b, " tenant=%s", v.Tenant)
		}
		file := v.File
		if file == "" {
			file = "?"
		}
		fmt.Fprintf(&b, " file=%s key=%s reason=%s", file, v.Key, v.Reason)
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
// tenantSources is the hierarchy snapshot installConfig returned (empty in
// single-file mode, where every tenant's file is m.path); rejected is the
// commit's flatScanState.rejected (nil in single-file mode and on the flat
// incremental path, which holds no `_defaults` file).
func (m *ConfigManager) auditValuesNotServed(cfg *ThresholdConfig, tenantSources map[string]string,
	flatScan *flatScanState, context string,
) []valueNotServed {
	var root string
	var rejected map[string]map[string]bool
	sources := tenantSources
	if flatScan != nil {
		rejected = flatScan.rejected
		if flatScan.tree != nil {
			root = flatScan.tree.AbsRoot
		}
	} else {
		sources = make(map[string]string, len(cfg.Tenants))
		for tid := range cfg.Tenants {
			sources[tid] = filepath.Base(m.path)
		}
	}
	vs := collectValuesNotServed(cfg, m.now(), sources, root, rejected)
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
		b.WriteString(v.File)
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
