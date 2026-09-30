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
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// putReceiverBody PUTs `_routing` (indented under the tenant) for tenant
// "rs-t" into a fresh git tree without domain policies; pr selects PR
// write-back. Returns the status, the response body and every branch's
// copy of the tenant file (empty when nothing was written anywhere).
func putReceiverBody(t *testing.T, routing string, pr bool) (int, string, string) {
	t.Helper()
	return putReceiverDoc(t, "tenants:\n  rs-t:\n    _routing:\n"+routing, pr)
}

// putReceiverDoc is putReceiverBody with the whole tenant document as body.
func putReceiverDoc(t *testing.T, body string, pr bool) (int, string, string) {
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
		// #2295 review: a key PyYAML reads as a non-string on the receiver
		// itself or in proxy_connect_header leaves it a mapping (the route
		// generator and Alertmanager take both); in http_config itself
		// Alertmanager refuses the unknown key, so that stays refused.
		{"receiver with a 1: key", webhookOK + "        1: x\n", nil},
		{"proxy_connect_header with an on: key", webhookOK + "        http_config:\n" +
			"          proxy_url: http://p.example.com:3128\n          proxy_connect_header:\n            on: [x]\n", nil},
		{"http_config with a 1: key", webhookOK + "        http_config:\n          1: x\n          bearer_token: t\n",
			[]string{"tenants.rs-t._routing.receiver.http_config"}},
		// #2431: a matcher value the body writes that the route generator's
		// PyYAML does not read as a string — a routes match value, an
		// override alertname / metric_group — is refused the same way;
		// quoted, it is taken.
		{"routes match value plain 1:30", webhookOK + "      routes:\n      - match: {team: 1:30}\n" +
			"        receiver: {type: webhook, url: https://hook.example.com/b}\n",
			[]string{"tenants.rs-t._routing.routes[0].match.team"}},
		{"routes match value quoted 1:30", webhookOK + "      routes:\n      - match: {team: '1:30'}\n" +
			"        receiver: {type: webhook, url: https://hook.example.com/b}\n", nil},
		{"override alertname plain yes, metric_group null", webhookOK + "      overrides:\n      - alertname: yes\n" +
			"        receiver: {type: webhook, url: https://hook.example.com/b}\n      - metric_group: ~\n" +
			"        receiver: {type: webhook, url: https://hook.example.com/c}\n",
			[]string{"tenants.rs-t._routing.overrides[0].alertname", "tenants.rs-t._routing.overrides[1].metric_group"}},
		{"override alertname quoted yes", webhookOK + "      overrides:\n      - alertname: \"yes\"\n" +
			"        receiver: {type: webhook, url: https://hook.example.com/b}\n", nil},
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

// TestPutTenant_OversizeBodyIsTheSizeGates (#2295 review): a body over the
// #1722 per-document cap is answered by that gate — the Writer's 400 naming
// TA_MAX_TENANT_DOC_BYTES, nothing written, as before the receiver check
// existed — and the receiver check does not parse it first (the parse is the
// cost the gate bounds). The body's receiver is one the check refuses, so a
// check that parsed before the gate would answer with its violation instead.
func TestPutTenant_OversizeBodyIsTheSizeGates(t *testing.T) {
	const bad = "tenants:\n  rs-t:\n    _routing:\n      receiver:\n        type: bogus\n"
	var b strings.Builder
	b.WriteString(bad)
	for int64(b.Len()) <= gitops.MaxTenantDocBytes() {
		b.WriteString("#" + strings.Repeat("x", 62) + "\n")
	}
	for _, pr := range []bool{false, true} {
		code, resp, written := putReceiverDoc(t, b.String(), pr)
		if code != http.StatusBadRequest || written != "" ||
			!strings.Contains(resp, "TA_MAX_TENANT_DOC_BYTES") || strings.Contains(resp, "receiver") {
			t.Errorf("pr=%v: status %d, written %q; want 400 from the size gate, no receiver violation, no write; body: %.300s",
				pr, code, written, resp)
		}
	}
	// Control: under the cap the same receiver is refused by the check.
	if code, resp, _ := putReceiverDoc(t, bad, false); code != http.StatusBadRequest ||
		!strings.Contains(resp, "tenants.rs-t._routing.receiver.type") {
		t.Fatalf("control: status %d, want the receiver violation; body: %s", code, resp)
	}
}

// TestPutTenant_GeneratorRepeatedKey (#2295): a key the route generator's
// StrictLoader counts as written twice — an alias key beside its anchor,
// which yaml.v3 does not count — makes the generator refuse the WHOLE file,
// wherever the repeat sits and whichever copy is the bad one. PUT answers it
// as it answers a plain repeated key (400 "invalid YAML: … already defined"
// — "already set" where yaml.v3's struct decode catches it first — nothing
// written, both write modes), and POST /validate agrees. Positions:
// the tenant-file ones revS10 measured going through.
func TestPutTenant_GeneratorRepeatedKey(t *testing.T) {
	const (
		bad  = "{type: webhook}"
		good = "{type: webhook, url: 'https://hook.example.com/g'}"
		b    = "    cpu_usage_percent: \"50\"\n"
		head = "tenants:\n  rs-t:\n" + b
	)
	url := map[string]string{bad: "x", good: "https://hook.example.com/a"}
	positions := map[string]func(x, y string) string{
		"receiver": func(x, y string) string {
			return head + "    _routing:\n      &r receiver : " + x + "\n      *r : " + y + "\n"
		},
		"override receiver": func(x, y string) string {
			return head + "    _routing:\n      receiver: " + good + "\n      overrides:\n        - alertname: X\n          &r receiver : " + x + "\n          *r : " + y + "\n"
		},
		"override alertname": func(x, _ string) string {
			return head + "    _routing:\n      receiver: " + good + "\n      overrides:\n        - &n alertname : X\n          *n : Y\n          receiver: " + x + "\n"
		},
		"route receiver": func(x, y string) string {
			return head + "    _routing:\n      receiver: " + good + "\n      routes:\n        - match: {team: a}\n          &r receiver : " + x + "\n          *r : " + y + "\n"
		},
		"overrides key": func(x, y string) string {
			return head + "    _routing:\n      receiver: " + good + "\n      &o overrides :\n        - alertname: X\n          receiver: " + x + "\n      *o :\n        - alertname: X\n          receiver: " + y + "\n"
		},
		"routes key": func(x, y string) string {
			return head + "    _routing:\n      receiver: " + good + "\n      &o routes :\n        - match: {team: a}\n          receiver: " + x + "\n      *o :\n        - match: {team: a}\n          receiver: " + y + "\n"
		},
		"receiver url": func(x, y string) string {
			return head + "    _routing:\n      receiver:\n        type: webhook\n        &u url : " + url[x] + "\n        *u : " + url[y] + "\n"
		},
		"tenants key": func(x, y string) string {
			return "&t tenants :\n  rs-t:\n" + b + "    _routing:\n      receiver: " + x + "\n*t :\n  rs-t:\n" + b + "    _routing:\n      receiver: " + y + "\n"
		},
		"tenant id": func(x, y string) string {
			return "tenants:\n  &a rs-t :\n" + b + "    _routing:\n      receiver: " + x + "\n  *a :\n" + b + "    _routing:\n      receiver: " + y + "\n"
		},
		"_routing key": func(x, y string) string {
			return head + "    &k _routing :\n      receiver: " + x + "\n    *k :\n      receiver: " + y + "\n"
		},
		"unrelated body key": func(x, _ string) string {
			return "tenants:\n  rs-t:\n    &q cpu_usage_percent : \"50\"\n    *q : \"60\"\n    _routing:\n      receiver: " + good + "\n"
		},
		"two merge keys": func(x, y string) string {
			return "tenants:\n  rs-t:\n" + b + "    _routing:\n      <<: {receiver: " + x + "}\n      <<: {group_wait: 30s}\n"
		},
		"plain repeat (control)": func(x, y string) string {
			return head + "    _routing:\n      receiver: " + x + "\n      receiver: " + y + "\n"
		},
	}
	for name, build := range positions {
		for order, xy := range map[string][2]string{"bad first": {bad, good}, "good first": {good, bad}} {
			doc := build(xy[0], xy[1])
			for _, pr := range []bool{false, true} {
				label := name + ", " + order + fmt.Sprintf(" (pr=%v)", pr)
				code, resp, written := putReceiverDoc(t, doc, pr)
				if code != http.StatusBadRequest || written != "" ||
					!strings.Contains(resp, "invalid YAML") ||
					!strings.Contains(resp, "already defined") && !strings.Contains(resp, "already set") {
					t.Errorf("%s: status %d, written %q; want 400 invalid YAML … already defined, no write; body: %s",
						label, code, written, resp)
				}
				if valid, warnings := validateReceiverDoc(t, doc, pr); valid {
					t.Errorf("%s: validate says valid (%v), PUT refuses", label, warnings)
				}
			}
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
	return validateReceiverDoc(t, "tenants:\n  rs-t:\n    _routing:\n"+routing, pr)
}

// validateReceiverDoc is validateReceiverBody with the whole tenant document
// as body.
func validateReceiverDoc(t *testing.T, body string, pr bool) (bool, []string) {
	t.Helper()
	const tenant = "rs-t"
	dir := seedGitTree(t, map[string]string{"_defaults.yaml": "defaults:\n  cpu_usage_percent: 80\n"})
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, WriteMode: WriteModeDirect}
	if pr {
		d.WriteMode = WriteModePR
		d.PRClient = &mockPlatformClient{}
		d.PRTracker = &mockPlatformTracker{}
	}
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
