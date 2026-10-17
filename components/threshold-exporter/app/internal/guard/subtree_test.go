package guard

import (
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

// #1976 r4: the fix names both places a threshold can be declared. (No `_`
// key reaches the finding: the row generator skips every one, #3045 — the
// `_`-only "root only" sentence went with that.)
func TestCheckSubtreeUndeliverable_FixNamesRootAndOptionalOverrides(t *testing.T) {
	t.Parallel()
	report, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs:       map[string]map[string]any{"tenant-a": {}},
		UndeliverableInherited: map[string][]string{"tenant-a": {"redis_x"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	const both = "Declare `redis_x` in the conf.d root `_defaults.yaml` or in `optional_overrides:`."
	if len(report.Findings) != 1 || !strings.Contains(report.Findings[0].Message, both) {
		t.Errorf("findings = %+v; want one whose message contains %q", report.Findings, both)
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
		if f.Kind != findingkind.FindingSubtreeDefaultUndeliverable || f.Severity != SeverityWarn {
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
