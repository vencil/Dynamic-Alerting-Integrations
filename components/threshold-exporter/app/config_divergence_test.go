package main

// Tests for the conf.d dual-scanner divergence audit (#1521).
//
// The audit is observability-only: it must be LOUD when a tenant is
// visible to the hierarchical scanner but invisible to the collector, and
// completely SILENT when the same tenant content lives at the conf.d
// root. Both halves are asserted here — the silent (control) half is what
// makes the loud half mean something.
//
// Injection discipline (CLAUDE.md §測試注入 Seam): metrics via SetMetrics
// + freshMetrics, logger via SetLogger. No global swaps.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

const divergenceTenantYAML = "tenants:\n  hier-tenant:\n    mysql_connections: \"95\"\n"

// newAuditedManager wires a ConfigManager with an isolated metrics
// instance and a capturing logger, both through the documented seams.
func newAuditedManager(t *testing.T, dir string) (*ConfigManager, *configMetrics, *bytes.Buffer) {
	t.Helper()
	fresh, _ := freshMetrics(t)
	var logBuf bytes.Buffer
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&logBuf, "", 0))
	t.Cleanup(m.Close)
	return m, fresh, &logBuf
}

// writeNestedTenant drops a tenant file one directory below the conf.d
// root — the exact shape from #1521 (`conf.d/db/hier-tenant.yaml`).
func writeNestedTenant(t *testing.T, dir string) string {
	t.Helper()
	sub := filepath.Join(dir, "db")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("mkdir db: %v", err)
	}
	path := filepath.Join(sub, "hier-tenant.yaml")
	writeTestYAML(t, path, divergenceTenantYAML)
	return path
}

// writeDivergenceRoot lays down the always-present root files: a
// _defaults.yaml (so hierarchical mode activates) plus one healthy
// root-level tenant that must never be reported as divergent.
func writeDivergenceRoot(t *testing.T, dir string) {
	t.Helper()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  root-tenant:\n    mysql_connections: \"90\"\n")
}

// TestDivergenceAudit_ColdStart_NestedTenantIsLoud drives the real entry
// point (NewConfigManager → Load) against a mixed flat+nested tree and
// asserts the tenant is named, the consequence is spelled out, and the
// gauge reads 1.
func TestDivergenceAudit_ColdStart_NestedTenantIsLoud(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	nestedPath := writeNestedTenant(t, dir)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load must NOT fail on divergence (option C is not fail-closed): %v", err)
	}

	// The defect itself, pinned: /effective sees it, /metrics does not.
	if _, ok := m.Resolve("hier-tenant"); !ok {
		t.Fatalf("precondition: hierarchical scanner should resolve hier-tenant")
	}
	if _, ok := m.GetConfig().Tenants["hier-tenant"]; ok {
		t.Fatalf("precondition changed: hier-tenant now reaches the collector config — " +
			"the #1521 defect appears fixed, this audit test needs revisiting")
	}

	logs := logBuf.String()
	if !strings.Contains(logs, "ERROR: conf.d scanner divergence") {
		t.Errorf("expected divergence ERROR log, got:\n%s", logs)
	}
	if !strings.Contains(logs, "hier-tenant") {
		t.Errorf("ERROR log must name the affected tenant, got:\n%s", logs)
	}
	if !strings.Contains(logs, nestedPath) {
		t.Errorf("ERROR log must name the source file %s, got:\n%s", nestedPath, logs)
	}
	if !strings.Contains(logs, "user_threshold") {
		t.Errorf("ERROR log must state the consequence (no user_threshold series), got:\n%s", logs)
	}
	if !strings.Contains(logs, "1521") {
		t.Errorf("ERROR log must point at the tracking issue, got:\n%s", logs)
	}
	// The healthy root-level tenant must not be swept into the report.
	if strings.Contains(logs, "root-tenant (") {
		t.Errorf("root-level tenant must not be reported as divergent, got:\n%s", logs)
	}

	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Errorf("da_config_hierarchy_divergent_tenants = %v, want 1", got)
	}
}

