package main

// served_disable_test.go — served-values --disable-effects (ADR-038
// decision 8): what each switched-off key's `disable` does to the
// user_threshold rows /metrics serves.

import (
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func disableOf(t *testing.T, doc servedOut, tenant, key string) string {
	t.Helper()
	e, ok := doc.Tenants[tenant].Keys[key]
	if !ok {
		t.Fatalf("%s: keys[%q] missing; keys=%v", tenant, key, keysOf(doc.Tenants[tenant].Keys))
	}
	return e.Disable
}

func TestServedValues_DisableEffects(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedWith(t, map[string]string{
		"_defaults.yaml": "dimensions:\n  redis_hits: [db]\n  redis_queue_length: [queue]\ndefaults:\n  mysql_connections: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n" +
			"  redis_queue_length: 5\n  container_cpu: 75\n  container_memory: 60\n" +
			"state_filters:\n  maint: {reasons: [X], severity: critical}\n",
		"a.yaml": "tenants:\n  a:\n" +
			"    mysql_connections: disable\n" + // switches off a row
			"    mysql_slave_lagg: disable\n" + // misspelt: nothing to switch off
			"    redis_memory: disable\n" +
			"    redis_memory_critical: \"90\"\n" + // switched off with its base (#3046)
			"    redis_queue_length: disable\n" +
			"    redis_queue_length{ queue = \"tasks\" }: 7\n" + // switched off with its base; named as written
			"    container_cpu:\n      default: \"70\"\n      overrides:\n" +
			"        - {window: \"01:00-02:00\", value: disable}\n" + // off one hour, outside --at
			"    nothing_here:\n      default: \"5\"\n      overrides:\n" +
			"        - {window: \"01:00-02:00\", value: disable}\n" + // undeclared: never a row
			"    container_memory: {default: disable, expires: \"2020-01-01T00:00:00Z\"}\n" + // expired: ignored
			// Served all day but one hour; in that hour nothing would be served
			// without the `disable` either (no default): the key stays, the
			// `disable` switches nothing off.
			"    redis_hits{db=\"x\"}:\n      default: \"5\"\n      overrides:\n" +
			"        - {window: \"01:00-02:00\", value: disable}\n" +
			"    _state_maint: disable\n" + // a registered `_` key: judged by its reader
			"    _silent_mode: disable\n",
		"b.yaml": "tenants:\n  b:\n    mysql_connections: disable\n",
	}, "2026-07-01T03:00:00Z", "--disable-effects")
	mustOK(t, code, stderr)
	want := map[string]string{
		"mysql_connections":  disableEffective,
		"mysql_slave_lagg":   disableNoop,
		"redis_memory":       disableEffective,
		"redis_queue_length": disableEffective,
		"container_cpu":      disableEffective,
		"nothing_here":       disableNoop,
		"container_memory":   disableNoop,
		`redis_hits{db="x"}`: disableNoop,
		"_state_maint":       "",
		"_silent_mode":       "",
	}
	for k, w := range want {
		if got := disableOf(t, doc, "a", k); got != w {
			t.Errorf("a: keys[%q] disable %q, want %q", k, got, w)
		}
	}
	if got := disableOf(t, doc, "b", "mysql_connections"); got != disableEffective {
		t.Errorf("b: keys[mysql_connections].disable = %q, want effective", got)
	}
	// #3046: a key switched off with its base has no disable of its own; it
	// is served no row, reason base_disabled.
	for _, k := range []string{"redis_memory_critical", `redis_queue_length{ queue = "tasks" }`} {
		e := doc.Tenants["a"].Keys[k]
		if e.Disable != "" || e.Exporter || e.Reason != keyReasonBaseDisabled {
			t.Errorf("a: keys[%q] = %+v, want reason base_disabled, no disable", k, e)
		}
		if _, served := doc.Tenants["a"].Values[k]; served {
			t.Errorf("a: %q still served", k)
		}
	}
}

// Without the flag nothing is resolved again, and no key carries disable.
func TestServedValues_DisableEffectsOnlyWhenAsked(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"a.yaml":         "tenants:\n  a:\n    mysql_connections: disable\n",
	}, "")
	mustOK(t, code, stderr)
	if got := disableOf(t, doc, "a", "mysql_connections"); got != "" {
		t.Errorf("disable = %q without --disable-effects, want none", got)
	}
}

