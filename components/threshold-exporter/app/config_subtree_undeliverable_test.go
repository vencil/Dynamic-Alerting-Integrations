package main

// Tests for the subtree-undeliverable audit (config_subtree_undeliverable.go;
// the #1521 dual-scanner divergence audit until #1957 removed its cause (a)).
//
// The audit is observability-only: it must be LOUD when a tenant inherits a
// key that exists only in a subtree `_defaults.yaml` (which /effective
// reports and the collector cannot emit), and completely SILENT on a tree
// where every inherited key is deliverable. Both halves are asserted — the
// silent (control) half is what makes the loud half mean something.
//
// ⛔ Since #1957 a tenant that /effective serves and /metrics lacks is NOT
// this audit's business any more: both planes judge a file with one decode,
// so the same bytes get the same tenant verdict (pinned by
// config_unified_parse_test.go, together with the #1980 exception of the
// incremental tenant-only reload). The tests below that used to hand the audit
// "a tenant missing from cfg" now hand it "a tenant with a refused key".
//
// Injection discipline (CLAUDE.md §測試注入 Seam): metrics via SetMetrics
// + freshMetrics, logger via SetLogger. No global swaps.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

const nestedTenantYAML = "tenants:\n  hier-tenant:\n    mysql_connections: \"95\"\n"

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
	writeTestYAML(t, path, nestedTenantYAML)
	return path
}

// writeAuditRoot lays down the always-present root files: a _defaults.yaml
// (so hierarchical mode activates) plus one healthy root-level tenant that
// must never be reported.
func writeAuditRoot(t *testing.T, dir string) {
	t.Helper()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  root-tenant:\n    mysql_connections: \"90\"\n")
}

// refusedFor gives every tenant in sources one undeliverable key — the
// input shape applySubtreeDefaults produces for a subtree-only key.
func refusedFor(sources map[string]string) map[string][]string {
	out := make(map[string][]string, len(sources))
	for tid := range sources {
		out[tid] = []string{"redis_evicted_keys"}
	}
	return out
}

// TestUndeliverableAudit_ColdStart_NestedTenantIsSilent: a nested tenant
// whose every inherited key is deliverable must be on the output plane and
// must not be reported. (Before #1521 this tree was the audit's first true
// positive; the assertion is kept as the relapse tripwire.)
func TestUndeliverableAudit_ColdStart_NestedTenantIsSilent(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)
	writeNestedTenant(t, dir)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	// Both planes, same tenant. The pair is the point — either one alone was
	// satisfied before #1521.
	if _, ok := m.Resolve("hier-tenant"); !ok {
		t.Fatalf("precondition: the hierarchical scanner must still resolve hier-tenant")
	}
	if _, ok := m.GetConfig().Tenants["hier-tenant"]; !ok {
		t.Errorf("#1521 REGRESSED: the nested tenant is back to resolving through "+
			"/effective while missing from the collector config, so its alerts "+
			"cannot fire. Tenants: %v", keysOfTenants(m.GetConfig()))
	}
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
		t.Errorf("da_config_subtree_undeliverable_tenants = %v, want 0", got)
	}
	if logs := logBuf.String(); strings.Contains(logs, undeliverableAnchor) {
		t.Errorf("a fully deliverable tree must produce no ERROR, got:\n%s", logs)
	}
}

// TestUndeliverableAudit_FlatControl_IsSilent is the counterfactual: the very
// same tenant content at the conf.d root must produce no ERROR and a zero
// gauge.
func TestUndeliverableAudit_FlatControl_IsSilent(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)
	writeTestYAML(t, filepath.Join(dir, "hier-tenant.yaml"), nestedTenantYAML)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	if _, ok := m.GetConfig().Tenants["hier-tenant"]; !ok {
		t.Fatalf("control precondition: root-level tenant must reach the collector config")
	}
	if logs := logBuf.String(); strings.Contains(logs, undeliverableAnchor) {
		t.Errorf("flat layout must NOT trigger the ERROR, got:\n%s", logs)
	}
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
		t.Errorf("da_config_subtree_undeliverable_tenants = %v, want 0 for a flat layout", got)
	}
}