// TestDivergenceAudit_FlatControl_IsSilent is the counterfactual: the very
// same tenant content at the conf.d root must produce no ERROR and a zero
// gauge. Without this, the test above only proves "it prints something".
func TestDivergenceAudit_FlatControl_IsSilent(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	writeTestYAML(t, filepath.Join(dir, "hier-tenant.yaml"), divergenceTenantYAML)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	if _, ok := m.GetConfig().Tenants["hier-tenant"]; !ok {
		t.Fatalf("control precondition: root-level tenant must reach the collector config")
	}
	if logs := logBuf.String(); strings.Contains(logs, "conf.d scanner divergence") {
		t.Errorf("flat layout must NOT trigger the divergence ERROR, got:\n%s", logs)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("da_config_hierarchy_divergent_tenants = %v, want 0 for a flat layout", got)
	}
}

// TestDivergenceAudit_GaugeRecoversToZero proves the gauge is state-coded
// (the gauge-over-counter argument): remove the misplaced file, reload,
// and the gauge must fall back to 0.
func TestDivergenceAudit_GaugeRecoversToZero(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	nestedPath := writeNestedTenant(t, dir)

	m, fresh, _ := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Fatalf("setup: gauge = %v, want 1", got)
	}

	if err := os.Remove(nestedPath); err != nil {
		t.Fatalf("remove nested tenant: %v", err)
	}
	if err := m.fullDirLoad(); err != nil {
		t.Fatalf("reload after fix: %v", err)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("gauge did not recover after the layout was fixed: %v, want 0", got)
	}
}

// TestDivergenceAudit_HotReload_HierarchicalPath is the load-bearing one:
// #1521 explicitly left "is the hot-reload path also broken?" unverified.
// It drives the real detect→debounce→diffAndReload chain via tickOnce
// (debounce window 0 = synchronous, the seam config_debounce.go documents)
// and answers both halves:
//
//	(a) does a nested tenant added AFTER startup reach m.config?  → no
//	(b) does the audit fire on that path?                         → yes
func TestDivergenceAudit_HotReload_HierarchicalPath(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Fatalf("baseline: gauge = %v, want 0 before the nested file exists", got)
	}
	logBuf.Reset()

	// Now introduce the nested tenant into a RUNNING exporter.
	nestedPath := writeNestedTenant(t, dir)
	// mtime-guard safety window is 2s for the flat scanner; the
	// hierarchical detect path re-hashes unconditionally, so tickOnce sees
	// the new file immediately.
	m.tickOnce()

	// (a) Is the hot-reload path itself broken the same way?
	if _, ok := m.GetConfig().Tenants["hier-tenant"]; ok {
		t.Errorf("FINDING CHANGED: hot-reload now admits the nested tenant into the collector config; " +
			"#1521 hot-reload divergence would be fixed and this test needs revisiting")
	}
	if _, ok := m.Resolve("hier-tenant"); !ok {
		t.Errorf("hot-reload should have made the nested tenant resolvable via /effective")
	}

	// (b) Does option C speak up on this path?
	logs := logBuf.String()
	if !strings.Contains(logs, "ERROR: conf.d scanner divergence") {
		t.Errorf("hot-reload path emitted no divergence ERROR, got:\n%s", logs)
	}
	if !strings.Contains(logs, nestedPath) {
		t.Errorf("hot-reload ERROR must name the source file %s, got:\n%s", nestedPath, logs)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Errorf("da_config_hierarchy_divergent_tenants after hot reload = %v, want 1", got)
	}
}

