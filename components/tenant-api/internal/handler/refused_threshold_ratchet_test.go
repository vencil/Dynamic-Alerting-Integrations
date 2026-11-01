package handler

// #3267 (ADR-038 decision 12): a threshold value the exporter refuses —
// other than a non-finite number (#3140, strict) — blocks a write that puts
// it there, and is a notice when the tenant file already holds it as the
// exporter reads it, on every write path: PUT, the batches and custom-alerts.

import (
	"net/http"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const (
	rtAbc       = "tenants:\n  t-a:\n    mysql_connections: \"abc\"\n"
	rtNotice    = `NOTICE: tenant=t-a: mysql_connections \"abc\" is not a threshold the exporter reads`
	rtRefusal   = "is not a threshold the exporter reads"
	rtNonFinite = "is not a finite number"
)

func TestPutTenant_RefusedThresholdRatchet(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, file, id, body string
		want                 int
		text                 string
	}{
		{"unchanged is a notice", rtAbc, "t-a", rtAbc + "    _silent_mode: warning\n", http.StatusOK, rtNotice},
		{"unquoted, spaced, suffixed alike", rtAbc, "t-a",
			"tenants:\n  t-a:\n    mysql_connections: ' abc:WARNING '\n    _silent_mode: warning\n", http.StatusOK, "NOTICE: tenant=t-a: mysql_connections"},
		{"changed is refused", rtAbc, "t-a", "tenants:\n  t-a:\n    mysql_connections: \"abd\"\n", http.StatusBadRequest, rtRefusal},
		{"new on a readable value is refused", "tenants:\n  t-a:\n    mysql_connections: \"70\"\n", "t-a",
			"tenants:\n  t-a:\n    mysql_connections: \"1e999\"\n", http.StatusBadRequest, rtRefusal},
		{"another tenant's value is not this one's", rtAbc, "t-b",
			"tenants:\n  t-b:\n    mysql_connections: \"abc\"\n", http.StatusBadRequest, rtRefusal},
		{"no file before is strict", rtAbc, "t-new",
			"tenants:\n  t-new:\n    mysql_connections: \"abc\"\n", http.StatusBadRequest, rtRefusal},
		{"unchanged NaN is still refused (#3140)", "tenants:\n  t-a:\n    mysql_connections: \"NaN\"\n", "t-a",
			"tenants:\n  t-a:\n    mysql_connections: \"NaN\"\n    _silent_mode: warning\n", http.StatusBadRequest, rtNonFinite},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			f := newSilentModeFixture(t, c.file)
			w := putTenantIn(t, f, c.id, c.body)
			if w.Code != c.want || !strings.Contains(w.Body.String(), c.text) {
				t.Errorf("%d %s, want %d with %q", w.Code, w.Body.String(), c.want, c.text)
			}
		})
	}
}

func TestBatchTenants_RefusedThresholdRatchet(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, file, patch string
		want              int
	}{
		{"another key: the op passes with the notice", rtAbc, `{"_silent_mode":"warning"}`, http.StatusOK},
		{"the same value again: passes with the notice", rtAbc, `{"mysql_connections":"abc"}`, http.StatusOK},
		{"a changed value fails as the op", rtAbc, `{"mysql_connections":"abd"}`, http.StatusUnprocessableEntity},
		{"a new value fails as the op", "tenants:\n  t-a:\n    mysql_connections: \"70\"\n", `{"mysql_connections":"70:high"}`, http.StatusUnprocessableEntity},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			f := newSilentModeFixture(t, c.file)
			code, results, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":`+c.patch+`}]}`, false)
			if code != c.want {
				t.Fatalf("status %d, want %d: %s", code, c.want, f.lastBody)
			}
			if c.want == http.StatusOK {
				if len(results) != 1 || !strings.Contains(strings.Join(results[0].Warnings, "\n"), strings.ReplaceAll(rtNotice, `\"`, `"`)) {
					t.Errorf("results %+v, want the notice on the op", results)
				}
				return
			}
			if len(results) != 1 || results[0].Code != "BAD_REQUEST" || !strings.Contains(results[0].Message, rtRefusal) {
				t.Errorf("results %+v", results)
			}
		})
	}
}

// custom-alerts writes the whole tenant file it merged: a threshold value the
// file already holds is a notice there too, and a non-finite one refuses.
func TestPutCustomAlerts_RefusedThresholdRatchet(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		file string
		want int
		text string
	}{
		{rtAbc, http.StatusOK, rtNotice},
		{"tenants:\n  t-a:\n    mysql_connections: \"NaN\"\n", http.StatusBadRequest, rtNonFinite},
	} {
		f := newSilentModeFixture(t, c.file)
		body := `{"base_hash":"` + cfg.ComputeSourceHash([]byte(c.file)) + `","custom_alerts":[{"recipe":"threshold","name":"q",` +
			`"metric":"queue_depth","threshold":"1000","window":"5m"}]}`
		resp := putCustomAlerts(t, f.deps, "t-a", body, "alice@example.com", []string{"admins"})
		b, _ := readBody(resp)
		if resp.StatusCode != c.want || !strings.Contains(b, c.text) {
			t.Errorf("file %q: %d %s, want %d with %q", c.file, resp.StatusCode, b, c.want, c.text)
		}
	}
}

// The four batch endpoints (both write modes) alike: a new refused value
// fails the op (422, op code BAD_REQUEST, nothing written); one the file
// already holds passes with the notice; a value the exporter reads passes.
func TestBatchEndpoints_RefusedThresholdRatchet(t *testing.T) {
	t.Parallel()
	own := "tenants:\n" + paritySec(parityIn, "warning")
	held := "tenants:\n  " + parityIn + ":\n    _silent_mode: \"warning\"\n    mysql_connections: \"abc\"\n"
	notice := `NOTICE: tenant=` + parityIn + `: mysql_connections \"abc\" is not a threshold the exporter reads`
	for _, ep := range batchEndpoints {
		for _, v := range refusedThresholdValues {
			t.Run("new/"+v+"/"+ep.name, func(t *testing.T) {
				t.Parallel()
				w, moved := serveBatch(t, nonFiniteTree(own), ep, map[string]string{"mysql_connections": v})
				if w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), `"code":"BAD_REQUEST"`) ||
					!strings.Contains(w.Body.String(), rtRefusal) || moved {
					t.Errorf("%d moved=%v %s", w.Code, moved, w.Body.String())
				}
			})
		}
		for _, v := range acceptedThresholdValues {
			t.Run("accepted/"+v+"/"+ep.name, func(t *testing.T) {
				t.Parallel()
				if w, _ := serveBatch(t, nonFiniteTree(own), ep, map[string]string{"mysql_connections": v}); w.Code != http.StatusOK {
					t.Errorf("%d %s", w.Code, w.Body.String())
				}
			})
		}
		t.Run("held/"+ep.name, func(t *testing.T) {
			t.Parallel()
			w, _ := serveBatch(t, nonFiniteTree(held), ep, map[string]string{"_silent_mode": "critical"})
			if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), notice) {
				t.Errorf("%d %s", w.Code, w.Body.String())
			}
		})
	}
}
