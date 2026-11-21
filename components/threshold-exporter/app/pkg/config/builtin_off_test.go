package config

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

// builtin_off_test.go — the built-in layer's off table (#4163): every key the
// platform ships no value for is valued off, so a tree that neither values
// nor lists it lets a tenant set it, and every rule of a root-valued key
// holds — except that off serves no row and switches no family off, and
// `expires:` does not lapse to it.

// offRows is tenant's rows of component at at, as "<component>_<metric>
// <severity> <value>", sorted.
func offRows(cfg *ThresholdConfig, tenant, component string, at time.Time) []string {
	var out []string
	for _, r := range cfg.ResolveAt(at) {
		if r.Tenant == tenant && r.Component == component {
			out = append(out, r.Component+"_"+r.Metric+" "+r.Severity+" "+strconv.FormatFloat(r.Value, 'g', -1, 64))
		}
	}
	sort.Strings(out)
	return out
}

// A root that writes no redis key: each tenant shape against what /metrics
// serves. The keys are of the off table (the generated unsetThresholdKeys).
func TestBuiltinOff_ATenantSetsAKeyTheRootNeitherValuesNorLists(t *testing.T) {
	t.Parallel()
	for _, k := range []string{"redis_connected_clients", "redis_replication_lag"} {
		if !BuiltinOff(k) {
			t.Fatalf("%s is not in the off table: the fixture needs another key", k)
		}
	}
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  container_cpu: 80\n",
		"_profiles.yaml": "profiles:\n  std:\n    redis_connected_clients: \"300\"\n",
		"t.yaml": "tenants:\n" +
			"  both:\n    redis_connected_clients: \"200\"\n    redis_connected_clients_critical: \"400\"\n" +
			"  crit:\n    redis_replication_lag_critical: \"60\"\n" +
			"  prof:\n    _profile: std\n" +
			"  none: {}\n",
	})
	for tenant, want := range map[string][]string{
		"both": {"redis_connected_clients critical 400", "redis_connected_clients warning 200"},
		"crit": {"redis_replication_lag critical 60"}, // no base value: the critical row alone
		"prof": {"redis_connected_clients warning 300"},
		"none": nil, // off serves no row of its own
	} {
		if got := offRows(cfg, tenant, "redis", fixedNow); strings.Join(got, "|") != strings.Join(want, "|") {
			t.Errorf("%s: rows %v, want %v", tenant, got, want)
		}
	}
}

// A root `disable` of an off key is the tree's opt-out, as for a key with a
// platform value: the family is off for a tenant that gives the key nothing,
// and back for one that gives it a value.
func TestBuiltinOff_ARootDisableSwitchesTheFamilyOff(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  redis_connected_clients: disable\n",
		"t.yaml": "tenants:\n" +
			"  crit:\n    redis_connected_clients_critical: \"400\"\n" +
			"  back:\n    redis_connected_clients: \"200\"\n    redis_connected_clients_critical: \"400\"\n",
	})
	if !RootDisabledDefault(cfg.Defaults["redis_connected_clients"]) {
		t.Fatalf("root disable decoded as %v, want the root opt-out", cfg.Defaults["redis_connected_clients"])
	}
	if got := offRows(cfg, "crit", "redis", fixedNow); len(got) != 0 {
		t.Errorf("crit: rows %v, want none (the base's family is off at the root)", got)
	}
	want := "redis_connected_clients critical 400|redis_connected_clients warning 200"
	if got := offRows(cfg, "back", "redis", fixedNow); strings.Join(got, "|") != want {
		t.Errorf("back: rows %v, want %s", got, want)
	}
}

// `expires:` does not lapse to off: the value is served past it, the write
// check refuses it naming the key, and no expiry event is raised for it. A
// key the root gives a number still lapses (the control).
func TestBuiltinOff_ExpiresDoesNotLapseToOff(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  redis_replication_lag: 30\n",
		"t.yaml": "tenants:\n  tx:\n" +
			"    redis_connected_clients:\n      default: \"200\"\n      expires: \"2020-01-01T00:00:00Z\"\n" +
			"    redis_replication_lag:\n      default: \"90\"\n      expires: \"2020-01-01T00:00:00Z\"\n",
	})
	want := "redis_connected_clients warning 200|redis_replication_lag warning 30"
	if got := offRows(cfg, "tx", "redis", fixedNow); strings.Join(got, "|") != want {
		t.Errorf("rows %v, want %s (the off key keeps its value; the valued key lapses to the root's)", got, want)
	}
	errs := strings.Join(cfg.ValidateTenantKeys().Errors, "\n")
	if !strings.Contains(errs, "`expires:` on \"redis_connected_clients\" is refused") {
		t.Errorf("write check: %q, want the refusal for the off key", errs)
	}
	if strings.Contains(errs, "\"redis_replication_lag\" is refused") {
		t.Errorf("write check refuses the valued key's expires: %q", errs)
	}
	events := map[string]bool{}
	for _, e := range cfg.ResolveThresholdExpiriesAt(fixedNow) {
		events[e.MetricKey] = true
	}
	if events["redis_connected_clients"] || !events["redis_replication_lag"] {
		t.Errorf("expiry events for %v, want the valued key's only", events)
	}
}

