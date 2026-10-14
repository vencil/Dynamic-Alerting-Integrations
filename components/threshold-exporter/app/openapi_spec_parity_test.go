package main

// docs/api/threshold-exporter-openapi.yaml keeps only what this test can hold
// against the running handlers (#2998); its old metric list, examples and
// unused schemas had drifted with nothing reading them.
//
//   - paths: every route buildMux registers (read with go/ast, as
//     ops_readme_parity_test.go does) is in the file, except the ones in
//     openapiNotDescribed; every path in the file is registered.
//   - operations and status codes: each declared (method, path, status) has an
//     entry in openapiScenarios, which sends a request to buildMux behind a
//     real httptest.Server (the operation's method, or another one for a
//     status such as 405) and must get that status. A declared status with
//     no scenario, or a scenario whose status is not declared, fails.
//   - media types: when a response declares content, the served Content-Type
//     must have one of the declared types, and every parameter the doc names
//     must have the served value.
//   - query parameters: sending one must change the response body, so a
//     parameter the handler no longer reads fails.
//   - ConfigIdentity: required and properties are both exactly the keys of
//     the served JSON, each value has the declared type, and the enums are
//     configIdentitySchema and the two values Mode() returns.
//   - info.version is the /api/<version>/ prefix of every /api/ path.
//
// The file is decoded with KnownFields, so a field this test does not check
// (example, x-code-samples, servers, another schema) fails it instead of
// becoming a second copy that drifts. Prose (summary, description) is not
// checked.
//
// NOT COVERED: methods a handler accepts but the file does not declare (most
// handlers here accept any method); the /metrics body (metrics_readme_parity_test.go).

