package config

// family_disable_test.go — #3046: a base's `disable` switches its family
// off — `<base>_critical` and every dimensional `<base>{…}`, the tenant's
// own and the root `defaults:` ones — wherever the merge took the
// `disable` from, at the minutes it reads `disable` (familyHeadOff).

import (
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// rowsOf is tenant's rows at fixedNow as "metric severity value labels".
func rowsOf(t *testing.T, cfg *ThresholdConfig, tenant string) []string {
	t.Helper()
	var out []string
	for _, r := range cfg.ResolveAt(fixedNow) {
		if r.Tenant != tenant {
			continue
		}
		var labels []string
		for k, v := range r.CustomLabels {
			labels = append(labels, k+"="+v)
		}
		for k, v := range r.RegexLabels {
			labels = append(labels, k+"=~"+v)
		}
		sort.Strings(labels)
		out = append(out, r.Metric+" "+r.Severity+" "+strconv.FormatFloat(r.Value, 'g', -1, 64)+" "+strings.Join(labels, ","))
	}
	sort.Strings(out)
	return out
}

// The family of a switched-off base is served no row — critical, the
// tenant's dimensional key, the root's dimensional default — and each of
// the tenant's own keys is not_served base_disabled; another family is
// untouched.
func TestFamilyDisable_SwitchesTheWholeFamilyOff(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Dimensions: map[string][]string{"redis_memory": {"db"}, "mysql_connections": {"db"}},
		Defaults: map[string]float64{"redis_memory": 70, `redis_memory{db="y"}`: 40,
			"mysql_connections": 80, `mysql_connections{db="y"}`: 30},
		Tenants: map[string]map[string]ScheduledValue{"t": {
			"redis_memory":               {Default: "disable"},
			"redis_memory_critical":      {Default: "90"},
			`redis_memory{db="x"}`:       {Default: "12"},
			"mysql_connections_critical": {Default: "95"},
		}},
	}
	got := rowsOf(t, cfg, "t")
	want := []string{
		"connections critical 95 ", "connections warning 30 db=y", "connections warning 80 ",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("rows %q, want %q", got, want)
	}
	ns := cfg.ValuesNotServed(fixedNow)["t"]
	wantNS := map[string]string{"redis_memory_critical": NotServedBaseDisabled, `redis_memory{db="x"}`: NotServedBaseDisabled}
	if !reflect.DeepEqual(ns, wantNS) {
		t.Errorf("not served %v, want %v", ns, wantNS)
	}
}

// A scheduled `disable` switches the family off in its window only.
func TestFamilyDisable_FollowsTheSchedule(t *testing.T) {
	t.Parallel()
	sched := func(window string) *ThresholdConfig {
		return &ThresholdConfig{
			Defaults: map[string]float64{"redis_memory": 70},
			Tenants: map[string]map[string]ScheduledValue{"t": {
				"redis_memory":          {Default: "70", Overrides: []TimeWindowOverride{{Window: window, Value: "disable"}}},
				"redis_memory_critical": {Default: "90"},
			}},
		}
	}
	if got := rowsOf(t, sched("09:00-11:00"), "t"); len(got) != 0 { // fixedNow is 10:00
		t.Errorf("in the window: rows %q, want none", got)
	}
	if got := rowsOf(t, sched("01:00-02:00"), "t"); !reflect.DeepEqual(got, []string{"memory critical 90 ", "memory warning 70 "}) {
		t.Errorf("outside the window: rows %q", got)
	}
}

// The base's expiry rule is the base phase's (headLive): a lapsed `disable`
// of a key the root values is treated as absent — the base is served its
// default, and so is its family; a declared base keeps its `disable`
// (no expiry), and so its family stays off.
func TestFamilyDisable_ExpiryIsTheBasePhases(t *testing.T) {
	t.Parallel()
	lapsed := ScheduledValue{Default: "disable", Expiry: &ExpiryMeta{Expires: "2020-01-01T00:00:00Z"}}
	valued := &ThresholdConfig{
		Defaults: map[string]float64{"redis_memory": 70},
		Tenants: map[string]map[string]ScheduledValue{"t": {
			"redis_memory": lapsed, "redis_memory_critical": {Default: "90"}}},
	}
	if got := rowsOf(t, valued, "t"); !reflect.DeepEqual(got, []string{"memory critical 90 ", "memory warning 70 "}) {
		t.Errorf("valued base, lapsed disable: rows %q", got)
	}
	declared := &ThresholdConfig{
		OptionalOverrides: []string{"redis_rate"},
		Dimensions:        map[string][]string{"redis_rate": {"db"}},
		Tenants: map[string]map[string]ScheduledValue{"t": {
			"redis_rate": lapsed, `redis_rate{db="a"}`: {Default: "5"}}},
	}
	if got := rowsOf(t, declared, "t"); len(got) != 0 {
		t.Errorf("declared base, lapsed disable: rows %q, want none", got)
	}
}

