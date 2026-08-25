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

// TestDivergenceAudit_ColdStart_NestedTenantIsNowSilent is the INVERTED
// tripwire. It used to assert the audit shouted about a nested tenant; #1521
// closed the divergence, so the same tree must now be silent AND the tenant
// must be on the output plane.
//
// ⛔ Kept rather than deleted, and kept driving the REAL entry: the audit code
// stays in the tree for one release as an invariant (gauge must read 0), so
// this is the assertion that a relapse — a scanner that stops recursing, a key
// shape that collides — is loud again on the next commit.
func TestDivergenceAudit_ColdStart_NestedTenantIsNowSilent(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	writeNestedTenant(t, dir)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	// Both planes, same tenant. The pair is the point — either one alone was
	// satisfied before the fix.
	if _, ok := m.Resolve("hier-tenant"); !ok {
		t.Fatalf("precondition: the hierarchical scanner must still resolve hier-tenant")
	}
	if _, ok := m.GetConfig().Tenants["hier-tenant"]; !ok {
		t.Errorf("#1521 REGRESSED: the nested tenant is back to resolving through "+
			"/effective while missing from the collector config, so its alerts "+
			"cannot fire. Tenants: %v", keysOfTenants(m.GetConfig()))
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("da_config_hierarchy_divergent_tenants = %v, want 0", got)
	}
	if logs := logBuf.String(); strings.Contains(logs, "conf.d scanner divergence") {
		t.Errorf("a reconciled tree must produce no divergence ERROR, got:\n%s", logs)
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
// (the gauge-over-counter argument): a divergent set reads N, a clean one
// falls back to 0.
//
// ⛔ Driven through `auditHierarchyDivergence` directly rather than through a
// nested fixture, because #1521 removed the only way to make a real tree
// diverge. The property under test was never about the tree — it is about the
// gauge being Set on every commit rather than incremented.
func TestDivergenceAudit_GaugeRecoversToZero(t *testing.T) {
	t.Parallel()
	m, fresh, _ := newAuditedManager(t, t.TempDir())

	divergent := map[string]string{"hier-tenant": "/conf.d/db/hier-tenant.yaml"}
	clean := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{}}
	if n := m.auditHierarchyDivergence(clean, divergent, nil, "probe"); n != 1 {
		t.Fatalf("setup: audit reported %d divergent tenant(s), want 1", n)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Fatalf("setup: gauge = %v, want 1", got)
	}

	reconciled := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
		"hier-tenant": {},
	}}
	if n := m.auditHierarchyDivergence(reconciled, divergent, nil, "probe"); n != 0 {
		t.Errorf("audit still reports %d divergent tenant(s) after reconciliation", n)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("gauge did not recover: %v, want 0", got)
	}
}

// TestDivergenceAudit_HotReload_HierarchicalPath drives the real
// detect→debounce→diffAndReload chain via tickOnce (debounce window 0 = the
// synchronous seam config_debounce.go documents) and asserts the half that
// used to fail: a nested tenant added to a RUNNING exporter now reaches
// m.config, not just Resolve().
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

	writeNestedTenant(t, dir)
	m.tickOnce()

	if _, ok := m.Resolve("hier-tenant"); !ok {
		t.Errorf("hot reload should have made the nested tenant resolvable via /effective")
	}
	if _, ok := m.GetConfig().Tenants["hier-tenant"]; !ok {
		t.Errorf("#1521 REGRESSED on the hot-reload path: the nested tenant did not "+
			"reach the collector config. Tenants: %v", keysOfTenants(m.GetConfig()))
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("gauge after hot reload = %v, want 0", got)
	}
	if logs := logBuf.String(); strings.Contains(logs, "conf.d scanner divergence") {
		t.Errorf("hot reload of a reconciled tree must be silent, got:\n%s", logs)
	}
}

