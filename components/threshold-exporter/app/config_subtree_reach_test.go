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
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"gopkg.in/yaml.v3"
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

// TestASubtreeOnlyKeyIsRefusedLoudly pins the half of #1521 this PR does NOT
// deliver, and pins that it is not silent about it.
//
// ⛔ WHY IT IS NOT DELIVERED. Nothing iterates a tenant's override map to
// decide what to emit: `resolveBaseRows` walks `cfg.Defaults` and
// `resolveDeclaredRows` walks `cfg.OptionalOverrides`. A key that appears only
// in a subtree `_defaults.yaml` is in neither, because nested `_` files are
// deliberately excluded from the merged config so they cannot re-price the
// whole tree.
//
// ⛔ AND THE OBVIOUS FIX WAS MEASURED WORSE THAN THE GAP. Declaring such keys
// in `cfg.OptionalOverrides` made them emit and produced four defects, each
// reproduced: a `default_foo`/`foo` pair collided into one label set and
// failed the WHOLE Prometheus `Gather`; an unrelated tenant's edit deleted
// another tenant's live series; the subtree's only tenant setting the key
// itself meant it was never declared, so its OWN value stopped emitting; and
// an `expires:` in a subtree defaults file was charged to every tenant under
// it, blocking their writes. All four share one root cause — that field is a
// flat global list with no subtree scope. Fixing it properly needs per-subtree
// scope in `ThresholdConfig`, i.e. #1568.
//
// So the contract asserted here is: the value does NOT reach the output plane,
// nothing is silently written anywhere, and the divergence audit NAMES the
// tenant and the key. Silence is the one outcome this ticket forbids.
func TestASubtreeOnlyKeyIsRefusedLoudly(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 80\n")
	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  mysql_connections: 60\n  redis_evicted_keys: 100\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	// The half that IS delivered: a key the ROOT declares carries the
	// subtree's value. Without this control the test would pass with the
	// overlay removed entirely.
	if got, ok := seriesFor(t, m, "t1", "connections"); !ok || got != 60 {
		t.Fatalf("control: the root-declared key should carry the subtree's 60, got %v (present=%v)", got, ok)
	}

	// The half that is NOT delivered — and must not pretend to be.
	if _, present := seriesFor(t, m, "t1", "evicted_keys"); present {
		t.Errorf("a subtree-only key emitted a series; either #1568 landed or the " +
			"global declared surface was widened again")
	}
	if _, leaked := m.GetConfig().Defaults["redis_evicted_keys"]; leaked {
		t.Errorf("the subtree's key leaked into the global Defaults map; every tenant " +
			"in the tree now carries a value it never inherited")
	}
	for _, declared := range m.GetConfig().OptionalOverrides {
		if declared == "redis_evicted_keys" {
			t.Errorf("the subtree's key was added to the global declared surface — the " +
				"design that produced the /metrics-wide Gather failure")
		}
	}
	if _, written := m.GetConfig().Tenants["t1"]["redis_evicted_keys"]; written {
		t.Errorf("the value was written into the tenant's map where no emitter reads " +
			"it, which also makes ValidateTenantKeys log `unknown key` every commit")
	}
	// ⛔ Not writing it is only acceptable BECAUSE of the next two assertions.
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Errorf("da_config_hierarchy_divergent_tenants = %v, want 1 — a tenant whose "+
			"/effective value can never become a series is a divergence, and the gauge "+
			"reporting 0 here is the silence this ticket exists to remove", got)
	}
	line := logBuf.String()
	if !strings.Contains(line, "conf.d scanner divergence") {
		t.Fatalf("no divergence ERROR at all; log:\n%s", line)
	}
	for _, want := range []string{"t1", "redis_evicted_keys"} {
		if !strings.Contains(line, want) {
			t.Errorf("the ERROR does not name %q, so an operator cannot act on it; log:\n%s", want, line)
		}
	}
	if strings.Contains(line, `unknown key "redis_evicted_keys"`) {
		t.Errorf("the key was still handed to ValidateTenantKeys; log:\n%s", line)
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

// TestTheShallowestDefaultsFileIsNotMistakenForTheRoot keeps the `chain[0]`
// fix pinned now that a subtree-only key is refused rather than delivered.
//
// ⛔ `collectDefaultsChain` appends only levels that actually HAVE a defaults
// file, so with no `_defaults.yaml` at the conf.d root, `chain[0]` is the
// shallowest SUBTREE file. Skipping it by index treated it as the root's —
// already-merged — when its keys are in fact in no merged surface at all.
//
// With no root defaults file `cfg.Defaults` is empty, so BOTH levels' keys are
// unreachable; the discriminating assertion is that both are NAMED. Under the
// index-based skip the shallowest file is never examined, so only the deeper
// key appears.
func TestTheShallowestDefaultsFileIsNotMistakenForTheRoot(t *testing.T) {
	dir := t.TempDir()
	mkSub(t, dir, "finance")
	mkSub(t, dir, filepath.Join("finance", "us"))
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  mysql_connections: 60\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "us", "_defaults.yaml"),
		"defaults:\n  redis_evicted_keys: 70\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "us", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Errorf("da_config_hierarchy_divergent_tenants = %v, want 1", got)
	}
	line := logBuf.String()
	if !strings.Contains(line, "mysql_connections") {
		t.Errorf("the SHALLOWEST defaults file was skipped as if it were the root's, so "+
			"its key is neither delivered nor reported — the worst of both; log:\n%s", line)
	}
	if !strings.Contains(line, "redis_evicted_keys") {
		t.Errorf("the deeper subtree key is not reported either; log:\n%s", line)
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
// ⛔ MEASURED: reverting the two LIVE `scanKeyBase(name)` call sites to the whole key
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

// TestARelativeConfigDirStillTellsTheRootApart guards the comparison that
// decides which chain entry IS the root.
//
// ⛔ The chain stores absolute paths (`scanDirHierarchical` runs
// `filepath.Abs`+`Clean`) while `-config-dir` is frequently relative. Comparing
// the two directly never matches, so EVERY level looks like a subtree and the
// ROOT defaults get copied into each tenant's own map — measured on the repo's
// own golden fixtures, where three trees with no subdirectory at all picked up
// spurious entries.
func TestARelativeConfigDirStillTellsTheRootApart(t *testing.T) {
	abs := t.TempDir()
	writeTestYAML(t, filepath.Join(abs, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n")
	mkSub(t, abs, "finance")
	writeTestYAML(t, filepath.Join(abs, "finance", "_defaults.yaml"),
		"defaults:\n  redis_evicted_keys: 44\n")
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

	m, _, logBuf := newAuditedManager(t, rel)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	// ⛔ THE DISCRIMINATOR IS THE ROOT KEY THE SUBTREE NEVER MENTIONS. If the
	// root defaults file is mistaken for a subtree one it gets walked as
	// inheritable, and `mysql_connections` — which IS in cfg.Defaults, so it
	// passes the reachability check — is copied into the tenant's own map.
	// Nothing else discriminates: a key the subtree also sets lands in that
	// map either way, and a reachable key is never reported, so an assertion
	// on the log alone cannot see this. (Measured: an earlier version of this
	// test asserted only on the log and the mutation stayed green.)
	overrides := m.GetConfig().Tenants["t1"]
	if _, copied := overrides["mysql_connections"]; copied {
		t.Errorf("the ROOT defaults key was copied into the tenant's own map through a "+
			"relative -config-dir; the root file was mistaken for a subtree one. "+
			"tenant map = %v", overrides)
	}
	if line := logBuf.String(); !strings.Contains(line, "redis_evicted_keys") {
		t.Errorf("control: the genuine subtree-only key must still be reported, or this "+
			"test would pass with the overlay doing nothing at all; log:\n%s", line)
	}
}

// TestNonThresholdSubtreeKeysStayOutOfTheCollectorPlane pins that the shape
// filter runs BEFORE the reachability decision.
//
// ⛔ A `region: us-east` in a subtree `_defaults.yaml` is not a threshold and
// must not appear anywhere on the collector plane — not in the tenant's map,
// not on the declared surface, and NOT in the divergence report either.
// Reporting it would train operators to ignore the line: the audit's whole
// value is that everything it names is genuinely a threshold that cannot fire.
func TestNonThresholdSubtreeKeysStayOutOfTheCollectorPlane(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n")
	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  region: us-east\n  pages:\n    - b\n  mysql_connections: 44\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	cfg := m.GetConfig()

	for _, key := range []string{"region", "pages"} {
		if _, carried := cfg.Tenants["t1"][key]; carried {
			t.Errorf("non-threshold key %q entered the tenant's threshold map", key)
		}
		for _, declared := range cfg.OptionalOverrides {
			if declared == key {
				t.Errorf("non-threshold key %q was added to the declared surface", key)
			}
		}
		if strings.Contains(logBuf.String(), key) {
			t.Errorf("non-threshold key %q was reported as an unreachable threshold; the "+
				"audit must only name things that genuinely cannot fire", key)
		}
	}
	// Control: a threshold-shaped key the ROOT declares still arrives.
	if got, ok := seriesFor(t, m, "t1", "connections"); !ok || got != 44 {
		t.Fatalf("control: the threshold-shaped subtree value must still arrive, got %v (present=%v)", got, ok)
	}
	// A tree whose only oddity is non-threshold metadata is HEALTHY.
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("da_config_hierarchy_divergent_tenants = %v, want 0 — non-threshold "+
			"metadata in a subtree defaults file is not a divergence", got)
	}
	if logs := logBuf.String(); strings.Contains(logs, "invalid declared threshold") ||
		strings.Contains(logs, "unknown key") {
		t.Errorf("a valid tree logs threshold-validation noise:\n%s", logs)
	}
}

// TestTheScalarFastPathMatchesTheRoundTrip pins the one property that makes
// the fast path in `scheduledValueFromRaw` safe.
//
// ⛔ WHY THIS EXISTS. The function's contract is "render the value exactly as
// a tenant file's own value would have been parsed", which it gets by handing
// the value to the same `ScheduledValue.UnmarshalYAML` the tenant path uses.
// A fast path is a SECOND implementation of that contract, and a second
// implementation that silently disagrees is how the overlay's first version
// went wrong. So the reference is computed here, in the test, and compared.
//
// ⛔ float64 IS ASSERTED TO STAY ON THE SLOW PATH. Measured: YAML renders 1e6
// as "1e+06" while `strconv.FormatFloat(…, 'f', -1, 64)` renders "1000000",
// and at MaxFloat64 the latter is a 300-digit string. Adding `case float64`
// to the fast path is exactly the mistake this half of the test catches.
func TestTheScalarFastPathMatchesTheRoundTrip(t *testing.T) {
	t.Parallel()

	// The reference: what the function did before the fast path existed.
	roundTrip := func(raw any) (string, bool) {
		encoded, err := yaml.Marshal(raw)
		if err != nil {
			return "", false
		}
		var sv ScheduledValue
		if err := yaml.Unmarshal(encoded, &sv); err != nil {
			return "", false
		}
		return sv.Default, true
	}

	// Every shape a defaults file can hold that the fast path claims.
	// The odd-looking strings are the ones YAML would otherwise reinterpret.
	for _, raw := range []any{
		"80", "80:critical", "disable", "yes", "no", "on", "off", "null", "~",
		"", " 80 ", "1e6", "0x10", "007", "true",
		int(60), int(0), int(-5), int(1000000),
		int64(60), int64(math.MaxInt64), int64(math.MinInt64),
	} {
		want, wantOK := roundTrip(raw)
		got, gotOK := scheduledValueFromRaw(raw)
		if !wantOK || !gotOK {
			t.Errorf("%#v: ok mismatch (round-trip=%v fast=%v)", raw, wantOK, gotOK)
			continue
		}
		if got.Default != want {
			t.Errorf("%#v: fast path gives %q, the round-trip gives %q — the two "+
				"implementations of one contract disagree", raw, got.Default, want)
		}
	}

	// And the type the fast path must NOT claim.
	for raw, want := range map[float64]string{
		1e6:  "1e+06",
		1e21: "1e+21",
		60.5: "60.5",
	} {
		got, ok := scheduledValueFromRaw(raw)
		if !ok {
			t.Fatalf("%v: rejected outright", raw)
		}
		if got.Default != want {
			t.Errorf("%v rendered as %q, want %q — float64 was added to the scalar "+
				"fast path, where strconv and YAML disagree", raw, got.Default, want)
		}
	}
}

// TestTheGaugeHelpStatesTheCurrentCause applies to the OTHER operator-facing
// surface this change set rewrote.
//
// ⛔ MEASURED: the round that rewrote both the ERROR log and this HELP text
// asserted only the log. Reinstating the pre-#1521 diagnosis in the HELP —
// "the known cause is a tenant file in a conf.d sub-directory … move the
// tenant file to the conf.d root" — left the whole package green. The HELP is
// what an operator actually reads in Prometheus or Grafana when the gauge
// fires, so it is the surface most likely to be believed. (#1569 blind
// review.)
func TestTheGaugeHelpStatesTheCurrentCause(t *testing.T) {
	t.Parallel()
	_, reg := freshMetrics(t)
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	var help string
	for _, fam := range families {
		if fam.GetName() == "da_config_hierarchy_divergent_tenants" {
			help = fam.GetHelp()
		}
	}
	if help == "" {
		t.Fatalf("gauge is not registered, so its HELP reaches no operator")
	}
	for _, stale := range []string{
		"the known cause is a tenant file in a conf.d sub-directory",
		"move the tenant file to the conf.d root",
		"is an OPEN defect",
	} {
		if strings.Contains(help, stale) {
			t.Errorf("HELP carries the pre-#1521 diagnosis %q", stale)
		}
	}
	for _, required := range []string{"recursively", "parse"} {
		if !strings.Contains(strings.ToLower(help), required) {
			t.Errorf("HELP no longer states the current cause — %q missing; got:\n%s", required, help)
		}
	}
}

// TestANestedTenantKeepsItsSubtreeDefaultAcrossAnIncrementalReload covers the
// `anyNestedKey` redirect, which had no test at all.
//
// ⛔ MEASURED: making `anyNestedKey` return false — the obvious future
// optimisation, since the redirect is what costs nested trees their
// tenant-patch fast path — left the package green while a nested tenant
// silently reverted to the ROOT threshold after an ordinary edit: 60 became
// 80 while `/effective` still said 60. Presence with the wrong number, this
// ticket's exact residual, reachable through the reload path rather than the
// cold one. (#1569 blind review.)
func TestANestedTenantKeepsItsSubtreeDefaultAcrossAnIncrementalReload(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 80\n")
	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  mysql_connections: 60\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	m := NewConfigManager(dir)
	defer m.Close()
	captureLoad(t, m)
	if got, ok := seriesFor(t, m, "t1", "connections"); !ok || got != 60 {
		t.Fatalf("cold load: got %v (present=%v), want the subtree's 60", got, ok)
	}

	// An ordinary edit to the nested tenant's own file.
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"),
		"tenants:\n  t1: {}\n# touched\n")
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad: %v", err)
	}
	got, ok := seriesFor(t, m, "t1", "connections")
	if !ok {
		t.Fatalf("the nested tenant vanished from the output plane after a reload")
	}
	if got != 60 {
		t.Fatalf("after an incremental reload the nested tenant emits %v — the ROOT "+
			"default — while /effective still resolves the subtree's 60; the reload "+
			"path did not refresh the inheritance state", got)
	}
}