// TestDivergenceAudit_HotReload_FlatModeNeverDetects documents a boundary
// the audit cannot cover: with no _defaults.yaml anywhere, hierarchical
// mode never activates, so detectChange uses the flat top-level composite
// hash — adding a file in a sub-directory triggers no reload at all, hence
// no commitConfig and no audit. The cold-start audit still catches it on
// the next restart (asserted at the end).
func TestDivergenceAudit_HotReload_FlatModeNeverDetects(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  root-tenant:\n    mysql_connections: \"90\"\n")

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}
	writeNestedTenant(t, dir)
	m.tickOnce()

	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("flat-mode hot reload unexpectedly re-audited: gauge = %v", got)
	}
	if logs := logBuf.String(); strings.Contains(logs, "conf.d scanner divergence") {
		t.Errorf("flat-mode tick was not expected to reload at all, got:\n%s", logs)
	}

	// Second boundary, measured rather than reasoned: even when an
	// UNRELATED root-level edit does trigger a reload, the flat path taken
	// (diffAndReload → scanAndCheckHierarchical fallback → IncrementalLoad
	// → commitConfig) never refreshes m.hierarchy.tenantSources, so the
	// audit compares against a stale hierarchy snapshot and still reports
	// 0. The audit call site covers this path; the DATA it reads does not.
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  root-tenant:\n    mysql_connections: \"91\"\n")
	logBuf.Reset()
	m.tickOnce()
	if _, ok := m.GetConfig().Tenants["root-tenant"]; !ok {
		t.Fatalf("setup: the root edit should have reloaded")
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("BOUNDARY CHANGED: flat IncrementalLoad path now reports divergence (gauge=%v); "+
			"the stale-tenantSources limitation documented here would be gone", got)
	}

	// A restart (cold start) does see it, because populateHierarchyState
	// runs regardless of whether hierarchical mode is enabled.
	m2, fresh2, logBuf2 := newAuditedManager(t, dir)
	if err := m2.Load(); err != nil {
		t.Fatalf("restart Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh2.hierarchyDivergentTenants); got != 1 {
		t.Errorf("cold start after restart: gauge = %v, want 1", got)
	}
	if !strings.Contains(logBuf2.String(), "hier-tenant") {
		t.Errorf("cold start after restart must name the tenant, got:\n%s", logBuf2.String())
	}
}

// TestHierarchyDivergentTenants_OneDirectionOnly pins the pure helper's
// asymmetry: tenants present only in the flat config (single-file mode,
// `_`-prefixed root files) are NOT divergence, otherwise the gauge would
// never sit at 0.
func TestHierarchyDivergentTenants_OneDirectionOnly(t *testing.T) {
	t.Parallel()
	if got := hierarchyDivergentTenants(nil, &ThresholdConfig{}); got != nil {
		t.Errorf("empty tenantSources must yield no divergence, got %v", got)
	}

	// ⛔ The REVERSE case has to actually exist in the fixture, and in the
	// first version of this test it did not: every tenant in cfg was also
	// in sources, so the reverse difference was the empty set and the
	// assertion held no matter which direction the implementation
	// compared. Measured — adding a reverse comparison to
	// hierarchyDivergentTenants left this test green.
	//
	// `underscore-tenant` is the shape the one-directional rule exists
	// for and it is not hypothetical: the hierarchical walker skips every
	// `_*.yaml` as a non-tenant file, while the flat merge keeps a
	// `tenants:` block found in one. Reporting it would make the gauge
	// non-zero at rest on a legal layout and destroy "0 means healthy".
	cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
		"flat-only":         {},
		"underscore-tenant": {}, // in cfg, absent from the hierarchy
	}}
	sources := map[string]string{
		"flat-only": "/x/flat-only.yaml",
		"nested":    "/x/db/nested.yaml", // in the hierarchy, absent from cfg
	}
	got := hierarchyDivergentTenants(sources, cfg)
	if len(got) != 1 || got[0] != "nested" {
		t.Errorf("hierarchyDivergentTenants = %v, want [nested] only — "+
			"`underscore-tenant` is the reverse difference and must NOT "+
			"be reported", got)
	}
}

