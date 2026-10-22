package config

// #3227: the write gate judges a reserved key's value with the exporter's
// reader (CheckReservedValue), and Ratchet lets a value already on disk
// through when the write leaves it as the reader reads it — compared by that
// reading, not by the YAML spelling.

import (
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// valueTenantsOf decodes a tenant file.
func valueTenantsOf(t *testing.T, doc string) map[string]map[string]ScheduledValue {
	t.Helper()
	var c ThresholdConfig
	if err := yaml.Unmarshal([]byte(doc), &c); err != nil {
		t.Fatalf("decode %q: %v", doc, err)
	}
	return c.Tenants
}

// judge is the write gate's view of cur against base ("" = no base).
func judge(t *testing.T, base, cur string) KeyValidationByTenant {
	t.Helper()
	c := ThresholdConfig{Tenants: valueTenantsOf(t, cur)}
	kv := c.ValidateTenantKeysByTenant()
	if base == "" {
		return kv
	}
	return kv.Ratchet(PriorValues(valueTenantsOf(t, base)))
}

func countWith(msgs []string, sub string) int {
	n := 0
	for _, m := range msgs {
		if strings.Contains(m, sub) {
			n++
		}
	}
	return n
}

func TestValueCheck_ErrorsAndValuesAreWrittenTogether(t *testing.T) {
	t.Parallel()
	kv := judge(t, "", "tenants:\n  t1:\n    _silent_mode: purple\n")
	v := kv.Tenants["t1"]
	if len(v.Errors) != 1 || len(v.Values) != 1 || v.Values[0].Message != v.Errors[0] {
		t.Fatalf("want one error mirrored in Values, got errors=%q values=%+v", v.Errors, v.Values)
	}
	want := `WARN: tenant=t1: _silent_mode "purple" is not a value the exporter reads ` +
		`(unknown silent mode "purple" (valid: warning, critical, all, disable)) — nothing is silenced while it is set`
	if v.Errors[0] != want {
		t.Errorf("error = %q\nwant    %q", v.Errors[0], want)
	}
}

// Every value the reader takes without a WARN passes, including the disabled
// words and the null / empty spellings.
func TestValueCheck_ReadableValuesPass(t *testing.T) {
	t.Parallel()
	for _, val := range []string{"warning", "Critical", `" all "`, "disable", "disabled", "off", "false", `""`, "~", "null",
		"\n      target: warning\n      expires: \"2026-07-01T00:00:00Z\"", "\n      target: off",
		"\n      target: warning\n      expires: 2026-07-01"} {
		kv := judge(t, "", "tenants:\n  t1:\n    _silent_mode: "+val+"\n")
		if v := kv.Tenants["t1"]; len(v.Errors)+len(v.Values) != 0 {
			t.Errorf("_silent_mode: %s → errors %q", val, v.Errors)
		}
	}
}

func TestRatchet_Boundaries(t *testing.T) {
	t.Parallel()
	const head = "tenants:\n  t1:\n"
	cases := []struct {
		name       string
		base, cur  string // tenant t1's keys, indented 4; base "-" = no base file
		wantNotice bool   // false: the finding stays an Error
	}{
		{"new tenant", "-", "    _silent_mode: purple\n", false},
		{"unchanged, other key edited", "    _silent_mode: purple\n    _severity_dedup: enable\n",
			"    _silent_mode: purple\n    _severity_dedup: disable\n", true},
		{"changed to another bad value", "    _silent_mode: purple\n", "    _silent_mode: orange\n", false},
		{"added to a tenant that had none", "    _severity_dedup: enable\n", "    _silent_mode: purple\n", false},
		{"Purple to purple: the reader lower-cases", "    _silent_mode: Purple\n", "    _silent_mode: purple\n", true},
		{"scalar quoted", "    _silent_mode: purple\n", "    _silent_mode: \"purple\"\n", true},
		{"structured: key order, quotes, comments",
			"    _silent_mode:\n      target: purple\n      reason: x\n",
			"    _silent_mode:\n      # why\n      reason: \"x\"\n      target: 'purple'\n", true},
		{"structured: target quoted", "    _silent_mode:\n      target: purple\n",
			"    _silent_mode:\n      target: \"purple\"\n", true},
		{"structured: bad expires quoted / unquoted",
			"    _silent_mode:\n      target: warning\n      expires: next week\n",
			"    _silent_mode:\n      target: warning\n      expires: \"next week\"\n", true},
		{"structured: bad expires changed",
			"    _silent_mode:\n      target: warning\n      expires: next week\n",
			"    _silent_mode:\n      target: warning\n      expires: next month\n", false},
		// Not a spelling: unquoted, YAML reads a date as a timestamp and the
		// reader gets RFC3339 (valid); quoted, it gets "2026-07-01" (not).
		{"structured: a date-only expires gains quotes",
			"    _silent_mode:\n      target: purple\n      expires: 2026-07-01\n",
			"    _silent_mode:\n      target: warning\n      expires: \"2026-07-01\"\n", false},
		{"string form and mapping form read alike",
			"    _silent_mode: \"target: purple\"\n",
			"    _silent_mode:\n      target: purple\n", true},
		{"mapping form becomes an unknown scalar", "    _silent_mode:\n      target: purple\n",
			"    _silent_mode: purple\n", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			base := head + c.base
			if c.base == "-" {
				base = "tenants: {}\n"
			}
			v := judge(t, base, head+c.cur).Tenants["t1"]
			gotErr := countWith(v.Errors, "_silent_mode")
			gotNotice := countWith(v.Notices, "_silent_mode")
			if c.wantNotice && (gotErr != 0 || gotNotice != 1 || len(v.Values) != 0) {
				t.Errorf("want a notice: errors=%q notices=%q", v.Errors, v.Notices)
			}
			if !c.wantNotice && (gotErr != 1 || gotNotice != 0 || len(v.Values) != 1) {
				t.Errorf("want an error: errors=%q notices=%q", v.Errors, v.Notices)
			}
		})
	}
}

