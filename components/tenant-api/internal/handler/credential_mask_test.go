package handler

// #1560 option (d): receiver credentials in a tenant file are masked for a
// caller who may read the tenant but not write it, on every route that
// returns the file's content — GET /tenants/{id}, /effective and /diff — and
// a PUT carrying the placeholder is refused.
//
// The fixture is a git-backed conf.d served through the real RBAC
// middleware. Credentials are CANARY strings, so "leaked" is a substring
// test over the whole response body, not a check of one field.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/credmask"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

const maskTenantID = "mask-probe"

// maskTenantYAML holds a credential in the main receiver, a multi-line one
// (block scalar) in an override, and two more in comments (a line comment on
// a value, and a commented-out line).
const maskTenantYAML = `tenants:
  mask-probe:
    _silent_mode: "warning"
    _routing:
      receiver:
        type: slack
        api_url: "https://hooks.slack.com/services/CANARY-SLACK"
      overrides:
        - alertname: "HighCPU"  # was CANARY-LINE-COMMENT
          receiver:
            type: pagerduty
            routing_key: >-
              CANARY-PD-1
              CANARY-PD-2
    # previous: https://hooks.slack.com/services/CANARY-COMMENT
`

// maskRBAC: viewers read every tenant; writers write every tenant;
// probe-writers write only this fixture's tenant; other-writers write only
// tenants it does not cover.
const maskRBAC = `groups:
  - name: viewers
    tenants: ["*"]
    permissions: [read]
  - name: writers
    tenants: ["*"]
    permissions: [read, write]
  - name: probe-writers
    tenants: ["mask-*"]
    permissions: [read, write]
  - name: other-writers
    tenants: ["other-*"]
    permissions: [read, write]
`

type maskFixture struct {
	dir string
	d   *Deps
	mgr *rbac.Manager
}