// TestHierarchyDivergentTenants_IsSorted pins the ORDER, which decides
// which ten of a large divergent set the operator is shown.
//
// ⛔ The ordering had no test at all. `TestFormatDivergenceLog_CapsTheSample`
// looks like it covers this, and does not: it hands `formatDivergenceLog`
// an already-ordered slice, so it never exercises the `sort.Strings` that
// produces one. Measured — reversing that sort left the package green.
// Without it the divergent set comes out of Go's randomised map order, so
// `shown[:10]` is a DIFFERENT ten on every reload and an operator chasing
// one misplaced sub-tree watches the names churn.
func TestHierarchyDivergentTenants_IsSorted(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{}}
	sources := map[string]string{
		"zulu": "/x/db/zulu.yaml", "alpha": "/x/db/alpha.yaml",
		"mike": "/x/db/mike.yaml", "bravo": "/x/db/bravo.yaml",
	}
	// Map iteration order is randomised per run, so a single pass could
	// pass by luck; repeat until the odds of a false green are gone.
	want := []string{"alpha", "bravo", "mike", "zulu"}
	for i := 0; i < 20; i++ {
		got := hierarchyDivergentTenants(sources, cfg)
		if len(got) != len(want) {
			t.Fatalf("got %v, want %v", got, want)
		}
		for j := range want {
			if got[j] != want[j] {
				t.Fatalf("iteration %d: got %v, want sorted %v", i, got, want)
			}
		}
	}
}

// TestHierarchyDivergentTenants_NilConfigIsNotAPanic covers the other half
// of the guard clause. Untested, deleting `|| cfg == nil` stayed green —
// and a nil cfg would then panic on `cfg.Tenants[tid]` INSIDE commitConfig,
// taking the whole reload down from an observability-only code path.
func TestHierarchyDivergentTenants_NilConfigIsNotAPanic(t *testing.T) {
	t.Parallel()
	sources := map[string]string{"nested": "/x/db/nested.yaml"}
	if got := hierarchyDivergentTenants(sources, nil); got != nil {
		t.Errorf("nil cfg must yield no divergence, got %v", got)
	}
}

// TestFormatDivergenceLog_CapsTheSample keeps a misplaced 100-tenant
// sub-tree from producing an unreadable log record.
func TestFormatDivergenceLog_CapsTheSample(t *testing.T) {
	t.Parallel()
	sources := make(map[string]string)
	var divergent []string
	for i := 0; i < divergenceLogSampleLimit+5; i++ {
		id := "t" + string(rune('a'+i))
		sources[id] = "/conf.d/db/" + id + ".yaml"
		divergent = append(divergent, id)
	}
	line := formatDivergenceLog(divergent, sources, "/conf.d", "Config loaded (directory)")
	if !strings.Contains(line, "and 5 more") {
		t.Errorf("expected truncation suffix, got:\n%s", line)
	}
	// ⛔ The literal 10, not `divergenceLogSampleLimit`. Both sides of the
	// old assertion referenced the constant, so shrinking it to 3 — which
	// silently cuts what the operator is shown — kept the test green. It
	// pinned that truncation HAPPENS, never how much survives.
	if strings.Count(line, "/conf.d/db/") != 10 {
		t.Errorf("expected exactly 10 sampled paths, got:\n%s", line)
	}
	// The sample must be the FIRST ten of the slice it was handed, not an
	// arbitrary ten. (Whether that slice is sorted is
	// `hierarchyDivergentTenants`' job and is pinned by
	// TestHierarchyDivergentTenants_IsSorted — this test hands in an
	// already-ordered slice, so it cannot speak to the sort.)
	if !strings.Contains(line, " ta (") || strings.Contains(line, " tk (") {
		t.Errorf("sample must be the first ten in sorted order (ta..tj), got:\n%s", line)
	}
	// The two variable fields the message is useless without: WHICH
	// conf.d, and WHICH commit. Passing empty strings for both stayed
	// green before this.
	if !strings.Contains(line, "top level of /conf.d") {
		t.Errorf("log must name the conf.d root, got:\n%s", line)
	}
	if !strings.Contains(line, "(Config loaded (directory))") {
		t.Errorf("log must name the commit context, got:\n%s", line)
	}
}