// TestWhatASubtreeMayAndMayNotHandDown pins the threshold-shape filter across
// the forms it has to judge, each of which was measured wrong at some point.
func TestWhatASubtreeMayAndMayNotHandDown(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n  redis_evicted_keys: 10\n  mongo_replication_lag: 5\n")
	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n"+
			// ⛔ `disable` must be inheritable: neutralising the IsDisabled branch
			// left the package green while the tenant emitted the root's number
			// and /effective said disabled.
			"  mysql_connections: \"disable\"\n"+
			// ⛔ the inline-severity form resolveBaseRows accepts from tenant files;
			// no fixture used it, so dropping the `:` split went unnoticed.
			"  redis_evicted_keys: \"77:critical\"\n"+
			// ⛔ a YAML bool is a flag, not a threshold — but the round-trip renders
			// it "false" and IsDisabled counts "false" as a disable synonym, so it
			// entered the THRESHOLD map and the declared surface.
			"  paging_enabled: false\n"+
			// ⛔ a schedule whose value lives ENTIRELY in the windows. Judging
			// `Default` alone dropped it, leaving /effective rendering the
			// schedule while the series carried the root value. A
			// tenant-authored copy of the same block resolves to 88, measured.
			"  mongo_replication_lag:\n"+
			"    default: \"\"\n"+
			"    overrides:\n"+
			"      - window: \"00:00-23:59\"\n"+
			"        value: \"88\"\n")
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"),
		"tenants:\n  t1: {}\n")

	m := NewConfigManager(dir)
	defer m.Close()
	captureLoad(t, m)
	cfg := m.GetConfig()

	if _, present := seriesFor(t, m, "t1", "connections"); present {
		t.Errorf("a subtree `disable` was not inherited: the tenant still emits a " +
			"threshold while /effective resolves it to disabled")
	}
	if got, ok := seriesFor(t, m, "t1", "evicted_keys"); !ok || got != 77 {
		t.Errorf("inline `value:severity` from a subtree: got %v (present=%v), want 77", got, ok)
	}
	if got, ok := seriesFor(t, m, "t1", "replication_lag"); !ok || got != 88 {
		t.Errorf("a schedule whose value lives only in `overrides:` was dropped: "+
			"got %v (present=%v), want the window's 88", got, ok)
	}
	if _, carried := cfg.Tenants["t1"]["paging_enabled"]; carried {
		t.Errorf("a YAML bool entered the tenant's THRESHOLD map; tenant map = %v", cfg.Tenants["t1"])
	}
	for _, declared := range cfg.OptionalOverrides {
		if declared == "paging_enabled" {
			t.Errorf("a YAML bool widened the platform's declared write surface")
		}
	}
}