// TestUndeliverableAudit_GaugeRecoversToZero proves the gauge is state-coded
// (the gauge-over-counter argument): a refused set reads N, a clean one falls
// back to 0. Driven through the audit directly: the property is about the
// gauge being Set on every commit rather than incremented, not about a tree.
func TestUndeliverableAudit_GaugeRecoversToZero(t *testing.T) {
	t.Parallel()
	m, fresh, _ := newAuditedManager(t, t.TempDir())

	sources := map[string]string{"hier-tenant": "/conf.d/db/hier-tenant.yaml"}
	if n := m.auditSubtreeUndeliverable(sources, refusedFor(sources), "probe"); n != 1 {
		t.Fatalf("setup: audit reported %d tenant(s), want 1", n)
	}
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 1 {
		t.Fatalf("setup: gauge = %v, want 1", got)
	}

	// The key was declared at the root: nothing is refused any more.
	if n := m.auditSubtreeUndeliverable(sources, nil, "probe"); n != 0 {
		t.Errorf("audit still reports %d tenant(s) after the key became deliverable", n)
	}
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
		t.Errorf("gauge did not recover: %v, want 0", got)
	}
}

// TestUndeliverableAudit_HotReload_HierarchicalPath drives the real
// detect→debounce→diffAndReload chain via tickOnce (debounce window 0 = the
// synchronous seam config_debounce.go documents): a nested tenant added to a
// RUNNING exporter reaches m.config, not just Resolve(), and is not reported.
func TestUndeliverableAudit_HotReload_HierarchicalPath(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
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
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
		t.Errorf("gauge after hot reload = %v, want 0", got)
	}
	if logs := logBuf.String(); strings.Contains(logs, undeliverableAnchor) {
		t.Errorf("hot reload of a deliverable tree must be silent, got:\n%s", logs)
	}
}

// TestUndeliverableAudit_HotReload_FlatModeDetects: with no `_defaults.yaml`
// anywhere, hierarchical mode never activates, so `detectChange` uses the
// flat composite hash. Before #1521 a file added in a subdirectory was not in
// it and triggered no reload at all; the recursive walk puts it there.
func TestUndeliverableAudit_HotReload_FlatModeDetects(t *testing.T) {
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
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
		t.Errorf("gauge = %v, want 0", got)
	}
	if logs := logBuf.String(); strings.Contains(logs, undeliverableAnchor) {
		t.Errorf("no ERROR expected, got:\n%s", logs)
	}
}

// TestSubtreeUndeliverableTenants_OnlyServedTenantsWithARefusedKey pins the
// pure helper's selection.
//
// ⛔ The "absent from the merged config" arm is GONE (#1957), and this is
// where that is asserted: the helper no longer takes the merged config at
// all, so a tenant /effective serves is reported iff it has a refused key.
// A tenant with an empty refused list, or a refused list for a tenant the
// hierarchy does not serve, is not reported.
func TestSubtreeUndeliverableTenants_OnlyServedTenantsWithARefusedKey(t *testing.T) {
	t.Parallel()
	if got := subtreeUndeliverableTenants(nil, map[string][]string{"x": {"k"}}); got != nil {
		t.Errorf("empty tenantSources must yield nothing, got %v", got)
	}
	sources := map[string]string{
		"refused":  "/x/db/refused.yaml",
		"clean":    "/x/db/clean.yaml",
		"emptyset": "/x/db/emptyset.yaml",
	}
	if got := subtreeUndeliverableTenants(sources, nil); got != nil {
		t.Errorf("no refused keys must yield nothing, got %v", got)
	}
	unreachable := map[string][]string{
		"refused":  {"redis_evicted_keys"},
		"emptyset": {},
		"unserved": {"redis_evicted_keys"}, // not in tenantSources
	}
	got := subtreeUndeliverableTenants(sources, unreachable)
	if len(got) != 1 || got[0] != "refused" {
		t.Errorf("subtreeUndeliverableTenants = %v, want [refused] only", got)
	}
}