// TestDivergenceAudit_HotReload_FlatModeNowDetects closes the boundary this
// test used to DOCUMENT.
//
// ⛔ Before #1521 it asserted the opposite and said so in its name: with no
// `_defaults.yaml` anywhere, hierarchical mode never activates, so
// `detectChange` used the flat TOP-LEVEL composite hash and a file added in a
// subdirectory triggered no reload at all — no commit, no audit, nothing until
// the next restart. Making the flat scanner recurse moves those files INTO the
// composite hash, so the same edit is now a detected change.
//
// Measured, not reasoned: `detectChange` returns changed=true / reason=source
// on this fixture, where it returned false before.
func TestDivergenceAudit_HotReload_FlatModeNowDetects(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"),
		"tenants:\n  root-tenant:\n    mysql_connections: \"90\"\n")

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}
	m.mu.RLock()
	hierarchical := m.hierarchy.enabled
	m.mu.RUnlock()
	if hierarchical {
		t.Fatalf("precondition: this tree has no _defaults.yaml, so hierarchical " +
			"mode must be off — otherwise the flat branch under test is not the " +
			"one being exercised")
	}

	// mtime guard has a 2s safety window for files younger than that; the new
	// file is new, so its hash is computed rather than reused.
	writeNestedTenant(t, dir)

	changed, reason, err := m.detectChange()
	if err != nil {
		t.Fatalf("detectChange: %v", err)
	}
	if !changed {
		t.Errorf("flat-mode detectChange did not see a file added in a "+
			"subdirectory (reason=%q). Before #1521 this was the documented "+
			"dead spot: nothing reloaded until the next restart", reason)
	}

	m.tickOnce()
	if _, ok := m.GetConfig().Tenants["hier-tenant"]; !ok {
		t.Errorf("flat-mode hot reload did not admit the nested tenant: %v",
			keysOfTenants(m.GetConfig()))
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("gauge = %v, want 0", got)
	}
	if logs := logBuf.String(); strings.Contains(logs, "conf.d scanner divergence") {
		t.Errorf("no divergence expected, got:\n%s", logs)
	}
}

