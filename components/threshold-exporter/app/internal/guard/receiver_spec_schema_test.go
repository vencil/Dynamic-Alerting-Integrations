package guard

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"testing"
)

// TestReceiverTypeSpecs_MatchSchema pins receiverTypeSpecs to the
// receiver definitions in docs/schemas/tenant-config.schema.json — the
// hub the Python RECEIVER_TYPES is pinned to as well
// (tests/shared/test_receiver_spec_parity.py). Both sides read the
// schema as JSON, so neither parses another language's source.
//
// The schema's presence contract per receiver definition is read as:
//   - `required` minus "type"               → Required
//   - `oneOf` whose every branch is exactly
//     {"required": [<one field>]}            → one ExactlyOneOf group
//
// Any other presence-shaping keyword on a receiver definition fails the
// test instead of being skipped: an unmodelled constraint must not read
// as "no constraint".
func TestReceiverTypeSpecs_MatchSchema(t *testing.T) {
	fromSchema := receiverSpecsFromSchema(t)
	if len(fromSchema) == 0 {
		t.Fatal("no receiver definitions read from the schema")
	}
	norm := func(s receiverTypeSpec) receiverTypeSpec {
		out := receiverTypeSpec{Required: sortedCopy(s.Required)}
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

func sortedCopy(in []string) []string {
	if len(in) == 0 {
		return nil
	}
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
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
		if len(branch.Ref) <= len(prefix) || branch.Ref[:len(prefix)] != prefix {
			t.Fatalf("receiver branch %q is not a local definition $ref", branch.Ref)
		}
		name := branch.Ref[len(prefix):]
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
			Required   []string `json:"required"`
			Properties struct {
				Type struct {
					Const string `json:"const"`
				} `json:"type"`
			} `json:"properties"`
			OneOf []map[string]json.RawMessage `json:"oneOf"`
		}
		if err := json.Unmarshal(schema.Definitions[name], &def); err != nil {
			t.Fatalf("definition %s: %v", name, err)
		}
		rtype := def.Properties.Type.Const
		if rtype == "" {
			t.Fatalf("definition %s has no properties.type.const", name)
		}
		var spec receiverTypeSpec
		for _, f := range def.Required {
			if f != "type" {
				spec.Required = append(spec.Required, f)
			}
		}
		if len(def.OneOf) > 0 {
			var group []string
			for i, br := range def.OneOf {
				var req []string
				if len(br) != 1 || json.Unmarshal(br["required"], &req) != nil || len(req) != 1 {
					t.Fatalf("definition %s oneOf[%d] is not {\"required\": [<one field>]}", name, i)
				}
				group = append(group, req[0])
			}
			spec.ExactlyOneOf = [][]string{group}
		}
		out[rtype] = spec
	}
	return out
}