// TestSubtreeUndeliverableTenants_IsSorted pins the ORDER, which decides
// which ten of a large set the operator is shown. Without the sort the set
// comes out of Go's randomised map order, so `shown[:10]` is a DIFFERENT ten
// on every reload.
func TestSubtreeUndeliverableTenants_IsSorted(t *testing.T) {
	t.Parallel()
	sources := map[string]string{
		"zulu": "/x/db/zulu.yaml", "alpha": "/x/db/alpha.yaml",
		"mike": "/x/db/mike.yaml", "bravo": "/x/db/bravo.yaml",
	}
	unreachable := refusedFor(sources)
	// Map iteration order is randomised per run, so a single pass could
	// pass by luck; repeat until the odds of a false green are gone.
	want := []string{"alpha", "bravo", "mike", "zulu"}
	for i := 0; i < 20; i++ {
		got := subtreeUndeliverableTenants(sources, unreachable)
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

// TestFormatUndeliverableLog_CapsTheSample keeps a 100-tenant subtree from
// producing an unreadable log record.
func TestFormatUndeliverableLog_CapsTheSample(t *testing.T) {
	t.Parallel()
	sources := make(map[string]string)
	var affected []string
	for i := 0; i < undeliverableLogSampleLimit+5; i++ {
		id := "t" + string(rune('a'+i))
		sources[id] = "/conf.d/db/" + id + ".yaml"
		affected = append(affected, id)
	}
	sort.Strings(affected)
	line := formatUndeliverableLog(affected, sources, refusedFor(sources), "/conf.d", "Config loaded (directory)")
	if !strings.Contains(line, "and 5 more") {
		t.Errorf("expected truncation suffix, got:\n%s", line)
	}
	// ⛔ The literal 10, not `undeliverableLogSampleLimit`: both sides of an
	// assertion referencing the constant kept the test green when it shrank.
	if strings.Count(line, "/conf.d/db/") != 10 {
		t.Errorf("expected exactly 10 sampled paths, got:\n%s", line)
	}
	// The sample must be the FIRST ten of the slice it was handed. (Whether
	// that slice is sorted is subtreeUndeliverableTenants' job — see
	// TestSubtreeUndeliverableTenants_IsSorted.)
	if !strings.Contains(line, " ta (") || strings.Contains(line, " tk (") {
		t.Errorf("sample must be the first ten in sorted order (ta..tj), got:\n%s", line)
	}
	assertNamesTheRootBeforeTheList(t, line, "/conf.d")
	assertStatesTheCurrentCause(t, line)
	if !strings.Contains(line, "(Config loaded (directory))") {
		t.Errorf("log must name the commit context, got:\n%s", line)
	}
}

// TestUndeliverableAudit_PairsOneInstant pins that the audit judges the
// snapshot it was HANDED, never a hierarchy it re-reads for itself.
// Reloads are not serialised (see auditSubtreeUndeliverable), so a self-read
// could pair reload N's refused set with reload N+1's population.
//
// The test drives that apart deliberately: the live hierarchy is clean, the
// handed-in snapshot is not. If someone reinstates the internal read, the
// audit will consult the clean live maps and report 0 — and this fails.
func TestUndeliverableAudit_PairsOneInstant(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
		t.Fatalf("setup: tree should start clean, gauge = %v", got)
	}
	m.mu.RLock()
	_, leaked := m.hierarchy.tenantSources["ghost-tenant"]
	m.mu.RUnlock()
	if leaked {
		t.Fatalf("setup: live hierarchy must not already contain the probe tenant")
	}

	handed := map[string]string{"ghost-tenant": filepath.Join(dir, "db", "ghost-tenant.yaml")}
	logBuf.Reset()
	got := m.auditSubtreeUndeliverable(handed, refusedFor(handed), "pairing-probe")

	if got != 1 {
		t.Errorf("audit judged something other than the handed snapshot: got %d, want 1 "+
			"(a 0 here means it re-read m.hierarchy, which is the overlapping-reload bug)", got)
	}
	if gauge := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); gauge != 1 {
		t.Errorf("gauge = %v, want 1 — it must follow the handed snapshot too", gauge)
	}
	assertLogLineWith(t, logBuf.String(), undeliverableAnchor, "ghost-tenant")
}