func newMaskFixture(t *testing.T, tenantYAML string) *maskFixture {
	t.Helper()
	dir := shortTempDir(t)
	if err := os.WriteFile(filepath.Join(dir, maskTenantID+".yaml"), []byte(tenantYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	initGitRepo(t, dir)
	mgr := newRBACManager(t, maskRBAC)
	return &maskFixture{dir: dir, mgr: mgr, d: &Deps{
		Writer:    gitops.NewWriter(dir, dir),
		WriteMode: WriteModeDirect,
		ConfigDir: dir,
		RBAC:      mgr,
	}}
}

func (f *maskFixture) call(t *testing.T, h http.HandlerFunc, perm rbac.Permission, method, path, groups, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam(method, path, "id", maskTenantID, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", groups)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, f.mgr, perm, TenantIDFromPath).ServeHTTP(w, req)
	return w
}

func (f *maskFixture) get(t *testing.T, groups string) *httptest.ResponseRecorder {
	return f.call(t, GetTenant(f.d), rbac.PermRead, "GET", "/api/v1/tenants/"+maskTenantID, groups, "")
}

func (f *maskFixture) diff(t *testing.T, groups, proposed string) *httptest.ResponseRecorder {
	return f.call(t, DiffTenant(f.d), rbac.PermRead, "POST", "/api/v1/tenants/"+maskTenantID+"/diff", groups, proposed)
}

func (f *maskFixture) put(t *testing.T, groups, body string) *httptest.ResponseRecorder {
	return f.call(t, PutTenant(f.d), rbac.PermWrite, "PUT", "/api/v1/tenants/"+maskTenantID, groups, body)
}

func (f *maskFixture) onDisk(t *testing.T) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(f.dir, maskTenantID+".yaml"))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func decodeJSON(t *testing.T, w *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &m); err != nil {
		t.Fatalf("decode %q: %v", w.Body.String(), err)
	}
	return m
}

func TestCredentialMask_GetTenant(t *testing.T) {
	t.Parallel()
	f := newMaskFixture(t, maskTenantYAML)

	w := f.get(t, "viewers")
	if w.Code != http.StatusOK {
		t.Fatalf("viewer GET: %d %s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "CANARY") {
		t.Fatalf("viewer GET leaks a credential: %s", w.Body.String())
	}
	m := decodeJSON(t, w)
	if m["masked"] != true {
		t.Errorf("viewer GET: masked = %v, want true", m["masked"])
	}
	raw, _ := m["raw_yaml"].(string)
	if strings.Count(raw, credmask.Placeholder) != 2 {
		t.Errorf("viewer raw_yaml should hold 2 placeholders:\n%s", raw)
	}
	if !strings.Contains(raw, "_silent_mode: \"warning\"") && !strings.Contains(raw, "_silent_mode: warning") {
		t.Errorf("viewer raw_yaml lost a non-credential value:\n%s", raw)
	}

	w = f.get(t, "writers")
	if w.Code != http.StatusOK {
		t.Fatalf("writer GET: %d %s", w.Code, w.Body.String())
	}
	m = decodeJSON(t, w)
	if m["raw_yaml"] != maskTenantYAML {
		t.Errorf("writer raw_yaml is not the file verbatim:\n%v", m["raw_yaml"])
	}
	if _, ok := m["masked"]; ok {
		t.Errorf("writer GET carries masked: %v", m["masked"])
	}
}

// R2: an anchor/alias pair can carry a credential through a key the mask
// does not name. Such a file is withheld whole from a viewer.
func TestCredentialMask_GetTenantWithheldWhenUnmaskable(t *testing.T) {
	t.Parallel()
	cases := map[string]string{
		"alias": `tenants:
  mask-probe:
    _metadata:
      owner: &hook "https://hooks.slack.com/services/CANARY-ANCHOR"
    _routing:
      receiver:
        type: slack
        api_url: *hook
`,
		// Not YAML: served with config_error, raw_yaml and source_hash.
		"malformed": "tenants:\n  mask-probe:\n    _routing: {receiver: {api_url: CANARY-BROKEN\n",
	}
	for name, file := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			f := newMaskFixture(t, file)
			w := f.get(t, "viewers")
			if w.Code != http.StatusOK {
				t.Fatalf("viewer GET: %d %s", w.Code, w.Body.String())
			}
			if strings.Contains(w.Body.String(), "CANARY") {
				t.Fatalf("viewer GET leaks a credential: %s", w.Body.String())
			}
			m := decodeJSON(t, w)
			if m["raw_yaml_withheld"] != true || m["raw_yaml"] != "" {
				t.Errorf("viewer GET: raw_yaml_withheld = %v, raw_yaml = %q; want true and empty", m["raw_yaml_withheld"], m["raw_yaml"])
			}
			w = f.get(t, "writers")
			if !strings.Contains(w.Body.String(), "CANARY") {
				t.Errorf("writer GET lost the file: %s", w.Body.String())
			}
		})
	}
}

// custom_alerts is the same file decoded: masked with raw_yaml, and empty
// when raw_yaml is withheld (the decode followed the aliases).
func TestCredentialMask_GetTenantCustomAlerts(t *testing.T) {
	t.Parallel()
	recipe := "    _custom_alerts:\n      - name: probe\n        webhook_url: https://hook.example/CANARY-RECIPE\n"
	plain := "tenants:\n  mask-probe:\n" + recipe
	aliased := "tenants:\n  mask-probe:\n    _metadata: {owner: &h CANARY-ALIAS}\n" +
		"    _custom_alerts:\n      - name: probe\n        description: *h\n"
	for name, file := range map[string]string{"plain": plain, "aliased": aliased} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			f := newMaskFixture(t, file)
			w := f.get(t, "viewers")
			if w.Code != http.StatusOK {
				t.Fatalf("viewer GET: %d %s", w.Code, w.Body.String())
			}
			if strings.Contains(w.Body.String(), "CANARY") {
				t.Errorf("viewer GET leaks a credential through custom_alerts: %s", w.Body.String())
			}
			if w := f.get(t, "writers"); !strings.Contains(w.Body.String(), "CANARY") {
				t.Errorf("writer GET lost the recipe: %s", w.Body.String())
			}
		})
	}
}