// Rounds take one key per tenant: a tenant with more switched-off keys than
// another is judged key by key, each against its own baseline.
func TestServedValues_DisableEffectsAcrossRounds(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedWith(t, map[string]string{
		"_defaults.yaml": fixtureDims + "defaults:\n  k1: 1\n  k3: 3\n  k5: 5\n",
		"a.yaml":         "tenants:\n  a:\n    k1: disable\n    k2: disable\n    k3: disable\n    k4: disable\n    k5: disable\n",
		"b.yaml":         "tenants:\n  b:\n    k2: disable\n    k3: disable\n",
	}, "", "--disable-effects")
	mustOK(t, code, stderr)
	for tenant, byKey := range map[string]map[string]string{
		"a": {"k1": disableEffective, "k2": disableNoop, "k3": disableEffective, "k4": disableNoop, "k5": disableEffective},
		"b": {"k2": disableNoop, "k3": disableEffective},
	} {
		for k, w := range byKey {
			if got := disableOf(t, doc, tenant, k); got != w {
				t.Errorf("%s: keys[%q].disable = %q, want %q", tenant, k, got, w)
			}
		}
	}
}

// Taking a's `disable` away makes /metrics fail as a whole (the root's
// dimensional default and a's `q_re` key give one series). That is a's change, not c's: the round is split, and c's
// `disable`, which switches nothing off, stays noop.
func TestServedValues_DisableEffectsSplitsARoundThatBreaksTheGather(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedWith(t, map[string]string{
		// The root's dimensional default is a's series once a's `disable` is gone.
		"_defaults.yaml": defaultsOnly + "  redis_queue_length{q=~\"a\"}: 2\n",
		"a.yaml": "tenants:\n  a:\n    redis_queue_length{q=~\"a\"}: disable\n" +
			"    redis_queue_length{q_re=\"a\"}: 3\n",
		"c.yaml": "tenants:\n  c:\n    no_such_key: disable\n",
	}, "", "--disable-effects")
	mustOK(t, code, stderr)
	if got := disableOf(t, doc, "a", `redis_queue_length{q=~"a"}`); got != disableEffective {
		t.Errorf("a: disable = %q, want effective", got)
	}
	if got := disableOf(t, doc, "c", "no_such_key"); got != disableNoop {
		t.Errorf("c: disable = %q, want noop", got)
	}
}

// a's own rows cannot be gathered in 01:00–02:00 (two keys, one series), the
// hour b switches a key off. a's failure is not b's evidence: b is judged
// apart in that hour, and its `disable` is effective.
func TestServedValues_DisableEffectsSplitsAPartOfTheDayTheBaselineCannotGather(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedWith(t, map[string]string{
		"_defaults.yaml": fixtureDims + "defaults:\n  mysql_connections: 80\n  container_cpu: 75\n",
		"a.yaml": "tenants:\n  a:\n    mysql_connections:\n      default: \"70\"\n      overrides:\n" +
			"        - {window: \"01:00-02:00\", value: \"70:critical\"}\n" +
			"    mysql_connections_critical: 95\n" +
			"    container_cpu: disable\n", // judged in the same round as b's
		"b.yaml": "tenants:\n  b:\n    container_cpu:\n      default: \"70\"\n      overrides:\n" +
			"        - {window: \"01:00-02:00\", value: disable}\n",
	}, "2026-07-01T03:00:00Z", "--disable-effects")
	mustOK(t, code, stderr)
	if got := disableOf(t, doc, "b", "container_cpu"); got != disableEffective {
		t.Errorf("b: disable = %q, want effective", got)
	}
}

// What is compared is what /metrics serves: without the `disable` the row
// would be built and dropped (a label named `tenant`), so nothing served
// changes.
func TestServedValues_DisableOfARowTheCollectorWouldDropIsNoop(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedWith(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "  mysql_connections{tenant=\"x\"}: 5\n",
		"a.yaml":         "tenants:\n  a:\n    mysql_connections{tenant=\"x\"}: disable\n",
		"b.yaml":         "tenants:\n  b:\n    mysql_connections: 70\n",
	}, "", "--disable-effects")
	mustOK(t, code, stderr)
	if got := disableOf(t, doc, "a", `mysql_connections{tenant="x"}`); got != disableNoop {
		t.Errorf("a: disable = %q, want noop", got)
	}
	if _, ok := doc.Tenants["b"].Dropped[`mysql_connections{tenant="x"}`]; !ok {
		t.Errorf("control: b's row of that default is dropped: %v", doc.Tenants["b"].Dropped)
	}
}