import (
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

type oaSchema struct {
	Type        string               `yaml:"type"`
	Format      string               `yaml:"format"`
	Ref         string               `yaml:"$ref"`
	Description string               `yaml:"description"`
	Enum        []any                `yaml:"enum"`
	Required    []string             `yaml:"required"`
	Properties  map[string]*oaSchema `yaml:"properties"`
	Items       *oaSchema            `yaml:"items"`
}

type oaResponse struct {
	Description string `yaml:"description"`
	Content     map[string]struct {
		Schema oaSchema `yaml:"schema"`
	} `yaml:"content"`
}

type oaOperation struct {
	Summary     string   `yaml:"summary"`
	OperationID string   `yaml:"operationId"`
	Tags        []string `yaml:"tags"`
	Parameters  []struct {
		Name        string   `yaml:"name"`
		In          string   `yaml:"in"`
		Description string   `yaml:"description"`
		Schema      oaSchema `yaml:"schema"`
	} `yaml:"parameters"`
	Responses map[string]oaResponse `yaml:"responses"`
}

type oaSpec struct {
	OpenAPI string `yaml:"openapi"`
	Info    struct {
		Title       string `yaml:"title"`
		Version     string `yaml:"version"`
		Description string `yaml:"description"`
	} `yaml:"info"`
	Paths      map[string]map[string]*oaOperation `yaml:"paths"`
	Components struct {
		Schemas map[string]*oaSchema `yaml:"schemas"`
	} `yaml:"components"`
	Tags []struct {
		Name string `yaml:"name"`
	} `yaml:"tags"`
}

// openapiNotDescribed: routes buildMux registers that the file leaves out,
// and where their contract is instead.
var openapiNotDescribed = map[string]string{
	"/api/v1/tenants/simulate": "components/threshold-exporter/README.md §3.1",
}

// openapiScenarios: "<operation> <path> <status>" as the file declares it →
// the request that produces it: the method to send (the operation's own,
// unless the status is about other methods, e.g. 405) and the manager state.
type openapiScenario struct {
	send  string
	state func() *ConfigManager
}

var openapiScenarios = map[string]openapiScenario{
	"GET /metrics 200":                 {"GET", openapiLoaded},
	"GET /health 200":                  {"GET", openapiLoaded},
	"GET /ready 200":                   {"GET", openapiLoaded},
	"GET /ready 503":                   {"GET", func() *ConfigManager { return newTestManager(nil) }},
	"GET /api/v1/config 200":           {"GET", openapiLoaded},
	"GET /api/v1/config/identity 200":  {"GET", openapiLoaded},
	"GET /api/v1/config/identity 405":  {"POST", openapiLoaded},
	"HEAD /api/v1/config/identity 200": {"HEAD", openapiLoaded},
}

func openapiLoaded() *ConfigManager { return newTestManager(&ThresholdConfig{}) }

func openapiLoad(t *testing.T) *oaSpec {
	t.Helper()
	f, err := os.Open(filepath.Join("..", "..", "..", "docs", "api", "threshold-exporter-openapi.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	dec := yaml.NewDecoder(f)
	dec.KnownFields(true)
	var spec oaSpec
	if err := dec.Decode(&spec); err != nil {
		t.Fatalf("openapi: %v (a field this test does not check is not allowed; see the file header)", err)
	}
	if len(spec.Paths) == 0 {
		t.Fatal("openapi: no paths")
	}
	return &spec
}

func openapiServe(t *testing.T, m *ConfigManager, method, target string) (*http.Response, []byte) {
	t.Helper()
	srv := httptest.NewServer(buildMux(m, NewThresholdCollector(m)))
	defer srv.Close()
	req, err := http.NewRequest(method, srv.URL+target, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp, body
}

// openapiMediaMatch: same type, and every parameter the doc names has the
// served value (the server may add more, e.g. charset).
func openapiMediaMatch(doc, served string) error {
	dt, dp, err := mime.ParseMediaType(doc)
	if err != nil {
		return err
	}
	st, sp, err := mime.ParseMediaType(served)
	if err != nil {
		return fmt.Errorf("served %q: %v", served, err)
	}
	if dt != st {
		return fmt.Errorf("type %s, served %q", dt, served)
	}
	for k, v := range dp {
		if sp[k] != v {
			return fmt.Errorf("%s=%s, served %q", k, v, served)
		}
	}
	return nil
}

func TestOpenAPISpecPaths(t *testing.T) {
	spec := openapiLoad(t)
	fset, files := opsParsePackage(t)
	routes := opsSourceRoutes(t, fset, files)
	for p, pos := range routes {
		_, inSpec := spec.Paths[p]
		where, excluded := openapiNotDescribed[p]
		switch {
		case inSpec && excluded:
			t.Errorf("openapi describes %s, which openapiNotDescribed says is in %s; drop one", p, where)
		case !inSpec && !excluded:
			t.Errorf("%s is registered at %s but missing from the openapi file; describe it or add it to openapiNotDescribed", p, pos)
		}
	}
	for p := range spec.Paths {
		if _, ok := routes[p]; !ok {
			t.Errorf("openapi describes %s but buildMux does not register it", p)
		}
		if strings.HasPrefix(p, "/api/") && !strings.HasPrefix(p, "/api/"+spec.Info.Version+"/") {
			t.Errorf("openapi: %s is not under /api/%s/ (info.version)", p, spec.Info.Version)
		}
	}
	for p := range openapiNotDescribed {
		if _, ok := routes[p]; !ok {
			t.Errorf("openapiNotDescribed lists %s, which buildMux does not register", p)
		}
	}
}

func TestOpenAPISpecResponses(t *testing.T) {
	spec := openapiLoad(t)
	declared := map[string]bool{}
	for path, ops := range spec.Paths {
		for method, op := range ops {
			M := strings.ToUpper(method)
			if len(op.Responses) == 0 {
				t.Errorf("openapi: %s %s declares no responses", M, path)
			}
			for code, resp := range op.Responses {
				key := fmt.Sprintf("%s %s %s", M, path, code)
				declared[key] = true
				sc, ok := openapiScenarios[key]
				if !ok {
					t.Errorf("openapi declares %s; add a request that produces it to openapiScenarios", key)
					continue
				}
				r, body := openapiServe(t, sc.state(), sc.send, path)
				if got := fmt.Sprint(r.StatusCode); got != code {
					t.Errorf("%s: handler answered %s", key, got)
					continue
				}
				if len(resp.Content) == 0 {
					continue
				}
				ct := r.Header.Get("Content-Type")
				var errs []string
				matched := ""
				for mt := range resp.Content {
					if err := openapiMediaMatch(mt, ct); err != nil {
						errs = append(errs, mt+": "+err.Error())
					} else {
						matched = mt
					}
				}
				if matched == "" {
					sort.Strings(errs)
					t.Errorf("%s: served Content-Type %q matches no declared type (%s)", key, ct, strings.Join(errs, "; "))
					continue
				}
				openapiCheckBody(t, spec, key, resp.Content[matched].Schema, body)
			}
			for _, p := range op.Parameters {
				if p.In != "query" {
					t.Errorf("openapi: %s %s parameter %s is in %q; this test only checks query parameters", M, path, p.Name, p.In)
					continue
				}
				if p.Schema.Type != "string" || p.Schema.Format != "date-time" {
					t.Errorf("openapi: %s %s ?%s is %s/%s; this test only sends date-time strings", M, path, p.Name, p.Schema.Type, p.Schema.Format)
					continue
				}
				_, without := openapiServe(t, openapiLoaded(), M, path)
				_, with := openapiServe(t, openapiLoaded(), M, path+"?"+p.Name+"=2026-03-12T03:30:00Z")
				if string(with) == string(without) {
					t.Errorf("openapi: %s %s ?%s does not change the response; the handler does not read it", M, path, p.Name)
				}
			}
		}
	}
	for key := range openapiScenarios {
		if !declared[key] {
			t.Errorf("openapiScenarios has %s, which the openapi file does not declare", key)
		}
	}
}

func openapiCheckBody(t *testing.T, spec *oaSpec, key string, s oaSchema, body []byte) {
	t.Helper()
	if s.Ref == "" {
		if s.Type != "string" {
			t.Errorf("%s: inline schema type %q; this test only checks string bodies and $ref ConfigIdentity", key, s.Type)
		}
		return
	}
	if s.Ref != "#/components/schemas/ConfigIdentity" {
		t.Errorf("%s: $ref %s; this test only checks ConfigIdentity", key, s.Ref)
		return
	}
	var got map[string]any
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatalf("%s: body is not a JSON object: %v", key, err)
	}
	ci := spec.Components.Schemas["ConfigIdentity"]
	keys := func(m map[string]any) []string {
		out := []string{}
		for k := range m {
			out = append(out, k)
		}
		sort.Strings(out)
		return out
	}
	served := keys(got)
	props := []string{}
	for k := range ci.Properties {
		props = append(props, k)
	}
	sort.Strings(props)
	req := append([]string{}, ci.Required...)
	sort.Strings(req)
	if !reflect.DeepEqual(props, served) {
		t.Errorf("ConfigIdentity properties %v, served keys %v", props, served)
	}
	if !reflect.DeepEqual(req, served) {
		t.Errorf("ConfigIdentity required %v, served keys %v", req, served)
	}
	for k, v := range got {
		p := ci.Properties[k]
		if p == nil {
			continue
		}
		want := map[string]string{"integer": "float64", "boolean": "bool", "string": "string", "array": "[]interface {}"}[p.Type]
		if fmt.Sprintf("%T", v) != want {
			t.Errorf("ConfigIdentity.%s is %s, served %T", k, p.Type, v)
		}
		if p.Type == "array" && (p.Items == nil || p.Items.Type != "string") {
			t.Errorf("ConfigIdentity.%s: items must be string (configIdentityDoc is []string)", k)
		}
	}
}

func TestOpenAPISpecSchemas(t *testing.T) {
	spec := openapiLoad(t)
	for name := range spec.Components.Schemas {
		if name != "ConfigIdentity" {
			t.Errorf("openapi: schema %s has no check in this test", name)
		}
	}
	ci := spec.Components.Schemas["ConfigIdentity"]
	if ci == nil {
		t.Fatal("openapi: no ConfigIdentity schema")
	}
	enum := func(field string) []string {
		p := ci.Properties[field]
		if p == nil {
			t.Fatalf("ConfigIdentity has no %s", field)
		}
		out := []string{}
		for _, v := range p.Enum {
			out = append(out, fmt.Sprint(v))
		}
		sort.Strings(out)
		return out
	}
	if got, want := enum("schema"), []string{fmt.Sprint(configIdentitySchema)}; !reflect.DeepEqual(got, want) {
		t.Errorf("ConfigIdentity.schema enum %v, configIdentitySchema is %v", got, want)
	}
	modes := []string{(&ConfigManager{isDir: true}).Mode(), (&ConfigManager{}).Mode()}
	sort.Strings(modes)
	if got := enum("mode"); !reflect.DeepEqual(got, modes) {
		t.Errorf("ConfigIdentity.mode enum %v, Mode() returns %v", got, modes)
	}
	for name, p := range ci.Properties {
		if len(p.Enum) > 0 && name != "schema" && name != "mode" {
			t.Errorf("ConfigIdentity.%s has an enum this test does not check", name)
		}
	}
}