// TestDivergenceAudit_PairsOneInstant pins the invariant that the audit
// judges the snapshot it was HANDED, never a hierarchy it re-reads for
// itself.
//
// Why this matters, and why it is asserted rather than reasoned about:
// reloads are not serialised. `fireDebounced` clears `debounce.timer`,
// releases `debounce.mu`, and only then calls `diffAndReload`, so a fresh
// event can arm another timer and a second reload can overlap the first.
// While the audit read `m.hierarchy.tenantSources` under its own RLock —
// after commitConfig had released `m.mu` — it could pair reload N's config
// with reload N+1's hierarchy and name a tenant that was never actually
// missing at any single instant. A check that exists to be believed about
// which tenants have no metrics cannot itself invent one.
//
// The test drives that apart deliberately: the live hierarchy is clean, the
// handed-in snapshot is not. If someone reinstates the internal read, the
// audit will consult the clean live map and report 0 — and this fails.
func TestDivergenceAudit_PairsOneInstant(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n")
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"),
		"tenants:\n  root-tenant:\n    mysql_connections: \"90\"\n")

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}

	// Baseline: the live tree is flat and clean, so a self-read would see
	// nothing to report. This is the control — without it, a passing
	// assertion below could just mean "the tree was broken all along".
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Fatalf("setup: flat tree should start clean, gauge = %v", got)
	}
	m.mu.RLock()
	liveSources := m.hierarchy.tenantSources
	m.mu.RUnlock()
	if _, leaked := liveSources["ghost-tenant"]; leaked {
		t.Fatalf("setup: live hierarchy must not already contain the probe tenant")
	}

	// Hand it a snapshot the live manager does not hold.
	handed := map[string]string{
		"ghost-tenant": filepath.Join(dir, "db", "ghost-tenant.yaml"),
	}
	logBuf.Reset()
	got := m.auditHierarchyDivergence(m.GetConfig(), handed, "pairing-probe")

	if got != 1 {
		t.Errorf("audit judged something other than the handed snapshot: got %d, want 1 "+
			"(a 0 here means it re-read m.hierarchy, which is the overlapping-reload bug)", got)
	}
	if gauge := testutil.ToFloat64(fresh.hierarchyDivergentTenants); gauge != 1 {
		t.Errorf("gauge = %v, want 1 — it must follow the handed snapshot too", gauge)
	}
	if logs := logBuf.String(); !strings.Contains(logs, "ghost-tenant") {
		t.Errorf("ERROR log must name the tenant from the handed snapshot, got:\n%s", logs)
	}
}

// ── what the first round of tests could not see ──────────────────────────
//
// Every test below exists because an adversarial pass MEASURED that the
// behaviour above it survives being broken. Each one names the mutation it
// kills, so a later reader can re-run the counterfactual instead of
// trusting this comment.

// TestDivergenceAudit_SnapshotIsTakenInsideTheLockWindow pins the actual
// content of the pairing fix.
//
// ⛔ `TestDivergenceAudit_PairsOneInstant` pins that the audit FUNCTION
// judges what it was handed. It does not pin where the CALLER read it —
// and the caller is what the fix changed. Measured: moving
// `m.hierarchy.tenantSources` out of the swap into its own RLock
// afterwards left `go test -count=1 .` at rc=0, so the whole commit could
// be reverted in silence.
//
// The seam makes the two implementations distinguishable with no
// concurrency and no timing: swap the live hierarchy in the one instant
// after the lock is released. A snapshot taken inside the window still
// carries the pre-swap population; one taken afterwards sees the clean map
// and reports nothing.
func TestDivergenceAudit_SnapshotIsTakenInsideTheLockWindow(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	writeNestedTenant(t, dir)

	m, fresh, logBuf := newAuditedManager(t, dir)
	m.SetAfterCommitUnlockForTest(func() {
		// Stand in for a second, overlapping reload landing a clean
		// hierarchy between the swap and the audit.
		m.mu.Lock()
		m.hierarchy.tenantSources = map[string]string{}
		m.mu.Unlock()
	})

	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}

	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Fatalf("gauge = %v, want 1 — the audit compared against the "+
			"hierarchy as it stood AFTER the commit, not the snapshot "+
			"taken inside the lock window", got)
	}
	if !strings.Contains(logBuf.String(), "hier-tenant") {
		t.Errorf("ERROR log must still name the tenant, got:\n%s", logBuf.String())
	}
	// Control: the live map really was replaced, so the assertion above
	// distinguishes the two implementations rather than passing because
	// nothing changed.
	m.mu.RLock()
	live := len(m.hierarchy.tenantSources)
	m.mu.RUnlock()
	if live != 0 {
		t.Fatalf("setup: hook did not replace the live hierarchy (%d entries)", live)
	}
}

