package receiverspec

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"slices"
	"sort"
	"strings"
	"testing"
)

// TestSpecs_MatchSchema pins specs to the receiver definitions in
// docs/schemas/tenant-config.schema.json — the hub the Python
// RECEIVER_TYPES is pinned to as well
// (tests/shared/test_receiver_spec_parity.py). Both sides read the schema
// as JSON, so neither parses another language's source.
//
// The schema's contract per receiver definition is read as:
//   - `required` minus "type"                          → Required
//   - `oneOf` whose every branch is
//     {"required": [k], "properties": {k: {"type": "string", "minLength": 1}}},
//     read as one ExactlyOneOf group
//   - properties typed `boolean`                       → Bools (#2295)
//   - a property `$ref`-ing #/definitions/httpConfig   → HTTPConfig (#2295)
//
// Emptiness is part of the contract (cases:
// testdata/receiver_presence_cases.json): "" and null are unset, as in
// Alertmanager (its config is a Go struct; both decode to the zero value). So
// every required field must reject "" and null in the schema (a single
// type plus minLength >= 1, or minItems >= 1 for an array), and every
// exactly-one branch must carry `type: string` + minLength >= 1 —
// without them, {service_key: "", routing_key: "r"} or
// {service_key: null, routing_key: "r"} would match both branches and
// the schema would reject what Go and Python accept.
//
// Any other presence-shaping keyword on a receiver definition, or any
// other branch shape, fails the test instead of being skipped: an
// unmodelled constraint must not read as "no constraint".
func TestSpecs_MatchSchema(t *testing.T) {
	fromSchema := specsFromSchema(t)
	if len(fromSchema) == 0 {
		t.Fatal("no receiver definitions read from the schema")
	}
	norm := func(s Spec) Spec {
		out := Spec{
			Required:    sortedCopy(s.Required),
			StringLists: sortedCopy(s.StringLists),
			Bools:       sortedCopy(s.Bools),
			HTTPConfig:  s.HTTPConfig,
		}
		if len(s.Patterns) > 0 {
			out.Patterns = s.Patterns
		}
		for _, g := range s.ExactlyOneOf {
			out.ExactlyOneOf = append(out.ExactlyOneOf, sortedCopy(g))
		}
		sort.Slice(out.ExactlyOneOf, func(i, j int) bool {
			return out.ExactlyOneOf[i][0] < out.ExactlyOneOf[j][0]
		})
		return out
	}
	for rtype, want := range fromSchema {
		got, ok := specs[rtype]
		if !ok {
			t.Errorf("schema defines receiver type %q; specs does not", rtype)
			continue
		}
		if !reflect.DeepEqual(norm(got), norm(want)) {
			t.Errorf("receiver type %q: Go %+v, schema %+v", rtype, norm(got), norm(want))
		}
	}
	for rtype := range specs {
		if _, ok := fromSchema[rtype]; !ok {
			t.Errorf("specs has type %q; the schema does not", rtype)
		}
	}
}

// TestHTTPConfig_MatchSchema pins the http_config half (#2295) to
// definitions.httpConfig: proxy_url's pattern is ProxyURLPattern, and every
// auth key the schema's `not` forbids together is in HTTPConfigAuthFields.
// The auth keys the schema does not list as properties must be refused by
// its additionalProperties: false — otherwise the schema would accept a
// combination Go and Python refuse.
func TestHTTPConfig_MatchSchema(t *testing.T) {
	defs := schemaDefinitions(t)
	var hc struct {
		Properties           map[string]map[string]json.RawMessage `json:"properties"`
		AdditionalProperties *bool                                 `json:"additionalProperties"`
		Not                  struct {
			Required   []string                              `json:"required"`
			Properties map[string]map[string]json.RawMessage `json:"properties"`
		} `json:"not"`
	}
	if err := json.Unmarshal(defs["httpConfig"], &hc); err != nil {
		t.Fatalf("definitions.httpConfig: %v", err)
	}
	if got := schemaPattern(t, defs, "httpConfig.proxy_url", hc.Properties["proxy_url"]); got != ProxyURLPattern {
		t.Errorf("httpConfig.proxy_url pattern differs from ProxyURLPattern:\nschema %q\nGo     %q", got, ProxyURLPattern)
	}
	if len(hc.Not.Required) < 2 {
		t.Fatalf("httpConfig.not.required should name the auth keys that cannot be set together, got %v", hc.Not.Required)
	}
	for _, k := range hc.Not.Required {
		if !slices.Contains(HTTPConfigAuthFields, k) {
			t.Errorf("schema forbids %q together with the other auth keys; HTTPConfigAuthFields lacks it", k)
		}
	}
	for _, k := range HTTPConfigAuthFields {
		_, listed := hc.Properties[k]
		if !listed && (hc.AdditionalProperties == nil || *hc.AdditionalProperties) {
			t.Errorf("auth key %q is not a httpConfig property and additionalProperties is not false", k)
		}
		if listed && !slices.Contains(hc.Not.Required, k) {
			t.Errorf("auth key %q is a httpConfig property the schema's `not` does not cover", k)
		}
	}
}

