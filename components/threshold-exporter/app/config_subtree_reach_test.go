package main

// Round 2 of #1521 (PR #1569): the defects an adversarial blind review found
// in round 1's own fix, and the mutations round 1 shipped no test for.
//
// ⛔ WHY A SECOND FILE. `config_nested_tenant_test.go` asserts the ACCEPTANCE
// criterion — "a nested tenant reaches the output plane" — and every fixture
// in it writes a root `_defaults.yaml` with a single subtree level. That
// shape is precisely what made it blind to everything below: a key the root
// never declares, a tree with no root defaults file at all, a defaults value
// that is not a bare number, and a config root that is a symlink. Keeping the
// criterion and its counter-examples apart makes it obvious which is which.
//
// ⛔ EVERY TEST HERE WAS RUN AGAINST THE BROKEN CODE FIRST. The comment on
// each one names the measurement it produced then, not what it "should"
// catch. A test whose red was never observed is a claim, not a guard.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

// captureLoad runs a real Load with a captured logger and returns the log.
func captureLoad(t *testing.T, m *ConfigManager) string {
	t.Helper()
	var buf bytes.Buffer
	m.SetLogger(log.New(&buf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	return buf.String()
}

// TestASubtreeOnlyKeyReachesTheOutputPlane covers the half of the overlay that
// silently did nothing.
//
// ⛔ MEASURED BEFORE THE FIX: the tenant's own map held
// `redis_evicted_keys:100` and `/effective` reported 100, while
// `user_threshold` had NO such series — because nothing iterates a tenant's
// map to decide what to emit (`resolveBaseRows` walks `cfg.Defaults`,
// `resolveDeclaredRows` walks `cfg.OptionalOverrides`) and a nested `_` file
// feeds neither. Every load also logged
// `WARN: tenant=t1: unknown key "redis_evicted_keys" not in defaults`.
//
// ⛔ WORSE THAN THE DEFECT IT REPLACED, which is why this is a test and not a
// ticket: the tenant IS in `GetConfig()` now, so the #1526 divergence audit
// sees a reconciled tree and stays silent. Before the overlay the tenant was
// absent and the gauge read 1.
func TestASubtreeOnlyKeyReachesTheOutputPlane(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 80\n")
	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  mysql_connections: 60\n  redis_evicted_keys: 100\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	m := NewConfigManager(dir)
	defer m.Close()
	logged := captureLoad(t, m)

	// Control: the key the ROOT declares was already working before this fix.
	if got, ok := seriesFor(t, m, "t1", "connections"); !ok || got != 60 {
		t.Fatalf("control: the root-declared key should carry the subtree's 60, got %v (present=%v)", got, ok)
	}
	// The key only `finance/_defaults.yaml` introduces.
	got, ok := seriesFor(t, m, "t1", "evicted_keys")
	if !ok {
		t.Fatalf("a key introduced by a subtree _defaults.yaml emits no user_threshold " +
			"series; the inherited value landed in a map no emitter reads")
	}
	if got != 100 {
		t.Fatalf("subtree-only key emitted %v, want the inherited 100", got)
	}
	// And it must be emitted as a DECLARED key, not by widening the global
	// defaults — that would re-price every tenant in the tree.
	if _, leaked := m.GetConfig().Defaults["redis_evicted_keys"]; leaked {
		t.Errorf("the subtree's key leaked into the global Defaults map; " +
			"every tenant in the tree now carries a value it never inherited")
	}
	if strings.Contains(logged, `unknown key "redis_evicted_keys"`) {
		t.Errorf("the key is still not on the platform's declared surface — "+
			"ValidateTenantKeys rejects it on every commit; log:\n%s", logged)
	}
}

// TestAScheduledSubtreeDefaultAgreesOnBothPlanes covers the value shapes the
// old hand-written type switch could not represent.
//
// ⛔ MEASURED BEFORE THE FIX: `/effective` rendered
// `map[mysql_connections:map[default:90]]` while the series carried the ROOT's
// 50 — the two-planes-disagree shape this whole ticket exists to remove,
// surviving in the one case where the subtree speaks the schedule form that
// `ScheduledValue` was built for. The same load also logged
// `ERROR: skip unparseable defaults/profiles file …`, which the recursion had
// introduced on a tree that is entirely valid: `Defaults` is
// `map[string]float64`, so the flat parse of a nested defaults file using the
// schedule form fails, and pre-#1521 the flat scanner never read that file.
func TestAScheduledSubtreeDefaultAgreesOnBothPlanes(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n")
	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  mysql_connections:\n    default: \"90\"\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	m := NewConfigManager(dir)
	defer m.Close()
	logged := captureLoad(t, m)

	got, ok := seriesFor(t, m, "t1", "connections")
	if !ok {
		t.Fatalf("no series for the inheriting tenant at all")
	}
	if got != 90 {
		t.Fatalf("series carries %v; /effective resolves the subtree's schedule to 90, "+
			"so the two planes disagree again", got)
	}
	if strings.Contains(logged, "skip unparseable defaults/profiles file") {
		t.Errorf("a valid tree logs a parse ERROR: the flat loader is still trying to "+
			"parse a nested _defaults.yaml whose content it then discards; log:\n%s", logged)
	}
}

// TestASubtreeIsInheritedWhenTheRootDeclaresNoDefaults covers the assumption
// that `chain[0]` is the root's defaults file.
//
// ⛔ MEASURED BEFORE THE FIX: `/effective` reported both keys and the series
// reported NEITHER. `collectDefaultsChain` appends only the levels that
// actually hold a defaults file, so with no file at the root `chain[0]` is
// `finance/_defaults.yaml` — and `chain[1:]` dropped it as if it were global.
//
// ⚠️ ADR-016 documents `domain/region/env`, so a tree whose top level is
// nothing but directories is a shape real deployments take.
func TestASubtreeIsInheritedWhenTheRootDeclaresNoDefaults(t *testing.T) {
	dir := t.TempDir()
	mkSub(t, dir, "finance")
	mkSub(t, dir, filepath.Join("finance", "us"))
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  mysql_connections: 60\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "us", "_defaults.yaml"),
		"defaults:\n  redis_evicted_keys: 70\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "us", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	m := NewConfigManager(dir)
	defer m.Close()
	captureLoad(t, m)

	// The shallowest level that HAS a defaults file is not the root, and its
	// keys must still be inherited.
	if got, ok := seriesFor(t, m, "t1", "connections"); !ok || got != 60 {
		t.Errorf("the shallowest subtree defaults file was skipped as if it were the "+
			"root's: got %v (present=%v), want 60", got, ok)
	}
	if got, ok := seriesFor(t, m, "t1", "evicted_keys"); !ok || got != 70 {
		t.Errorf("deeper subtree key: got %v (present=%v), want 70", got, ok)
	}
}

// TestASymlinkedConfigRootStillLoads covers a hard regression the recursion
// introduced.
//
// ⛔ MEASURED BEFORE THE FIX: `Load(real dir)` returned nil and
// `Load(symlink)` returned "no .yaml files found". `filepath.WalkDir` LSTATS
// its root and does not follow a symlink, so the walk visited the link once
// as a non-directory and ended with zero files and a NIL error — the empty
// guard in `fullDirLoad` then failed the load. The pre-#1521 `os.ReadDir`
// followed the link.
func TestASymlinkedConfigRootStillLoads(t *testing.T) {
	base := t.TempDir()
	real := filepath.Join(base, "real")
	mkSub(t, base, "real")
	writeTestYAML(t, filepath.Join(real, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n")
	writeTestYAML(t, filepath.Join(real, "t1.yaml"),
		"tenants:\n  t1:\n    mysql_connections: \"70\"\n")

	link := filepath.Join(base, "link")
	if err := os.Symlink(real, link); err != nil {
		t.Skipf("symlinks unavailable on this filesystem: %v", err)
	}

	m := NewConfigManager(link)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load through a symlinked config root: %v", err)
	}
	if got, ok := seriesFor(t, m, "t1", "connections"); !ok || got != 70 {
		t.Fatalf("tenant behind a symlinked root: got %v (present=%v), want 70", got, ok)
	}
}

// TestTheDivergenceAuditRunsOnEveryCommit pins the CALL SITE, which nothing
// did after round 1 inverted the polarity of the integration tests.
//
// ⛔ MEASURED: deleting `m.auditHierarchyDivergence(cfg, hierTenantSources,
// logHeader)` from `installConfig`'s caller — the sole `m.config` assignment
// point, and therefore the only thing that makes the audit universal — left
// the whole package GREEN. Every surviving Load-driven assertion expects
// "gauge 0 / no ERROR", which is exactly the state of an audit that never
// runs.
//
// The fixture is a divergence that #1521's recursion does NOT close, so this
// stays meaningful now that depth is no longer a cause: `Defaults` is
// `map[string]float64`, so a `defaults:` block of the wrong shape makes
// `parsePartialConfig` discard the WHOLE file including its `tenants:`, while
// the hierarchical walker reads the same file for its `tenants:` declarations
// and registers them. Measured on this fixture: `cfg.Tenants` empty,
// `hierarchy.tenantSources` = [t-bad].
func TestTheDivergenceAuditRunsOnEveryCommit(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n")
	writeTestYAML(t, filepath.Join(dir, "bad.yaml"),
		"defaults:\n  mysql_connections:\n    nested: map\ntenants:\n  t-bad:\n    mysql_connections: \"66\"\n")

	m := NewConfigManager(dir)
	defer m.Close()
	fresh, _ := freshMetrics(t)
	m.SetMetrics(fresh)
	logged := captureLoad(t, m)

	// Precondition, asserted rather than assumed: the fixture really does
	// produce the split. Without this a broken fixture reads as a pass.
	if _, visible := m.GetConfig().Tenants["t-bad"]; visible {
		t.Fatalf("fixture no longer diverges — the flat loader kept t-bad, so this " +
			"test cannot say anything about the audit")
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Fatalf("da_config_hierarchy_divergent_tenants = %v, want 1 — the audit did "+
			"not run on this commit", got)
	}
	if !strings.Contains(logged, "conf.d scanner divergence") {
		t.Errorf("no divergence ERROR logged; the gauge is the machine-readable half "+
			"but the log is the half an operator reads:\n%s", logged)
	}
	assertNoStaleRemediation(t, logged)
}

// TestAnUppercaseExtensionAtTheRootStillEmits covers the case-insensitivity
// half of the scanner change, which round 1 measured as a defect and then
// shipped with no guard.
//
// ⛔ MEASURED: reverting `lower := strings.ToLower(name)` to `lower := name`
// left the package GREEN — no fixture anywhere used `.YAML` or `.YML`. The
// defect it silently restores needs no nesting at all: a `UPPER.YAML` at the
// conf.d ROOT resolves through `Resolve()` and emits nothing, because the two
// enumerators over one tree disagreed on which extensions count.
func TestAnUppercaseExtensionAtTheRootStillEmits(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n")
	writeTestYAML(t, filepath.Join(dir, "UPPER.YAML"),
		"tenants:\n  t-upper:\n    mysql_connections: \"81\"\n")
	writeTestYAML(t, filepath.Join(dir, "mixed.YmL"),
		"tenants:\n  t-mixed:\n    mysql_connections: \"82\"\n")

	m := NewConfigManager(dir)
	defer m.Close()
	captureLoad(t, m)

	for tenant, want := range map[string]float64{"t-upper": 81, "t-mixed": 82} {
		if got, ok := seriesFor(t, m, tenant, "connections"); !ok || got != want {
			t.Errorf("%s: got %v (present=%v), want %v — the flat scanner's extension "+
				"match is case-sensitive again", tenant, got, ok, want)
		}
	}
}

// TestTheUnderscoreConventionJudgesTheFileName pins `scanKeyBase`'s contract
// DIRECTLY, and says plainly why it cannot be pinned through a Load.
//
// ⛔ MEASURED: reverting both `scanKeyBase(name)` call sites to the whole key
// left the package GREEN. That is not because the helper is pointless — it is
// because both flat-mode loops now drop nested `_` files BEFORE reaching
// these functions, and `anyNestedKey` sends any nested change to
// `fullDirLoad` before `isTenantOnlyChange` is consulted. So no integration
// path can deliver a key like `nested/_defaults.yaml` here today.
//
// ⚠️ That makes this a CONTRACT test, not a regression test for a live path,
// and it is written as one on purpose: the functions are called with a
// root-relative key and must judge the file name, so the guarantee is worth
// holding even while the callers pre-filter. Claiming it guards a live
// scenario would be the same overstatement this round is cleaning up.
func TestTheUnderscoreConventionJudgesTheFileName(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	partial := ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 60},
	}
	applyBoundaryRules("nested/_defaults.yaml", &partial, log.New(&buf, "", 0))
	if partial.Defaults == nil {
		t.Errorf("a nested _defaults.yaml was treated as a TENANT file and had its " +
			"platform sections stripped; the underscore convention judged the whole " +
			"key instead of the file name")
	}
	if strings.Contains(buf.String(), "should only be in _defaults.yaml") {
		t.Errorf("nested platform file warned about as if it were a tenant file: %s", buf.String())
	}

	// The same question on the incremental fast path: a defaults edit must
	// never be routed through the tenant-only patch.
	if isTenantOnlyChange([]string{"nested/_defaults.yaml"}, nil, nil) {
		t.Errorf("a nested _defaults.yaml edit was classified as a tenant-only change; " +
			"patchTenants would apply a platform edit as if it were a tenant's")
	}
}

