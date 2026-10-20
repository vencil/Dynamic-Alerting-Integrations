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
