package handler

// #2295: PUT /api/v1/tenants/{id} refuses a receiver the body writes that
// Alertmanager could not load, with 400 INVALID_BODY and one violation per
// problem, in both write modes and before anything is written; POST
// /{id}/validate gives the same verdict (both run gitops' receiver check:
// ReceiverPreflight / the dry-runs). The contract itself (pkg/receiverspec) is
// pinned to the schema and Alertmanager by the shared receiver case table;
// these tests pin the handlers around it.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// putReceiverBody PUTs `_routing` (indented under the tenant) for tenant
// "rs-t" into a fresh git tree without domain policies; pr selects PR
// write-back. Returns the status, the response body and every branch's
// copy of the tenant file (empty when nothing was written anywhere).
func putReceiverBody(t *testing.T, routing string, pr bool) (int, string, string) {
	t.Helper()
	const tenant = "rs-t"
	dir := seedGitTree(t, map[string]string{"_defaults.yaml": "defaults:\n  cpu_usage_percent: 80\n"})
	rb := adminRBAC(t)
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rb, WriteMode: WriteModeDirect}
	if pr {
		d.WriteMode = WriteModePR
		d.PRClient = &mockPlatformClient{}
		d.PRTracker = &mockPlatformTracker{}
	}
	body := "tenants:\n  " + tenant + ":\n    _routing:\n" + routing
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenant, "id", tenant, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(d), rb, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	var written strings.Builder
	refs, err := exec.Command("git", "-C", dir, "for-each-ref", "--format=%(refname:short)", "refs/heads").Output()
	if err != nil {
		t.Fatalf("list branches: %v", err)
	}
	for _, ref := range strings.Fields(string(refs)) {
		if out, err := exec.Command("git", "-C", dir, "show", ref+":"+tenant+".yaml").Output(); err == nil {
			written.WriteString(ref + ": " + string(out))
		}
	}
	return w.Code, w.Body.String(), written.String()
}

const webhookOK = "      receiver:\n        type: webhook\n        url: https://hook.example.com/a\n"

// Plain and quoted `on` as the bearer_token of the webhook receiver above;
// bearerField is where the plain one is refused.
const (
	webhookOnPlain  = webhookOK + "        http_config:\n          bearer_token: on\n"
	webhookOnQuoted = webhookOK + "        http_config:\n          bearer_token: \"on\"\n"
)

var bearerField = []string{"tenants.rs-t._routing.receiver.http_config.bearer_token"}