// TestUndeliverableAudit_SnapshotIsTakenInsideTheLockWindow: the tree is
// clean and a hook DIRTIES the live hierarchy (both halves the audit reads)
// right after the commit releases m.mu. A gauge of 0 proves the audit used
// the snapshot taken inside the lock window; an implementation that re-read
// the live maps would report 1.
func TestUndeliverableAudit_SnapshotIsTakenInsideTheLockWindow(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)

	m, fresh, _ := newAuditedManager(t, dir)
	ghostPath := filepath.Join(dir, "db", "ghost.yaml")
	m.SetAfterCommitUnlockForTest(func() {
		// Stand in for a second, overlapping reload landing a refused key
		// between the swap and the audit.
		m.mu.Lock()
		m.hierarchy.tenantSources = map[string]string{"ghost-tenant": ghostPath}
		m.hierarchy.unreachableInherited = map[string][]string{"ghost-tenant": {"redis_evicted_keys"}}
		m.mu.Unlock()
	})

	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}

	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
		t.Fatalf("gauge = %v, want 0 — the audit judged the hierarchy as it stood "+
			"AFTER the commit, not the snapshot taken inside the lock window", got)
	}
	// Control: the live maps really were replaced.
	m.mu.RLock()
	_, ghost := m.hierarchy.tenantSources["ghost-tenant"]
	m.mu.RUnlock()
	if !ghost {
		t.Fatalf("setup: hook did not replace the live hierarchy")
	}
}

// TestUndeliverableAudit_GaugeIsRegisteredUnderItsPublishedName asserts the
// metric reaches /metrics at all, under the name #1957 gave it.
//
// ⛔ Every other test reads the Go field through `testutil.ToFloat64`, which
// bypasses the registry entirely: deleting `reg.MustRegister(...)` or
// renaming the metric left the package green before this test existed.
func TestUndeliverableAudit_GaugeIsRegisteredUnderItsPublishedName(t *testing.T) {
	t.Parallel()
	const name = "da_config_subtree_undeliverable_tenants"
	fresh, reg := freshMetrics(t)
	fresh.SetSubtreeUndeliverableTenants(3)

	if got := testutil.CollectAndCount(reg, name); got != 1 {
		t.Fatalf("%s: %d series in the registry, want 1 — the gauge is not "+
			"registered, so it never reaches /metrics", name, got)
	}
	// Value through the registry too, but WITHOUT `GatherAndCompare`, which
	// would couple this assertion to the exact HELP prose.
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather(): %v", err)
	}
	var seen bool
	for _, fam := range families {
		if strings.Contains(fam.GetName(), "divergent") {
			t.Errorf("a family named %q is still registered — #1957 renamed the gauge", fam.GetName())
		}
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

// TestUndeliverableAudit_GaugeCountsTenantsNotJustPresence pins that the
// gauge is a COUNT of tenants, not a 0/1 flag and not a count of keys.
func TestUndeliverableAudit_GaugeCountsTenantsNotJustPresence(t *testing.T) {
	t.Parallel()
	m, fresh, _ := newAuditedManager(t, t.TempDir())

	sources := map[string]string{}
	for _, id := range []string{"one", "two", "three"} {
		sources[id] = "/conf.d/db/" + id + ".yaml"
	}
	unreachable := refusedFor(sources)
	unreachable["one"] = []string{"kafka_lag_seconds", "redis_evicted_keys"} // two keys, one tenant

	if n := m.auditSubtreeUndeliverable(sources, unreachable, "probe"); n != 3 {
		t.Errorf("audit returned %d, want 3", n)
	}
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 3 {
		t.Errorf("gauge = %v, want 3 — the gauge counts TENANTS (not keys, not presence)", got)
	}
}

// TestUndeliverableAudit_RepeatsOnlyWhenTheSetChanges pins the
// de-duplication: the gauge is Set on EVERY commit, the log repeats only
// when the SET changes, and recovery to zero re-arms it so a relapse is loud.
func TestUndeliverableAudit_RepeatsOnlyWhenTheSetChanges(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)
	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}

	one := map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}
	two := map[string]string{
		"nested-a": filepath.Join(dir, "db", "a.yaml"),
		"nested-b": filepath.Join(dir, "db", "b.yaml"),
	}
	count := func() int { return strings.Count(logBuf.String(), undeliverableAnchor) }

	logBuf.Reset()
	m.auditSubtreeUndeliverable(one, refusedFor(one), "commit-1")
	if count() != 1 {
		t.Fatalf("first sighting must be logged, got %d lines", count())
	}
	m.auditSubtreeUndeliverable(one, refusedFor(one), "commit-2")
	m.auditSubtreeUndeliverable(one, refusedFor(one), "commit-3")
	if count() != 1 {
		t.Errorf("an unchanged set must not re-log: %d lines", count())
	}
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 1 {
		t.Errorf("gauge = %v, want 1 after the repeat commits", got)
	}

	m.auditSubtreeUndeliverable(two, refusedFor(two), "commit-4")
	if count() != 2 {
		t.Errorf("a changed set must log again: %d lines", count())
	}
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 2 {
		t.Errorf("gauge = %v, want 2", got)
	}

	m.auditSubtreeUndeliverable(nil, nil, "commit-5-healthy")
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
		t.Fatalf("gauge = %v, want 0 on recovery", got)
	}
	m.auditSubtreeUndeliverable(two, refusedFor(two), "commit-6-relapse")
	if count() != 3 {
		t.Errorf("a relapse after recovery must log again: %d lines", count())
	}
}

