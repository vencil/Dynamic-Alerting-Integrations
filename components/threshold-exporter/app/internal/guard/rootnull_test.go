package guard

import (
	"reflect"
	"strings"
	"testing"
)

// #2518: one warn finding per (tenant, key) of RootNullUndeclared, only for
// tenants in EffectiveConfigs, sorted; the fix sentence offers
// `optional_overrides:` only for a key not starting with `_`.
func TestCheckRootNullUndeclared(t *testing.T) {
	t.Parallel()
	report, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"tenant-a": {}, "tenant-b": {}},
		RootNullUndeclared: map[string][]string{
			"tenant-b":   {"redis_x", "_myth"},
			"tenant-out": {"k3"}, // not in EffectiveConfigs: not reported
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	var got [][3]string
	msg := map[string]string{}
	for _, f := range report.Findings {
		if f.Severity != SeverityWarn || f.Kind != FindingRootDefaultNullUndeclared {
			t.Errorf("unexpected finding %+v", f)
		}
		got = append(got, [3]string{f.TenantID, f.Field, string(f.Kind)})
		msg[f.Field] = f.Message
	}
	want := [][3]string{
		{"tenant-b", "_myth", string(FindingRootDefaultNullUndeclared)},
		{"tenant-b", "redis_x", string(FindingRootDefaultNullUndeclared)},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("findings = %v, want %v", got, want)
	}
	if report.Summary.Warnings != 2 || report.Summary.Errors != 0 || report.Summary.PassedTenantCount != 2 {
		t.Errorf("summary = %+v, want 2 warnings, 0 errors, 2 passing", report.Summary)
	}
	if !strings.Contains(msg["redis_x"], "optional_overrides:") {
		t.Errorf("redis_x message lacks optional_overrides: %s", msg["redis_x"])
	}
	if strings.Contains(msg["_myth"], "optional_overrides:") {
		t.Errorf("_myth message offers optional_overrides:, which does not serve `_` keys: %s", msg["_myth"])
	}
}