// Deleting the key leaves nothing to say.
func TestRatchet_KeyDeleted(t *testing.T) {
	t.Parallel()
	v := judge(t, "tenants:\n  t1:\n    _silent_mode: purple\n", "tenants:\n  t1:\n    _severity_dedup: enable\n").Tenants["t1"]
	if len(v.Errors)+len(v.Notices)+len(v.Values) != 0 {
		t.Errorf("got errors=%q notices=%q", v.Errors, v.Notices)
	}
}

// Ratchet moves value findings only: a key error the same base also had
// (an unknown `_silent_mod`) still blocks.
func TestRatchet_LeavesOtherErrors(t *testing.T) {
	t.Parallel()
	doc := "tenants:\n  t1:\n    _silent_mod: warning\n    _silent_mode: purple\n"
	v := judge(t, doc, doc).Tenants["t1"]
	if countWith(v.Errors, `unknown reserved key "_silent_mod"`) != 1 || countWith(v.Errors, "_silent_mode") != 0 {
		t.Errorf("errors = %q", v.Errors)
	}
	if countWith(v.Notices, "_silent_mode") != 1 {
		t.Errorf("notices = %q", v.Notices)
	}
}

// A flat file: every tenant is ratcheted against its own section of the base.
func TestRatchet_EveryTenantOfAFlatFile(t *testing.T) {
	t.Parallel()
	base := "tenants:\n  a:\n    _silent_mode: purple\n  b:\n    _silent_mode: purple\n"
	cur := "tenants:\n  a:\n    _silent_mode: purple\n  b:\n    _silent_mode: orange\n"
	kv := judge(t, base, cur)
	if a := kv.Tenants["a"]; len(a.Errors) != 0 || len(a.Notices) != 1 {
		t.Errorf("a: errors=%q notices=%q", a.Errors, a.Notices)
	}
	if b := kv.Tenants["b"]; len(b.Errors) != 1 || len(b.Notices) != 0 {
		t.Errorf("b: errors=%q notices=%q", b.Errors, b.Notices)
	}
}

func TestRatchet_NoPrevStaysStrict(t *testing.T) {
	t.Parallel()
	c := ThresholdConfig{Tenants: valueTenantsOf(t, "tenants:\n  t1:\n    _silent_mode: purple\n")}
	kv := c.ValidateTenantKeysByTenant()
	for name, got := range map[string]KeyValidationByTenant{
		"nil prev":      kv.Ratchet(nil),
		"knows nothing": kv.Ratchet(func(string, string) (ScheduledValue, bool) { return ScheduledValue{}, false }),
	} {
		if v := got.Tenants["t1"]; len(v.Errors) != 1 || len(v.Notices) != 0 {
			t.Errorf("%s: errors=%q notices=%q", name, v.Errors, v.Notices)
		}
	}
}

func TestSelfRatchet(t *testing.T) {
	t.Parallel()
	c := ThresholdConfig{Tenants: valueTenantsOf(t, "tenants:\n  t1:\n    _silent_mode: purple\n    _silent_mod: x\n")}
	v := c.ValidateTenantKeysByTenant().SelfRatchet().Tenants["t1"]
	if countWith(v.Errors, "_silent_mode ") != 0 || countWith(v.Notices, "_silent_mode ") != 1 ||
		countWith(v.Errors, "_silent_mod\"") != 1 {
		t.Errorf("errors=%q notices=%q", v.Errors, v.Notices)
	}
	if !strings.HasPrefix(v.Notices[len(v.Notices)-1], "NOTICE: tenant=t1: _silent_mode \"purple\"") {
		t.Errorf("notice = %q", v.Notices)
	}
}

// A key with no value check is never read alike: nothing says how.
func TestReadAlike_NoCheckIsNeverAlike(t *testing.T) {
	t.Parallel()
	if readAlike("_profile", ScheduledValue{Default: "p"}, ScheduledValue{Default: "p"}) {
		t.Error("readAlike on a key with no check = true")
	}
}

// ValidateTenantKeys (the exporter's own load log, the non-split callers)
// carries the value findings too.
func TestValidateTenantKeys_CarriesValues(t *testing.T) {
	t.Parallel()
	c := ThresholdConfig{Tenants: valueTenantsOf(t, "tenants:\n  t1:\n    _silent_mode: purple\n")}
	if kv := c.ValidateTenantKeys(); len(kv.Values) != 1 || len(kv.Errors) != 1 {
		t.Errorf("errors=%q values=%+v", kv.Errors, kv.Values)
	}
}