func TestCredentialMask_Effective(t *testing.T) {
	t.Parallel()
	f := newMaskFixture(t, maskTenantYAML)
	path := "/api/v1/tenants/" + maskTenantID + "/effective"

	w := f.call(t, GetTenantEffective(f.d), rbac.PermRead, "GET", path, "viewers", "")
	if w.Code != http.StatusOK {
		t.Fatalf("viewer /effective: %d %s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "CANARY") {
		t.Fatalf("viewer /effective leaks a credential: %s", w.Body.String())
	}
	if m := decodeJSON(t, w); m["masked"] != true {
		t.Errorf("viewer /effective: masked = %v, want true", m["masked"])
	}

	w = f.call(t, GetTenantEffective(f.d), rbac.PermRead, "GET", path, "writers", "")
	if !strings.Contains(w.Body.String(), "CANARY-SLACK") {
		t.Errorf("writer /effective lost the credential: %s", w.Body.String())
	}
}

// A yaml.v3 type error quotes the value it could not decode, and that value
// can be a credential: the viewer gets a fixed text instead.
func TestCredentialMask_EffectiveDecodeErrorText(t *testing.T) {
	t.Parallel()
	f := newMaskFixture(t, maskTenantYAML)
	writeFile(t, filepath.Join(f.dir, "_defaults.yaml"), "defaults:\n  cpu: 70\nx: !!int CANARY-DECODE\n")
	path := "/api/v1/tenants/" + maskTenantID + "/effective"

	w := f.call(t, GetTenantEffective(f.d), rbac.PermRead, "GET", path, "writers", "")
	if w.Code != http.StatusInternalServerError || !strings.Contains(w.Body.String(), "CANARY-DECODE") {
		t.Fatalf("writer /effective: %d %s — want the 500 quoting the value (else this test proves nothing)", w.Code, w.Body.String())
	}
	w = f.call(t, GetTenantEffective(f.d), rbac.PermRead, "GET", path, "viewers", "")
	if w.Code != http.StatusInternalServerError || !strings.Contains(w.Body.String(), CodeConfigDecode) {
		t.Fatalf("viewer /effective: %d %s, want 500 %s", w.Code, w.Body.String(), CodeConfigDecode)
	}
	if strings.Contains(w.Body.String(), "CANARY") {
		t.Errorf("viewer /effective decode error quotes the value: %s", w.Body.String())
	}
}

// R1: the viewer's preview must not tell a correct credential guess from a
// wrong one. Before #1560, Writer.Diff compared the raw bytes first, so the
// exact file came back "no diff" and a one-character change did not.
func TestCredentialMask_DiffIsNoGuessingOracle(t *testing.T) {
	t.Parallel()
	f := newMaskFixture(t, maskTenantYAML)
	right := maskTenantYAML
	wrong := strings.Replace(maskTenantYAML, "CANARY-SLACK", "CANARY-GUESS", 1)

	wr, ww := f.diff(t, "viewers", right), f.diff(t, "viewers", wrong)
	if wr.Code != http.StatusOK || ww.Code != http.StatusOK {
		t.Fatalf("viewer diff: %d %s / %d %s", wr.Code, wr.Body.String(), ww.Code, ww.Body.String())
	}
	if wr.Body.String() != ww.Body.String() {
		t.Errorf("viewer diff tells a right guess from a wrong one:\nright: %s\nwrong: %s", wr.Body.String(), ww.Body.String())
	}
	m := decodeJSON(t, ww)
	if m["has_diff"] != false || m["masked"] != true {
		t.Errorf("viewer diff of a credential-only change: has_diff = %v, masked = %v; want false, true", m["has_diff"], m["masked"])
	}

	// The writer's preview is unchanged: it does see the credential change.
	if m := decodeJSON(t, f.diff(t, "writers", wrong)); m["has_diff"] != true {
		t.Errorf("writer diff of a credential change: has_diff = %v, want true", m["has_diff"])
	}
}

// A real change previews for a viewer, with neither side's credentials and
// no server path in the header; the writer's header has no path either.
func TestCredentialMask_DiffShowsChangesNotCredentials(t *testing.T) {
	t.Parallel()
	f := newMaskFixture(t, maskTenantYAML)
	proposed := strings.Replace(maskTenantYAML, `_silent_mode: "warning"`, `_silent_mode: "critical"`, 1)
	proposed = strings.Replace(proposed, "CANARY-PD-1", "CANARY-NEW-PD", 1)

	for _, groups := range []string{"viewers", "writers"} {
		w := f.diff(t, groups, proposed)
		if w.Code != http.StatusOK {
			t.Fatalf("%s diff: %d %s", groups, w.Code, w.Body.String())
		}
		m := decodeJSON(t, w)
		diff, _ := m["diff"].(string)
		if m["has_diff"] != true || !strings.Contains(diff, "critical") {
			t.Errorf("%s diff misses the _silent_mode change:\n%s", groups, diff)
		}
		if strings.Contains(diff, f.dir) || strings.Contains(diff, os.TempDir()) {
			t.Errorf("%s diff names a server path:\n%s", groups, diff)
		}
		if !strings.Contains(diff, "current/"+maskTenantID+".yaml") {
			t.Errorf("%s diff header does not name current/%s.yaml:\n%s", groups, maskTenantID, diff)
		}
		if groups == "viewers" && strings.Contains(w.Body.String(), "CANARY") {
			t.Errorf("viewer diff leaks a credential:\n%s", diff)
		}
	}
}

// R2 / R4 / R5: what the viewer's preview cannot mask is refused, never
// diffed raw.
func TestCredentialMask_DiffRefusals(t *testing.T) {
	t.Parallel()
	f := newMaskFixture(t, maskTenantYAML)

	alias := "tenants:\n  mask-probe:\n    _metadata: {owner: &h x}\n    _routing: {receiver: {type: slack, api_url: *h}}\n"
	w := f.diff(t, "viewers", alias)
	if w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), CodeMaskedPreviewUnavailable) {
		t.Errorf("viewer diff of an aliased proposal: %d %s, want 422 %s", w.Code, w.Body.String(), CodeMaskedPreviewUnavailable)
	}
	if strings.Contains(w.Body.String(), "CANARY") {
		t.Errorf("refusal leaks a credential: %s", w.Body.String())
	}
	if w := f.diff(t, "writers", alias); w.Code != http.StatusOK {
		t.Errorf("writer diff of the same proposal: %d %s, want 200", w.Code, w.Body.String())
	}

	huge := strings.Repeat("a", int(gitops.DefaultTenantDocBytes())+1)
	if w := f.diff(t, "viewers", huge); w.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("viewer diff of an oversize proposal: %d, want 413", w.Code)
	}
}

