package config

// hierarchy_null_test.go — pins ADR-017's explicit-null rule, which is
// per-field rather than blanket (#1339 P0).
//
// The bug this guards against: deepMerge used to delete ANY key whose
// override was nil. On a threshold key that contradicts the emitting path —
// collector.go → ThresholdConfig.ResolveAtWithStats decodes the null into
// ScheduledValue.Default == "", warns, and falls back to the platform
// default — so GET /tenants/{id}/effective told operators a threshold was
// gone while /metrics was still exporting it. Diagnostics that disagree with
// production are worse than missing diagnostics: the operator is debugging
// "why is this alert still firing?" precisely when they call this endpoint.
//
// The golden parity fixtures did not catch it because every scenario there
// used a synthetic `threshold: {cpu, memory}` / `alert_group` shape that no
// shipped _defaults.yaml has. tests/golden/fixtures/opt-out-null-threshold
// now covers the real flat-metric-key shape; these tests cover the rule
// directly.

import (
	"testing"
)

func TestDeepMerge_NullOnThresholdKey_KeepsInheritedValue(t *testing.T) {
	t.Parallel()

	base := map[string]any{
		"mysql_connections":       80,
		"container_memory":        85,
		"redis_connected_clients": 5000,
	}
	override := map[string]any{
		"mysql_connections": nil,       // null is NOT an opt-out here
		"container_memory":  "disable", // the sanctioned way to stop alerting
	}

	got := deepMerge(base, override)

	if v, ok := got["mysql_connections"]; !ok || v != 80 {
		t.Errorf("mysql_connections: want the inherited 80 to survive an explicit null, got %v (present=%v)", v, ok)
	}
	if got["container_memory"] != "disable" {
		t.Errorf(`container_memory: want "disable" to pass through, got %v`, got["container_memory"])
	}
	if got["redis_connected_clients"] != 5000 {
		t.Errorf("redis_connected_clients: omitted keys must still inherit, got %v", got["redis_connected_clients"])
	}
}

func TestDeepMerge_NullOnReservedKey_StillDeletes(t *testing.T) {
	t.Parallel()

	// ADR-017's opt-out is kept for reserved keys.
	//
	// ⛔ Do NOT delete this branch as dead code. TWO SEPARATE propositions,
	// kept separate on purpose — an earlier comment collapsed them and got the
	// reachability claim wrong in BOTH directions:
	//
	//   (a) `_`-prefixed sibling keys DO reach deepMerge's override map from a
	//       shipped _defaults.yaml. extractDefaultsBlock falls through and
	//       returns the WHOLE document whenever the assertion on m["defaults"]
	//       fails — no `defaults:` key at all, or one that is explicitly null.
	//       rule-packs/recipes/examples/conf.d/finance/_defaults.yaml does this
	//       today via `_custom_alerts` — whose value is a LIST, so it merges
	//       normally and never reaches the delete below.
	//
	//   (b) This delete branch additionally requires that key's value to be an
	//       explicit null. Measured: 0 of the 17 shipped `_defaults*.yaml` files
	//       currently carry a `_`-prefixed key with a null value.
	//
	// So: the path into this branch exists in shipped data (a); the triggering
	// value does not occur in the repo today (b). No config file exercises it,
	// which means THIS hand-constructed test is the only thing holding the
	// branch in place — remove the branch and this test, and nothing else in
	// the repo goes red. That is why it is pinned here.
	// See ADR-017 §Merge 語意, the reserved-key null rule.
	base := map[string]any{
		"_silent_mode":      "warning",
		"mysql_connections": 80,
	}
	override := map[string]any{"_silent_mode": nil}

	got := deepMerge(base, override)

	if _, ok := got["_silent_mode"]; ok {
		t.Errorf("_silent_mode: reserved keys keep the null opt-out, got %v", got["_silent_mode"])
	}
	if got["mysql_connections"] != 80 {
		t.Errorf("unrelated threshold key must be untouched, got %v", got["mysql_connections"])
	}
}

func TestDeepMerge_NullNestedUnderThresholdKey_KeepsInheritedValue(t *testing.T) {
	t.Parallel()

	// A scheduledValue is a map under a threshold key. The rule applies at
	// every depth: `default` is not `_`-prefixed, so a null there is not an
	// opt-out either.
	base := map[string]any{
		"mysql_connections": map[string]any{"default": "80", "overrides": []any{"x"}},
	}
	override := map[string]any{
		"mysql_connections": map[string]any{"default": nil},
	}

	got := deepMerge(base, override)

	inner, ok := got["mysql_connections"].(map[string]any)
	if !ok {
		t.Fatalf("mysql_connections: want a map, got %T", got["mysql_connections"])
	}
	if inner["default"] != "80" {
		t.Errorf(`nested default: want the inherited "80" to survive, got %v`, inner["default"])
	}
}

func TestComputeEffectiveConfig_NullThreshold_MatchesEmittingPath(t *testing.T) {
	t.Parallel()

	// End-to-end over the bytes the /effective handler actually feeds in,
	// using the real shipped shape: `defaults:` holds unquoted numbers,
	// tenant values are quoted strings.
	defaults := []byte("defaults:\n  mysql_connections: 80\n  container_memory: 85\n")
	tenant := []byte("tenants:\n  tenant-a:\n    mysql_connections: ~\n    container_memory: \"disable\"\n")

	merged, err := computeEffectiveConfigBytes(tenant, "tenant-a", [][]byte{defaults})
	if err != nil {
		t.Fatalf("computeEffectiveConfigBytes: %v", err)
	}

	// 80 is what collector.go emits for this tenant, so 80 is what the
	// diagnostic must report.
	if got, ok := merged["mysql_connections"]; !ok || got != 80 {
		t.Errorf("mysql_connections: /effective must agree with the emitted series (80), got %v (present=%v)", got, ok)
	}
	if merged["container_memory"] != "disable" {
		t.Errorf(`container_memory: want "disable", got %v`, merged["container_memory"])
	}
}