func TestPutTenant_ReceiverShape(t *testing.T) {
	cases := []struct {
		name, routing string
		fields        []string // the violations' fields, in order; nil = 200
	}{
		{"legal webhook", webhookOK, nil},
		{"legal webhook with send_resolved and one auth", webhookOK +
			"        send_resolved: false\n        http_config:\n          bearer_token: t\n          proxy_url: http://proxy.example:3128\n", nil},
		{"routing without a receiver of its own", "      group_wait: 30s\n", nil},
		// An override with no receiver of its own is NOT judged by this check
		// (it only judges receivers the body writes). It is not a valid
		// override either: the route generator skips it and da-guard reports
		// it. This row pins only that tenant-api lets it through.
		{"override without a receiver is not judged here", webhookOK + "      overrides:\n      - alertname: X\n        group_wait: 1m\n", nil},
		{"unknown main type", "      receiver:\n        type: bogus\n",
			[]string{"tenants.rs-t._routing.receiver.type"}},
		{"webhook without url", "      receiver:\n        type: webhook\n",
			[]string{"tenants.rs-t._routing.receiver.url"}},
		{"scalar receiver", "      receiver: webhook\n",
			[]string{"tenants.rs-t._routing.receiver"}},
		{"send_resolved a YAML 1.1 word", webhookOK + "        send_resolved: yes\n", nil},
		{"send_resolved and http_config null", webhookOK + "        send_resolved:\n        http_config:\n", nil},
		{"proxy_url Alertmanager parses", webhookOK + "        http_config:\n          proxy_url: proxy.example:3128\n", nil},
		{"send_resolved y (deliberately refused)", webhookOK + "        send_resolved: y\n",
			[]string{"tenants.rs-t._routing.receiver.send_resolved"}},
		{"send_resolved not a boolean", webhookOK + "        send_resolved: maybe\n",
			[]string{"tenants.rs-t._routing.receiver.send_resolved"}},
		{"email require_tls not a boolean", "      receiver:\n        type: email\n        to: [a@example.com]\n" +
			"        smarthost: smtp.example.com:587\n        from: b@example.com\n        require_tls: maybe\n",
			[]string{"tenants.rs-t._routing.receiver.require_tls"}},
		{"http_config with two auth methods", webhookOK +
			"        http_config:\n          bearer_token: t\n          basic_auth: {username: u, password: p}\n",
			[]string{"tenants.rs-t._routing.receiver.http_config.bearer_token"}},
		{"http_config proxy_url unparsable", webhookOK + "        http_config:\n          proxy_url: '::not a url'\n",
			[]string{"tenants.rs-t._routing.receiver.http_config.proxy_url"}},
		// #2295: the receiver is read as the route generator's PyYAML reads
		// it — plain `on` / `1:30` are a boolean and an integer, which the
		// generator would skip; quoted they are the strings it takes.
		{"bearer_token plain on", webhookOK + "        http_config:\n          bearer_token: on\n",
			[]string{"tenants.rs-t._routing.receiver.http_config.bearer_token"}},
		{"bearer_token quoted on", webhookOK + "        http_config:\n          bearer_token: \"on\"\n", nil},
		{"bearer_token quoted 1:30", webhookOK + "        http_config:\n          bearer_token: '1:30'\n", nil},
		{"override receiver bearer_token plain 1:30", webhookOK + "      overrides:\n      - alertname: X\n" +
			"        receiver: {type: webhook, url: https://hook.example.com/b, http_config: {bearer_token: 1:30}}\n",
			[]string{"tenants.rs-t._routing.overrides[0].receiver.http_config.bearer_token"}},
		// #2295 review: a key PyYAML reads as a non-string beside the
		// receiver (`on:` a boolean, `1:` an integer, `~:` null, an alias to
		// one) does not make the check fall back to the yaml.v3 reading, where
		// plain `on` is a string. Quoted, it is taken, as the route generator
		// takes it beside the same keys.
		{"plain on beside an on: key", "      on: x\n" + webhookOnPlain, bearerField},
		{"quoted on beside an on: key", "      on: x\n" + webhookOnQuoted, nil},
		{"plain on beside a 1: key", "      1: x\n" + webhookOnPlain, bearerField},
		{"quoted on beside a 1: key", "      1: x\n" + webhookOnQuoted, nil},
		{"plain on beside a ~: key", "      ~: x\n" + webhookOnPlain, bearerField},
		{"quoted on beside a ~: key", "      ~: x\n" + webhookOnQuoted, nil},
		{"plain on beside an alias key", "      1: &k on\n      *k : x\n" + webhookOnPlain, bearerField},
		{"quoted on beside an alias key", "      1: &k on\n      *k : x\n" + webhookOnQuoted, nil},
		{"override plain on beside a 1: key", webhookOK + "      overrides:\n      - alertname: X\n        1: y\n" +
			"        receiver: {type: webhook, url: https://hook.example.com/b, http_config: {bearer_token: on}}\n",
			[]string{"tenants.rs-t._routing.overrides[0].receiver.http_config.bearer_token"}},
		{"override quoted on beside a 1: key", webhookOK + "      overrides:\n      - alertname: X\n        1: y\n" +
			"        receiver: {type: webhook, url: https://hook.example.com/b, http_config: {bearer_token: \"on\"}}\n", nil},
		{"every receiver of the body, one violation each", "      receiver:\n        type: bogus\n" +
			"      overrides:\n      - alertname: X\n        receiver: {type: webhook}\n" +
			"      routes:\n      - match: {severity: critical}\n        receiver: {type: pagerduty, service_key: a, routing_key: b}\n",
			[]string{
				"tenants.rs-t._routing.receiver.type",
				"tenants.rs-t._routing.overrides[0].receiver.url",
				"tenants.rs-t._routing.routes[0].receiver.routing_key",
			}},
	}
	for _, tc := range cases {
		for _, pr := range []bool{false, true} {
			mode := "direct"
			if pr {
				mode = "pr"
			}
			t.Run(tc.name+"/"+mode, func(t *testing.T) {
				code, resp, written := putReceiverBody(t, tc.routing, pr)
				if tc.fields == nil {
					if code != http.StatusOK || written == "" {
						t.Fatalf("status = %d (written %q), want 200 and a write; body: %s", code, written, resp)
					}
					return
				}
				if code != http.StatusBadRequest {
					t.Fatalf("status = %d, want 400; body: %s", code, resp)
				}
				if written != "" {
					t.Errorf("refused PUT wrote the tenant file:\n%s", written)
				}
				var env ErrorResponse
				if err := json.Unmarshal([]byte(resp), &env); err != nil {
					t.Fatalf("unmarshal: %v", err)
				}
				var got []string
				for _, v := range env.Violations {
					got = append(got, v.Field)
					if v.Reason == "" {
						t.Errorf("violation %s has no reason", v.Field)
					}
				}
				if env.Code != CodeInvalidBody || strings.Join(got, "|") != strings.Join(tc.fields, "|") {
					t.Errorf("code %q violations %v, want %s on %v", env.Code, got, CodeInvalidBody, tc.fields)
				}
			})
		}
	}
}

