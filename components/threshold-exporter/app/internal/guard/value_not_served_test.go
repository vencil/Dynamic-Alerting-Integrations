package guard

import (
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// #2065: one error per (tenant, key) of ValuesNotServed whose reason is one
// of the four written-value reasons, only for tenants in EffectiveConfigs,
// sorted; the message starts with the reason and names the file.
func TestCheckValuesNotServed(t *testing.T) {
	t.Parallel()
	ns := func(r, f string) config.NotServedKey { return config.NotServedKey{Reason: r, File: f} }
	report, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"tx": {}, "ty": {}, "tz": {}},
		ValuesNotServed: map[string]map[string]config.NotServedKey{
			"ty": {
				"mysql_connections":          ns(config.NotServedWindowInvalid, "team/ty.yaml"),
				"mysql_connections_critical": ns(config.NotServedValueUnparsedDropped, "team/ty.yaml"),
				"redis_memory":               ns(config.NotServedUndeliverable, "team/_defaults.yaml"), // its own finding
				"pg_connections":             ns(config.NotServedRootNullUndeclared, "team/ty.yaml"),   // its own finding
			},
			"tx": {
				"mysql_connections":     ns(config.NotServedValueUnparsed, "team/tx.yaml"),
				"mysql_threads_running": ns(config.NotServedValueRejected, "team/_defaults.yaml"),
				"defaults":              ns(config.NotServedParseFailed, "_defaults.yaml"),        // exit 3's
				"_routing_defaults":     ns(config.NotServedValueRejected, "team/_defaults.yaml"), // reserved: its own finding
			},
			"tout": {"mysql_connections": ns(config.NotServedValueUnparsed, "b/t.yaml")}, // not in scope
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	var got [][2]string
	for _, f := range report.Findings {
		if f.Kind != findingkind.FindingValueNotServed || f.Severity != SeverityError {
			t.Errorf("unexpected finding %+v", f)
			continue
		}
		got = append(got, [2]string{f.TenantID, f.Field})
		reason := strings.SplitN(f.Message, ":", 2)[0]
		if !config.ValueNotServedAsWritten(f.Field, reason) {
			t.Errorf("message does not start with a reason: %s", f.Message)
		}
	}
	want := [][2]string{
		{"tx", "mysql_connections"}, {"tx", "mysql_threads_running"},
		{"ty", "mysql_connections"}, {"ty", "mysql_connections_critical"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("findings = %v, want %v", got, want)
	}
	if report.Summary.Errors != 4 || report.Summary.PassedTenantCount != 1 {
		t.Errorf("summary = %+v, want 4 errors, 1 passing", report.Summary)
	}
	for _, f := range report.Findings {
		if f.TenantID == "tx" && f.Field == "mysql_threads_running" &&
			(!strings.HasPrefix(f.Message, "value_rejected: team/_defaults.yaml writes `mysql_threads_running`")) {
			t.Errorf("value_rejected message: %s", f.Message)
		}
		if f.TenantID == "ty" && f.Field == "mysql_connections" &&
			(!strings.HasPrefix(f.Message, "window_invalid: team/ty.yaml") || !strings.Contains(f.Message, "start equal to its end")) {
			t.Errorf("window_invalid message: %s", f.Message)
		}
	}
}

// Nil ValuesNotServed (the pre-#2065 caller) adds nothing.
func TestCheckValuesNotServed_NilSkips(t *testing.T) {
	t.Parallel()
	if got := checkValuesNotServed(CheckInput{EffectiveConfigs: map[string]map[string]any{"tx": {}}}); got != nil {
		t.Errorf("findings = %v, want none", got)
	}
}

// #3140 / #3141: both value_unparsed messages name a non-finite number among
// the values the exporter cannot read, and the two spellings a
// `disable:<severity>` may have meant — with the key's own critical tier for
// a plain key.
func TestValueNotServedMessage_NamesNonFiniteAndDisableSpellings(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct{ key, reason, critical string }{
		{"mysql_connections", config.NotServedValueUnparsed, "`mysql_connections_critical: disable`"},
		{"mysql_connections_critical", config.NotServedValueUnparsedDropped, "`<key>_critical: disable`"},
		{`mysql_connections{db="a"}`, config.NotServedValueUnparsedDropped, "`<key>_critical: disable`"},
	} {
		msg := valueNotServedMessage(tc.key, config.NotServedKey{Reason: tc.reason, File: "t.yaml"})
		for _, want := range []string{"not a finite number", "`NaN`", "`disable:critical`",
			"write `disable` to switch the whole key off", tc.critical} {
			if !strings.Contains(msg, want) {
				t.Errorf("%s/%s: message lacks %q: %s", tc.key, tc.reason, want, msg)
			}
		}
	}
}

// #3209: CustomAlertsRefused alone — no ValuesNotServed at all — is one
// value_not_served error per entry, after the tenant's keys, only for tenants
// in EffectiveConfigs; the message starts with value_unparsed_dropped (no
// default to fall back to) and names the entry, its threshold and the
// exporter's reason.
func TestCheckValuesNotServed_CustomAlertsRefused(t *testing.T) {
	t.Parallel()
	ca := func(i int, thr, refused string) RefusedCustomAlert {
		return RefusedCustomAlert{Index: i, Name: "q", Threshold: thr, Refused: refused, Reason: "exporter says no"}
	}
	in := CheckInput{
		EffectiveConfigs: map[string]map[string]any{"tx": {}, "ty": {}},
		CustomAlertsRefused: map[string][]RefusedCustomAlert{
			"ty":   {ca(2, "NaN", config.RefusedNonFinite)},
			"tx":   {ca(0, "abc", config.RefusedUnparsed), ca(3, "70:high", config.RefusedUnparsed)},
			"tout": {ca(0, "abc", config.RefusedUnparsed)}, // not in scope
		},
	}
	var got [][2]string
	for _, f := range checkValuesNotServed(in) {
		if f.Kind != findingkind.FindingValueNotServed || f.Severity != SeverityError {
			t.Errorf("unexpected finding %+v", f)
		}
		got = append(got, [2]string{f.TenantID, f.Field})
		if !strings.HasPrefix(f.Message, "value_unparsed_dropped: ") || !strings.Contains(f.Message, "exporter says no") ||
			!strings.Contains(f.Message, `"q"`) {
			t.Errorf("message: %s", f.Message)
		}
	}
	want := [][2]string{{"tx", "_custom_alerts[0]"}, {"tx", "_custom_alerts[3]"}, {"ty", "_custom_alerts[2]"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("findings = %v, want %v", got, want)
	}
}
