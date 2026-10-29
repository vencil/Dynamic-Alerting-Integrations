package main

import (
	"strings"
	"testing"
)

// A URL's password is printed: config.URL's own MarshalYAML writes `xxxxx`
// (Redacted), and secrets are read as their values. remote_write's `url`,
// a scrape config's `proxy_url` and kubernetes_sd's `api_server` are all
// prometheus/common config.URL.
func TestPrometheusURLsKeepTheirPassword(t *testing.T) {
	t.Parallel()
	in := "remote_write:\n- url: http://u:pw@h/w\n" + // trufflehog:ignore
		"scrape_configs:\n- job_name: a\n  proxy_url: 'http://p:pw3@px:3128'\n" + // trufflehog:ignore
		"  kubernetes_sd_configs:\n  - {role: pod, api_server: 'http://x:pw2@k:8080'}\n" // trufflehog:ignore
	want := `{"remote_write":[{"url":"http://u:pw@h/w"}],"scrape_configs":[{"job_name":"a",` + // trufflehog:ignore
		`"proxy_url":"http://p:pw3@px:3128","kubernetes_sd_configs":[{"role":"pod",` + // trufflehog:ignore
		`"api_server":"http://x:pw2@k:8080"}]}]}` // trufflehog:ignore
	if got := promDocument(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}

// config.Load panics on `alerting: {alertmanagers: [~]}` (it dereferences
// the nil *AlertmanagerConfig). The file is refused like any loader error:
// rc 2, nothing on stdout, a message naming the loader, no goroutine dump.
func TestPrometheusLoaderPanicIsARefusal(t *testing.T) {
	t.Parallel()
	rc, out, errOut := runPrometheus(t, "alerting:\n  alertmanagers: [~]\n")
	if rc != exitCallerErr || out != "" ||
		!strings.Contains(errOut, "Prometheus' config loader panicked on this input") ||
		strings.Contains(errOut, "goroutine") || strings.Count(errOut, "\n") != 1 {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2 and one line naming the loader's panic", rc, out, errOut)
	}
}

// Any panic of a reader refuses that file; in a rule batch the other files
// are still read.
func TestAPanicRefusesOnlyThatFile(t *testing.T) {
	t.Parallel()
	_, err := guarded(func([]byte) ([]byte, error) { panic("boom") }, nil)
	if err == nil || !strings.Contains(err.Error(), "da-read panicked while reading this file") ||
		!strings.Contains(err.Error(), "boom") {
		t.Fatalf("err = %v", err)
	}
}

// A later document that cannot be read is refused by da-read's own rule
// (Prometheus loads only the first document), not reported as the loader's.
func TestUnreadableLaterDocumentIsDaReadsRefusal(t *testing.T) {
	t.Parallel()
	rc, out, errOut := runPrometheus(t, "global: {scrape_interval: 5s}\n---\na: [\n")
	if rc != exitCallerErr || out != "" ||
		!strings.Contains(errOut, "YAML document 2 of the file cannot be read") ||
		strings.Contains(errOut, "refused by Prometheus' config loader") {
		t.Fatalf("prometheus: rc=%d stdout=%q stderr=%q", rc, out, errOut)
	}
	rrc, rout, _, rerr := runRulesOn(t, "groups: []\n---\na: [\n")
	if rrc != exitCallerErr || rout.Files[0].Problem == nil ||
		!strings.Contains(rout.Files[0].Problem.Message, "YAML document 2 of the file cannot be read") ||
		strings.Contains(rerr, "refused by Prometheus' rule file loader") {
		t.Fatalf("rules: rc=%d out=%+v stderr=%q", rrc, rout, rerr)
	}
}

// scrape_config_files is not read by config.Load; Prometheus loads scrape
// configs from those files later. A reading without them is not the scrape
// configs Prometheus runs, so the file is refused, naming the key.
func TestPrometheusRefusesScrapeConfigFiles(t *testing.T) {
	t.Parallel()
	rc, out, errOut := runPrometheus(t, "scrape_config_files: [sc/a.yml]\n"+
		"scrape_configs:\n- job_name: inline\n  static_configs: [{targets: ['i:1']}]\n")
	if rc != exitCallerErr || out != "" || !strings.Contains(errOut, "scrape_config_files [sc/a.yml]") ||
		!strings.Contains(errOut, "da-read does not read these files") {
		t.Fatalf("rc=%d stdout=%q stderr=%q", rc, out, errOut)
	}
	// Written empty, there is nothing unread.
	if got := promDocument(t, "scrape_config_files: []\n"); got != `{"scrape_config_files":[]}` {
		t.Fatalf("document %s", got)
	}
}

// Null entries read as their loader reads them. go-yaml v3 (rulefmt) drops a
// null group or rule — rulefmt loads `groups: [~]` as no group — and the walk
// pairs the text with what is left. go-yaml v2 (config.Load) keeps a null
// entry as the zero value, and Prometheus refuses the null sections it
// checks; both are measured here.
func TestNullEntriesAreReadAsTheLoaderReadsThem(t *testing.T) {
	t.Parallel()
	for in, want := range map[string]string{
		"groups: [~]\n": `{"groups":[]}`,
		"groups:\n- ~\n- name: g\n  rules: [{record: a, expr: up}]\n":     `{"groups":[{"name":"g","rules":[{"record":"a","expr":"up"}]}]}`,
		"groups:\n- name: g\n  rules: [~, {record: a, expr: up}, null]\n": `{"groups":[{"name":"g","rules":[{"record":"a","expr":"up"}]}]}`,
	} {
		if got := ruleDocument(t, in); got != want {
			t.Errorf("%q: document %s\nwant     %s", in, got, want)
		}
	}
	if got := promDocument(t, "rule_files: [a.yml, ~]\n"); got != `{"rule_files":["a.yml",""]}` {
		t.Errorf("rule_files: document %s", got)
	}
	for in, wantErr := range map[string]string{
		"scrape_configs: [~]\n":                                          "empty or null scrape config section",
		"scrape_configs:\n- job_name: a\n  static_configs: [~]\n":        "empty or null section in static_configs",
		"scrape_configs:\n- job_name: a\n  kubernetes_sd_configs: [~]\n": "empty or null section in kubernetes_sd_configs",
	} {
		rc, out, errOut := runPrometheus(t, in)
		if rc != exitCallerErr || out != "" || !strings.Contains(errOut, wantErr) {
			t.Errorf("%q: rc=%d stdout=%q stderr=%q; want %q", in, rc, out, errOut, wantErr)
		}
	}
}