// #3046 invariant (familyLeak): a part of the day in which the tenant
// switches a base off and a row of its family is still served is an
// internal error; a part in which the base is not off — a lapsed
// `disable`, a value — or that cannot be gathered is no evidence.
func TestFamilyLeak(t *testing.T) {
	t.Parallel()
	part := func(v config.ScheduledValue) *config.ThresholdConfig {
		return &config.ThresholdConfig{
			Defaults: map[string]float64{"redis_memory": 70},
			Tenants:  map[string]map[string]config.ScheduledValue{"a": {"redis_memory": v}},
		}
	}
	leaked := &cutReading{series: map[string]map[string][]metricsSeries{"a": {
		"redis_memory_critical": {{MetricKey: "redis_memory"}}}}}
	tk := tenantKey{"a", "redis_memory"}
	at := time.Date(2026, 7, 1, 3, 0, 0, 0, time.UTC)
	if err := familyLeak(part(config.ScheduledValue{Default: "disable"}), leaked, tk, at); err == nil {
		t.Error("a family row served under an off base: no error")
	}
	lapsed := config.ScheduledValue{Default: "disable", Expiry: &config.ExpiryMeta{Expires: "2020-01-01T00:00:00Z"}}
	for name, c := range map[string]struct {
		v    config.ScheduledValue
		read *cutReading
	}{
		"lapsed":     {lapsed, leaked},
		"value":      {config.ScheduledValue{Default: "60"}, leaked},
		"not gather": {config.ScheduledValue{Default: "disable"}, &cutReading{failed: true}},
		"no family": {config.ScheduledValue{Default: "disable"}, &cutReading{series: map[string]map[string][]metricsSeries{
			"a": {"redis_hits": {{MetricKey: "redis_hits"}}}}}},
	} {
		if err := familyLeak(part(c.v), c.read, tk, at); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// A `_critical` key no phase reads (its base has no root value) is no_row,
// not base_disabled, though its base reads `disable`: base_disabled is a key
// the phase read and switched off (#3046).
func TestServedValues_BaseDisabledOnlyForAKeyAPhaseReads(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedWith(t, map[string]string{
		"_defaults.yaml": "dimensions:\n  bar_depth: [db]\ndefaults:\n  mysql_connections: 80\n",
		"a.yaml": "tenants:\n  a:\n    bar_depth: disable\n    bar_depth_critical: \"9\"\n" +
			"    bar_depth{db=\"a\"}: \"5\"\n",
	}, "2026-07-01T03:00:00Z")
	mustOK(t, code, stderr)
	for k, want := range map[string]string{"bar_depth_critical": keyReasonNoRow, `bar_depth{db="a"}`: keyReasonBaseDisabled} {
		if got := doc.Tenants["a"].Keys[k].Reason; got != want {
			t.Errorf("keys[%q].reason = %q, want %q", k, got, want)
		}
	}
}

// A root `defaults:` dimensional default is switched off with the tenant's
// base too, and its keys entry says so (#3046): base_disabled, not no_row.
func TestServedValues_ARootDimensionalDefaultIsSwitchedOffWithItsBase(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedWith(t, map[string]string{
		"_defaults.yaml": "dimensions:\n  mysql_connections: [db]\ndefaults:\n  mysql_connections: 80\n" +
			"  mysql_connections{db=\"x\"}: 12\n",
		"a.yaml": "tenants:\n  a:\n    mysql_connections: disable\n",
		"b.yaml": "tenants:\n  b:\n    mysql_connections: \"70\"\n",
	}, "2026-07-01T03:00:00Z")
	mustOK(t, code, stderr)
	const k = `mysql_connections{db="x"}`
	if e := doc.Tenants["a"].Keys[k]; e.Exporter || e.Reason != keyReasonBaseDisabled {
		t.Errorf("a: keys[%q] = %+v, want reason base_disabled", k, e)
	}
	if _, served := doc.Tenants["b"].Values[k]; !served {
		t.Errorf("b: %q not served", k)
	}
}
