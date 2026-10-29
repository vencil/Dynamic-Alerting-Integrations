package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"testing"

	amconfig "github.com/prometheus/alertmanager/config"
	commoncfg "github.com/prometheus/common/config"
	"github.com/prometheus/common/promslog"
	promconfig "github.com/prometheus/prometheus/config"
)

// The move group of HTTPClientConfig (bearer_token, bearer_token_file,
// authorization) in every combination the text can write — each key absent,
// written empty, written with a value, or written null, in both key orders —
// for both loaders. Where the loader refuses, da-read exits 2. Where it
// reads, the JSON has no key twice (checked with a decoder that keeps
// duplicates), `authorization` is printed iff the loader holds one, each
// field printed under it equals the loader's, the loader's credential is
// among them, and a source key is printed only when written empty.

// noDuplicateKeys walks the JSON *b* token by token and returns the first key
// an object holds twice, or "".
func noDuplicateKeys(b []byte) (string, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	var walk func() (string, error)
	walk = func() (string, error) {
		tok, err := dec.Token()
		if err != nil {
			return "", err
		}
		d, ok := tok.(json.Delim)
		if !ok {
			return "", nil
		}
		switch d {
		case '{':
			seen := map[string]bool{}
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return "", err
				}
				k := kt.(string)
				if seen[k] {
					return k, nil
				}
				seen[k] = true
				if dup, err := walk(); dup != "" || err != nil {
					return dup, err
				}
			}
		case '[':
			for dec.More() {
				if dup, err := walk(); dup != "" || err != nil {
					return dup, err
				}
			}
		}
		_, err = dec.Token() // the closing delimiter
		return "", err
	}
	dup, err := walk()
	if err == io.EOF {
		err = nil
	}
	return dup, err
}

type groupCase struct {
	values map[string]string // key -> written YAML value; absent when missing
	order  []string
}

func (c groupCase) mapping(indent string) string {
	var b strings.Builder
	for _, k := range c.order {
		if v, ok := c.values[k]; ok {
			fmt.Fprintf(&b, "%s%s: %s\n", indent, k, v)
		}
	}
	return b.String()
}

func (c groupCase) String() string {
	return strings.TrimSpace(strings.ReplaceAll(c.mapping(""), "\n", "; "))
}

func groupCases() []groupCase {
	states := map[string][]string{
		"bearer_token":      {"", `""`, "s3cret", "~"},
		"bearer_token_file": {"", `""`, "/etc/token", "~"},
		"authorization":     {"", `""`, "{credentials: c}", "~", "{credentials_file: /etc/c}"},
	}
	orders := [][]string{
		{"bearer_token", "bearer_token_file", "authorization"},
		{"authorization", "bearer_token_file", "bearer_token"},
	}
	var out []groupCase
	for _, bt := range states["bearer_token"] {
		for _, btf := range states["bearer_token_file"] {
			for _, au := range states["authorization"] {
				vals := map[string]string{}
				for k, v := range map[string]string{"bearer_token": bt, "bearer_token_file": btf, "authorization": au} {
					if v != "" {
						vals[k] = v
					}
				}
				for _, o := range orders {
					out = append(out, groupCase{values: vals, order: o})
				}
			}
		}
	}
	return out
}

// checkGroup checks the printed http_config object *obj* against the
// loader's *hc* for the written case *c*.
func checkGroup(t *testing.T, c groupCase, obj map[string]interface{}, hc *commoncfg.HTTPClientConfig) {
	t.Helper()
	au, printed := obj["authorization"]
	if hc == nil || hc.Authorization == nil {
		if printed && au != nil {
			t.Errorf("%s: authorization %v printed, the loader has none", c, au)
		}
	} else {
		m, ok := au.(map[string]interface{})
		if !ok {
			t.Errorf("%s: authorization %v; the loader has %+v", c, au, *hc.Authorization)
			return
		}
		loader := map[string]string{"type": hc.Authorization.Type,
			"credentials": string(hc.Authorization.Credentials), "credentials_file": hc.Authorization.CredentialsFile}
		for k, v := range m {
			if s, _ := v.(string); s != loader[k] {
				t.Errorf("%s: authorization.%s = %v, the loader's is %q", c, k, v, loader[k])
			}
		}
		for _, k := range []string{"credentials", "credentials_file"} {
			if loader[k] != "" && m[k] != loader[k] {
				t.Errorf("%s: the loader's authorization.%s %q is not printed (%v)", c, k, loader[k], m)
			}
		}
	}
	for _, s := range []string{"bearer_token", "bearer_token_file"} {
		v, isPrinted := obj[s]
		w, written := c.values[s]
		writtenEmpty := written && (w == `""` || w == "~")
		if isPrinted != writtenEmpty || (isPrinted && v != "") {
			t.Errorf("%s: %s printed=%v (%v); want printed iff written empty, as \"\"", c, s, isPrinted, v)
		}
	}
}