// TestHierarchyDivergentTenants_OneDirectionOnly pins the pure helper's
// asymmetry: tenants present only in the flat config (single-file mode,
// `_`-prefixed root files) are NOT divergence, otherwise the gauge would
// never sit at 0.
func TestHierarchyDivergentTenants_OneDirectionOnly(t *testing.T) {
	t.Parallel()
	if got := hierarchyDivergentTenants(nil, &ThresholdConfig{}, nil); got != nil {
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
	got := hierarchyDivergentTenants(sources, cfg, nil)
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
		got := hierarchyDivergentTenants(sources, cfg, nil)
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
	if got := hierarchyDivergentTenants(sources, nil, nil); got != nil {
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
	line := formatDivergenceLog(divergent, sources, nil, "/conf.d", "Config loaded (directory)")
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
	//
	// ⛔ The root is asserted in the HEADER, not anywhere in the line. Pinning
	// the literal "top level of /conf.d" kept a sentence alive that stopped
	// being true the moment #1521 made the scanner recursive; relaxing it to
	// a bare `Contains(line, dir)` then made it a TAUTOLOGY, because the
	// `Affected: t (…/db/x.yaml)` tail already carries the root. Measured:
	// with the root dropped from the Cause sentence entirely, the whole
	// package stayed green. Splitting at "Affected:" asks the question the
	// comment below claims to be asking. (#1569 blind review.)
	assertNamesTheRootBeforeTheList(t, line, "/conf.d")
	assertNoStaleRemediation(t, line)
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
	got := m.auditHierarchyDivergence(m.GetConfig(), handed, nil, "pairing-probe")

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

// TestDivergenceAudit_SnapshotIsTakenInsideTheLockWindow keeps the property
// and INVERTS the rig, because #1521 removed the tree that used to diverge.
//
// Before: the tree was divergent and the hook wiped the live hierarchy after
// the commit; a gauge of 1 proved the audit had used the snapshot.
// Now: the tree is clean and the hook DIRTIES the live hierarchy after the
// commit. A gauge of 0 proves the same thing — the audit read the snapshot
// taken inside the lock window, not whatever the live map became afterwards.
// Same discrimination, opposite polarity: an implementation that re-read the
// live map would report 1 here.
func TestDivergenceAudit_SnapshotIsTakenInsideTheLockWindow(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)

	m, fresh, _ := newAuditedManager(t, dir)
	m.SetAfterCommitUnlockForTest(func() {
		// Stand in for a second, overlapping reload landing a DIVERGENT
		// hierarchy between the swap and the audit.
		m.mu.Lock()
		m.hierarchy.tenantSources = map[string]string{
			"ghost-tenant": filepath.Join(dir, "db", "ghost.yaml"),
		}
		m.mu.Unlock()
	})

	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}

	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Fatalf("gauge = %v, want 0 — the audit compared against the hierarchy "+
			"as it stood AFTER the commit, not the snapshot taken inside the "+
			"lock window", got)
	}
	// Control: the live map really was replaced, so the assertion above
	// distinguishes the two implementations rather than passing because
	// nothing changed.
	m.mu.RLock()
	_, ghost := m.hierarchy.tenantSources["ghost-tenant"]
	m.mu.RUnlock()
	if !ghost {
		t.Fatalf("setup: hook did not replace the live hierarchy")
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

// TestDivergenceAudit_GaugeCountsTenantsNotJustPresence pins that the gauge is
// a COUNT, not a 0/1 flag — an implementation that Set(1) on any divergence
// would pass every other assertion in this file.
//
// ⛔ Driven through `auditHierarchyDivergence` directly: #1521 removed the tree
// shape that produced three divergent tenants, and the property was never
// about the tree.
func TestDivergenceAudit_GaugeCountsTenantsNotJustPresence(t *testing.T) {
	t.Parallel()
	m, fresh, _ := newAuditedManager(t, t.TempDir())

	sources := map[string]string{}
	for _, id := range []string{"one", "two", "three"} {
		sources[id] = "/conf.d/db/" + id + ".yaml"
	}
	empty := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{}}

	if n := m.auditHierarchyDivergence(empty, sources, nil, "probe"); n != 3 {
		t.Errorf("audit returned %d, want 3", n)
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
	m.auditHierarchyDivergence(cfg, one, nil, "commit-1")
	if count() != 1 {
		t.Fatalf("first sighting must be logged, got %d lines", count())
	}
	m.auditHierarchyDivergence(cfg, one, nil, "commit-2")
	m.auditHierarchyDivergence(cfg, one, nil, "commit-3")
	if count() != 1 {
		t.Errorf("an unchanged divergent set must not re-log: %d lines", count())
	}
	// ...but the gauge is a level and is re-Set every time.
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Errorf("gauge = %v, want 1 after the repeat commits", got)
	}

	// A CHANGED set is news.
	m.auditHierarchyDivergence(cfg, two, nil, "commit-4")
	if count() != 2 {
		t.Errorf("a changed divergent set must log again: %d lines", count())
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 2 {
		t.Errorf("gauge = %v, want 2", got)
	}

	// Recovery clears the memory, so a relapse is loud rather than
	// swallowed as "same as last time".
	m.auditHierarchyDivergence(cfg, nil, nil, "commit-5-healthy")
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Fatalf("gauge = %v, want 0 on recovery", got)
	}
	m.auditHierarchyDivergence(cfg, two, nil, "commit-6-relapse")
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
	m.auditHierarchyDivergence(cfg, one, nil, "reload-N")
	m.auditHierarchyDivergence(cfg, nil, nil, "reload-N+1-healthy")
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Fatalf("gauge = %v after the healthy commit, want 0", got)
	}

	// ⛔ THE ASSERTION. A genuine recurrence must be audible. Under the old
	// two-statement shape the memory could still hold the set here.
	before := count()
	m.auditHierarchyDivergence(cfg, one, nil, "genuine-recurrence")
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
		map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}, nil, "c1")
	if count() != 1 {
		t.Fatalf("first sighting must log, got %d", count())
	}
	// Same tenant, DIFFERENT file.
	m.auditHierarchyDivergence(cfg,
		map[string]string{"nested-a": filepath.Join(dir, "db", "deep", "a.yaml")}, nil, "c2")
	if count() != 2 {
		t.Errorf("a moved source path must re-log: %d lines", count())
	}
	if !strings.Contains(logBuf.String(), filepath.Join("db", "deep", "a.yaml")) {
		t.Errorf("the new path must appear in the log:\n%s", logBuf.String())
	}
	// ...and an unchanged repeat still does not.
	m.auditHierarchyDivergence(cfg,
		map[string]string{"nested-a": filepath.Join(dir, "db", "deep", "a.yaml")}, nil, "c3")
	if count() != 2 {
		t.Errorf("an unchanged repeat must stay quiet: %d lines", count())
	}
}

// TestDivergenceAudit_LogNamesTheRootAndTheCommit pins the two pieces of
// context an operator needs to act on the line: WHICH tree, and WHICH commit
// produced it. Driven through the audit directly for the same reason as its
// siblings — the real tree no longer diverges.
func TestDivergenceAudit_LogNamesTheRootAndTheCommit(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	m, _, logBuf := newAuditedManager(t, dir)

	m.auditHierarchyDivergence(
		&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{}},
		map[string]string{"hier-tenant": filepath.Join(dir, "db", "hier-tenant.yaml")},
		nil,
		"Config loaded (directory)")

	line := logBuf.String()
	assertNamesTheRootBeforeTheList(t, line, dir)
	assertNoStaleRemediation(t, line)
	if !strings.Contains(line, "(Config loaded (directory))") {
		t.Errorf("log must name the commit context, got:\n%s", line)
	}
}

