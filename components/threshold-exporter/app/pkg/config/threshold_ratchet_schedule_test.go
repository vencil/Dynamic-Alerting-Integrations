package config

// #3508: the write gate's ratchet (ADR-038 decision 12) keeps a refused
// threshold already on disk as a notice when the write leaves it READ the
// same way. For a schedule that is the value served at each minute of the
// day and the instant it lapses — not the position of its windows, a
// `reason:`, or how `expires` is spelt.

import "testing"

func judgeThreshold(t *testing.T, base, cur string) KeyValidation {
	t.Helper()
	defaults := map[string]float64{"mysql_connections": 80}
	c := ThresholdConfig{Defaults: defaults, Tenants: valueTenantsOf(t, cur)}
	return c.ValidateTenantKeysByTenant().
		Ratchet(PriorValues(valueTenantsOf(t, base))).Tenants["t1"]
}

func TestRatchet_ScheduledThresholdReadTheSame(t *testing.T) {
	t.Parallel()
	const head = "tenants:\n  t1:\n    mysql_connections:\n      default: abc\n"
	cases := []struct {
		name       string
		base, cur  string
		wantNotice bool // false: the refused value stays an error (400)
	}{
		{"windows that do not overlap, reordered",
			"      overrides:\n        - {window: \"01:00-02:00\", value: \"1\"}\n        - {window: \"03:00-04:00\", value: \"2\"}\n",
			"      overrides:\n        - {window: \"03:00-04:00\", value: \"2\"}\n        - {window: \"01:00-02:00\", value: \"1\"}\n",
			true},
		{"only reason added",
			"      expires: \"2030-01-01T00:00:00Z\"\n",
			"      expires: \"2030-01-01T00:00:00Z\"\n      reason: migration\n",
			true},
		{"expires, same instant, another offset",
			"      expires: \"2030-01-01T00:00:00Z\"\n",
			"      expires: \"2030-01-01T08:00:00+08:00\"\n",
			true},
		// Control: overlapping windows reordered — another one wins 01:30-02:00.
		{"overlapping windows, reordered",
			"      overrides:\n        - {window: \"01:00-02:00\", value: \"1\"}\n        - {window: \"01:30-03:00\", value: \"2\"}\n",
			"      overrides:\n        - {window: \"01:30-03:00\", value: \"2\"}\n        - {window: \"01:00-02:00\", value: \"1\"}\n",
			false},
		// A refused value a write adds under a window that never applies
		// (invalid, empty, or wholly shadowed) is never served, yet it is a
		// new refused value, not the one already on disk.
		{"new refused value under an invalid window",
			"      overrides:\n        - {window: \"01:00-02:00\", value: \"1\"}\n",
			"      overrides:\n        - {window: \"01:00-02:00\", value: \"1\"}\n        - {window: \"bogus\", value: \"xyz\"}\n",
			false},
		{"new refused value under a shadowed window",
			"      overrides:\n        - {window: \"01:00-03:00\", value: \"1\"}\n",
			"      overrides:\n        - {window: \"01:00-03:00\", value: \"1\"}\n        - {window: \"01:30-02:00\", value: \"xyz\"}\n",
			false},
		{"expires moved to another instant",
			"      expires: \"2030-01-01T00:00:00Z\"\n",
			"      expires: \"2031-01-01T00:00:00Z\"\n",
			false},
	}
	for _, c := range cases {
		c := c
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			v := judgeThreshold(t, head+c.base, head+c.cur)
			gotErr := countWith(v.Errors, "mysql_connections")
			gotNotice := countWith(v.Notices, "mysql_connections")
			if c.wantNotice && (gotErr != 0 || gotNotice == 0) {
				t.Errorf("want a notice: errors=%q notices=%q", v.Errors, v.Notices)
			}
			if !c.wantNotice && gotErr == 0 {
				t.Errorf("want an error: errors=%q notices=%q", v.Errors, v.Notices)
			}
		})
	}
}