// TestUndeliverableAudit_GaugeAndMemoryCannotDisagree replays the
// interleaving that made the first version of the de-duplication lose a
// signal outright (see recordAndDecide), by hand rather than by racing
// goroutines, so it can never be flaky.
func TestUndeliverableAudit_GaugeAndMemoryCannotDisagree(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)
	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	one := map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}
	count := func() int { return strings.Count(logBuf.String(), undeliverableAnchor) }

	logBuf.Reset()
	m.auditSubtreeUndeliverable(one, refusedFor(one), "reload-N")
	m.auditSubtreeUndeliverable(nil, nil, "reload-N+1-healthy")
	if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
		t.Fatalf("gauge = %v after the healthy commit, want 0", got)
	}

	before := count()
	m.auditSubtreeUndeliverable(one, refusedFor(one), "genuine-recurrence")
	if testutil.ToFloat64(fresh.subtreeUndeliverableTenants) != 1 {
		t.Fatalf("gauge did not rise on recurrence")
	}
	if count() == before {
		t.Errorf("LOST SIGNAL: the gauge rose but no ERROR line was written — "+
			"the level and the memory disagreed (%d lines before and after)", before)
	}
}

// TestUndeliverableAudit_TheGaugeIsSetUnderTheSameLockAsTheMemory pins the
// atomicity itself, structurally: while `setGauge` runs, d.mu must be HELD.
// `TryLock` answers that with no scheduler involved. (A single-threaded test
// of whole audit calls cannot see a defect that lives between two statements
// inside one call, and racing goroutines needed 300,000 rounds under -race
// to hit it 9 times.)
func TestUndeliverableAudit_TheGaugeIsSetUnderTheSameLockAsTheMemory(t *testing.T) {
	t.Parallel()
	var d undeliverableLogState
	var lockHeldDuringSet, setCalled bool

	probe := func(int) {
		setCalled = true
		if d.mu.TryLock() {
			d.mu.Unlock() // it was free — the gauge is outside the lock
			return
		}
		lockHeldDuringSet = true
	}

	d.recordAndDecide([]string{"a"}, map[string]string{"a": "/x/a.yaml"}, nil, probe)
	if !setCalled {
		t.Fatal("setGauge was never called — the probe proves nothing")
	}
	if !lockHeldDuringSet {
		t.Error("the gauge is Set OUTSIDE d.mu: the level and the memory can then be " +
			"updated by different reloads in either order, after which a genuine " +
			"recurrence is silent")
	}

	lockHeldDuringSet, setCalled = false, false
	d.recordAndDecide(nil, nil, nil, probe)
	if !setCalled || !lockHeldDuringSet {
		t.Errorf("empty-set path: setCalled=%v lockHeld=%v — the gauge must be "+
			"Set under the lock on BOTH branches", setCalled, lockHeldDuringSet)
	}
}