// TestDivergenceAudit_TheMemoryTracksTheLatestSetNotTheFirst pins that
// `d.last` is REPLACED on every logged set, not written once.
//
// ⛔ Surviving mutation (A-tier, mutation review round 3): making the
// assignment conditional — `if d.last == "" { d.last = key }` — left the
// whole suite green. Every existing sequence happened to be A→A→B or
// A→B→recovery→B, and a memory frozen on the FIRST set answers all of
// those identically: A is suppressed because it matches, B logs because
// it does not, and the healthy commit clears the memory back to "" before
// the next comparison can expose the freeze.
//
// The sequence that separates them is A→B→A with no healthy commit in
// between. Correct behaviour logs three times (the operator's last line
// must always describe the CURRENT set). A frozen memory logs twice and
// then goes quiet on a divergence that is live — the exact failure this
// de-duplication was rewritten once already to avoid.
func TestDivergenceAudit_TheMemoryTracksTheLatestSetNotTheFirst(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	m, _, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	cfg := m.GetConfig()

	setA := map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}
	setB := map[string]string{"nested-b": filepath.Join(dir, "db", "b.yaml")}
	count := func() int { return strings.Count(logBuf.String(), "scanner divergence") }

	logBuf.Reset()
	m.auditHierarchyDivergence(cfg, setA, nil, "commit-A1")
	if count() != 1 {
		t.Fatalf("first sighting of A must log, got %d", count())
	}
	m.auditHierarchyDivergence(cfg, setB, nil, "commit-B")
	if count() != 2 {
		t.Fatalf("a different set must log, got %d", count())
	}
	// ⛔ The one that matters. B is what the memory should now hold, so A
	// is news again. With the memory stuck on A this is silent.
	m.auditHierarchyDivergence(cfg, setA, nil, "commit-A2")
	if got := count(); got != 3 {
		t.Errorf("going back to a PREVIOUS divergent set must log again "+
			"(the last line an operator has must describe the current set): "+
			"%d line(s), want 3\nlog:\n%s", got, logBuf.String())
	}
	if !strings.Contains(logBuf.String(), "commit-A2") {
		t.Errorf("the third line must be the A2 commit; log:\n%s", logBuf.String())
	}
}

// TestDivergenceAudit_TheGaugeIsRepublishedEvenWhenTheLogIsSuppressed pins
// that the gauge is Set on EVERY commit, including the ones the
// de-duplication silences.
//
// ⛔ Surviving mutation (A-tier): moving `setGauge` below the
// `d.last == key` early return — so the level is only written when the
// line is written — left the suite green. Nothing noticed, because the
// gauge already HELD the right number from the commit that did log, and
// every assertion read it after that.
//
// The consequence is real rather than stylistic: a gauge written only on
// change is a change-detector wearing a level's name, and it is wrong the
// moment anything else moves the underlying metric. This test moves it the
// way this package's own seam does — `SetMetrics` with a fresh registry,
// which is exactly what a metrics re-registration does in production. The
// divergence is unchanged and still live, so the log stays (correctly)
// silent; the new gauge must nevertheless carry the count. Under the
// mutation it reads 0 — a platform reporting "healthy" over a defect it is
// still observing.
func TestDivergenceAudit_TheGaugeIsRepublishedEvenWhenTheLogIsSuppressed(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	m, first, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	cfg := m.GetConfig()
	sources := map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}
	count := func() int { return strings.Count(logBuf.String(), "scanner divergence") }

	logBuf.Reset()
	m.auditHierarchyDivergence(cfg, sources, nil, "commit-1")
	if count() != 1 {
		t.Fatalf("first sighting must log, got %d", count())
	}
	if got := testutil.ToFloat64(first.hierarchyDivergentTenants); got != 1 {
		t.Fatalf("first gauge = %v, want 1", got)
	}

	// The metric object is replaced; the divergence is not.
	second, _ := freshMetrics(t)
	m.SetMetrics(second)
	if got := testutil.ToFloat64(second.hierarchyDivergentTenants); got != 0 {
		t.Fatalf("a fresh metrics instance must start at 0, got %v", got)
	}

	m.auditHierarchyDivergence(cfg, sources, nil, "commit-2")
	if got := count(); got != 1 {
		t.Errorf("the unchanged set must still not re-log: %d line(s)", got)
	}
	if got := testutil.ToFloat64(second.hierarchyDivergentTenants); got != 1 {
		t.Errorf("gauge on the CURRENT metrics = %v, want 1 — the level must "+
			"be re-published on every commit, not only on the ones that log, "+
			"or a metrics re-registration leaves the platform reporting 0 "+
			"over a divergence it is still observing", got)
	}
}

