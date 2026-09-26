package main

// #2000: a structured _silent_mode / _state_maintenance whose `expires:` is
// not RFC3339 fails CLOSED — the setting is ignored as a whole (nothing is
// silenced, maintenance is not active). Before #2000 it failed open: the
// mute stayed on with no end, i.e. alerts were lost without anyone saying so.
//
// The malformed shapes are the ones that actually reach the exporter:
// neither write path rejects them (tenant-api leaves structured expires to
// downstream; patch-config re-serialises RFC3339 as the space-separated
// form), so every one of them must be exercised here, not just "garbage".
//
// ⚠️ Threshold overrides are deliberately NOT covered by this rule — their
// malformed expires stays fail-open (isThresholdExpired). The last test
// pins that so a future "make them consistent" edit has to go red first.

import (
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// failClosedNow is fixed so "future" / "past" never depend on wall clock.
var failClosedNow = time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)

// malformedExpires are values time.Parse(RFC3339) rejects. Keep the
// date-only and space-separated rows: those are what humans and
// patch-config really write, and the reason this fix exists.
var malformedExpires = []struct {
	name    string
	expires string
}{
	{"DateOnly", "2026-12-31"},
	{"SpaceSeparated", "2026-12-31 00:00:00+00:00"},
	{"NoTimezone", "2026-12-31T00:00:00"},
	{"Garbage", "not-a-date"},
}

func TestSilentMode_MalformedExpiresFailsClosed(t *testing.T) {
	t.Parallel()
	const tenant = "tenant-x"
	for _, target := range []string{"warning", "critical", "all"} {
		for _, m := range malformedExpires {
			t.Run(target+"/"+m.name, func(t *testing.T) {
				t.Parallel()
				cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
					tenant: {"_silent_mode": SV("expires: \"" + m.expires + "\"\nreason: r\ntarget: " + target + "\n")},
				}}
				if got := cfg.ResolveSilentModesAt(failClosedNow); len(got) != 0 {
					t.Fatalf("expires %q is not RFC3339 → setting must be ignored (0 silent entries), got %+v", m.expires, got)
				}
			})
		}
	}
}

func TestSilentMode_ValidExpiresUnchanged(t *testing.T) {
	t.Parallel()
	const tenant = "tenant-x"
	future := failClosedNow.Add(24 * time.Hour).Format(time.RFC3339)
	past := failClosedNow.Add(-time.Hour).Format(time.RFC3339)
	tests := []struct {
		name        string
		expires     string
		wantExpired bool
	}{
		{"Future", future, false},
		{"Past", past, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
				tenant: {"_silent_mode": SV("expires: \"" + tt.expires + "\"\ntarget: warning\n")},
			}}
			got := cfg.ResolveSilentModesAt(failClosedNow)
			if len(got) != 1 {
				t.Fatalf("want 1 entry, got %+v", got)
			}
			if got[0].Expired != tt.wantExpired || got[0].Expires.IsZero() {
				t.Errorf("want Expired=%v with non-zero Expires, got %+v", tt.wantExpired, got[0])
			}
		})
	}
}

func maintenanceCfg(tenant, expires string) *ThresholdConfig {
	return &ThresholdConfig{
		StateFilters: map[string]StateFilter{
			"maintenance": {Severity: "warning", DefaultState: "disable"},
		},
		Tenants: map[string]map[string]ScheduledValue{
			tenant: {"_state_maintenance": SV("expires: \"" + expires + "\"\nreason: r\ntarget: enable\n")},
		},
	}
}

func TestMaintenance_MalformedExpiresFailsClosed(t *testing.T) {
	t.Parallel()
	const tenant = "tenant-x"
	for _, m := range malformedExpires {
		t.Run(m.name, func(t *testing.T) {
			t.Parallel()
			cfg := maintenanceCfg(tenant, m.expires)
			// The three readers must agree: not active, no filter, no expiry event.
			if got := cfg.ResolveStateFiltersAt(failClosedNow); len(got) != 0 {
				t.Errorf("ResolveStateFiltersAt: expires %q not RFC3339 → maintenance must not be emitted, got %+v", m.expires, got)
			}
			if cfg.OperationalStatesAt(failClosedNow).ByTenant(cfg)[tenant].MaintenanceActive {
				t.Errorf("OperationalStatesAt: expires %q not RFC3339 → maintenance must be inactive", m.expires)
			}
			if got := cfg.ResolveMaintenanceExpiriesAt(failClosedNow); len(got) != 0 {
				t.Errorf("ResolveMaintenanceExpiriesAt: ignored setting has no window to report, got %+v", got)
			}
		})
	}
}

