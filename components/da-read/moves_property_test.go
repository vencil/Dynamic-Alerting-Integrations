package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"reflect"
	"strings"
	"testing"

	amconfig "github.com/prometheus/alertmanager/config"
	commoncfg "github.com/prometheus/common/config"
	"github.com/prometheus/common/promslog"
	promconfig "github.com/prometheus/prometheus/config"
	"go.yaml.in/yaml/v3"
)

// The move group of HTTPClientConfig (bearer_token, bearer_token_file,
// authorization) in every combination the text can write — each key absent,
// written empty, written with a value, or written null, in every order of
// the three, and with one of them brought in by a `<<` merge written before
// or after the others — for both loaders, inside a mapping that also holds
// keys outside the group. Where the loader refuses, da-read exits 2. Where
// it reads:
//
//   - the JSON has no key twice (a decoder that keeps duplicates);
//   - the printed object's keys are, in order, the written keys (as go-yaml
//     v2 composes them, `<<` included) with the group printed as the loader
//     read it: `authorization` once, at the place of the first group key
//     written, iff the loader holds one or the text writes it; a source the
//     loader moved (written non-empty) left out; a source written empty kept;
//   - each field printed under `authorization` equals the loader's, and the
//     loader's credential is among them; a printed source is "".

// jsonObj is a JSON object with its keys in order.
type jsonObj struct {
	keys []string
	vals map[string]interface{}
}

// decodeOrdered decodes the JSON *b* keeping each object's key order, and
// returns the first key an object holds twice ("" when none).
func decodeOrdered(b []byte) (interface{}, string, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	dup := ""
	var value func() (interface{}, error)
	value = func() (interface{}, error) {
		tok, err := dec.Token()
		if err != nil {
			return nil, err
		}
		d, ok := tok.(json.Delim)
		if !ok {
			return tok, nil
		}
		switch d {
		case '{':
			o := &jsonObj{vals: map[string]interface{}{}}
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return nil, err
				}
				k := kt.(string)
				if _, seen := o.vals[k]; seen && dup == "" {
					dup = k
				}
				v, err := value()
				if err != nil {
					return nil, err
				}
				if _, seen := o.vals[k]; !seen {
					o.keys = append(o.keys, k)
				}
				o.vals[k] = v
			}
			_, err = dec.Token()
			return o, err
		case '[':
			var a []interface{}
			for dec.More() {
				v, err := value()
				if err != nil {
					return nil, err
				}
				a = append(a, v)
			}
			_, err = dec.Token()
			return a, err
		}
		return nil, fmt.Errorf("unexpected %v", d)
	}
	v, err := value()
	if err == io.EOF {
		err = nil
	}
	return v, dup, err
}

// at follows *path* (object keys and list indexes) from *v*.
func at(v interface{}, path ...interface{}) *jsonObj {
	for _, p := range path {
		switch s := p.(type) {
		case string:
			o, ok := v.(*jsonObj)
			if !ok {
				return nil
			}
			v = o.vals[s]
		case int:
			a, ok := v.([]interface{})
			if !ok || s >= len(a) {
				return nil
			}
			v = a[s]
		}
	}
	o, _ := v.(*jsonObj)
	return o
}

var groupKeys = []string{"bearer_token", "bearer_token_file", "authorization"}

var groupStates = map[string][]string{
	"bearer_token":      {"", `""`, "s3cret", "~"},
	"bearer_token_file": {"", `""`, "/etc/token", "~"},
	"authorization":     {"", `""`, "{credentials: c}", "~", "{credentials_file: /etc/c}"},
}

// anchored is what a `<<` merge brings in for each group key.
var anchoredValues = map[string][]string{
	"bearer_token":      {"anch", `""`},
	"bearer_token_file": {"/anch", `""`},
	"authorization":     {"{credentials: anch}", "~"},
}

type groupCase struct {
	values   map[string]string // key -> written YAML value; absent when missing
	order    []string          // the group keys, and "<<" where the merge is written
	anchored string            // the group key the merge brings in ("" for none)
	anchorV  string
}

// mapping writes the group keys (and the merge) in order.
func (c groupCase) mapping(indent string) string {
	var b strings.Builder
	for _, k := range c.order {
		if k == "<<" {
			fmt.Fprintf(&b, "%s<<: *a\n", indent)
			continue
		}
		if v, ok := c.values[k]; ok {
			fmt.Fprintf(&b, "%s%s: %s\n", indent, k, v)
		}
	}
	return b.String()
}

func (c groupCase) String() string {
	s := strings.TrimSpace(strings.ReplaceAll(c.mapping(""), "\n", "; "))
	if c.anchored != "" {
		s += fmt.Sprintf(" [*a = {%s: %s}]", c.anchored, c.anchorV)
	}
	return s
}

