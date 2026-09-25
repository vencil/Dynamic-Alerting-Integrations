package main

// ============================================================
// Subtree-only defaults the collector cannot deliver (#1521 → #1957 → #1976)
// ============================================================
//
// WHAT THIS FILE REPORTS
//
// conf.d/ feeds two planes from ONE walk and, since #1957, ONE decode:
//
//	scanDirTree → commitFlatFrom → mergePartialConfigs → m.config
//	            → GetConfig() → ThresholdCollector → /metrics
//	scanDirTree → m.hierarchy.tenantSources → Resolve() → /effective
//
// So the same file gets the same tenant verdict on both planes. (Not "both
// planes hold the same tenants" unconditionally: the incremental tenant-only
// reload keeps a now-broken file's last good tenants, which the stateless
// readers — tenant-api, da-guard — cannot (#1980), and a `_`-prefixed file
// declaring `tenants:` reaches /metrics but not /effective (#1982). Neither
// is this audit's business.) What this audit reports is one KEY shape: a key that exists ONLY in a subtree `_defaults.yaml`.
// /effective resolves the tenant's inheritance chain and reports the value;
// the collector cannot emit it, because it iterates the ROOT defaults and the
// declared surface (`optional_overrides:`), and a nested `_` file feeds
// neither (applySubtreeDefaults refuses to widen either global list — see
// pkg/config/subtree_defaults.go for the four defects that caused). Such a tenant
// shows a threshold on /effective that never becomes a series, so the alert
// can never fire. That set — tenantID → undeliverable keys, the manager's
// `unreachableInherited` — is what this file publishes, as the gauge
// da_config_subtree_undeliverable_tenants plus an ERROR log.
//
// ⛔ HISTORY, because the gauge had another cause until #1957. It was the
// "dual-scanner divergence" audit (#1521), and its cause (a) was a tenant the
// hierarchical walker registered but the merged config lacked: the walker
// decoded only `tenants:` while the flat plane decoded the whole file, so a
// file whose `defaults:` block (or a tenant body) failed the full decode was
// dropped by one plane and kept by the other. #1957 made the walker judge
// every file with config.ParseConfigFile — the flat plane's decode — so a
// file's bytes no longer get two verdicts, and cause (a) was removed rather
// than kept as an alarm for a state one decode cannot produce (the two
// remaining exceptions above are not scanner disagreements). What remains is a
// known, tracked delivery gap (#1976: deliver per-subtree scope, or reject such
// a key at validation time), not a divergence between scanners.
//
// ⛔ WHY THIS IS NOT FAIL-CLOSED (rejecting the load when the set is non-empty)
//
//  1. Every OTHER key and tenant in the tree is delivered correctly. Refusing
//     the load would drop the whole config — including every healthy tenant —
//     and turn one undeliverable threshold into a total alerting outage.
//  2. Fail-closed at *load* time is also fail-closed at *reload* time: a
//     running exporter that hot-reloads into a rejected config keeps serving
//     stale thresholds indefinitely, silently.
//
// So: loud, precise, and non-blocking. ERROR log + gauge, load proceeds.
// Rejecting such a key belongs at VALIDATION time (the write gate), which is
// one of #1976's two options — not a side effect of this observability.

import (
	"fmt"
	"sort"
	"strings"
	"sync"
)

// undeliverableLogSampleLimit caps how many tenant IDs are named inline in
// the ERROR line. A subtree `_defaults.yaml` can sit above hundreds of
// tenants; naming 10 plus a count is enough to identify the offending
// directory without producing an unreadable log record.
const undeliverableLogSampleLimit = 10

// undeliverableIssueURL is the tracking issue operators are pointed at: the
// fix is to deliver per-subtree scope, or to refuse such a key when it is
// written (#1976).
const undeliverableIssueURL = "https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976"