func TestMaintenance_ValidExpiresUnchanged(t *testing.T) {
	t.Parallel()
	const tenant = "tenant-x"
	tests := []struct {
		name       string
		expires    string
		wantActive bool
	}{
		{"Future", failClosedNow.Add(24 * time.Hour).Format(time.RFC3339), true},
		{"Past", failClosedNow.Add(-time.Hour).Format(time.RFC3339), false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			cfg := maintenanceCfg(tenant, tt.expires)
			wantFilters := 0
			if tt.wantActive {
				wantFilters = 1
			}
			if got := cfg.ResolveStateFiltersAt(failClosedNow); len(got) != wantFilters {
				t.Errorf("ResolveStateFiltersAt: want %d, got %+v", wantFilters, got)
			}
			if got := cfg.OperationalStatesAt(failClosedNow).ByTenant(cfg)[tenant].MaintenanceActive; got != tt.wantActive {
				t.Errorf("OperationalStatesAt: want maintenance active=%v, got %v", tt.wantActive, got)
			}
			exp := cfg.ResolveMaintenanceExpiriesAt(failClosedNow)
			if len(exp) != 1 || exp[0].Expired == tt.wantActive {
				t.Errorf("ResolveMaintenanceExpiriesAt: want 1 entry with Expired=%v, got %+v", !tt.wantActive, exp)
			}
		})
	}
}

// TestThresholdOverride_MalformedExpiresStillFailsOpen pins the half of #2000
// that did NOT change: a time-boxed threshold override with a malformed
// expires keeps its (looser) value instead of reverting to the default.
func TestThresholdOverride_MalformedExpiresStillFailsOpen(t *testing.T) {
	t.Parallel()
	for _, m := range malformedExpires {
		t.Run(m.name, func(t *testing.T) {
			t.Parallel()
			cfg := &ThresholdConfig{
				Defaults: map[string]float64{"mysql_connections": 80},
				Tenants: map[string]map[string]ScheduledValue{
					"tenant-x": {"mysql_connections": {Default: "2000", Expiry: &ExpiryMeta{Expires: m.expires}}},
				},
			}
			rows := cfg.ResolveAt(failClosedNow)
			if len(rows) != 1 || rows[0].Value != 2000 {
				t.Errorf("threshold expires %q: must stay fail-open (keep 2000), got %+v", m.expires, rows)
			}
		})
	}
}

// TestSilentMode_FromTenantYAML runs the real parse path (ScheduledValue's
// mapping round-trip) rather than a hand-built SV. It pins one non-obvious
// fact the docs rely on: an UNQUOTED date-only expires is a YAML timestamp,
// and yaml.v3 re-serialises it as RFC3339 midnight UTC — so it is honoured,
// while the same text QUOTED stays a plain string and is ignored.
func TestSilentMode_FromTenantYAML(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		expiresLn string
		wantLen   int
	}{
		{"UnquotedDateOnly_NormalisedToRFC3339", "expires: 2026-12-31", 1},
		{"QuotedDateOnly_Ignored", `expires: "2026-12-31"`, 0},
		{"SpaceSeparated_Ignored", "expires: 2026-12-31 00:00:00+00:00", 0},
		{"Garbage_Ignored", "expires: not-a-date", 0},
		{"RFC3339_Honoured", "expires: 2026-12-31T00:00:00Z", 1},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			var overrides map[string]ScheduledValue
			doc := "_silent_mode:\n  target: warning\n  " + tt.expiresLn + "\n"
			if err := yaml.Unmarshal([]byte(doc), &overrides); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"tenant-x": overrides}}
			if got := cfg.ResolveSilentModesAt(failClosedNow); len(got) != tt.wantLen {
				t.Errorf("%s: want %d entries, got %+v (stored as %q)", tt.expiresLn, tt.wantLen, got, overrides["_silent_mode"].Default)
			}
		})
	}
}