// TestDivergenceAudit_TheReturnValueSurvivesSuppression pins that the
// count handed back to callers is the size of the divergent set, not
// "how much did I print".
//
// ⛔ Surviving mutation (B-tier): `return 0` on the suppressed path left
// the suite green — every existing assertion on the return value happened
// to be on a FIRST sighting, which logs. The value is the audit's
// programmatic answer (`commitConfig` and the tests read it); tying it to
// the de-duplication would make "is there a divergence right now?" mean
// "did we happen to print about it this time?".
func TestDivergenceAudit_TheReturnValueSurvivesSuppression(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	m, _, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	cfg := m.GetConfig()
	sources := map[string]string{
		"nested-a": filepath.Join(dir, "db", "a.yaml"),
		"nested-b": filepath.Join(dir, "db", "b.yaml"),
	}

	logBuf.Reset()
	if got := m.auditHierarchyDivergence(cfg, sources, nil, "commit-1"); got != 2 {
		t.Fatalf("first sighting returned %d, want 2", got)
	}
	got := m.auditHierarchyDivergence(cfg, sources, nil, "commit-2")
	if lines := strings.Count(logBuf.String(), "scanner divergence"); lines != 1 {
		t.Fatalf("precondition: the second call must be suppressed, %d line(s)", lines)
	}
	if got != 2 {
		t.Errorf("suppressed call returned %d, want 2 — the return value is "+
			"the size of the divergent set, not whether this call printed", got)
	}
}

// TestDivergenceLogState_TheKeyCannotBeAmbiguousAcrossTenantAndSource pins
// the separator BETWEEN a tenant ID and its source path.
//
// ⛔ Surviving mutation (B-tier): dropping that `WriteByte(0)` left the
// suite green. Concatenation without it is not injective — `{a → bc}` and
// `{ab → c}` both render as "abc", so a genuinely different divergent set
// is mistaken for a repeat and the operator is told nothing.
//
// ⚠️ The SECOND separator (after the source path) has its own test —
// see TestDivergenceLogState_TheKeyCannotBeAmbiguousAcrossAdjacentPairs.
// An earlier revision of this comment argued it was an EQUIVALENT mutation
// and needed none. That argument was wrong and a blind reviewer broke it:
// counting NUL bytes fixes how many PAIRS the key holds, but not where
// `source[i]` ends and `tenant[i+1]` begins, and that boundary is exactly
// what the trailing byte marks.
func TestDivergenceLogState_TheKeyCannotBeAmbiguousAcrossTenantAndSource(t *testing.T) {
	t.Parallel()
	var d divergenceLogState
	noop := func(int) {}

	if !d.recordAndDecide([]string{"a"}, map[string]string{"a": "bc"}, noop) {
		t.Fatalf("first set must be reported as new")
	}
	if !d.recordAndDecide([]string{"ab"}, map[string]string{"ab": "c"}, noop) {
		t.Errorf("{ab → c} is a DIFFERENT divergent set from {a → bc} and must " +
			"be reported as new; without the tenant/source separator both " +
			"render as \"abc\" and the second one is silently swallowed")
	}
}