// TestDivergenceAudit_GaugeIsRegisteredUnderItsPublishedName asserts the
// metric reaches /metrics at all.
//
// ⛔ Every other test in this file reads the Go field through
// `testutil.ToFloat64(fresh.hierarchyDivergentTenants)`, which bypasses the
// registry entirely. Measured: deleting `reg.MustRegister(...)` — after
// which the series never appears on /metrics and the alert in the HELP
// text can never fire — left the package green; so did renaming the metric.
// This gauge is the only machine-readable output of the whole #1521
// stopgap, and nothing asserted it existed.
func TestDivergenceAudit_GaugeIsRegisteredUnderItsPublishedName(t *testing.T) {
	t.Parallel()
	const name = "da_config_hierarchy_divergent_tenants"
	fresh, reg := freshMetrics(t)
	fresh.SetHierarchyDivergentTenants(3)

	if got := testutil.CollectAndCount(reg, name); got != 1 {
		t.Fatalf("%s: %d series in the registry, want 1 — the gauge is not "+
			"registered, so it never reaches /metrics", name, got)
	}
	// Value through the registry too, but WITHOUT `GatherAndCompare`:
	// that helper insists on an exact HELP line, which would couple this
	// assertion to a paragraph of operator prose and turn every wording
	// edit into a test failure. Gather and read the family instead.
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather(): %v", err)
	}
	var seen bool
	for _, fam := range families {
		if fam.GetName() != name {
			continue
		}
		seen = true
		if got := fam.GetMetric()[0].GetGauge().GetValue(); got != 3 {
			t.Errorf("%s = %v via the registry, want 3", name, got)
		}
	}
	if !seen {
		t.Errorf("%s absent from Gather() output", name)
	}
}

// TestDivergenceAudit_GaugeCountsTenantsNotJustPresence pins the magnitude.
//
// ⛔ Every integration scenario in this file has exactly ONE nested tenant,
// so the gauge is only ever observed as 0 or 1. Measured: clamping it to
// `min(len, 1)` — turning "how many tenants are dark" into a boolean —
// left the package green. A misplaced sub-directory is commonly a whole
// sub-tree, and the dashboard number would be wrong by that whole factor.
func TestDivergenceAudit_GaugeCountsTenantsNotJustPresence(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	sub := filepath.Join(dir, "db")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("mkdir db: %v", err)
	}
	for _, id := range []string{"one", "two", "three"} {
		writeTestYAML(t, filepath.Join(sub, id+".yaml"),
			"tenants:\n  "+id+":\n    mysql_connections: \"95\"\n")
	}

	m, fresh, _ := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 3 {
		t.Errorf("gauge = %v, want 3 — the gauge must count tenants, not "+
			"report presence", got)
	}
}

// TestDivergenceAudit_RepeatsOnlyWhenTheSetChanges pins the de-duplication
// decided in review.
//
// The divergent state is persistent by construction — #1521 is not fixed —
// while config commits are driven by unrelated fleet churn: any tenant
// editing their own thresholds (ADR-024) causes detectChange → reload →
// commitConfig. The unconditional version re-printed the same ERROR about
// the same untouched tenant every time, so the line's repetition rate
// measured how busy the fleet was rather than how bad the problem was.
// The sibling mechanism already worked this way: `classifyTenant` returns
// early when neither a tenant's source nor its defaults chain moved.
//
// ⛔ Three separate properties, because dropping any one of them turns a
// noise fix into a lost signal: the gauge is still Set on EVERY commit,
// the log repeats when the SET changes, and recovery to zero re-arms it so
// a relapse is loud again.
func TestDivergenceAudit_RepeatsOnlyWhenTheSetChanges(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}

	cfg := m.GetConfig()
	one := map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}
	two := map[string]string{
		"nested-a": filepath.Join(dir, "db", "a.yaml"),
		"nested-b": filepath.Join(dir, "db", "b.yaml"),
	}
	count := func() int { return strings.Count(logBuf.String(), "scanner divergence") }

	logBuf.Reset()
	m.auditHierarchyDivergence(cfg, one, "commit-1")
	if count() != 1 {
		t.Fatalf("first sighting must be logged, got %d lines", count())
	}
	m.auditHierarchyDivergence(cfg, one, "commit-2")
	m.auditHierarchyDivergence(cfg, one, "commit-3")
	if count() != 1 {
		t.Errorf("an unchanged divergent set must not re-log: %d lines", count())
	}
	// ...but the gauge is a level and is re-Set every time.
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Errorf("gauge = %v, want 1 after the repeat commits", got)
	}

	// A CHANGED set is news.
	m.auditHierarchyDivergence(cfg, two, "commit-4")
	if count() != 2 {
		t.Errorf("a changed divergent set must log again: %d lines", count())
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 2 {
		t.Errorf("gauge = %v, want 2", got)
	}

	// Recovery clears the memory, so a relapse is loud rather than
	// swallowed as "same as last time".
	m.auditHierarchyDivergence(cfg, nil, "commit-5-healthy")
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Fatalf("gauge = %v, want 0 on recovery", got)
	}
	m.auditHierarchyDivergence(cfg, two, "commit-6-relapse")
	if count() != 3 {
		t.Errorf("a relapse after recovery must log again: %d lines", count())
	}
}

