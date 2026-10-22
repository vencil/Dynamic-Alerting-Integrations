package gitops

// #3269: a reserved key written with a schedule is refused by the write gate
// (the exporter's own judgement, cfg.ValidateTenantKeys), and ratchets like
// #3227's value findings — but as written: the same schedule on disk is a
// notice, a moved window is a change.

import "testing"

const schedNote = "_silent_mode writes `overrides:`"

func schedBody(window string) string {
	return vrBody("    _silent_mode: {default: disable, overrides: [{window: \"" + window + "\", value: all}]}\n")
}

func TestValidate_ScheduleRatchet(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, base, body string
		wantErr          bool
	}{
		{"new tenant (ENOENT) is strict", "", schedBody("03:00-04:00"), true},
		{"added to a tenant that had a plain value", vrBody("    _silent_mode: disable\n"), schedBody("03:00-04:00"), true},
		{"unchanged, another key edited", schedBody("03:00-04:00"),
			schedBody("03:00-04:00") + "    _severity_dedup: disable\n", false},
		{"window moved", schedBody("03:00-04:00"), schedBody("05:00-06:00"), true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			errs, notices := validateOn(t, c.base, c.body)
			if c.wantErr {
				if countSub(errs, schedNote) != 1 || countSub(notices, schedNote) != 0 {
					t.Errorf("want an error: errs=%q notices=%q", errs, notices)
				}
				return
			}
			if len(errs) != 0 || countSub(notices, "NOTICE: tenant=vr-t: "+schedNote) != 1 {
				t.Errorf("want a notice: errs=%q notices=%q", errs, notices)
			}
		})
	}
}

// configDir == "" has no base: strict.
func TestValidate_ScheduleRatchet_NoConfigDirIsStrict(t *testing.T) {
	t.Parallel()
	errs, _ := validate("", "vr-t", "vr-t.yaml", schedBody("03:00-04:00"))
	if countSub(errs, schedNote) != 1 {
		t.Errorf("errs = %q", errs)
	}
}