// TestValidateTenant_AgreesWithPut: POST /{id}/validate and PUT /{id} give
// the same verdict on the same body — valid ⇔ PUT 200, invalid ⇔ PUT 400 — in
// both write modes, because both run gitops' receiver check (B3 of #2295).
// Compared through the HTTP handlers, not the Writer, so a check added to
// only one handler shows up here.
func TestValidateTenant_AgreesWithPut(t *testing.T) {
	bodies := map[string]string{
		"legal":                     webhookOK,
		"unknown type":              "      receiver:\n        type: bogus\n",
		"missing url":               "      receiver:\n        type: webhook\n",
		"send_resolved maybe":       webhookOK + "        send_resolved: maybe\n",
		"send_resolved off":         webhookOK + "        send_resolved: off\n",
		"two auth methods":          webhookOK + "        http_config:\n          bearer_token: t\n          basic_auth: {username: u}\n",
		"proxy_url unparsable":      webhookOK + "        http_config:\n          proxy_url: '::x'\n",
		"no_proxy without proxy":    webhookOK + "        http_config:\n          no_proxy: localhost\n",
		"bad override receiver":     webhookOK + "      overrides:\n      - alertname: X\n        receiver: {type: webhook}\n",
		"bad routes receiver":       webhookOK + "      routes:\n      - match: {severity: critical}\n        receiver: {type: pagerduty}\n",
		"override without receiver": webhookOK + "      overrides:\n      - alertname: X\n",
	}
	for name, routing := range bodies {
		for _, pr := range []bool{false, true} {
			mode := "direct"
			if pr {
				mode = "pr"
			}
			t.Run(name+"/"+mode, func(t *testing.T) {
				valid, warnings := validateReceiverBody(t, routing, pr)
				code, resp, _ := putReceiverBody(t, routing, pr)
				switch {
				case valid && code != http.StatusOK:
					t.Errorf("validate says valid, PUT answers %d: %s", code, resp)
				case !valid && code != http.StatusBadRequest:
					t.Errorf("validate says invalid (%v), PUT answers %d: %s", warnings, code, resp)
				}
			})
		}
	}
}

// validateReceiverBody POSTs the same body putReceiverBody PUTs to
// /{id}/validate, on the same kind of tree and write mode.
func validateReceiverBody(t *testing.T, routing string, pr bool) (bool, []string) {
	t.Helper()
	const tenant = "rs-t"
	dir := seedGitTree(t, map[string]string{"_defaults.yaml": "defaults:\n  cpu_usage_percent: 80\n"})
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, WriteMode: WriteModeDirect}
	if pr {
		d.WriteMode = WriteModePR
		d.PRClient = &mockPlatformClient{}
		d.PRTracker = &mockPlatformTracker{}
	}
	body := "tenants:\n  " + tenant + ":\n    _routing:\n" + routing
	req := newRequestWithChiParam("POST", "/api/v1/tenants/"+tenant+"/validate", "id", tenant, bytes.NewBufferString(body))
	w := httptest.NewRecorder()
	ValidateTenant(d)(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("validate status = %d: %s", w.Code, w.Body.String())
	}
	var resp ValidateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return resp.Valid, resp.Warnings
}

// TestPutCustomAlerts_NotRefusedOverReceiverOnDisk (#2295 B-1): the receiver
// check belongs to PUT /tenants/{id} only. A custom-alerts PUT writes the whole
// file through the Writer too, but changes only `_custom_alerts`; a receiver
// already broken on disk must not refuse it (the same reason batch is exempt).
func TestPutCustomAlerts_NotRefusedOverReceiverOnDisk(t *testing.T) {
	const tenant = "rs-ca"
	onDisk := "tenants:\n  " + tenant + ":\n    mysql_connections: \"70\"\n" +
		"    _routing:\n      receiver:\n        type: webhook\n        send_resolved: maybe\n"
	dir := setupConfigDir(t, map[string]string{tenant + ".yaml": onDisk, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	rb := newRBACManager(t, "groups:\n  - name: ops\n    tenants: [\""+tenant+"\"]\n    permissions: [read, write]\n")
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: rb}

	body := `{"base_hash":"` + cfg.ComputeSourceHash([]byte(onDisk)) +
		`","custom_alerts":[{"recipe":"threshold","name":"queue_high","metric":"queue_depth","threshold":"1000","window":"5m"}]}`
	resp := putCustomAlerts(t, deps, tenant, body, "alice@example.com", []string{"ops"})
	if resp.StatusCode != http.StatusOK {
		b, _ := readBody(resp)
		t.Fatalf("status = %d, want 200 (the broken receiver is on disk, not in this write); body: %s", resp.StatusCode, b)
	}
	out, _ := os.ReadFile(filepath.Join(dir, tenant+".yaml"))
	if !strings.Contains(string(out), "queue_high") {
		t.Errorf("custom alert not written:\n%s", out)
	}
	// Control: the same file as a PUT /tenants/{id} body is refused.
	code, resp2, _ := putReceiverBody(t, "      receiver:\n        type: webhook\n        url: https://hook.example.com/a\n        send_resolved: maybe\n", false)
	if code != http.StatusBadRequest {
		t.Errorf("control: PUT of a body with that receiver = %d, want 400: %s", code, resp2)
	}
}