func permutations(keys []string) [][]string {
	if len(keys) <= 1 {
		return [][]string{append([]string(nil), keys...)}
	}
	var out [][]string
	for i := range keys {
		rest := append(append([]string(nil), keys[:i]...), keys[i+1:]...)
		for _, p := range permutations(rest) {
			out = append(out, append([]string{keys[i]}, p...))
		}
	}
	return out
}

func groupCases() []groupCase {
	var out []groupCase
	for _, bt := range groupStates["bearer_token"] {
		for _, btf := range groupStates["bearer_token_file"] {
			for _, au := range groupStates["authorization"] {
				vals := map[string]string{}
				for k, v := range map[string]string{"bearer_token": bt, "bearer_token_file": btf, "authorization": au} {
					if v != "" {
						vals[k] = v
					}
				}
				for _, o := range permutations(groupKeys) {
					out = append(out, groupCase{values: vals, order: o})
				}
				// One key brought in by `<<`, the merge written before or after
				// the others (two orders of the others).
				for _, k := range groupKeys {
					for _, av := range anchoredValues[k] {
						for _, o := range [][]string{groupKeys, {groupKeys[2], groupKeys[1], groupKeys[0]}} {
							out = append(out,
								groupCase{values: vals, order: append([]string{"<<"}, o...), anchored: k, anchorV: av},
								groupCase{values: vals, order: append(append([]string(nil), o...), "<<"), anchored: k, anchorV: av})
						}
					}
				}
			}
		}
	}
	return out
}

// targetMapping is the written mapping node of the group's owner, found by
// *path*: mapping keys, and "[N]" for a list item.
func targetMapping(t *testing.T, in string, path ...string) *yaml.Node {
	t.Helper()
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(in), &root); err != nil {
		t.Fatal(err)
	}
	n := root.Content[0]
	for _, p := range path {
		var i int
		if _, err := fmt.Sscanf(p, "[%d]", &i); err == nil {
			n = deref(n).Content[i]
			continue
		}
		wm, err := newComposed().entries(deref(n))
		if err != nil {
			t.Fatal(err)
		}
		n = wm.value[p]
	}
	return deref(n)
}

// wantKeys is the keys the owner's object prints, in order: the written
// keys (composed by go-yaml v2, which both loaders decode with) with the
// group as the loader reads it.
func wantKeys(t *testing.T, owner *yaml.Node, loaderHas bool) []string {
	t.Helper()
	wm, err := newComposed().entries(owner)
	if err != nil {
		t.Fatal(err)
	}
	isGroup := map[string]bool{}
	for _, k := range groupKeys {
		isGroup[k] = true
	}
	_, authWritten := wm.value["authorization"]
	var out []string
	placed := false
	for _, k := range wm.order {
		if !isGroup[k] {
			out = append(out, k)
			continue
		}
		if !placed {
			placed = true
			if loaderHas || authWritten {
				out = append(out, "authorization")
			}
		}
		if k != "authorization" && writtenEmpty(wm.value[k]) {
			out = append(out, k)
		}
	}
	return out
}

// checkGroup checks the printed owner object *obj* against the loader's
// *hc* and the written *owner* mapping.
func checkGroup(t *testing.T, c groupCase, obj *jsonObj, owner *yaml.Node, hc *commoncfg.HTTPClientConfig) {
	t.Helper()
	if obj == nil {
		t.Errorf("%s: the owner object is not printed", c)
		return
	}
	loaderHas := hc != nil && hc.Authorization != nil
	if want := wantKeys(t, owner, loaderHas); !reflect.DeepEqual(obj.keys, want) {
		t.Errorf("%s: keys %v; want %v (authorization at the first group key written)", c, obj.keys, want)
	}
	au := obj.vals["authorization"]
	if !loaderHas {
		if au != nil {
			t.Errorf("%s: authorization %v printed, the loader has none", c, au)
		}
	} else {
		m, ok := au.(*jsonObj)
		if !ok {
			t.Errorf("%s: authorization %v; the loader has %+v", c, au, *hc.Authorization)
			return
		}
		loader := map[string]string{"type": hc.Authorization.Type,
			"credentials": string(hc.Authorization.Credentials), "credentials_file": hc.Authorization.CredentialsFile}
		for k, v := range m.vals {
			if s, _ := v.(string); s != loader[k] {
				t.Errorf("%s: authorization.%s = %v, the loader's is %q", c, k, v, loader[k])
			}
		}
		for _, k := range []string{"credentials", "credentials_file"} {
			if loader[k] != "" && m.vals[k] != loader[k] {
				t.Errorf("%s: the loader's authorization.%s %q is not printed (%v)", c, k, loader[k], m.vals)
			}
		}
	}
	for _, s := range []string{"bearer_token", "bearer_token_file"} {
		if v, isPrinted := obj.vals[s]; isPrinted && v != "" {
			t.Errorf("%s: %s printed as %v; a printed source is \"\"", c, s, v)
		}
	}
}