func mkSub(t *testing.T, dir, sub string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, sub), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", sub, err)
	}
}

// TestARelativeConfigDirStillTellsTheRootApart covers a defect this round's
// own fix introduced, found by measuring the repo's real trees rather than by
// reading the diff.
//
// ⛔ MEASURED: `-config-dir` is frequently RELATIVE, while the defaults chain
// is stored absolute (`scanDirHierarchical` does `filepath.Abs` + `Clean`).
// Comparing the two directly never matched, so the ROOT `_defaults.yaml`
// looked like a subtree file to the overlay. On three of the repo's own
// golden fixtures — trees with no subdirectory at all — every root defaults
// key was copied into the tenant's map and declared: `_metadata`,
// `alert_group`, `threshold`.
//
// The assertion is that a root-declared key stays OUT of the tenant's own
// map: it is already in `cfg.Defaults` and applies to every tenant from
// there, so a copy per tenant is pure duplication of the platform surface.
func TestARelativeConfigDirStillTellsTheRootApart(t *testing.T) {
	abs := t.TempDir()
	writeTestYAML(t, filepath.Join(abs, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n")
	mkSub(t, abs, "finance")
	writeTestYAML(t, filepath.Join(abs, "finance", "_defaults.yaml"),
		"defaults:\n  redis_evicted_keys: 33\n")
	writeTestYAML(t, filepath.Join(abs, "finance", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	cwd, err := os.Getwd()
	if err != nil {
		t.Fatalf("Getwd: %v", err)
	}
	rel, err := filepath.Rel(cwd, abs)
	if err != nil {
		t.Skipf("no relative path from %s to %s: %v", cwd, abs, err)
	}
	if filepath.IsAbs(rel) {
		t.Skipf("relative form is still absolute (%s); nothing to test here", rel)
	}

	m := NewConfigManager(rel)
	defer m.Close()
	captureLoad(t, m)

	overrides := m.GetConfig().Tenants["t1"]
	if _, copied := overrides["mysql_connections"]; copied {
		t.Errorf("the ROOT defaults key was copied into the tenant's own map through a "+
			"relative -config-dir; the root file was mistaken for a subtree one. "+
			"tenant map = %v", overrides)
	}
	// The genuine subtree key must still come through, or the test would pass
	// simply by the overlay doing nothing at all.
	if got, ok := seriesFor(t, m, "t1", "evicted_keys"); !ok || got != 33 {
		t.Errorf("control: the real subtree key should still be inherited, got %v (present=%v)", got, ok)
	}
}

// TestNonThresholdSubtreeKeysStayOutOfTheCollectorPlane pins the overlay's
// scope.
//
// ⛔ MEASURED on the repo's `full-l0-l3` golden fixture: copying a subtree's
// non-threshold keys (`region: us-east`, `pages: [b]`, `level: L3`) into the
// tenant's threshold map produced no series either way, but it produced
// `WARN: invalid declared threshold "us-east" …` from inside
// `ResolveAtWithStats` — which runs ON EVERY SCRAPE. Three junk keys, three
// WARN lines per scrape. Leaving them undeclared just moves the noise to
// `WARN: unknown key … not in defaults` once per commit instead.
func TestNonThresholdSubtreeKeysStayOutOfTheCollectorPlane(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n")
	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  region: us-east\n  redis_evicted_keys: 44\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	m := NewConfigManager(dir)
	defer m.Close()
	logged := captureLoad(t, m)

	if _, carried := m.GetConfig().Tenants["t1"]["region"]; carried {
		t.Errorf("a non-threshold subtree key entered the tenant's threshold map")
	}
	for _, declared := range m.GetConfig().OptionalOverrides {
		if declared == "region" {
			t.Errorf("a non-threshold subtree key was added to the platform's declared surface")
		}
	}
	// Reading the collector is what surfaces the per-scrape WARN.
	if got, ok := seriesFor(t, m, "t1", "evicted_keys"); !ok || got != 44 {
		t.Fatalf("control: the threshold-shaped subtree key must still arrive, got %v (present=%v)", got, ok)
	}
	if strings.Contains(logged, "invalid declared threshold") ||
		strings.Contains(logged, "unknown key") {
		t.Errorf("a valid tree logs threshold-validation noise:\n%s", logged)
	}
}
