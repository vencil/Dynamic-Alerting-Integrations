package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"go.yaml.in/yaml/v3"
)

// runOn writes *content* to a temp file and runs `da-read alertmanager` on it.
func runOn(t *testing.T, content string) (int, string, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "alertmanager.yml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	rc := run([]string{"alertmanager", path}, strings.NewReader(""), &stdout, &stderr)
	return rc, stdout.String(), stderr.String()
}

// document runs da-read on *in* and returns its one document, as printed.
func document(t *testing.T, in string) string {
	t.Helper()
	rc, out, errOut := runOn(t, in)
	if rc != exitOK || errOut != "" {
		t.Fatalf("%q: rc=%d stderr=%q", in, rc, errOut)
	}
	var got struct {
		Documents []json.RawMessage `json:"documents"`
	}
	if err := json.Unmarshal([]byte(out), &got); err != nil || len(got.Documents) != 1 {
		t.Fatalf("%q: %v; want one document in %q", in, err, out)
	}
	return string(got.Documents[0])
}

const minimal = "route: {receiver: r}\nreceivers: [{name: r}]\n"

// Only the keys the text writes are printed, in the order written; the
// defaults Alertmanager fills in, the values it carries in from `global` and
// the `labels` it gives each receiver are not.
func TestKeepsOnlyTheWrittenKeys(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, want string
	}{
		{"minimal", minimal, `{"route":{"receiver":"r"},"receivers":[{"name":"r"}]}`},
		{"global-defaults-left-out", "global: {resolve_timeout: 1m}\n" + minimal,
			`{"global":{"resolve_timeout":"1m"},"route":{"receiver":"r"},"receivers":[{"name":"r"}]}`},
		// slack_api_url is carried into the slack config by Alertmanager; the
		// slack config does not write api_url, so it is not printed there.
		{"global-carried-value-left-out", "global: {slack_api_url: 'https://hooks.slack.com/g'}\n" +
			"route: {receiver: r}\nreceivers: [{name: r, slack_configs: [{channel: '#c'}]}]\n",
			`{"global":{"slack_api_url":"https://hooks.slack.com/g"},"route":{"receiver":"r"},` +
				`"receivers":[{"name":"r","slack_configs":[{"channel":"#c"}]}]}`},
		// A mapping written as null writes no key.
		{"null-mapping", "global:\n" + minimal, `{"global":{},"route":{"receiver":"r"},"receivers":[{"name":"r"}]}`},
		// encoding/json leaves out an empty `omitempty` field; written, it is
		// printed as its value.
		// An empty list encoding/json leaves out (omitempty) is printed too.
		{"written-empty-lists", minimal + "inhibit_rules: []\ntemplates: []\n",
			`{"route":{"receiver":"r"},"receivers":[{"name":"r"}],"inhibit_rules":[],"templates":[]}`},
		{"written-zero-values", "route:\n  receiver: r\n  continue: false\n  routes:\n" +
			"  - {receiver: r, continue: false, matchers: []}\n" +
			"receivers: [{name: r, slack_configs: [{channel: '', api_url: 'https://h/x', send_resolved: false}]}]\n",
			`{"route":{"receiver":"r","continue":false,"routes":[{"receiver":"r","continue":false,"matchers":[]}]},` +
				`"receivers":[{"name":"r","slack_configs":[{"channel":"","api_url":"https://h/x","send_resolved":false}]}]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := document(t, tc.in); got != tc.want {
				t.Fatalf("document %s\nwant     %s", got, tc.want)
			}
		})
	}
}

// ADR-036 §12's example: `match: {tenant: 010}` is the string "010" to
// Alertmanager's loader (the field is a string, and yaml.v2 takes the text);
// read the Kubernetes way (sigs.k8s.io/yaml.YAMLToJSON, da-crdecode) the same
// text is the number 8 — threshold-exporter's
// cmd/da-crdecode TestConfigMapInnerLayerThroughStandardInput measures that
// side.
func TestValuesAreAlertmanagersReading(t *testing.T) {
	t.Parallel()
	in := "route:\n  receiver: r\n  routes:\n  - {receiver: r, match: {tenant: 010}, match_re: {tenant: 0x1F}}\n" +
		"receivers: [{name: r}]\n"
	want := `{"route":{"receiver":"r","routes":[{"receiver":"r","match":{"tenant":"010"},` +
		`"match_re":{"tenant":"0x1F"}}]},"receivers":[{"name":"r"}]}`
	if got := document(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}

// The fields onboarding reads: routes' match / match_re / matchers,
// inhibit_rules' source/target match and matchers, and the common fields of
// each kind of receiver — secrets printed as their values.
func TestRoutesInhibitRulesAndReceivers(t *testing.T) {
	t.Parallel()
	in := `global:
  resolve_timeout: 5m
route:
  receiver: default
  group_by: [alertname, tenant]
  group_wait: 30s
  routes:
    - match: {tenant: t1}
      match_re: {severity: "crit.*"}
      receiver: t1
      continue: true
    - matchers: ['tenant="t2"', 'severity=~"a|b"']
      receiver: t2
      repeat_interval: 4h
inhibit_rules:
  - source_match: {severity: critical}
    target_match: {severity: warning}
    equal: [tenant]
  - source_matchers: ['x="y"']
    target_matchers: ['z!="w"']
receivers:
  - name: default
  - name: t1
    webhook_configs:
      - url: 'https://example.com/hook?token=s3cret'
        send_resolved: true
    slack_configs:
      - api_url: 'https://hooks.slack.com/services/T/B/s3cret'
        channel: '#alerts'
        title: '{{ .CommonLabels.alertname }}'
    pagerduty_configs:
      - routing_key: 'rk-s3cret'
        severity: critical
  - name: t2
    email_configs:
      - to: 'ops@example.com'
        from: 'am@example.com'
        smarthost: 'smtp.example.com:587'
        auth_password: 'pw-s3cret'
    opsgenie_configs:
      - api_key: 'og-s3cret'
        priority: P1
    msteams_configs:
      - webhook_url: 'https://example.webhook.office.com/x'
    telegram_configs:
      - bot_token: 'tg-s3cret'
        chat_id: -100
`
	want := `{"global":{"resolve_timeout":"5m"},` +
		`"route":{"receiver":"default","group_by":["alertname","tenant"],"group_wait":"30s","routes":[` +
		`{"match":{"tenant":"t1"},"match_re":{"severity":"crit.*"},"receiver":"t1","continue":true},` +
		`{"matchers":["severity=~\"a|b\"","tenant=\"t2\""],"receiver":"t2","repeat_interval":"4h"}]},` +
		`"inhibit_rules":[{"source_match":{"severity":"critical"},"target_match":{"severity":"warning"},"equal":["tenant"]},` +
		`{"source_matchers":["x=\"y\""],"target_matchers":["z!=\"w\""]}],` +
		`"receivers":[{"name":"default"},` +
		`{"name":"t1","webhook_configs":[{"url":"https://example.com/hook?token=s3cret","send_resolved":true}],` +
		`"slack_configs":[{"api_url":"https://hooks.slack.com/services/T/B/s3cret","channel":"#alerts",` +
		`"title":"{{ .CommonLabels.alertname }}"}],` +
		`"pagerduty_configs":[{"routing_key":"rk-s3cret","severity":"critical"}]},` +
		`{"name":"t2","email_configs":[{"to":"ops@example.com","from":"am@example.com",` +
		`"smarthost":"smtp.example.com:587","auth_password":"pw-s3cret"}],` +
		`"opsgenie_configs":[{"api_key":"og-s3cret","priority":"P1"}],` +
		`"msteams_configs":[{"webhook_url":"https://example.webhook.office.com/x"}],` +
		`"telegram_configs":[{"bot_token":"tg-s3cret","chat_id":-100}]}]}`
	if got := document(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}

// A key a `<<` merge brings in counts as written; the merge order is
// go-yaml v2's (a later key wins; in a merged list, the earlier map), which
// Alertmanager's own reading of the values confirms.
func TestMergedKeysAreWritten(t *testing.T) {
	t.Parallel()
	in := "x-base: &base {channel: '#base', title: base}\n" +
		"x-other: &other {channel: '#other', text: other}\n" +
		"route: {receiver: r}\n" +
		"receivers:\n- name: r\n  slack_configs:\n" +
		"  - {<<: *base, api_url: 'https://h/1', title: own}\n" +
		"  - {api_url: 'https://h/2', <<: [*base, *other]}\n"
	// x-base / x-other are not Alertmanager fields: strict loading refuses
	// them, so the anchors go where Alertmanager reads them.
	if rc, _, errOut := runOn(t, in); rc != exitCallerErr || !strings.Contains(errOut, "x-base") {
		t.Fatalf("rc=%d stderr=%q; want the unknown top-level key refused by Alertmanager", rc, errOut)
	}
	in = "route: {receiver: r}\n" +
		"receivers:\n- name: r\n  slack_configs:\n" +
		"  - &base {api_url: 'https://h/0', channel: '#base', title: base}\n" +
		"  - {<<: *base, api_url: 'https://h/1', title: own}\n" +
		"  - {api_url: 'https://h/2', <<: [*base, {channel: '#other', text: other}]}\n"
	want := `{"route":{"receiver":"r"},"receivers":[{"name":"r","slack_configs":[` +
		`{"api_url":"https://h/0","channel":"#base","title":"base"},` +
		`{"api_url":"https://h/1","channel":"#base","title":"own"},` +
		`{"api_url":"https://h/0","channel":"#base","title":"base","text":"other"}]}]}`
	if got := document(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}

// What Alertmanager's loader refuses is refused, with its reason; so is a
// file of several documents, of which Alertmanager reads only the first.
func TestRefusesWhatAlertmanagerRefuses(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, wantErr string
	}{
		{"empty", "", "no route provided in config"},
		{"receivers-without-route", "receivers: [{name: r}]\n", "no routes provided"},
		{"undefined-receiver", "route: {receiver: nope}\nreceivers: [{name: r}]\n", `undefined receiver "nope"`},
		{"unknown-key", minimal + "tenants: {}\n", "field tenants not found"},
		{"continue-at-root", "route: {receiver: r, continue: true}\nreceivers: [{name: r}]\n",
			"cannot have continue in root route"},
		{"bad-matcher", "route: {receiver: r, routes: [{receiver: r, matchers: ['a=~(']}]}\nreceivers: [{name: r}]\n",
			"error parsing regexp"},
		{"two-documents", minimal + "---\n" + minimal, "YAML document 2 of the file has content"},
		{"not-yaml", "route: [\n", "refused by Alertmanager's config loader"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runOn(t, tc.in)
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, tc.wantErr) ||
				!strings.HasPrefix(errOut, programName+": ") {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2, no stdout, stderr with %q", rc, out, errOut, tc.wantErr)
			}
		})
	}
}

// go.mod's `godebug urlstrictcolons=0`: a proxy_url the released amtool
// accepts (#2295) is accepted here too.
func TestURLsAreJudgedAsTheReleasedAlertmanager(t *testing.T) {
	t.Parallel()
	in := "route: {receiver: r}\nreceivers:\n- name: r\n  webhook_configs:\n" +
		"  - url: 'https://example.com/'\n    http_config: {proxy_url: 'http://h:80:3128/'}\n"
	want := `{"route":{"receiver":"r"},"receivers":[{"name":"r","webhook_configs":[` +
		`{"url":"https://example.com/","http_config":{"proxy_url":"http://h:80:3128/"}}]}]}`
	if got := document(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}

// `-` reads standard input: the `alertmanager.yml` string of a ConfigMap,
// whose outer layer da-crdecode read.
func TestReadsStandardInput(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	rc := run([]string{"alertmanager", "-"}, strings.NewReader(minimal), &stdout, &stderr)
	want := `{"documents":[{"route":{"receiver":"r"},"receivers":[{"name":"r"}]}]}` + "\n"
	if rc != exitOK || stdout.String() != want || stderr.Len() != 0 {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want %q", rc, stdout.String(), stderr.String(), want)
	}
	stdout.Reset()
	rc = run([]string{"alertmanager", "-"}, strings.NewReader(""), &stdout, &stderr)
	if rc != exitCallerErr || stdout.Len() != 0 || !strings.HasPrefix(stderr.String(), programName+": "+stdinName+": ") {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2 naming %s", rc, stdout.String(), stderr.String(), stdinName)
	}
}

func TestCallerErrors(t *testing.T) {
	t.Parallel()
	missing := filepath.Join(t.TempDir(), "missing.yml")
	for name, args := range map[string][]string{
		"no-args":      nil,
		"no-file":      {"alertmanager"},
		"unknown-kind": {"prometheus", "-"},
		"three-args":   {"alertmanager", "a", "b"},
		"help":         {"alertmanager", "-h"},
		"empty":        {"alertmanager", ""},
		"missing-file": {"alertmanager", missing},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			var stdout, stderr bytes.Buffer
			if rc := run(args, strings.NewReader(""), &stdout, &stderr); rc != exitCallerErr ||
				stdout.Len() != 0 || stderr.Len() == 0 {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2, a message, no stdout", rc, stdout.String(), stderr.String())
			}
		})
	}
}

type hidden struct {
	Shown  string `yaml:"shown" json:"shown"`
	Hidden string `yaml:"hidden" json:"-"`
	Kept   string `yaml:"kept" json:"kept"`
}

type withList struct {
	Items []hidden `yaml:"items" json:"items"`
}

// A key the text writes that the reading has no place for is refused, never
// dropped. Alertmanager's strict loading refuses unknown keys before this
// walk runs, so the walk is driven directly, on a type whose field is not
// printed and a reading that lacks a written key or list entry.
func TestAWrittenKeyNotInTheReadingIsRefused(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in string
		read     interface{}
		rv       reflect.Value
		wantErr  string
	}{
		{"field-not-printed", "shown: a\nhidden: b\n", map[string]interface{}{"shown": "a"},
			reflect.ValueOf(hidden{Shown: "a", Hidden: "b"}), `hidden: the text writes it`},
		{"field-missing-from-reading", "kept: a\n", map[string]interface{}{},
			reflect.ValueOf(hidden{Kept: "a"}), `kept: the text writes it`},
		{"struct-list-length", "items: [{kept: a}, {kept: b}]\n",
			map[string]interface{}{"items": []interface{}{map[string]interface{}{"kept": "a"}}},
			reflect.ValueOf(withList{Items: []hidden{{Kept: "a"}}}),
			`items: the text writes it, but Alertmanager's reading has no such entry: 2 entries written, 1 read`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var root yaml.Node
			if err := yaml.Unmarshal([]byte(tc.in), &root); err != nil {
				t.Fatal(err)
			}
			w := &walker{c: newComposed()}
			err := w.walk(root.Content[0], tc.read, tc.rv, nil)
			if !errors.Is(err, errNotInReading) || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("err = %v; want errNotInReading with %q", err, tc.wantErr)
			}
		})
	}
}

