package keyverdict

// #3835: the served reading handed to Judge is the whole day's (da-guard's
// servedDay folds every part of the day into one ServedKey). These pin how
// Judge reads the folded key: a `disable` that switches something off in
// some part of the day reads the key whatever its reason elsewhere, and
// disable_noop is a key unread only because of its `disable`. Tenant ids are
// fixture names (CLAUDE.md #9).

import (
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func wholeDayInput(sk ServedKey) Input {
	const key = "cpu_pct"
	return Input{
		Served: map[string]ServedTenant{"ta": {Keys: map[string]ServedKey{key: sk}}},
		Effective: map[string]EffectiveTenant{"ta": {Config: map[string]any{key: map[string]any{}},
			Sources: map[string]config.KeySource{key: {Layer: config.KeyLayerTenant, File: "ta.yaml"}}}},
		Retired: func(string) string { return "" },
	}
}

func TestJudge_WholeDayDisableReasons(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name                       string
		sk                         ServedKey
		verdict, severity, reasons string
	}{
		// Every part of the day it is unread in reads `disable`, which
		// switches nothing off: a warning.
		{"disabled all its unread day, noop", ServedKey{Reason: ReasonDisabled, Disable: DisableNoop},
			Unread, SeverityWarning, ReasonDisableNoop},
		// Some part has no row for another reason (an undeclared key): that
		// reason and its error, not disable_noop (the reason was rewritten
		// to disable_noop whenever the `disable` was noop before #3835).
		{"no row in some part, noop disable elsewhere", ServedKey{Reason: "no_row", Disable: DisableNoop},
			Unread, SeverityError, "no_row"},
		// The `disable` switches something off in some part of the day: the
		// exporter read the key, whatever its reason in the rest of the day.
		{"no row in some part, effective disable elsewhere", ServedKey{Reason: "no_row", Disable: DisableEffective},
			Read, "", ""},
		{"disabled, effective", ServedKey{Reason: ReasonDisabled, Disable: DisableEffective}, Read, "", ""},
	}
	for _, tc := range cases {
		v := one(t, Judge(wholeDayInput(tc.sk)), "ta", "cpu_pct")
		if v.Verdict != tc.verdict || deref(v.Severity) != tc.severity || deref(v.Reason) != tc.reasons {
			t.Errorf("%s: verdict %s severity %q reason %q, want %s %q %q", tc.name, v.Verdict,
				deref(v.Severity), deref(v.Reason), tc.verdict, tc.severity, tc.reasons)
		}
	}
}