type loaderRun struct {
	name   string
	input  func(groupCase) string
	load   func(string) (*commoncfg.HTTPClientConfig, error)
	run    func(*testing.T, string) (int, string, string)
	owner  []string      // the path to the owner's written mapping
	object []interface{} // the path to its printed object
}

func runGroupProperty(t *testing.T, l loaderRun) {
	read, refused := 0, 0
	for _, c := range groupCases() {
		in := l.input(c)
		hc, loadErr := l.load(in)
		rc, out, errOut := l.run(t, in)
		if loadErr != nil {
			refused++
			if rc != exitCallerErr || out != "" {
				t.Errorf("%s: %s refuses (%v) but rc=%d stdout=%q", c, l.name, loadErr, rc, out)
			}
			continue
		}
		read++
		if rc != exitOK {
			t.Errorf("%s: %s reads it but rc=%d stderr=%q", c, l.name, rc, errOut)
			continue
		}
		doc, dup, err := decodeOrdered([]byte(out))
		if dup != "" || err != nil {
			t.Errorf("%s: key %q printed twice (%v): %s", c, dup, err, out)
			continue
		}
		checkGroup(t, c, at(doc, l.object...), targetMapping(t, in, l.owner...), hc)
	}
	t.Logf("%s: %d combinations read, %d refused by the loader", l.name, read, refused)
	if read == 0 || refused == 0 {
		t.Fatalf("read=%d refused=%d: the combinations no longer cover both sides", read, refused)
	}
}

// The owner is the second webhook's http_config; the first one carries the
// anchor `*a` a merge brings in. Keys outside the group sit before and after.
func TestMoveGroupPropertyAlertmanager(t *testing.T) {
	t.Parallel()
	runGroupProperty(t, loaderRun{
		name: "Alertmanager",
		input: func(c groupCase) string {
			anchor := "{}"
			if c.anchored != "" {
				anchor = fmt.Sprintf("{%s: %s}", c.anchored, c.anchorV)
			}
			return "route: {receiver: r}\nreceivers:\n- name: r\n  webhook_configs:\n" +
				"  - url: 'https://example.com/a'\n    http_config: &a " + anchor + "\n" +
				"  - url: 'https://example.com/'\n    http_config:\n      follow_redirects: true\n" +
				c.mapping("      ") + "      enable_http2: true\n"
		},
		load: func(in string) (*commoncfg.HTTPClientConfig, error) {
			cfg, err := amconfig.Load(in)
			if err != nil {
				return nil, err
			}
			return cfg.Receivers[0].WebhookConfigs[1].HTTPConfig, nil
		},
		run:    runOn,
		owner:  []string{"receivers", "[0]", "webhook_configs", "[1]", "http_config"},
		object: []interface{}{"documents", 0, "receivers", 0, "webhook_configs", 1, "http_config"},
	})
}

// The owner is the second scrape config, which inlines HTTPClientConfig;
// the first one is the anchor (its job_name and static_configs come along
// and are overridden by the ones written after the merge).
func TestMoveGroupPropertyPrometheus(t *testing.T) {
	t.Parallel()
	runGroupProperty(t, loaderRun{
		name: "Prometheus",
		input: func(c groupCase) string {
			anchored := ""
			if c.anchored != "" {
				anchored = fmt.Sprintf(", %s: %s", c.anchored, c.anchorV)
			}
			return "scrape_configs:\n" +
				"- &a {job_name: base, static_configs: [{targets: [b]}]" + anchored + "}\n" +
				"- honor_labels: true\n" + c.mapping("  ") +
				"  job_name: t\n  static_configs: [{targets: [h]}]\n"
		},
		load: func(in string) (*commoncfg.HTTPClientConfig, error) {
			cfg, err := promconfig.Load(in, promslog.NewNopLogger())
			if err != nil {
				return nil, err
			}
			return &cfg.ScrapeConfigs[1].HTTPClientConfig, nil
		},
		run:    runPrometheus,
		owner:  []string{"scrape_configs", "[1]"},
		object: []interface{}{"documents", 0, "scrape_configs", 1},
	})
}

func TestDecodeOrderedSeesDuplicatesAndOrder(t *testing.T) {
	t.Parallel()
	v, dup, err := decodeOrdered([]byte(`{"a":{"y":1,"x":2,"y":3}}`))
	if dup != "y" || err != nil || !reflect.DeepEqual(at(v, "a").keys, []string{"y", "x"}) {
		t.Fatalf("dup=%q err=%v", dup, err)
	}
	if _, dup, err := decodeOrdered([]byte(`{"a":[{"x":1},{"x":2}]}`)); dup != "" || err != nil {
		t.Fatalf("dup=%q err=%v", dup, err)
	}
}
