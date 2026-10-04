package config

import (
	"reflect"
	"testing"
)

// The exact problem strings (#2708): a null `window:` is not printed as a
// window — the value line then names only the entry. validate-config's
// Python copy (schedule_null_problems) asserts the same strings.
func TestScheduleNullProblems_Messages(t *testing.T) {
	t.Parallel()
	got := scheduleNullProblems(map[string]any{
		"default": nil,
		"overrides": []any{
			map[string]any{"window": nil, "value": nil},
			map[string]any{"window": "00:00-01:00", "value": nil},
			nil,
		},
	})
	want := []string{
		"`default:` is null beside 3 override window(s)",
		"`overrides[0]` has `window: null`",
		"`overrides[0]` has `value: null`",
		"`overrides[1]` (window \"00:00-01:00\") has `value: null`",
		"`overrides[2]` is null",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("scheduleNullProblems =\n%q\nwant\n%q", got, want)
	}
}
