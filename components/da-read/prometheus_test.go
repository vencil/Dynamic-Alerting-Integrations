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

// runPrometheus writes *content* to a temp file and runs `da-read prometheus`.
func runPrometheus(t *testing.T, content string) (int, string, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "prometheus.yml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	rc := run([]string{"prometheus", path}, strings.NewReader(""), &stdout, &stderr)
	return rc, stdout.String(), stderr.String()
}

func promDocument(t *testing.T, in string) string {
	t.Helper()
	rc, out, errOut := runPrometheus(t, in)
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

// Only the keys the text writes are printed, in the order written: the
// defaults Prometheus fills in (scrape_timeout, metrics_path, honor_labels,
// the relabel action / separator / regex, ...) and the values a scrape config
// takes from `global` are not. The service-discovery keys, which live in a
// field yaml never sees and reach the YAML through ScrapeConfig's own
// MarshalYAML, are walked like any other.
func TestPrometheusKeepsOnlyTheWrittenKeys(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, want string
	}{
		{"empty", "", `{}`},
		{"global", "global: {scrape_interval: 15s}\n", `{"global":{"scrape_interval":"15s"}}`},
		{"null-global", "global:\n", `{"global":{}}`},
		{"static-job", "scrape_configs:\n- job_name: p\n  static_configs: [{targets: ['localhost:9090'], labels: {a: '010'}}]\n",
			`{"scrape_configs":[{"job_name":"p","static_configs":[{"targets":["localhost:9090"],"labels":{"a":"010"}}]}]}`},
		{"kubernetes-sd", "scrape_configs:\n- job_name: k\n  kubernetes_sd_configs:\n  - role: service\n" +
			"    namespaces: {names: [monitoring]}\n  relabel_configs:\n" +
			"  - {source_labels: [__meta_kubernetes_namespace], action: Keep, regex: 'db-.+'}\n" +
			"  - {source_labels: [__meta_kubernetes_namespace], target_label: tenant}\n",
			`{"scrape_configs":[{"job_name":"k","kubernetes_sd_configs":[{"role":"service",` +
				`"namespaces":{"names":["monitoring"]}}],"relabel_configs":[` +
				`{"source_labels":["__meta_kubernetes_namespace"],"action":"keep","regex":"db-.+"},` +
				`{"source_labels":["__meta_kubernetes_namespace"],"target_label":"tenant"}]}]}`},
		// The values are Prometheus' reading: a duration in its own
		// spelling, a secret as its value, a written zero kept.
		{"values", "scrape_configs:\n- job_name: v\n  scrape_interval: 90s\n  honor_labels: false\n" +
			"  basic_auth: {username: u, password: s3cret}\n  static_configs: [{targets: [h]}]\n",
			`{"scrape_configs":[{"job_name":"v","scrape_interval":"1m30s","honor_labels":false,` +
				`"basic_auth":{"username":"u","password":"s3cret"},"static_configs":[{"targets":["h"]}]}]}`},
		{"http-headers", "scrape_configs:\n- job_name: h\n  http_headers: {X-A: {values: [v]}}\n" +
			"  static_configs: [{targets: [h]}]\n",
			`{"scrape_configs":[{"job_name":"h","http_headers":{"X-A":{"values":["v"]}},` +
				`"static_configs":[{"targets":["h"]}]}]}`},
		{"alerting", "alerting:\n  alertmanagers:\n  - static_configs: [{targets: ['am:9093']}]\n",
			`{"alerting":{"alertmanagers":[{"static_configs":[{"targets":["am:9093"]}]}]}}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := promDocument(t, tc.in); got != tc.want {
				t.Fatalf("document %s\nwant     %s", got, tc.want)
			}
		})
	}
}

// A key a `<<` merge brings in counts as written, by go-yaml v2's rules,
// which Prometheus' own reading of the values confirms: two `<<` in one
// mapping both merge, a later key replaces an earlier one (a `<<` written
// after an explicit key replaces it), and in a merged list the earlier
// mapping wins.
func TestPrometheusMergedKeysAreWritten(t *testing.T) {
	t.Parallel()
	in := "scrape_configs:\n- job_name: m\n  static_configs: [{targets: [h]}]\n  relabel_configs:\n" +
		"  - &base {source_labels: [a], target_label: base, replacement: base}\n" +
		"  - {<<: *base, target_label: own}\n" +
		"  - {target_label: own, <<: *base}\n" +
		"  - {<<: [{target_label: first}, {target_label: second, regex: '(x)'}], source_labels: [b]}\n" +
		"  - {<<: {target_label: one}, <<: {replacement: two}}\n"
	want := `{"scrape_configs":[{"job_name":"m","static_configs":[{"targets":["h"]}],"relabel_configs":[` +
		`{"source_labels":["a"],"target_label":"base","replacement":"base"},` +
		`{"source_labels":["a"],"target_label":"own","replacement":"base"},` +
		`{"target_label":"base","source_labels":["a"],"replacement":"base"},` +
		`{"target_label":"first","regex":"(x)","source_labels":["b"]},` +
		`{"target_label":"one","replacement":"two"}]}]}`
	if got := promDocument(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
	// Where the rules differ in what is written: a `<<` after the job's own
	// one-group static_configs replaces it with the merged two groups, and
	// Prometheus reads two. Composed by go-yaml v3's rules, the written list
	// would be the job's own and the walk would refuse it.
	in = "scrape_configs:\n- {job_name: s, static_configs: [{targets: [a]}], " +
		"<<: {static_configs: [{targets: [b]}, {targets: [c]}]}}\n"
	want = `{"scrape_configs":[{"job_name":"s","static_configs":[{"targets":["b"]},{"targets":["c"]}]}]}`
	if got := promDocument(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}

// What Prometheus' loader refuses is refused, with its reason; so is a file
// whose second document has content, and an external label whose value
// Prometheus takes from its own environment.
func TestPrometheusRefusesWhatPrometheusRefuses(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, wantErr string
	}{
		{"unknown-key", "scrape_configz: []\n", "field scrape_configz not found"},
		{"duplicate-job", "scrape_configs:\n- {job_name: a, static_configs: [{targets: [h]}]}\n" +
			"- {job_name: a, static_configs: [{targets: [h]}]}\n", `found multiple scrape configs with job name "a"`},
		{"bad-relabel-action", "scrape_configs:\n- job_name: a\n  relabel_configs: [{action: nope}]\n",
			"unknown relabel action"},
		{"kubernetes-sd-role", "scrape_configs:\n- job_name: a\n  kubernetes_sd_configs: [{role: nope}]\n",
			"unknown Kubernetes SD role"},
		{"second-document", "global: {scrape_interval: 15s}\n---\nscrape_configs: []\n",
			"YAML document 2 of the file has content; Prometheus reads only the first"},
		{"external-label-from-env", "global: {external_labels: {cluster: '${CLUSTER}', plain: x}}\n",
			"global.external_labels [cluster]: Prometheus expands the value from its own environment"},
		{"not-yaml", "global: [\n", "refused by Prometheus' config loader"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runPrometheus(t, tc.in)
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, tc.wantErr) ||
				!strings.HasPrefix(errOut, programName+": ") {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2, no stdout, stderr with %q", rc, out, errOut, tc.wantErr)
			}
		})
	}
}

// A later document that holds nothing is accepted, as for Alertmanager; `$$`
// in an external label is a literal `$`, not the environment.
func TestPrometheusAcceptsEmptyLaterDocumentsAndEscapedDollars(t *testing.T) {
	t.Parallel()
	for in, want := range map[string]string{
		"global: {scrape_interval: 15s}\n---\n# nothing\n---\n": `{"global":{"scrape_interval":"15s"}}`,
		"global: {external_labels: {price: '$$5'}}\n":           `{"global":{"external_labels":{"price":"$5"}}}`,
	} {
		if got := promDocument(t, in); got != want {
			t.Fatalf("%q: document %s\nwant     %s", in, got, want)
		}
	}
}

func TestPrometheusReadsStandardInput(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	rc := run([]string{"prometheus", "-"}, strings.NewReader("global: {scrape_interval: 1m}\n"), &stdout, &stderr)
	want := `{"documents":[{"global":{"scrape_interval":"1m"}}]}` + "\n"
	if rc != exitOK || stdout.String() != want || stderr.Len() != 0 {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want %q", rc, stdout.String(), stderr.String(), want)
	}
}

type ywInner struct {
	Kept string `yaml:"kept"`
}

type ywOuter struct {
	Shown  string    `yaml:"shown"`
	Hidden string    `yaml:"-"`
	Items  []ywInner `yaml:"items"`
}

// A key the text writes that the walk has no place for is refused, never
// dropped. Prometheus' strict loading refuses unknown keys before the walk
// runs, so the walk is driven directly on a type of this test.
func TestYAMLWalkRefusesAWrittenKeyNotInTheReading(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in string
		v        ywOuter
		wantErr  string
	}{
		{"field-yaml-dash", "shown: a\nHidden: b\n", ywOuter{Shown: "a"}, `Hidden: the text writes it`},
		{"unknown", "nope: a\n", ywOuter{}, `nope: the text writes it`},
		{"list-length", "items: [{kept: a}, {kept: b}]\n", ywOuter{Items: []ywInner{{Kept: "a"}}},
			`items: the text writes it, but the loader's reading has no such entry: 2 entries written, 1 read`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var root yaml.Node
			if err := yaml.Unmarshal([]byte(tc.in), &root); err != nil {
				t.Fatal(err)
			}
			w := &yamlWalker{c: newComposedWith(mergeV2), f: formatV2}
			err := w.walk(root.Content[0], reflect.ValueOf(tc.v), nil)
			if !errors.Is(err, errNotInReading) || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("err = %v; want errNotInReading with %q", err, tc.wantErr)
			}
		})
	}
}
