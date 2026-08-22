package main

// ============================================================
// conf.d dual-scanner divergence audit (#1521)
// ============================================================
//
// THE DEFECT THIS FILE MAKES AUDIBLE (it does NOT fix it)
//
// conf.d/ is read by two independent scanners with two different
// definitions of "which files exist":
//
//	flat_scanner.go  scanDirFileHashes    os.ReadDir + `if IsDir() { continue }`
//	                                      → top level ONLY
//	                 → fullDirLoad → mergePartialConfigs → m.config
//	                 → GetConfig() → ThresholdCollector → /metrics
//
//	config_hierarchy.go scanDirHierarchical  filepath.WalkDir
//	                                      → RECURSIVE, every depth
//	                 → m.hierarchy.tenantSources → Resolve() → /effective
//
// Nothing compared the two populations. A tenant file placed in a
// sub-directory (`conf.d/db/hier-tenant.yaml`) is therefore resolvable
// via /effective, absent from /metrics, and — before this file — emitted
// ZERO signal: no ERROR, no WARN, no parse_failure, nothing in the
// "Config loaded" stats line. ADR-016 §"目錄深度不影響 metric label"
// (docs/adr/016-conf-d-directory-hierarchy-mixed-mode.md:115) promises the
// opposite.
//
// SCOPE OF THIS FILE (deliberately narrow — issue #1521 "option C")
//
// Stop-the-bleeding only: compare the two populations on every config
// commit, name the casualties in an ERROR log, and expose the size of the
// divergence as a gauge. The nested tenants still produce no metrics
// afterwards. Actually teaching the collector-side pipeline to see nested
// tenant files is a separate change with a much larger blast radius
// (merge precedence, composite-hash construction, per-file cache keys,
// duplicate detection across depths) and is intentionally NOT done here.
//
// ⛔ WHY THIS IS NOT FAIL-CLOSED (rejecting the load on divergence)
//
// Refusing to commit a config that contains nested tenants would be the
// "safe" reflex, and it is the wrong call here:
//
//  1. The divergent state has been shippable for several releases. Any
//     deployment that already has nested tenant files is running right
//     now, and its OTHER (root-level) tenants are producing metrics and
//     alerting correctly. Turning this into a load error would drop the
//     whole config — including every healthy tenant — and convert a
//     partial observability gap into a total alerting outage. The blast
//     radius of the remedy would exceed the blast radius of the defect.
//  2. /effective already serves those nested tenants. Operators and the
//     tenant-api describe/preview flows may legitimately depend on that
//     surface today; a hard reject removes a working capability rather
//     than restoring a broken one.
//  3. Fail-closed at *load* time is also fail-closed at *reload* time: a
//     running exporter that hot-reloads into a rejected config keeps
//     serving stale thresholds indefinitely, silently, which is the same
//     class of failure this audit exists to expose.
//
// So: loud, precise, and non-blocking. ERROR log + gauge, load proceeds.
// Escalating to fail-closed is a deliberate follow-up decision (and would
// need a lever + a migration window), not a side effect of adding
// observability.

import (
	"fmt"
	"sort"
	"strings"
)

// divergenceLogSampleLimit caps how many tenant IDs are named inline in
// the ERROR line. A whole misplaced sub-tree could be hundreds of
// tenants; naming 10 plus a count is enough for an operator to identify
// the offending directory without producing an unreadable log record.
const divergenceLogSampleLimit = 10

// divergenceIssueURL is the tracking issue operators are pointed at.
const divergenceIssueURL = "https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1521"

// hierarchyDivergentTenants returns the sorted tenant IDs that the
// hierarchical scanner discovered (`tenantSources`, the population behind
// /effective) but that are missing from the merged flat config (`cfg`,
// the population behind /metrics).
//
// One direction only, on purpose. The reverse difference (present in
// cfg.Tenants, absent from tenantSources) is a legitimate steady state in
// several layouts — e.g. single-file mode, where the hierarchical scan
// never runs and tenantSources is nil, or a `_`-prefixed root file that
// carries a `tenants:` block (the flat merge keeps it; the hierarchical
// walker skips every `_*.yaml` as a non-tenant file). Reporting those
// would make the gauge noisy at rest and destroy its "0 means healthy"
// property.
//
// An empty tenantSources therefore yields nil rather than "everything is
// divergent": it means the hierarchical view has nothing to say (not
// populated / flat-only tree), not that every tenant vanished.
func hierarchyDivergentTenants(tenantSources map[string]string, cfg *ThresholdConfig) []string {
	if len(tenantSources) == 0 || cfg == nil {
		return nil
	}
	var divergent []string
	for tid := range tenantSources {
		if _, visible := cfg.Tenants[tid]; !visible {
			divergent = append(divergent, tid)
		}
	}
	sort.Strings(divergent)
	return divergent
}