// The Go type decides how far the text is followed: below a map, a list of
// strings or a type that marshals itself, the reading is printed whole. One
// written matcher entry `{a=b, c=d}` (or `a=b,c=d`) is two matchers to
// Alertmanager; a map's keys are its own. Before, these were refused as
// "written but not in the reading".
func TestOnlyStructsAndStructListsAreFollowed(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, want string
	}{
		{"matchers-braces", "route:\n  receiver: r\n  routes:\n" +
			"  - {receiver: r, matchers: ['{tenant=t1, severity=page}']}\nreceivers: [{name: r}]\n",
			`{"route":{"receiver":"r","routes":[{"receiver":"r","matchers":["severity=\"page\"","tenant=\"t1\""]}]},` +
				`"receivers":[{"name":"r"}]}`},
		{"matchers-comma", "route:\n  receiver: r\n  routes:\n" +
			"  - {receiver: r, matchers: ['tenant=t1,severity=page']}\nreceivers: [{name: r}]\n",
			`{"route":{"receiver":"r","routes":[{"receiver":"r","matchers":["severity=\"page\"","tenant=\"t1\""]}]},` +
				`"receivers":[{"name":"r"}]}`},
		{"inhibit-matchers", minimal + "inhibit_rules:\n- {source_matchers: ['{a=b, c=d}'], target_matchers: ['x=y']}\n",
			`{"route":{"receiver":"r"},"receivers":[{"name":"r"}],"inhibit_rules":[` +
				`{"source_matchers":["a=\"b\"","c=\"d\""],"target_matchers":["x=\"y\""]}]}`},
		// Alertmanager canonicalizes a header name (`a` is `A`): its reading.
		{"email-headers", "route: {receiver: r}\nreceivers:\n- name: r\n  email_configs:\n" +
			"  - {to: a@b.c, from: x@y.z, smarthost: 'h:25', headers: {a: b}}\n",
			`{"route":{"receiver":"r"},"receivers":[{"name":"r","email_configs":[` +
				`{"to":"a@b.c","from":"x@y.z","smarthost":"h:25","headers":{"A":"b"}}]}]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := document(t, tc.in); got != tc.want {
				t.Fatalf("document %s\nwant     %s", got, tc.want)
			}
		})
	}
}

// Alertmanager's loader reads only the first YAML document and drops the
// rest without a word. A later document that holds nothing — a trailing
// `---`, only comments or blank lines, an explicit null — is accepted, as
// da-crdecode skips it; one with content is refused.
func TestLaterDocumentsMustBeEmpty(t *testing.T) {
	t.Parallel()
	want := `{"route":{"receiver":"r"},"receivers":[{"name":"r"}]}`
	for name, in := range map[string]string{
		"trailing-separator": minimal + "---\n",
		"comment-only":       minimal + "---\n# nothing here\n",
		"blank":              minimal + "---\n\n\n---\n",
		"explicit-null":      minimal + "---\nnull\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if got := document(t, in); got != want {
				t.Fatalf("document %s; want %s", got, want)
			}
		})
	}
	for name, in := range map[string]string{
		"second-config": minimal + "---\n" + minimal,
		"third-scalar":  minimal + "---\n---\nx\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runOn(t, in)
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, "has content; Alertmanager reads only the first") {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2 naming the later document", rc, out, errOut)
			}
		})
	}
}

// A map whose values are structs is paired with the text key by key, and each
// value follows the struct rule. `http_headers` (prometheus/common Headers,
// which marshals itself as its inline map): a header whose lists are written
// empty keeps them — encoding/json's omitempty would print `{"X-A":{}}`.
func TestAMapOfStructsIsPairedByKey(t *testing.T) {
	t.Parallel()
	in := "route: {receiver: r}\nreceivers:\n- name: r\n  webhook_configs:\n" +
		"  - url: 'https://example.com/'\n" +
		"    http_config: {http_headers: {X-A: {values: [], secrets: [], files: []}, X-B: {values: [v]}}}\n"
	want := `{"route":{"receiver":"r"},"receivers":[{"name":"r","webhook_configs":[` +
		`{"url":"https://example.com/","http_config":{"http_headers":{` +
		`"X-A":{"values":[],"secrets":[],"files":[]},"X-B":{"values":["v"]}}}}]}]}`
	if got := document(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}

type mapped struct {
	Name  string `yaml:"name,omitempty" json:"name,omitempty"`
	Count int    `yaml:"count,omitempty" json:"count,omitempty"`
}

type withMaps struct {
	ByPointer map[string]*mapped `yaml:"by_pointer" json:"by_pointer"`
	ByValue   map[string]mapped  `yaml:"by_value" json:"by_value"`
}

// The same rule on map values that are struct pointers, driven directly on a
// type of this test: a written zero field comes back, an unwritten one stays
// out, and a written key the reading lacks is refused.
func TestAMapOfStructPointersIsPairedByKey(t *testing.T) {
	t.Parallel()
	walkOn := func(in string, read interface{}, v withMaps) (string, error) {
		var root yaml.Node
		if err := yaml.Unmarshal([]byte(in), &root); err != nil {
			t.Fatal(err)
		}
		w := &walker{c: newComposed()}
		err := w.walk(root.Content[0], read, reflect.ValueOf(v), nil)
		return w.out.String(), err
	}
	v := withMaps{
		ByPointer: map[string]*mapped{"a": {Name: "x"}},
		ByValue:   map[string]mapped{"b": {Count: 2}},
	}
	read := map[string]interface{}{
		"by_pointer": map[string]interface{}{"a": map[string]interface{}{"name": "x"}},
		"by_value":   map[string]interface{}{"b": map[string]interface{}{"count": 2}},
	}
	got, err := walkOn("by_pointer: {a: {name: x, count: 0}}\nby_value: {b: {count: 2}}\n", read, v)
	if err != nil || got != `{"by_pointer":{"a":{"name":"x","count":0}},"by_value":{"b":{"count":2}}}` {
		t.Fatalf("got %s, %v", got, err)
	}
	_, err = walkOn("by_pointer: {a: {name: x}, z: {name: y}}\n", read, v)
	if !errors.Is(err, errNotInReading) || !strings.Contains(err.Error(), "by_pointer.z:") {
		t.Fatalf("err = %v; want errNotInReading naming by_pointer.z", err)
	}
}