// Off is no refusal of its own: a tree whose root writes nothing has every
// off key valued off, and RefusedRootDefaults names none of them.
func TestBuiltinOff_IsNoRefusal(t *testing.T) {
	t.Parallel()
	if _, refused := RefusedDefault(builtinOffDefault()); refused {
		t.Error("RefusedDefault reports the off value refused")
	}
	cfg := loadTree(t, map[string]string{"t.yaml": "tenants:\n  tx: {}\n"})
	if got := cfg.RefusedRootDefaults(); len(got) != 0 {
		t.Errorf("a tree with no root file: refused root defaults %v, want none", got)
	}
	for _, k := range BuiltinOffKeys() {
		if !BuiltinOffDefault(cfg.Defaults[k]) {
			t.Errorf("%s: Defaults %v, want the off value", k, cfg.Defaults[k])
		}
	}
}

// A tree generated before the layer still lists its off keys under
// optional_overrides: — redundant, and blocking nothing: a profile fills the
// key as it fills any root-valued key, listed or not.
func TestBuiltinOff_AListingUnderOptionalOverridesDoesNotBlockAProfile(t *testing.T) {
	t.Parallel()
	for name, root := range map[string]string{
		"listed":     "optional_overrides: [redis_evicted_keys_rate]\n",
		"not listed": "defaults: {}\n",
	} {
		cfg := loadTree(t, map[string]string{
			"_defaults.yaml": root,
			"_profiles.yaml": "profiles:\n  gold:\n    redis_evicted_keys_rate: \"5\"\n",
			"t.yaml":         "tenants:\n  tx:\n    _profile: gold\n",
		})
		if got := offRows(cfg, "tx", "redis", fixedNow); strings.Join(got, "|") != "redis_evicted_keys_rate warning 5" {
			t.Errorf("%s: rows %v, want the profile's 5", name, got)
		}
	}
}

// A tenant value the resolver cannot read, for a key the layer values off,
// is dropped with a WARN that says the platform ships no value — not that
// the root default is no finite number.
func TestBuiltinOff_AnUnreadableTenantValueSaysThePlatformShipsNoValue(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{"t.yaml": "tenants:\n  tx:\n    kafka_consumer_lag: \"7O\"\n"})
	var logs []string
	cfg.resolveAtWithStats(fixedNow, nil, func(format string, args ...any) {
		logs = append(logs, fmt.Sprintf(format, args...))
	}, nil)
	joined := strings.Join(logs, "\n")
	if !strings.Contains(joined, `unknown value "7O" for tenant=tx metric=kafka_consumer_lag, and the platform ships no value for it, skipping`) {
		t.Errorf("logs %q, want the off-key WARN", joined)
	}
	if strings.Contains(joined, "root default is not a finite number") {
		t.Errorf("logs %q name a root default the tree never wrote", joined)
	}
}

// A root value the decode refuses, for a key of the off table, is kept in
// off's place — reading as off (a tenant value is served, `expires:` does
// not lapse) — and stays a refusal, so the mistake is still named.
func TestBuiltinOff_ARefusedRootValueStaysARefusal(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  redis_connected_clients: abc\n",
		"t.yaml": "tenants:\n  tx:\n    redis_connected_clients:\n      default: \"200\"\n      expires: \"2020-01-01T00:00:00Z\"\n" +
			"  ty: {}\n",
	})
	if got := cfg.RefusedRootDefaults(); got["redis_connected_clients"] != RefusedUnparsed {
		t.Errorf("refused root defaults %v, want redis_connected_clients unparsed", got)
	}
	if got := offRows(cfg, "tx", "redis", fixedNow); strings.Join(got, "|") != "redis_connected_clients warning 200" {
		t.Errorf("tx: rows %v, want its own 200 past expires", got)
	}
	if got := offRows(cfg, "ty", "redis", fixedNow); len(got) != 0 {
		t.Errorf("ty: rows %v, want none", got)
	}
	if errs := strings.Join(cfg.ValidateTenantKeys().Errors, "\n"); !strings.Contains(errs, "`expires:` on \"redis_connected_clients\" is refused — the platform ships no value") {
		t.Errorf("write check: %q, want the off-key refusal", errs)
	}
}