// TestUndeliverableAudit_ReLogsWhenTheSourcePathMoves pins the dedup key's
// contents: the ERROR names each tenant AND its source file, so the same
// tenant at a NEW path is news.
func TestUndeliverableAudit_ReLogsWhenTheSourcePathMoves(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)
	m, _, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	count := func() int { return strings.Count(logBuf.String(), undeliverableAnchor) }

	logBuf.Reset()
	srcA := map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}
	m.auditSubtreeUndeliverable(srcA, refusedFor(srcA), "c1")
	if count() != 1 {
		t.Fatalf("first sighting must log, got %d", count())
	}
	srcDeep := map[string]string{"nested-a": filepath.Join(dir, "db", "deep", "a.yaml")}
	m.auditSubtreeUndeliverable(srcDeep, refusedFor(srcDeep), "c2")
	if count() != 2 {
		t.Errorf("a moved source path must re-log: %d lines", count())
	}
	assertLastLogLineWith(t, logBuf.String(), undeliverableAnchor, filepath.Join("db", "deep", "a.yaml"))
	m.auditSubtreeUndeliverable(srcDeep, refusedFor(srcDeep), "c3")
	if count() != 2 {
		t.Errorf("an unchanged repeat must stay quiet: %d lines", count())
	}
}

// TestUndeliverableAudit_ReLogsWhenTheKeySetChanges pins the dedup key's
// third field: the line names the undeliverable keys, so a tenant whose key
// set changes while its ID and path do not is news.
func TestUndeliverableAudit_ReLogsWhenTheKeySetChanges(t *testing.T) {
	t.Parallel()
	m, _, logBuf := newAuditedManager(t, t.TempDir())
	count := func() int { return strings.Count(logBuf.String(), undeliverableAnchor) }
	src := map[string]string{"t1": "/conf.d/finance/t1.yaml"}

	m.auditSubtreeUndeliverable(src, map[string][]string{"t1": {"redis_evicted_keys"}}, "c1")
	m.auditSubtreeUndeliverable(src, map[string][]string{"t1": {"kafka_lag_seconds"}}, "c2")
	if count() != 2 {
		t.Errorf("a changed key set must re-log: %d lines", count())
	}
	assertLastLogLineWith(t, logBuf.String(), undeliverableAnchor, "kafka_lag_seconds")
}

// TestUndeliverableAudit_LogNamesTheRootAndTheCommit pins the two pieces of
// context an operator needs to act on the line: WHICH tree, and WHICH commit
// produced it.
func TestUndeliverableAudit_LogNamesTheRootAndTheCommit(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	m, _, logBuf := newAuditedManager(t, dir)

	src := map[string]string{"hier-tenant": filepath.Join(dir, "db", "hier-tenant.yaml")}
	m.auditSubtreeUndeliverable(src, refusedFor(src), "Config loaded (directory)")

	line := logBuf.String()
	assertNamesTheRootBeforeTheList(t, line, dir)
	assertStatesTheCurrentCause(t, line)
	if !strings.Contains(line, "(Config loaded (directory))") {
		t.Errorf("log must name the commit context, got:\n%s", line)
	}
}