// TestDivergenceAudit_GaugeAndMemoryCannotDisagree replays the interleaving
// that made the FIRST version of the de-duplication lose a signal outright.
//
// ⛔ Not a hypothetical: the gauge Set and the memory update used to be two
// unsynchronised statements, and overlapping reloads are real. Replaying four
// legal statements in a legal order left `gauge = 0` while the memory said the
// set had already been reported — and the next genuine recurrence then set the
// gauge back to non-zero and wrote nothing. Measured before the fix:
// `audit returned 1, gauge = 1, ERROR lines = 0`.
//
// The test drives the two halves apart the only way that is deterministic:
// it interleaves by hand rather than racing goroutines, so it can never be
// flaky and never passes by luck of the scheduler.
func TestDivergenceAudit_GaugeAndMemoryCannotDisagree(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	cfg := m.GetConfig()
	one := map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}
	count := func() int { return strings.Count(logBuf.String(), "scanner divergence") }

	// Reload N sees the divergence; reload N+1 (overlapping) sees a clean
	// tree. Whichever wins, the pair must stay consistent.
	logBuf.Reset()
	m.auditHierarchyDivergence(cfg, one, "reload-N")
	m.auditHierarchyDivergence(cfg, nil, "reload-N+1-healthy")
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Fatalf("gauge = %v after the healthy commit, want 0", got)
	}

	// ⛔ THE ASSERTION. A genuine recurrence must be audible. Under the old
	// two-statement shape the memory could still hold the set here.
	before := count()
	m.auditHierarchyDivergence(cfg, one, "genuine-recurrence")
	if testutil.ToFloat64(fresh.hierarchyDivergentTenants) != 1 {
		t.Fatalf("gauge did not rise on recurrence")
	}
	if count() == before {
		t.Errorf("LOST SIGNAL: the gauge rose but no ERROR line was written — "+
			"the level and the memory disagreed (%d lines before and after)", before)
	}
}