// R5: a body over the request cap is a 413 for the masked preview, never a
// preview of its truncated head. Only reachable with a cap below the
// tenant-document limit (the document gate catches the rest), so the cap is
// lowered here.
func TestCredentialMask_DiffOverBodyCapIsNotTruncated(t *testing.T) {
	t.Parallel()
	f := newMaskFixture(t, maskTenantYAML)
	f.d.MaxBodyBytes = 64
	proposed := strings.Replace(maskTenantYAML, `_silent_mode: "warning"`, `_silent_mode: "critical"`, 1)
	if w := f.diff(t, "viewers", proposed); w.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("viewer diff over the body cap: %d %s, want 413", w.Code, w.Body.String())
	}
}

func TestCredentialMask_DiffRefusesUnmaskableCurrentFile(t *testing.T) {
	t.Parallel()
	f := newMaskFixture(t, "a: &x 1\nb: *x\n# CANARY-IN-FILE\n")
	w := f.diff(t, "viewers", "a: 1\n")
	if w.Code != http.StatusUnprocessableEntity {
		t.Errorf("viewer diff against an aliased file: %d %s, want 422", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "CANARY") {
		t.Errorf("refusal leaks the file: %s", w.Body.String())
	}
}

// The masked text cannot be written back over the real credentials, however
// the placeholder is spelled — and the read-modify-write of a caller who CAN
// write (the threshold-governance machine identity's shape) keeps them.
func TestCredentialMask_PutRefusesPlaceholder(t *testing.T) {
	t.Parallel()
	f := newMaskFixture(t, maskTenantYAML)
	masked, _ := decodeJSON(t, f.get(t, "viewers"))["raw_yaml"].(string)
	p := credmask.Placeholder

	bodies := map[string]string{
		"the viewer's masked raw_yaml": masked,
		"single-quoted":                strings.Replace(maskTenantYAML, `"https://hooks.slack.com/services/CANARY-SLACK"`, `'`+p+`'`, 1),
		"str tag":                      strings.Replace(maskTenantYAML, `"https://hooks.slack.com/services/CANARY-SLACK"`, `!!str "`+p+`"`, 1),
		"alias": strings.Replace(
			strings.Replace(maskTenantYAML, `_silent_mode: "warning"`, `_silent_mode: "warning"`+"\n    _metadata: {owner: &p \""+p+"\"}", 1),
			`"https://hooks.slack.com/services/CANARY-SLACK"`, `*p`, 1),
	}
	for name, body := range bodies {
		w := f.put(t, "writers", body)
		if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "placeholder") {
			t.Errorf("%s: PUT = %d %s, want 400 naming the placeholder", name, w.Code, w.Body.String())
		}
		if got := f.onDisk(t); got != maskTenantYAML {
			t.Fatalf("%s: a refused PUT changed the file:\n%s", name, got)
		}
	}

	// The dry-run gives the same verdict as the PUT.
	w := f.call(t, ValidateTenant(f.d), rbac.PermRead, "POST", "/api/v1/tenants/"+maskTenantID+"/validate", "writers", masked)
	if m := decodeJSON(t, w); m["valid"] != false || !strings.Contains(w.Body.String(), "placeholder") {
		t.Errorf("/validate of the masked raw_yaml: %d %s, want valid=false naming the placeholder", w.Code, w.Body.String())
	}

	// Writer read-modify-write: raw in, raw out.
	raw, _ := decodeJSON(t, f.get(t, "writers"))["raw_yaml"].(string)
	edited := strings.Replace(raw, `_silent_mode: "warning"`, `_silent_mode: "critical"`, 1)
	if w := f.put(t, "writers", edited); w.Code != http.StatusOK {
		t.Fatalf("writer read-modify-write PUT: %d %s", w.Code, w.Body.String())
	}
	if got := f.onDisk(t); got != edited || !strings.Contains(got, "CANARY-SLACK") {
		t.Errorf("writer read-modify-write did not keep the credentials:\n%s", got)
	}
}

// ⛔ Masked ⇔ cannot PUT: the mask and the PUT gate share one predicate. If
// they drift, a masked reader that CAN write would write the placeholder
// back (refused by the writer, but the client is then stuck), or a writer
// that cannot... — either way the invariant the design rests on is gone.
func TestCredentialMask_MaskedExactlyWhenPutIsRefused(t *testing.T) {
	t.Parallel()
	for _, groups := range []string{"viewers", "writers", "viewers,probe-writers", "viewers,other-writers"} {
		f := newMaskFixture(t, maskTenantYAML)
		m := decodeJSON(t, f.get(t, groups))
		masked := m["masked"] == true
		edited := strings.Replace(maskTenantYAML, `_silent_mode: "warning"`, `_silent_mode: "critical"`, 1)
		w := f.put(t, groups, edited)
		putAllowed := w.Code == http.StatusOK
		if w.Code != http.StatusOK && w.Code != http.StatusForbidden {
			t.Fatalf("%s: PUT = %d %s, want 200 or 403", groups, w.Code, w.Body.String())
		}
		if masked == putAllowed {
			t.Errorf("%s: masked = %v but PUT allowed = %v; want masked exactly when PUT is refused", groups, masked, putAllowed)
		}
	}
}