// TestDivergenceLogState_TheKeyCannotBeAmbiguousAcrossAdjacentPairs pins the
// separator AFTER the source path — the boundary between one pair and the
// next.
//
// ⛔ This test exists because a blind reviewer broke an argument this file
// used to carry, and the argument was mine. The claim was that with the
// tenant/source separator present the encoding stays injective without this
// one, because "the number of NUL bytes equals the number of pairs". That is
// true and irrelevant: knowing there are N pairs does not say where
// `source[i]` stops and `tenant[i+1]` starts, and nothing else marks that
// boundary. Measured with the byte removed — two divergent sets that share
// no tenant ID and no path collapse onto the same key:
//
//	{acme-corp → /etc/conf.d/xy, z-tenant  → /etc/other.yaml}
//	{acme-corp → /etc/conf.d/x,  yz-tenant → /etc/other.yaml}
//	both → "acme-corp\0/etc/conf.d/xyz-tenant\0/etc/other.yaml"
//
// ⚠️ No NUL byte, no empty tenant ID, no exotic filename — ordinary
// alphanumeric-and-hyphen tenant IDs and ordinary absolute paths, which is
// what makes it worth a test rather than a caveat. The consequence is the
// same one the sibling test guards: the second set is taken for a repeat and
// the operator is told nothing about a divergence that is live.
func TestDivergenceLogState_TheKeyCannotBeAmbiguousAcrossAdjacentPairs(t *testing.T) {
	t.Parallel()
	var d divergenceLogState
	noop := func(int) {}

	first := d.recordAndDecide(
		[]string{"acme-corp", "z-tenant"},
		map[string]string{
			"acme-corp": "/etc/conf.d/xy",
			"z-tenant":  "/etc/other.yaml",
		}, noop)
	if !first {
		t.Fatalf("first set must be reported as new")
	}

	second := d.recordAndDecide(
		[]string{"acme-corp", "yz-tenant"},
		map[string]string{
			"acme-corp": "/etc/conf.d/x",
			"yz-tenant": "/etc/other.yaml",
		}, noop)
	if !second {
		t.Errorf("a divergent set that shares no tenant ID and no path with the " +
			"previous one must be reported as new; without the separator after " +
			"the source path the two render identically and the second is " +
			"silently swallowed")
	}
}

// assertNoStaleRemediation fails if the operator-facing divergence message
// still carries the pre-#1521 diagnosis.
//
// ⛔ THIS ASSERTS AN INVARIANT, NOT A SENTENCE, and the distinction is the
// whole point. "The flat scanner reads only the top level" and "move the
// tenant file to the conf.d root" were true when the message was written and
// false the moment the scanner became recursive — an operator following that
// remediation today would move a file that was never the problem. A test that
// pins the exact wording cannot tell "the prose was improved" from "the
// diagnosis went stale"; one that forbids the OBSOLETE CLAIM can. Found by
// blind review of #1569, which noticed the message and this file's own header
// still described the closed defect.
func assertNoStaleRemediation(t *testing.T, line string) {
	t.Helper()
	for _, stale := range []string{
		"top level of",
		"move the tenant file",
		"sub-directories never reach",
	} {
		if strings.Contains(line, stale) {
			t.Errorf("divergence message still carries the pre-#1521 diagnosis %q "+
				"(directory depth stopped being a cause when the flat scanner became "+
				"recursive); got:\n%s", stale, line)
		}
	}
	// ⛔ A BLACKLIST IS NOT AN INVARIANT, and pretending otherwise was this
	// round's own overstatement. A reviewer reinstated the exact obsolete
	// claim in different words — "only enumerates the first directory level",
	// "relocate the tenant YAML into the conf.d root" — and the package
	// stayed green: the helper forbids three STRINGS, not the CLAIM. The
	// positive half below is what actually holds the diagnosis in place; the
	// blacklist stays as a cheap tripwire for a literal revert. (#1569.)
	for _, required := range []string{"dropped", "parse"} {
		if !strings.Contains(strings.ToLower(line), required) {
			t.Errorf("operator text no longer states the CURRENT cause (a file dropped "+
				"while building the merged config because its platform block failed to "+
				"parse) — %q is missing; got:\n%s", required, line)
		}
	}
}

// assertNamesTheRootBeforeTheList checks the root is named in the message
// HEADER rather than merely appearing somewhere in the line.
//
// ⛔ The affected-tenant list ends every message with source PATHS, all of
// which contain the root, so `Contains(line, root)` is satisfied even when
// the header never says which conf.d produced the divergence. Measured: with
// the root removed from the Cause sentence, the package stayed green.
func assertNamesTheRootBeforeTheList(t *testing.T, line, root string) {
	t.Helper()
	head, _, found := strings.Cut(line, "Affected:")
	if !found {
		t.Fatalf("message has no %q section, so the header cannot be isolated:\n%s", "Affected:", line)
	}
	if !strings.Contains(head, root) {
		t.Errorf("the header never names the conf.d root %q — an operator reading a "+
			"multi-tree log stream cannot tell which tree diverged; header:\n%s", root, head)
	}
}
