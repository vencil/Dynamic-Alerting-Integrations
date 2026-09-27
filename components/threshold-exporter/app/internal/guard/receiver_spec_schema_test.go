package guard

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"testing"
)

// TestReceiverTypeSpecs_MatchSchema pins receiverTypeSpecs to the
// receiver definitions in docs/schemas/tenant-config.schema.json — the
// hub the Python RECEIVER_TYPES is pinned to as well
// (tests/shared/test_receiver_spec_parity.py). Both sides read the
// schema as JSON, so neither parses another language's source.
//
// The schema's presence contract per receiver definition is read as:
//   - `required` minus "type"                          → Required
//   - `oneOf` whose every branch is
//     {"required": [k], "properties": {k: {"type": "string", "minLength": 1}}},
//     read as one ExactlyOneOf group
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
func TestReceiverTypeSpecs_MatchSchema(t *testing.T) {
	fromSchema := receiverSpecsFromSchema(t)
	if len(fromSchema) == 0 {
		t.Fatal("no receiver definitions read from the schema")
	}
	norm := func(s receiverTypeSpec) receiverTypeSpec {
		out := receiverTypeSpec{Required: sortedCopy(s.Required), StringLists: sortedCopy(s.StringLists)}
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
		got, ok := receiverTypeSpecs[rtype]
		if !ok {
			t.Errorf("schema defines receiver type %q; receiverTypeSpecs does not", rtype)
			continue
		}
		if !reflect.DeepEqual(norm(got), norm(want)) {
			t.Errorf("receiver type %q: Go %+v, schema %+v", rtype, norm(got), norm(want))
		}
	}
	for rtype := range receiverTypeSpecs {
		if _, ok := fromSchema[rtype]; !ok {
			t.Errorf("receiverTypeSpecs has type %q; the schema does not", rtype)
		}
	}
}

// TestReceiverPresenceCases runs the shared case table
// (testdata/receiver_presence_cases.json) through the guard; the table
// is shared with tests/shared/test_receiver_spec_parity.py.
func TestReceiverPresenceCases(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("testdata", "receiver_presence_cases.json"))
	if err != nil {
		t.Fatalf("read cases: %v", err)
	}
	var cases []struct {
		Name     string         `json:"name"`
		Receiver map[string]any `json:"receiver"`
		Valid    bool           `json:"valid"`
	}
	if err := json.Unmarshal(data, &cases); err != nil || len(cases) == 0 {
		t.Fatalf("parse cases: %v (n=%d)", err, len(cases))
	}
	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			var errs []string
			for _, f := range runWithRouting(t, "t1", map[string]any{"receiver": tc.Receiver}) {
				if f.Severity == SeverityError {
					errs = append(errs, f.Message)
				}
			}
			if valid := len(errs) == 0; valid != tc.Valid {
				t.Errorf("guard valid=%v, table says %v: %s", valid, tc.Valid, strings.Join(errs, "; "))
			}
		})
	}
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

func receiverSpecsFromSchema(t *testing.T) map[string]receiverTypeSpec {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	// internal/guard → internal → app → threshold-exporter → components → repo root
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
	var union struct {
		OneOf []struct {
			Ref string `json:"$ref"`
		} `json:"oneOf"`
	}
	if err := json.Unmarshal(schema.Definitions["receiver"], &union); err != nil || len(union.OneOf) == 0 {
		t.Fatalf("definitions.receiver.oneOf not readable (err=%v)", err)
	}
	out := map[string]receiverTypeSpec{}
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
		var spec receiverTypeSpec
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
		out[rtype] = spec
	}
	return out
}