// TestPresenceCases runs the shared case table
// (testdata/receiver_presence_cases.json) through Check. The same rows are
// asserted by the guard (internal/guard TestReceiverPresenceCases), pytest
// (tests/shared/test_receiver_spec_parity.py) and Alertmanager itself
// (tests/alertmanager-inhibit, the `am` column).
func TestPresenceCases(t *testing.T) {
	for _, tc := range loadCases(t) {
		t.Run(tc.Name, func(t *testing.T) {
			problems := Check(tc.Receiver)
			if valid := len(problems) == 0; valid != tc.Valid {
				t.Errorf("Check valid=%v, table says %v: %+v", valid, tc.Valid, problems)
			}
		})
	}
}

type presenceCase struct {
	Name     string         `json:"name"`
	Receiver map[string]any `json:"receiver"`
	Valid    bool           `json:"valid"`
}

func loadCases(t *testing.T) []presenceCase {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "receiver_presence_cases.json"))
	if err != nil {
		t.Fatalf("read cases: %v", err)
	}
	var cases []presenceCase
	if err := json.Unmarshal(data, &cases); err != nil || len(cases) == 0 {
		t.Fatalf("parse cases: %v (n=%d)", err, len(cases))
	}
	return cases
}

// schemaPattern returns the `pattern` a property carries, directly or
// through `allOf: [{"$ref": "#/definitions/<name>"}]` (#2180: the URL and
// smarthost patterns are written once, as definitions). Any other allOf
// shape fails the test rather than reading as "no pattern".
func schemaPattern(t *testing.T, defs map[string]json.RawMessage, where string, prop map[string]json.RawMessage) string {
	t.Helper()
	var pattern string
	if raw, ok := prop["pattern"]; ok {
		if json.Unmarshal(raw, &pattern) != nil {
			t.Fatalf("%s: pattern is not a string", where)
		}
	}
	raw, ok := prop["allOf"]
	if !ok {
		return pattern
	}
	var refs []map[string]string
	if json.Unmarshal(raw, &refs) != nil {
		t.Fatalf("%s: allOf is not a list of {\"$ref\": ...}", where)
	}
	for i, r := range refs {
		const prefix = "#/definitions/"
		if len(r) != 1 || !strings.HasPrefix(r["$ref"], prefix) {
			t.Fatalf("%s: allOf[%d] is not a single local $ref: %v", where, i, r)
		}
		var target map[string]json.RawMessage
		if json.Unmarshal(defs[strings.TrimPrefix(r["$ref"], prefix)], &target) != nil {
			t.Fatalf("%s: allOf[%d] %s does not resolve", where, i, r["$ref"])
		}
		for k := range target {
			switch k {
			case "type", "title", "description", "$comment", "pattern":
			case "not":
				// Only the final-newline guard for Python re.search's `$`
				// (#2180). RE2's `$` is end of text and the pattern's
				// classes exclude control characters, so the Go copy needs
				// no counterpart; any other `not` is unmodelled.
				var not map[string]string
				if json.Unmarshal(target[k], &not) != nil || len(not) != 1 || not["pattern"] != `\n` {
					t.Fatalf("%s: %s `not` is not {\"pattern\": \"\\\\n\"}", where, r["$ref"])
				}
			default:
				t.Fatalf("%s: %s uses %q, which this parity check does not model", where, r["$ref"], k)
			}
		}
		var p string
		if raw, ok := target["pattern"]; ok && json.Unmarshal(raw, &p) == nil && p != "" {
			if pattern != "" {
				t.Fatalf("%s: more than one pattern", where)
			}
			pattern = p
		}
	}
	return pattern
}

func sortedCopy(in []string) []string {
	if len(in) == 0 {
		return nil
	}
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
}

// rejectsEmpty reports whether a property schema rejects every value the
// guard and Python read as unset: "" and null. It
// needs the type pinned as well as the length bound — minLength /
// minItems do not apply to null, so `type: ["string","null"]` +
// minLength would still let null through.
func rejectsEmpty(prop map[string]json.RawMessage) bool {
	var typ string
	if json.Unmarshal(prop["type"], &typ) != nil { // absent, or a type list
		return false
	}
	kw := map[string]string{"string": "minLength", "array": "minItems"}[typ]
	var n float64
	raw, ok := prop[kw]
	return kw != "" && ok && json.Unmarshal(raw, &n) == nil && n >= 1
}