// TestUndeliverableAudit_TheMemoryTracksTheLatestSetNotTheFirst pins that
// `d.last` is REPLACED on every logged set: A→B→A with no healthy commit in
// between must log three times. A memory frozen on the first set logs twice
// and then goes quiet on a live condition.
func TestUndeliverableAudit_TheMemoryTracksTheLatestSetNotTheFirst(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)
	m, _, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}

	setA := map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}
	setB := map[string]string{"nested-b": filepath.Join(dir, "db", "b.yaml")}
	count := func() int { return strings.Count(logBuf.String(), undeliverableAnchor) }

	logBuf.Reset()
	m.auditSubtreeUndeliverable(setA, refusedFor(setA), "commit-A1")
	if count() != 1 {
		t.Fatalf("first sighting of A must log, got %d", count())
	}
	m.auditSubtreeUndeliverable(setB, refusedFor(setB), "commit-B")
	if count() != 2 {
		t.Fatalf("a different set must log, got %d", count())
	}
	m.auditSubtreeUndeliverable(setA, refusedFor(setA), "commit-A2")
	if got := count(); got != 3 {
		t.Errorf("going back to a PREVIOUS set must log again: %d line(s), want 3\nlog:\n%s",
			got, logBuf.String())
	}
	assertLastLogLineWith(t, logBuf.String(), undeliverableAnchor, "commit-A2")
}

// TestUndeliverableAudit_TheGaugeIsRepublishedEvenWhenTheLogIsSuppressed:
// the gauge is Set on EVERY commit, including the ones the de-duplication
// silences. Moved the way this package's own seam does — SetMetrics with a
// fresh registry — the new gauge must carry the count while the log (still
// correctly) stays silent.
func TestUndeliverableAudit_TheGaugeIsRepublishedEvenWhenTheLogIsSuppressed(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)
	m, first, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	sources := map[string]string{"nested-a": filepath.Join(dir, "db", "a.yaml")}
	count := func() int { return strings.Count(logBuf.String(), undeliverableAnchor) }

	logBuf.Reset()
	m.auditSubtreeUndeliverable(sources, refusedFor(sources), "commit-1")
	if count() != 1 {
		t.Fatalf("first sighting must log, got %d", count())
	}
	if got := testutil.ToFloat64(first.subtreeUndeliverableTenants); got != 1 {
		t.Fatalf("first gauge = %v, want 1", got)
	}

	second, _ := freshMetrics(t)
	m.SetMetrics(second)
	if got := testutil.ToFloat64(second.subtreeUndeliverableTenants); got != 0 {
		t.Fatalf("a fresh metrics instance must start at 0, got %v", got)
	}

	m.auditSubtreeUndeliverable(sources, refusedFor(sources), "commit-2")
	if got := count(); got != 1 {
		t.Errorf("the unchanged set must still not re-log: %d line(s)", got)
	}
	if got := testutil.ToFloat64(second.subtreeUndeliverableTenants); got != 1 {
		t.Errorf("gauge on the CURRENT metrics = %v, want 1 — the level must be "+
			"re-published on every commit, not only on the ones that log", got)
	}
}

// TestUndeliverableAudit_TheReturnValueSurvivesSuppression pins that the
// count handed back is the size of the set, not "how much did I print".
func TestUndeliverableAudit_TheReturnValueSurvivesSuppression(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeAuditRoot(t, dir)
	m, _, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load(): %v", err)
	}
	sources := map[string]string{
		"nested-a": filepath.Join(dir, "db", "a.yaml"),
		"nested-b": filepath.Join(dir, "db", "b.yaml"),
	}

	logBuf.Reset()
	if got := m.auditSubtreeUndeliverable(sources, refusedFor(sources), "commit-1"); got != 2 {
		t.Fatalf("first sighting returned %d, want 2", got)
	}
	got := m.auditSubtreeUndeliverable(sources, refusedFor(sources), "commit-2")
	if lines := strings.Count(logBuf.String(), undeliverableAnchor); lines != 1 {
		t.Fatalf("precondition: the second call must be suppressed, %d line(s)", lines)
	}
	if got != 2 {
		t.Errorf("suppressed call returned %d, want 2", got)
	}
}

