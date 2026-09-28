package handler

// #2295: PUT /api/v1/tenants/{id} refuses a receiver the body writes that
// Alertmanager could not load, with 400 INVALID_BODY and one violation per
// problem, in both write modes and before anything is written. The contract
// itself (pkg/receiverspec) is pinned to the schema and Alertmanager by the
// shared receiver case table; these tests pin the handler around it.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
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

func TestPutTenant_ReceiverShape(t *testing.T) {
	cases := []struct {
		name, routing string
		fields        []string // the violations' fields, in order; nil = 200
	}{
		{"legal webhook", webhookOK, nil},
		{"legal webhook with send_resolved and one auth", webhookOK +
			"        send_resolved: false\n        http_config:\n          bearer_token: t\n          proxy_url: http://proxy.example:3128\n", nil},
		{"routing without a receiver of its own", "      group_wait: 30s\n", nil},
		{"override relying on the main receiver", webhookOK + "      overrides:\n      - alertname: X\n        group_wait: 1m\n", nil},
		{"unknown main type", "      receiver:\n        type: bogus\n",
			[]string{"tenants.rs-t._routing.receiver.type"}},
		{"webhook without url", "      receiver:\n        type: webhook\n",
			[]string{"tenants.rs-t._routing.receiver.url"}},
		{"scalar receiver", "      receiver: webhook\n",
			[]string{"tenants.rs-t._routing.receiver"}},
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