func TestMoveGroupPropertyAlertmanager(t *testing.T) {
	t.Parallel()
	read, refused := 0, 0
	for _, c := range groupCases() {
		in := "route: {receiver: r}\nreceivers:\n- name: r\n  webhook_configs:\n" +
			"  - url: 'https://example.com/'\n    http_config:\n" + c.mapping("      ")
		cfg, loadErr := amconfig.Load(in)
		rc, out, errOut := runOn(t, in)
		if loadErr != nil {
			refused++
			if rc != exitCallerErr || out != "" {
				t.Errorf("%s: Alertmanager refuses (%v) but rc=%d stdout=%q", c, loadErr, rc, out)
			}
			continue
		}
		read++
		if rc != exitOK {
			t.Errorf("%s: Alertmanager reads it but rc=%d stderr=%q", c, rc, errOut)
			continue
		}
		if dup, err := noDuplicateKeys([]byte(out)); dup != "" || err != nil {
			t.Errorf("%s: key %q printed twice (%v): %s", c, dup, err, out)
			continue
		}
		var doc struct {
			Documents []struct {
				Receivers []struct {
					WebhookConfigs []map[string]interface{} `json:"webhook_configs"`
				} `json:"receivers"`
			} `json:"documents"`
		}
		if err := json.Unmarshal([]byte(out), &doc); err != nil {
			t.Fatal(err)
		}
		obj, _ := doc.Documents[0].Receivers[0].WebhookConfigs[0]["http_config"].(map[string]interface{})
		checkGroup(t, c, obj, cfg.Receivers[0].WebhookConfigs[0].HTTPConfig)
	}
	t.Logf("alertmanager: %d combinations read, %d refused by the loader", read, refused)
	if read == 0 || refused == 0 {
		t.Fatalf("read=%d refused=%d: the combinations no longer cover both sides", read, refused)
	}
}

func TestMoveGroupPropertyPrometheus(t *testing.T) {
	t.Parallel()
	read, refused := 0, 0
	for _, c := range groupCases() {
		in := "scrape_configs:\n- job_name: a\n  static_configs: [{targets: [h]}]\n" + c.mapping("  ")
		cfg, loadErr := promconfig.Load(in, promslog.NewNopLogger())
		rc, out, errOut := runPrometheus(t, in)
		if loadErr != nil {
			refused++
			if rc != exitCallerErr || out != "" {
				t.Errorf("%s: Prometheus refuses (%v) but rc=%d stdout=%q", c, loadErr, rc, out)
			}
			continue
		}
		read++
		if rc != exitOK {
			t.Errorf("%s: Prometheus reads it but rc=%d stderr=%q", c, rc, errOut)
			continue
		}
		if dup, err := noDuplicateKeys([]byte(out)); dup != "" || err != nil {
			t.Errorf("%s: key %q printed twice (%v): %s", c, dup, err, out)
			continue
		}
		var doc struct {
			Documents []struct {
				ScrapeConfigs []map[string]interface{} `json:"scrape_configs"`
			} `json:"documents"`
		}
		if err := json.Unmarshal([]byte(out), &doc); err != nil {
			t.Fatal(err)
		}
		checkGroup(t, c, doc.Documents[0].ScrapeConfigs[0], &cfg.ScrapeConfigs[0].HTTPClientConfig)
	}
	t.Logf("prometheus: %d combinations read, %d refused by the loader", read, refused)
	if read == 0 || refused == 0 {
		t.Fatalf("read=%d refused=%d: the combinations no longer cover both sides", read, refused)
	}
}

func TestNoDuplicateKeysSeesDuplicates(t *testing.T) {
	t.Parallel()
	if dup, err := noDuplicateKeys([]byte(`{"a":{"x":1,"x":2}}`)); dup != "x" || err != nil {
		t.Fatalf("dup=%q err=%v", dup, err)
	}
	if dup, err := noDuplicateKeys([]byte(`{"a":[{"x":1},{"x":2}]}`)); dup != "" || err != nil {
		t.Fatalf("dup=%q err=%v", dup, err)
	}
}