// TestUndeliverableLogState_TheKeyCannotBeAmbiguousAcrossTenantAndSource
// pins the separator BETWEEN a tenant ID and its source path: without it
// `{a → bc}` and `{ab → c}` both render as "abc".
func TestUndeliverableLogState_TheKeyCannotBeAmbiguousAcrossTenantAndSource(t *testing.T) {
	t.Parallel()
	var d undeliverableLogState
	noop := func(int) {}

	if !d.recordAndDecide([]string{"a"}, map[string]string{"a": "bc"}, nil, noop) {
		t.Fatalf("first set must be reported as new")
	}
	if !d.recordAndDecide([]string{"ab"}, map[string]string{"ab": "c"}, nil, noop) {
		t.Errorf("{ab → c} is a DIFFERENT set from {a → bc} and must be reported as new")
	}
}

// TestUndeliverableLogState_TheKeyCannotBeAmbiguousAcrossAdjacentPairs pins
// the separator AFTER the source path — the boundary between one pair and the
// next. Measured with the byte removed: two sets that share no tenant ID and
// no path collapse onto the same key:
//
//	{acme-corp → /etc/conf.d/xy, z-tenant  → /etc/other.yaml}
//	{acme-corp → /etc/conf.d/x,  yz-tenant → /etc/other.yaml}
func TestUndeliverableLogState_TheKeyCannotBeAmbiguousAcrossAdjacentPairs(t *testing.T) {
	t.Parallel()
	var d undeliverableLogState
	noop := func(int) {}

	first := d.recordAndDecide(
		[]string{"acme-corp", "z-tenant"},
		map[string]string{
			"acme-corp": "/etc/conf.d/xy",
			"z-tenant":  "/etc/other.yaml",
		}, nil, noop)
	if !first {
		t.Fatalf("first set must be reported as new")
	}

	second := d.recordAndDecide(
		[]string{"acme-corp", "yz-tenant"},
		map[string]string{
			"acme-corp": "/etc/conf.d/x",
			"yz-tenant": "/etc/other.yaml",
		}, nil, noop)
	if !second {
		t.Errorf("a set that shares no tenant ID and no path with the previous one " +
			"must be reported as new; without the separator after the source path " +
			"the two render identically and the second is silently swallowed")
	}
}

// assertStatesTheCurrentCause fails if the operator-facing message carries a
// diagnosis that is no longer true, or stops stating the one that is.
//
// ⛔ AN INVARIANT, NOT A SENTENCE. Two retired diagnoses are forbidden: the
// pre-#1521 one (directory depth — "move the tenant file to the conf.d
// root") and the pre-#1957 cause (a) ("ABSENT from the merged config … fails
// to parse"), which one decode made impossible; an operator following either
// would chase a file that was never the problem. A blacklist alone is not an
// invariant — the same claim reworded passes it — so the positive half pins
// what the CURRENT cause and remedy must mention.
func assertStatesTheCurrentCause(t *testing.T, line string) {
	t.Helper()
	for _, stale := range []string{
		"top level of",
		"move the tenant file",
		"sub-directories never reach",
		"ABSENT from the merged config",
		"fails to parse",
		"dropped while building",
	} {
		if strings.Contains(line, stale) {
			t.Errorf("message still carries a retired diagnosis %q; got:\n%s", stale, line)
		}
	}
	for _, required := range []string{"subtree `_defaults.yaml`", "root `_defaults.yaml`", "optional_overrides"} {
		if !strings.Contains(strings.ToLower(line), strings.ToLower(required)) {
			t.Errorf("operator text no longer states the current cause and remedy — %q is missing; got:\n%s",
				required, line)
		}
	}
}

// assertNamesTheRootBeforeTheList checks the root is named in the message
// HEADER rather than merely appearing somewhere in the line: the affected
// list ends every message with source PATHS, all of which contain the root,
// so `Contains(line, root)` is satisfied even when the header never says
// which conf.d produced it.
func assertNamesTheRootBeforeTheList(t *testing.T, line, root string) {
	t.Helper()
	head, _, found := strings.Cut(line, "Affected:")
	if !found {
		t.Fatalf("message has no %q section, so the header cannot be isolated:\n%s", "Affected:", line)
	}
	if !strings.Contains(head, root) {
		t.Errorf("the header never names the conf.d root %q — an operator reading a "+
			"multi-tree log stream cannot tell which tree it is; header:\n%s", root, head)
	}
}