// A base only `dimensions:` names (no root value, not declared) is a family
// head too: its `disable` switches the tenant's dimensional keys off and is
// read — claimed, so it is neither unconsumed nor a `disable` nobody reads.
func TestFamilyDisable_ABaseOnlyDimensionsNames(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Dimensions: map[string][]string{"bar_depth": {"db"}},
		Tenants: map[string]map[string]ScheduledValue{"t": {
			"bar_depth": {Default: "disable"}, `bar_depth{db="a"}`: {Default: "5"}}},
	}
	if got := rowsOf(t, cfg, "t"); len(got) != 0 {
		t.Errorf("rows %q, want none", got)
	}
	table, disabled := cfg.ValuesNotServedAndDisabled(fixedNow)
	if want := map[string]string{`bar_depth{db="a"}`: NotServedBaseDisabled}; !reflect.DeepEqual(table["t"], want) {
		t.Errorf("not served %v, want %v", table["t"], want)
	}
	if len(disabled["t"]) != 0 {
		t.Errorf("disabled unclaimed %v, want none: the base's disable is read", disabled["t"])
	}
}

// `disable:<severity>` is no disable (#3141), so it switches no family off.
func TestFamilyDisable_DisableWithASeverityIsNone(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"redis_memory": 70},
		Tenants: map[string]map[string]ScheduledValue{"t": {
			"redis_memory": {Default: "disable:critical"}, "redis_memory_critical": {Default: "90"}}},
	}
	if got := rowsOf(t, cfg, "t"); !reflect.DeepEqual(got, []string{"memory critical 90 ", "memory warning 70 "}) {
		t.Errorf("rows %q", got)
	}
}

// A family member's own verdict comes first: an unreadable value is
// value_unparsed_dropped whatever its base does, so value_not_served still
// names it.
func TestFamilyDisable_AnUnreadableValueKeepsItsVerdict(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Dimensions: map[string][]string{"redis_memory": {"db"}},
		Defaults:   map[string]float64{"redis_memory": 70},
		Tenants: map[string]map[string]ScheduledValue{"t": {
			"redis_memory": {Default: "disable"}, "redis_memory_critical": {Default: "abc"},
			`redis_memory{db="x"}`: {Default: "NaN"}}},
	}
	want := map[string]string{"redis_memory_critical": NotServedValueUnparsedDropped,
		`redis_memory{db="x"}`: NotServedValueUnparsedDropped}
	if got := cfg.ValuesNotServed(fixedNow)["t"]; !reflect.DeepEqual(got, want) {
		t.Errorf("not served %v, want %v", got, want)
	}
}

// base_disabled is what the `disable` asks: not a value not served as
// written (no value_not_served, no da_config_values_not_served).
func TestValueNotServedAsWritten_NotBaseDisabled(t *testing.T) {
	t.Parallel()
	if ValueNotServedAsWritten("redis_memory_critical", NotServedBaseDisabled) {
		t.Error("base_disabled is selected as a value not served as written")
	}
	for _, r := range ValueNotServedReasons() {
		if r == NotServedBaseDisabled {
			t.Errorf("ValueNotServedReasons() holds %q", r)
		}
	}
}

// FamilyBase derives a family member's base as the phases do; any other key
// has none.
func TestFamilyBase(t *testing.T) {
	t.Parallel()
	for key, want := range map[string]string{
		"redis_memory_critical": "redis_memory",
		`redis_memory{db="x"}`:  "redis_memory",
		`redis_memory{db=~"x"}`: "redis_memory",
		"redis_memory":          "",
		"_state_x_critical":     "",
		`_myth{a="b"}`:          "",
	} {
		got, ok := FamilyBase(key)
		if got != want || ok != (want != "") {
			t.Errorf("FamilyBase(%q) = %q %v, want %q", key, got, ok, want)
		}
	}
}

// base_disabled is the weakest verdict of the day: a value refused at
// another minute keeps value_unparsed_dropped.
func TestFamilyDisable_IsTheWeakestVerdictOfTheDay(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"redis_memory": 70},
		Tenants: map[string]map[string]ScheduledValue{"t": {
			"redis_memory":          {Default: "70", Overrides: []TimeWindowOverride{{Window: "09:00-11:00", Value: "disable"}}},
			"redis_memory_critical": {Default: "90", Overrides: []TimeWindowOverride{{Window: "01:00-02:00", Value: "abc"}}},
		}},
	}
	if got := cfg.ValuesNotServed(fixedNow)["t"]["redis_memory_critical"]; got != NotServedValueUnparsedDropped {
		t.Errorf("not served %q, want %q", got, NotServedValueUnparsedDropped)
	}
}