// TestDivergenceAudit_TheGaugeIsSetUnderTheSameLockAsTheMemory pins the
// atomicity itself, structurally and deterministically.
//
// ⛔ Written because the FIRST regression test for this bug had no detection
// power, and only a mutation showed it: moving `setGauge` back outside
// `d.mu` — the exact shape that lost the signal — left
// TestDivergenceAudit_GaugeAndMemoryCannotDisagree green. That test
// interleaves whole audit CALLS, while the defect lives between two
// statements INSIDE one call, so a single-threaded test can never see it.
//
// ⚠️ And racing goroutines is not the answer either: the blind review that
// found this needed 300,000 rounds under `-race` to hit the fatal
// interleaving 9 times. A test like that is a coin flip in CI.
//
// So assert the invariant rather than the symptom: while `setGauge` runs, the
// lock must be HELD. `TryLock` answers that with no scheduler involved — it
// succeeds only if nobody holds the mutex, which here would mean the gauge is
// being written outside the critical section that guards the memory.
func TestDivergenceAudit_TheGaugeIsSetUnderTheSameLockAsTheMemory(t *testing.T) {
	t.Parallel()
	var d divergenceLogState
	var lockHeldDuringSet, setCalled bool

	probe := func(int) {
		setCalled = true
		if d.mu.TryLock() {
			d.mu.Unlock() // it was free — the gauge is outside the lock
			return
		}
		lockHeldDuringSet = true
	}

	d.recordAndDecide([]string{"a"}, map[string]string{"a": "/x/a.yaml"}, probe)
	if !setCalled {
		t.Fatal("setGauge was never called — the probe proves nothing")
	}
	if !lockHeldDuringSet {
		t.Error("the gauge is Set OUTSIDE d.mu: the level and the memory can " +
			"then be updated by different reloads in either order, leaving " +
			"gauge=0 while the memory says the set was already reported — " +
			"after which a genuine recurrence is silent")
	}

	// Control: the same probe must also run on the healthy (empty) path, so
	// the assertion above cannot be satisfied by one branch alone.
	lockHeldDuringSet, setCalled = false, false
	d.recordAndDecide(nil, nil, probe)
	if !setCalled || !lockHeldDuringSet {
		t.Errorf("empty-set path: setCalled=%v lockHeld=%v — the gauge must be "+
			"Set under the lock on BOTH branches", setCalled, lockHeldDuringSet)
	}
}

// TestDivergenceAudit_ReLogsWhenTheSOURCEPATHMoves pins the dedup key's
// contents, not just its existence.
//
// The ERROR line names each tenant AND its source file. A key built from
// tenant IDs alone made the same tenants at a NEW path look like a repeat, so
// nothing re-printed and the operator's one line kept pointing at a path that
// no longer existed.
func TestDivergenceAudit_ReLogsWhenTheSourcePathMoves(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	m, _, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	cfg := m.GetConfig()
	count := func() int { return strings.Count(logBuf.String(), "scanner divergence") }

	logBuf.Reset()
	m.auditHierarchyDivergence(cfg,
		map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}, "c1")
	if count() != 1 {
		t.Fatalf("first sighting must log, got %d", count())
	}
	// Same tenant, DIFFERENT file.
	m.auditHierarchyDivergence(cfg,
		map[string]string{"nested-a": filepath.Join(dir, "db", "deep", "a.yaml")}, "c2")
	if count() != 2 {
		t.Errorf("a moved source path must re-log: %d lines", count())
	}
	if !strings.Contains(logBuf.String(), filepath.Join("db", "deep", "a.yaml")) {
		t.Errorf("the new path must appear in the log:\n%s", logBuf.String())
	}
	// ...and an unchanged repeat still does not.
	m.auditHierarchyDivergence(cfg,
		map[string]string{"nested-a": filepath.Join(dir, "db", "deep", "a.yaml")}, "c3")
	if count() != 2 {
		t.Errorf("an unchanged repeat must stay quiet: %d lines", count())
	}
}

// TestDivergenceAudit_LogNamesTheRootAndTheCommit pins the two variable
// fields at the CALL SITE, not in the formatter.
//
// ⛔ `TestFormatDivergenceLog_CapsTheSample` passes `"/conf.d"` and a
// context string in by hand, so it proves the formatter renders whatever
// it is given — not that `auditHierarchyDivergence` gives it anything.
// Measured: replacing both arguments at the call site with `""` left the
// package green. The resulting line reads "the flat scanner reads only the
// top level of " and opens with an empty "()", i.e. the operator is told a
// tenant is dark without being told which conf.d or which reload.
func TestDivergenceAudit_LogNamesTheRootAndTheCommit(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	writeNestedTenant(t, dir)

	m, _, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}

	line := logBuf.String()
	if !strings.Contains(line, "top level of "+dir) {
		t.Errorf("log must name the conf.d root %q, got:\n%s", dir, line)
	}
	if !strings.Contains(line, "(Config loaded (directory))") {
		t.Errorf("log must name the commit context, got:\n%s", line)
	}
}