// subtreeUndeliverableTenants returns the sorted tenant IDs, among those the
// hierarchy serves (`tenantSources`, the population behind /effective), that
// inherit at least one key the collector cannot emit (`unreachable`, from
// applySubtreeDefaults).
//
// ⛔ Iterates tenantSources, not `unreachable`, on purpose: both come from ONE
// lock window (see auditSubtreeUndeliverable), and the log names each
// tenant's source file, so a tenant is reported only while /effective still
// serves it. An empty tenantSources yields nil: the hierarchical view has
// nothing to say (not populated / single-file mode).
func subtreeUndeliverableTenants(tenantSources map[string]string, unreachable map[string][]string) []string {
	if len(tenantSources) == 0 || len(unreachable) == 0 {
		return nil
	}
	var out []string
	for tid := range tenantSources {
		if len(unreachable[tid]) > 0 {
			out = append(out, tid)
		}
	}
	sort.Strings(out)
	return out
}

// formatUndeliverableLog builds the operator-facing ERROR body. Split out of
// auditSubtreeUndeliverable so a test can assert the wording (the
// consequence sentence is the whole point of the message) without driving
// a full load.
func formatUndeliverableLog(
	affected []string, tenantSources map[string]string, unreachable map[string][]string,
	root, context string,
) string {
	var b strings.Builder
	fmt.Fprintf(&b,
		"ERROR: conf.d subtree default undeliverable (%s): %d tenant(s) under %s inherit a key that exists "+
			"ONLY in a subtree `_defaults.yaml`. /effective reports its value, but the collector cannot emit it "+
			"(it walks the ROOT defaults and the declared surface, and a nested `_` file feeds neither), so "+
			"those alerts can never fire. Workaround: declare the key in the conf.d ROOT `_defaults.yaml` or in "+
			"`optional_overrides:`. Tracking: %s. Affected:",
		context, len(affected), root, undeliverableIssueURL)

	shown := affected
	if len(shown) > undeliverableLogSampleLimit {
		shown = shown[:undeliverableLogSampleLimit]
	}
	for i, tid := range shown {
		sep := " "
		if i > 0 {
			sep = ", "
		}
		fmt.Fprintf(&b, "%s%s (%s; undeliverable inherited key(s): %s)",
			sep, tid, tenantSources[tid], strings.Join(unreachable[tid], ", "))
	}
	if len(affected) > len(shown) {
		fmt.Fprintf(&b, ", and %d more", len(affected)-len(shown))
	}
	return b.String()
}

// auditSubtreeUndeliverable publishes the undeliverable set: sets the gauge
// on every call and emits the ERROR log when the set changed. Returns the
// size of the set so callers/tests can assert on it directly.
//
// Called from commitConfig — the single site in this package that assigns
// m.config — so every path that publishes a config is covered: Load (both
// modes), fullDirLoad, IncrementalLoad, and the hierarchical hot-reload
// path (diffAndReload → installNewHierarchyState → commitFlatFrom).
//
// ⛔ BOTH halves are passed in, and that is the whole point: they must come
// from ONE lock window. An earlier revision read `m.hierarchy.tenantSources`
// here under its own RLock, after commitConfig had already released m.mu.
// Reloads are not serialised — `fireDebounced` (config_debounce.go) sets
// `debounce.timer = nil`, unlocks, and only then calls diffAndReload, so a
// fresh event can arm a new timer and a second reload can overlap the first.
// In that window the audit could pair reload N's refused set with reload
// N+1's sources and report a state that never existed at any single instant.
//
// Reading both under commitConfig's existing Lock buys exactly one thing: the
// pair is the manager's OWN state at one instant — what /effective is serving
// and what the collector refuses at the moment cfg is installed. It does NOT
// guarantee both halves come from the same reload (populateHierarchyStateFrom
// installs tenantSources under its own Lock earlier); what a mixed state can
// produce is a TRANSIENT that the next commit's audit clears, because the
// gauge is Set on every commit.
//
// The gauge is Set (not Inc) on every commit, including the healthy case,
// so it returns to 0 as soon as the key is declared at the root or removed —
// see the gauge-vs-counter note on da_config_subtree_undeliverable_tenants
// in config_metrics.go.
func (m *ConfigManager) auditSubtreeUndeliverable(
	tenantSources map[string]string, unreachable map[string][]string, context string,
) int {
	affected := subtreeUndeliverableTenants(tenantSources, unreachable)
	// ⛔ getMetrics() BEFORE taking d.mu, never inside it: it acquires
	// m.mu.RLock and m.mu is not reentrant, so a future edit that moved this
	// audit inside commitConfig's lock window would self-deadlock. Taking the
	// leaf lock last also keeps the only possible order m.mu → d.mu.
	metrics := m.getMetrics()
	if !m.undeliverable.recordAndDecide(
		affected, tenantSources, unreachable, metrics.SetSubtreeUndeliverableTenants) {
		return len(affected)
	}
	m.getLogger().Print(formatUndeliverableLog(affected, tenantSources, unreachable, m.path, context))
	return len(affected)
}

