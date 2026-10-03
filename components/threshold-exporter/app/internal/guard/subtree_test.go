package guard

import (
	"reflect"
	"strings"
	"testing"
)

// #1976 r4: the fix sentence depends on the key. optional_overrides: does not
// serve `_` keys (resolveDeclaredRows skips them), so for those the message
// names the root only and says why.
func TestCheckSubtreeUndeliverable_FixDependsOnKey(t *testing.T) {
	t.Parallel()
	report, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs:       map[string]map[string]any{"tenant-a": {}},
		UndeliverableInherited: map[string][]string{"tenant-a": {"_myth2", "redis_x"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	msg := map[string]string{}
	for _, f := range report.Findings {
		msg[f.Field] = f.Message
	}
	const both = "in the conf.d root `_defaults.yaml` or in `optional_overrides:`."
	const rootOnly = "`optional_overrides:` does not serve keys starting with `_`"
	for _, tc := range []struct {
		key        string
		want, deny string
	}{
		{key: "_myth2", want: rootOnly, deny: both},
		{key: "redis_x", want: both, deny: rootOnly},
	} {
		t.Run(tc.key, func(t *testing.T) {
			m, ok := msg[tc.key]
			if !ok {
				t.Fatalf("no finding for %s; findings %v", tc.key, report.Findings)
			}
			if !strings.Contains(m, tc.want) || strings.Contains(m, tc.deny) {
				t.Errorf("message for %s = %q; want it to contain %q and not %q", tc.key, m, tc.want, tc.deny)
			}
		})
	}
}

// #1976: one warn finding per (tenant, key) of UndeliverableInherited, only for
// tenants in EffectiveConfigs, sorted; never an error, so no tenant fails.
func TestCheckSubtreeUndeliverable(t *testing.T) {
	t.Parallel()
	report, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"tenant-a": {}, "tenant-b": {}},
		UndeliverableInherited: map[string][]string{
			"tenant-b":   {"k2", "k1"},
			"tenant-out": {"k3"}, // not in EffectiveConfigs: not reported
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	var got [][3]string
	for _, f := range report.Findings {
		if f.Kind != FindingSubtreeDefaultUndeliverable || f.Severity != SeverityWarn {
			t.Errorf("unexpected finding %+v", f)
			continue
		}
		got = append(got, [3]string{f.TenantID, f.Field, string(f.Severity)})
	}
	want := [][3]string{{"tenant-b", "k1", "warn"}, {"tenant-b", "k2", "warn"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("findings = %v, want %v", got, want)
	}
	if report.Summary.Warnings != 2 || report.Summary.Errors != 0 || report.Summary.PassedTenantCount != 2 {
		t.Errorf("summary = %+v, want 2 warnings, 0 errors, 2 passing", report.Summary)
	}
}