// formatDivergenceLog builds the operator-facing ERROR body. Split out of
// auditHierarchyDivergence so a test can assert the wording (the
// consequence sentence is the whole point of the message) without driving
// a full load.
func formatDivergenceLog(divergent []string, tenantSources map[string]string, root, context string) string {
	var b strings.Builder
	fmt.Fprintf(&b,
		"ERROR: conf.d scanner divergence (%s): %d tenant(s) are visible to the hierarchical scanner (/effective) "+
			"but ABSENT from the merged config that feeds the collector — these tenants emit NO user_threshold series, "+
			"so their alerts can never fire. Cause: the flat scanner reads only the top level of %s, so tenant files in "+
			"sub-directories never reach /metrics (ADR-016 requires directory depth NOT to affect metric labels). "+
			"Workaround: move the tenant file to the conf.d root. Tracking: %s. Affected:",
		context, len(divergent), root, divergenceIssueURL)

	shown := divergent
	if len(shown) > divergenceLogSampleLimit {
		shown = shown[:divergenceLogSampleLimit]
	}
	for i, tid := range shown {
		sep := " "
		if i > 0 {
			sep = ", "
		}
		fmt.Fprintf(&b, "%s%s (%s)", sep, tid, tenantSources[tid])
	}
	if len(divergent) > len(shown) {
		fmt.Fprintf(&b, ", and %d more", len(divergent)-len(shown))
	}
	return b.String()
}

// auditHierarchyDivergence compares the two conf.d populations and, when
// they disagree, emits the ERROR log + sets the gauge. Returns the size of
// the divergent set so callers/tests can assert on it directly.
//
// Called from commitConfig — the single site in this package that assigns
// m.config — so every path that publishes a config is covered: Load (both
// modes), fullDirLoad, IncrementalLoad, and the hierarchical hot-reload
// path (diffAndReload → installNewHierarchyState → fullDirLoad).
//
// ⛔ BOTH halves are passed in, and that is the whole point: they must come
// from ONE lock window. An earlier revision took `cfg` as an argument but
// read `m.hierarchy.tenantSources` here under its own RLock, after
// commitConfig had already released m.mu. Reloads are not serialised —
// `fireDebounced` (config_debounce.go) sets `debounce.timer = nil`, unlocks,
// and only then calls diffAndReload, so a fresh event can arm a new timer
// and a second reload can overlap the first. In that window the audit could
// pair reload N's `cfg` with reload N+1's tenantSources and report a
// divergence that never existed at any single instant — a false ERROR
// naming a healthy tenant, from the very check whose job is to be
// trustworthy about which tenants are missing.
//
// Reading both under commitConfig's existing Lock buys exactly one thing,
// stated precisely because an earlier draft of this comment overstated it:
// the pair is the manager's OWN state at one instant — what /metrics and
// /effective are really serving at the moment cfg is installed. Nothing
// more.
//
// ⛔ In particular it does NOT guarantee both halves come from the same
// reload, and claiming that was wrong. `populateHierarchyState`
// (config.go) installs tenantSources under its own Lock and releases it;
// commitConfig acquires m.mu later, with a directory scan and a YAML parse
// in between. With reloads unserialised, reload B's hierarchy can be in
// place by the time reload A commits its cfg. Measured: audit then names a
// tenant from B against A's config.
//
// That report is still TRUE, which is why this is the accepted design and
// not an open bug — measured in the same probe: at that instant
// Resolve() DOES serve the named tenant and the collector does NOT emit
// it, so the ERROR describes the live split rather than inventing one.
// What the mixed state can produce is a TRANSIENT: a tenant that reload B
// is about to make visible to the flat side is reported divergent until
// B's own commit re-runs this audit and clears the gauge. Self-correcting
// by construction, because the gauge is Set on every commit.
//
// ⚠️ Serialising reloads outright would remove the transient, and is
// deliberately not done here — it is a behaviour change to the reload
// pipeline, not an observability fix. See #1521.
//
// The gauge is Set (not Inc) on every commit, including the healthy case,
// so it returns to 0 as soon as the offending file is moved or removed —
// see the gauge-vs-counter note on da_config_hierarchy_divergent_tenants
// in config_metrics.go.
func (m *ConfigManager) auditHierarchyDivergence(
	cfg *ThresholdConfig, tenantSources map[string]string, context string,
) int {
	divergent := hierarchyDivergentTenants(tenantSources, cfg)
	m.getMetrics().SetHierarchyDivergentTenants(len(divergent))
	if len(divergent) == 0 {
		return 0
	}
	m.getLogger().Print(formatDivergenceLog(divergent, tenantSources, m.path, context))
	return len(divergent)
}