// schemaDefinitions reads definitions of docs/schemas/tenant-config.schema.json.
func schemaDefinitions(t *testing.T) map[string]json.RawMessage {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	// pkg/receiverspec → pkg → app → threshold-exporter → components → repo root
	root := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..")
	data, err := os.ReadFile(filepath.Join(root, "docs", "schemas", "tenant-config.schema.json"))
	if err != nil {
		t.Fatalf("read schema: %v", err)
	}
	var schema struct {
		Definitions map[string]json.RawMessage `json:"definitions"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
		t.Fatalf("parse schema: %v", err)
	}
	return schema.Definitions
}

func specsFromSchema(t *testing.T) map[string]Spec {
	t.Helper()
	schema := struct{ Definitions map[string]json.RawMessage }{schemaDefinitions(t)}
	var union struct {
		OneOf []struct {
			Ref string `json:"$ref"`
		} `json:"oneOf"`
	}
	if err := json.Unmarshal(schema.Definitions["receiver"], &union); err != nil || len(union.OneOf) == 0 {
		t.Fatalf("definitions.receiver.oneOf not readable (err=%v)", err)
	}
	out := map[string]Spec{}
	for _, branch := range union.OneOf {
		const prefix = "#/definitions/"
		if !strings.HasPrefix(branch.Ref, prefix) {
			t.Fatalf("receiver branch %q is not a local definition $ref", branch.Ref)
		}
		name := strings.TrimPrefix(branch.Ref, prefix)
		var raw map[string]json.RawMessage
		if err := json.Unmarshal(schema.Definitions[name], &raw); err != nil {
			t.Fatalf("definition %s: %v", name, err)
		}
		for _, kw := range []string{"anyOf", "allOf", "not", "if", "dependencies", "dependentRequired"} {
			if _, has := raw[kw]; has {
				t.Fatalf("definition %s uses %q, which this parity check does not model", name, kw)
			}
		}
		var def struct {
			Required   []string                              `json:"required"`
			Properties map[string]map[string]json.RawMessage `json:"properties"`
			OneOf      []map[string]json.RawMessage          `json:"oneOf"`
		}
		if err := json.Unmarshal(schema.Definitions[name], &def); err != nil {
			t.Fatalf("definition %s: %v", name, err)
		}
		var rtype string
		if err := json.Unmarshal(def.Properties["type"]["const"], &rtype); err != nil || rtype == "" {
			t.Fatalf("definition %s has no properties.type.const", name)
		}
		var spec Spec
		for _, f := range def.Required {
			if f == "type" {
				continue
			}
			spec.Required = append(spec.Required, f)
			prop := def.Properties[f]
			if !rejectsEmpty(prop) {
				t.Errorf("%s.%s is required but the schema accepts it empty or null (needs a single type with minLength/minItems >= 1); the guard and Python treat both as missing", name, f)
			}
			var typ string
			_ = json.Unmarshal(prop["type"], &typ)
			if typ == "array" {
				// #2180: the list is joined into Alertmanager's `to`
				// string, where [""] reads as no address.
				var items map[string]json.RawMessage
				if json.Unmarshal(prop["items"], &items) != nil || !rejectsEmpty(items) {
					t.Errorf("%s.%s is a required array whose items accept \"\" or null (needs items: {type: string, minLength >= 1}); the guard and Python reject such items", name, f)
				}
				spec.StringLists = append(spec.StringLists, f)
			}
			if p := schemaPattern(t, schema.Definitions, name+"."+f, prop); p != "" {
				if spec.Patterns == nil {
					spec.Patterns = map[string]string{}
				}
				spec.Patterns[f] = p
			}
		}
		if len(def.OneOf) > 0 {
			var group []string
			for i, br := range def.OneOf {
				var req []string
				if json.Unmarshal(br["required"], &req) != nil || len(req) != 1 {
					t.Fatalf("definition %s oneOf[%d] has no single-field `required`", name, i)
				}
				for k := range br {
					if k != "required" && k != "properties" {
						t.Fatalf("definition %s oneOf[%d] uses %q, which this parity check does not model", name, i, k)
					}
				}
				var props map[string]map[string]json.RawMessage
				if raw, ok := br["properties"]; ok {
					if json.Unmarshal(raw, &props) != nil || len(props) != 1 || props[req[0]] == nil {
						t.Fatalf("definition %s oneOf[%d].properties must constrain exactly its required field %q", name, i, req[0])
					}
					for k := range props[req[0]] {
						if k != "minLength" && k != "type" {
							t.Fatalf("definition %s oneOf[%d].properties.%s uses %q, which this parity check does not model", name, i, req[0], k)
						}
					}
				}
				if !rejectsEmpty(props[req[0]]) {
					t.Errorf("definition %s oneOf[%d]: %q lacks `type: string` + `minLength >= 1`, so an empty or null %q still matches this branch; the guard and Python treat both as unset", name, i, req[0], req[0])
				}
				group = append(group, req[0])
			}
			spec.ExactlyOneOf = [][]string{group}
		}
		// #2295: optional value shapes. Every property typed boolean is a
		// Bools field; a property referencing httpConfig turns on HTTPConfig.
		for f, prop := range def.Properties {
			var typ string
			if json.Unmarshal(prop["type"], &typ) == nil && typ == "boolean" {
				spec.Bools = append(spec.Bools, f)
			}
			var ref string
			if raw, ok := prop["$ref"]; ok && json.Unmarshal(raw, &ref) == nil {
				if ref != "#/definitions/httpConfig" || f != "http_config" {
					t.Fatalf("definition %s.%s references %q, which this parity check does not model", name, f, ref)
				}
				spec.HTTPConfig = true
			}
		}
		out[rtype] = spec
	}
	return out
}