// undeliverableLogState remembers the last set that was written to the log,
// so the ERROR is emitted once per CHANGE rather than once per config commit.
//
// ⛔ Why this exists. The gauge is re-Set on every commit and that is
// correct — a gauge is a level. The log is not: the state is persistent by
// construction (the key stays subtree-only until someone moves it), while
// commits are driven by unrelated fleet churn. Every tenant in a
// self-service deployment who edits their own thresholds (ADR-024) triggers
// detectChange → reload → commitConfig, and an unconditional line re-printed
// the same ERROR naming the same unrelated tenant each time. The line's
// repetition rate then measured how busy the fleet was, not how bad the
// problem was.
//
// ⛔ What it deliberately does NOT do is suppress the line on a fresh
// process. `clear()` on a healthy commit resets the memory, and a new
// manager starts empty, so a restart and a recovery-then-relapse both log
// again with the full tenant list. An operator whose logs have rotated past
// the original line still gets it back at the next restart, and the gauge
// carries the state in between.
type undeliverableLogState struct {
	mu   sync.Mutex
	last string
}

// recordAndDecide sets the gauge, updates the memory, and answers whether
// this audit should write the ERROR line — ALL THREE under one lock.
//
// ⛔ THE GAUGE UPDATE IS IN HERE ON PURPOSE, and leaving it outside was a
// signal-losing bug in the first version of this de-duplication. Those were
// three unsynchronised statements, and overlapping reloads are real (see the
// pairing note above), so this interleaving was reachable:
//
//	A (affected = D):      Set(1)
//	B (affected = empty):         Set(0)
//	B:                            clear()      → last = ""
//	A:                     isNew(D) → true, logs, last = D
//	                       ⇒ gauge = 0 while last = D
//
// The manager then believes it has already reported D while the gauge says
// everything is healthy; the next genuine recurrence of D sets the gauge back
// to non-zero and writes NOTHING. Measured by deterministic replay of exactly
// those four statements: `audit returned 1, gauge = 1, ERROR lines = 0`.
// Updating the level and the memory atomically is what makes them unable to
// disagree — and the ERROR line is the only signal a human sees, since no
// PrometheusRule ships for the gauge (see config_metrics.go).
//
// ⚠️ `setGauge` is called with d.mu held. It must stay a plain value setter —
// a Prometheus Gauge.Set, which takes no locks of ours and calls nothing back.
// Do not pass anything that touches m.mu here.
func (d *undeliverableLogState) recordAndDecide(
	affected []string, sources map[string]string,
	unreachable map[string][]string, setGauge func(int),
) bool {
	// The key carries the SOURCE PATHS, not just the tenant IDs, because the
	// log line names both: with IDs alone, the same tenants moving to a new
	// file kept the key identical, so nothing re-printed and the one line the
	// operator had went on pointing at a path that no longer existed.
	//
	// ⛔ AND IT CARRIES THE UNDELIVERABLE KEYS FOR THE SAME REASON, one field
	// down. Measured with `finance/_defaults.yaml` rewritten from
	// `redis_evicted_keys` to `kafka_lag_seconds`: keyed on tenant and path
	// only, the second load printed ZERO lines while the gauge correctly
	// stayed at 1, so the operator's line went on naming a key that no longer
	// existed. (#1569 blind review.)
	var b strings.Builder
	for _, tid := range affected {
		b.WriteString(tid)
		b.WriteByte(0)
		b.WriteString(sources[tid])
		b.WriteByte(0)
		for _, k := range unreachable[tid] {
			b.WriteString(k)
			b.WriteByte(1)
		}
		b.WriteByte(0)
	}
	key := b.String()

	d.mu.Lock()
	defer d.mu.Unlock()
	setGauge(len(affected))
	if len(affected) == 0 {
		d.last = ""
		return false
	}
	if d.last == key {
		return false
	}
	d.last = key
	return true
}
