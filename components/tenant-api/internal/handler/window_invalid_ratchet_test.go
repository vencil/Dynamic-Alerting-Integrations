package handler

// #3683 (owner ruling C): the write gate judges each schedule entry once, by
// the defect that keeps it from being served.
//
//   - A value in an entry /metrics serves at no minute (a dead entry, #3502)
//     is not refused as a threshold as well: only the dead-entry finding.
//   - A `window:` the exporter cannot read (window_invalid) is refused where
//     it is written, and one the tenant file already holds, left exactly as
//     it is, is a notice — the ratchet a dead entry has. The value under
//     such a window is not refused as a threshold.
//
// The table is the 200/400 measurement posted on #3683, after the ruling.

import (
	"net/http"
	"strings"
	"testing"
)

// wiSched is a tenant file (or PUT body) for t-a whose mysql_connections is
// written as a schedule: def ("" = no default), then window/value pairs.
func wiSched(def string, pairs ...string) string {
	b := "tenants:\n  t-a:\n    mysql_connections:\n"
	if def != "" {
		b += "      default: \"" + def + "\"\n"
	}
	b += "      overrides:\n"
	for i := 0; i+1 < len(pairs); i += 2 {
		b += "        - window: \"" + pairs[i] + "\"\n          value: \"" + pairs[i+1] + "\"\n"
	}
	return b
}

const (
	wiRefusal = "is not a threshold the exporter reads"
	wiDead    = "serves at no minute"
	wiWindow  = "with a `window:` that is not a UTC HH:MM-HH:MM"
	wiOther   = "    _silent_mode: warning\n"
	// wiDeadNotice is the dead-entry notice's opening (#3502).
	wiDeadNotice = "NOTICE: tenant=t-a: mysql_connections writes a schedule with entries /metrics " + wiDead
	wiPlain      = "tenants:\n  t-a:\n    mysql_connections: \"70\"\n"
)

func TestPutTenant_EachScheduleEntryJudgedOnce(t *testing.T) {
	t.Parallel()
	big := []string{"22:00-06:00", "1000"}
	dead := func(v string) []string { return append(append([]string(nil), big...), "01:00-03:00", v) }
	covered := func(def string) string {
		return wiSched(def, "06:00-18:00", "70", "18:00-06:00", "80")
	}
	bad := func(v string) string { return wiSched("70", "25:00-26:00", v) }
	cases := []struct {
		name, file, body string
		want             int
		has, lacks       []string
	}{
		{"D1 dead window value abc: new", wiPlain, wiSched("70", dead("abc")...),
			http.StatusBadRequest, []string{wiDead}, []string{wiRefusal}},
		{"D2 dead window value abc: untouched", wiSched("70", dead("abc")...), wiSched("70", dead("abc")...) + wiOther,
			http.StatusOK, []string{wiDeadNotice}, []string{wiRefusal}},
		{"D3 dead window value changed", wiSched("70", dead("abc")...), wiSched("70", dead("abd")...),
			http.StatusBadRequest, []string{wiDead}, []string{wiRefusal}},
		{"D6 dead window value 5 -> abc", wiSched("70", dead("5")...), wiSched("70", dead("abc")...),
			http.StatusBadRequest, []string{wiDead}, []string{wiRefusal}},
		{"D7 covered default abc: new", wiPlain, covered("abc"),
			http.StatusBadRequest, []string{wiDead}, []string{wiRefusal}},
		{"D8 covered default abc: untouched", covered("abc"), covered("abc") + wiOther,
			http.StatusOK, []string{wiDeadNotice}, []string{wiRefusal}},
		{"D10 a window removed: the default abc is served", covered("abc"), wiSched("abc", "06:00-18:00", "70"),
			http.StatusBadRequest, []string{wiRefusal}, nil},
		{"W1 bad window value abc: new", wiPlain, bad("abc"),
			http.StatusBadRequest, []string{wiWindow}, []string{wiRefusal}},
		{"W2 bad window value 5: new", wiPlain, bad("5"),
			http.StatusBadRequest, []string{wiWindow}, nil},
		{"W3 bad window value abc: untouched", bad("abc"), bad("abc") + wiOther,
			http.StatusOK, []string{"NOTICE: tenant=t-a: mysql_connections writes a schedule " + wiWindow}, []string{wiRefusal}},
		{"W4 bad window value changed", bad("abc"), bad("abd"),
			http.StatusBadRequest, []string{wiWindow}, []string{wiRefusal}},
		{"W4b bad window moved", bad("5"), wiSched("70", "26:00-27:00", "5"),
			http.StatusBadRequest, []string{wiWindow}, nil},
		{"W5 window fixed: the value abc is served", bad("abc"), wiSched("70", "01:00-02:00", "abc"),
			http.StatusBadRequest, []string{wiRefusal}, []string{wiWindow}},
		{"R1 the unreadable entry removed, a live abc kept", wiSched("70", "01:00-02:00", "abc", "25:00-26:00", "5"),
			wiSched("70", "01:00-02:00", "abc"),
			http.StatusOK, []string{`NOTICE: tenant=t-a: mysql_connections \"abc\" ` + wiRefusal}, []string{wiWindow}},
		{"L1 live window value abc: new", wiPlain, wiSched("70", "01:00-02:00", "abc"),
			http.StatusBadRequest, []string{wiRefusal}, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			f := newSilentModeFixture(t, c.file)
			w := putTenantIn(t, f, "t-a", c.body)
			body := strings.ReplaceAll(w.Body.String(), `\u0060`, "`")
			if w.Code != c.want {
				t.Fatalf("%d %s, want %d", w.Code, body, c.want)
			}
			for _, s := range c.has {
				if !strings.Contains(body, s) {
					t.Errorf("body lacks %q: %s", s, body)
				}
			}
			for _, s := range c.lacks {
				if strings.Contains(body, s) {
					t.Errorf("body has %q: %s", s, body)
				}
			}
		})
	}
}

// A batch op takes the same ratchet: an unreadable window the file already
// holds does not fail an op on another key; the op carries the notice. (A
// batch patch writes string values only, so it cannot write a new window.)
func TestBatchTenants_WindowInvalidRatchet(t *testing.T) {
	t.Parallel()
	f := newSilentModeFixture(t, wiSched("70", "25:00-26:00", "5"))
	code, results, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`, false)
	if code != http.StatusOK {
		t.Fatalf("status %d, want 200: %s", code, f.lastBody)
	}
	want := "NOTICE: tenant=t-a: mysql_connections writes a schedule " + wiWindow
	if len(results) != 1 || !strings.Contains(strings.Join(results[0].Warnings, "\n"), want) {
		t.Errorf("results %+v, want %q on the op", results, want)
	}
}
