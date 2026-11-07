package handler

// #3267 (ADR-038 decision 12): a threshold value the exporter refuses —
// other than a non-finite number (#3140, strict) — blocks a write that puts
// it there, and is a notice when the tenant file already holds it as the
// exporter reads it, on every write path: PUT, the batches and custom-alerts.

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
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

// GET: a refused threshold value on disk is a validation notice (the
// self-ratchet a reserved value gets, SelfRatchetValues), not a validation
// warning (which says what blocks a write) — API-managed and git-managed alike.
func TestGetTenant_RefusedThresholdIsANotice(t *testing.T) {
	t.Parallel()
	const want = `mysql_connections "abc" is not a threshold the exporter reads`
	for name, file := range map[string]string{
		"api":  "tenants:\n  tx:\n    mysql_connections: \"abc\"\n",
		"git":  "tenants:\n  tx:\n    mysql_connections: \"abc\"\n  ty:\n    mysql_connections: \"70\"\n",
		"null": "tenants:\n  tx:\n    mysql_connections: null\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			configDir := setupConfigDir(t, map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
				"tx.yaml":        file,
			})
			detail := getTenantDetail(t, &Deps{ConfigDir: configDir}, "tx")
			all := strings.Join(append(append([]string(nil), detail.Warnings...), detail.Notices...), "\n")
			if name == "null" {
				if strings.Contains(all, "is not a threshold the exporter reads") {
					t.Errorf("a null is reported: warnings %q notices %q", detail.Warnings, detail.Notices)
				}
				return
			}
			if strings.Contains(strings.Join(detail.Warnings, "\n"), want) ||
				!strings.Contains(strings.Join(detail.Notices, "\n"), "NOTICE: tenant=tx: "+want) {
				t.Errorf("warnings %q notices %q", detail.Warnings, detail.Notices)
			}
		})
	}
}

// A threshold value finding about another tenant of the file goes through
// the same disclosure as a reserved one (DiscloseKeyValidation, #3039): a
// caller who may not read that tenant gets neither its value nor its key,
// only the fixed "another place in the file" notice. Both shapes that put
// another tenant in the file: a shared file, and a file whose stem is not
// the id it declares (id_mismatch: the stem's tenant gets nothing from it,
// #3504, and the declared tenant's findings are the ones disclosed).
func TestGetTenant_OtherTenantsThresholdFindingIsWithheld(t *testing.T) {
	t.Parallel()
	for name, file := range map[string]string{
		"shared":      "tenants:\n  " + orgEnfTenantIn + ":\n    mysql_connections: \"70\"\n  " + orgEnfTenantOut + ":\n    x_note: " + wOutSecret + "\n",
		"id_mismatch": "tenants:\n  " + orgEnfTenantOut + ":\n    x_note: " + wOutSecret + "\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			f := wFixture(t, "reader", file)
			w := wServe(f, GetTenant(f.deps()), rbac.PermRead, "GET", "", nil)
			if w.Code != http.StatusOK || strings.Contains(w.Body.String(), wOutSecret) || strings.Contains(w.Body.String(), "x_note") ||
				!strings.Contains(w.Body.String(), "another place in the file has notices") {
				t.Errorf("%d %s", w.Code, w.Body.String())
			}
		})
	}
}

// #3267 review: the write gate reads both sides as the exporter decodes them
// (cfg.DecodeTenantBody) — a dimensional key in any spelling is the row the
// exporter serves, and a null on disk is no value.
func TestRefusedThreshold_ExporterDecodeBothSides(t *testing.T) {
	t.Parallel()
	spellings := []string{`mysql_connections{a="1",b="2"}`, `mysql_connections{b="2",a="1"}`, `mysql_connections{ a="1" }`}
	for _, k := range spellings {
		for _, v := range []string{"abc", "70:high"} {
			t.Run("PUT new/"+k+"/"+v, func(t *testing.T) {
				t.Parallel()
				f := newSilentModeFixture(t, "tenants:\n  t-a:\n    mysql_connections: \"70\"\n")
				w := putTenantIn(t, f, "t-a", "tenants:\n  t-a:\n    mysql_connections: \"70\"\n    '"+k+"': \""+v+"\"\n")
				if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), rtRefusal) {
					t.Errorf("%d %s", w.Code, w.Body.String())
				}
			})
			t.Run("batch new/"+k+"/"+v, func(t *testing.T) {
				t.Parallel()
				f := newSilentModeFixture(t, "tenants:\n  t-a:\n    mysql_connections: \"70\"\n")
				patch, _ := json.Marshal(map[string]string{k: v})
				code, results, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":`+string(patch)+`}]}`, false)
				if code != http.StatusUnprocessableEntity || len(results) != 1 || results[0].Code != "BAD_REQUEST" ||
					!strings.Contains(results[0].Message, rtRefusal) {
					t.Errorf("%d %+v %s", code, results, f.lastBody)
				}
			})
		}
	}
	t.Run("PUT re-spelled is unchanged", func(t *testing.T) {
		t.Parallel()
		f := newSilentModeFixture(t, "tenants:\n  t-a:\n    'mysql_connections{b=\"2\",a=\"1\"}': \"abc\"\n")
		w := putTenantIn(t, f, "t-a", "tenants:\n  t-a:\n    'mysql_connections{a=\"1\", b=\"2\"}': \"abc\"\n    _silent_mode: warning\n")
		if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), "NOTICE: tenant=t-a: mysql_connections{") {
			t.Errorf("%d %s", w.Code, w.Body.String())
		}
	})
	for _, base := range []string{"mysql_connections: null", "mysql_connections: ~", "mysql_connections:\n      default: null"} {
		for _, v := range []string{"", ":warning"} {
			t.Run("PUT over a null/"+base+"/"+v, func(t *testing.T) {
				t.Parallel()
				f := newSilentModeFixture(t, "tenants:\n  t-a:\n    "+base+"\n")
				w := putTenantIn(t, f, "t-a", "tenants:\n  t-a:\n    mysql_connections: \""+v+"\"\n")
				if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), rtRefusal) {
					t.Errorf("%d %s", w.Code, w.Body.String())
				}
			})
		}
	}
}
