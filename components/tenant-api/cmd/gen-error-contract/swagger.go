package main

// The OpenAPI side of the Extra declarations (#3272): the ErrorResponse
// definition swag writes from the Go struct lists only the fixed fields
// (Extra is `json:"-"`), so this command adds one optional property per
// declared Extra field to it, in the three files swag writes —
// docs/swagger.json, docs/docs.go (the same JSON as a Go template) and
// docs/swagger.yaml (swagger.json converted, as swag converts it). Swagger 2.0
// has no oneOf, so one ErrorResponse is the union of every code's fields; each
// property's description names the codes that carry it.
//
// Why a pass over swag's output rather than a struct swag reads: the fields
// are declared once, in internal/errcode, as data; a struct for swag would be
// a second, generated copy of them in Go. The pass is exact and idempotent —
// it replaces the ErrorResponse definition's properties with the fixed ones
// swag wrote plus the declared ones, re-indented the way swag indents — so
// `make api-docs` (swag, then this command) and the CI drift check over
// docs/ hold the result, and `--check` refuses docs written by swag alone.
//
// Fail-closed: the definition must appear exactly once in each JSON file; the
// properties swag wrote (less any declared field) must be the envelope's
// fixed fields (handler.EnvelopeFieldNames — the struct and the spec agree);
// a declared Ref must name a definition of swagger.json.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"strings"

	"github.com/vencil/tenant-api/internal/errcode"
	"sigs.k8s.io/yaml"
)

// The swag output files, relative to the docs directory.
const (
	swaggerJSON = "swagger.json"
	swaggerGo   = "docs.go"
	swaggerYAML = "swagger.yaml"
)

// errorResponseMarker opens the ErrorResponse definition in swag's JSON (and
// in docs.go's template, which is the same JSON): a definitions entry, 8
// spaces in.
const errorResponseMarker = "\n        \"ErrorResponse\": {"

// schema is the subset of a Swagger 2.0 schema the Extra properties use, in
// the key order swag (go-openapi) writes.
type schema struct {
	Ref                  string   `json:"$ref,omitempty"`
	Description          string   `json:"description,omitempty"`
	Type                 string   `json:"type,omitempty"`
	Items                *schema  `json:"items,omitempty"`
	AllOf                []schema `json:"allOf,omitempty"`
	AdditionalProperties *bool    `json:"additionalProperties,omitempty"`
}

func refSchema(name string) schema { return schema{Ref: "#/definitions/" + name} }

// openObject is an object the spec does not define: any properties.
func openObject() schema {
	t := true
	return schema{Type: string(errcode.ExtraObject), AdditionalProperties: &t}
}

// extraProperties is the OpenAPI property of every declared Extra field,
// with the codes (of codes) that carry it named in its description. A field
// no code carries is an error (TestExtra_Declarations holds it too).
func extraProperties(fields []errcode.ExtraField, codes []errcode.Spec) (map[string]schema, error) {
	carriers := map[string][]string{}
	for _, c := range codes {
		for _, f := range c.Extra {
			carriers[f] = append(carriers[f], string(c.Code))
		}
	}
	out := map[string]schema{}
	for _, f := range fields {
		cs := carriers[f.Name]
		if len(cs) == 0 {
			return nil, fmt.Errorf("extra field %q is declared but no code carries it", f.Name)
		}
		slices.Sort(cs)
		desc := f.Doc + " Codes that carry it: " + strings.Join(cs, ", ") + "."
		var s schema
		switch f.Type {
		case errcode.ExtraString, errcode.ExtraInteger, errcode.ExtraBoolean:
			s = schema{Description: desc, Type: string(f.Type)}
		case errcode.ExtraArray:
			var items schema
			switch {
			case f.Items == errcode.ExtraObject && f.Ref != "":
				items = refSchema(f.Ref)
			case f.Items == errcode.ExtraObject:
				items = openObject()
			default:
				items = schema{Type: string(f.Items)}
			}
			s = schema{Description: desc, Type: string(errcode.ExtraArray), Items: &items}
		case errcode.ExtraObject:
			if f.Ref != "" {
				s = schema{Description: desc, AllOf: []schema{refSchema(f.Ref)}}
			} else {
				s = openObject()
				s.Description = desc
			}
		default:
			return nil, fmt.Errorf("extra field %q has type %q", f.Name, f.Type)
		}
		out[f.Name] = s
	}
	return out, nil
}

// checkRefs: every Ref of fields names a definition of the swagger.json doc.
func checkRefs(doc []byte, fields []errcode.ExtraField) error {
	var spec struct {
		Definitions map[string]json.RawMessage `json:"definitions"`
	}
	if err := json.Unmarshal(doc, &spec); err != nil {
		return fmt.Errorf("%s: %w", swaggerJSON, err)
	}
	for _, f := range fields {
		if f.Ref != "" && spec.Definitions[f.Ref] == nil {
			return fmt.Errorf("extra field %q refers to definition %q, which %s does not have", f.Name, f.Ref, swaggerJSON)
		}
	}
	return nil
}

// patchErrorResponse is doc (swag's JSON, or docs.go holding it) with the
// ErrorResponse definition's properties set to the fixed ones it lists plus
// props. fixed is the envelope's fixed field names: the properties doc lists,
// less the names of props, must be exactly these.
func patchErrorResponse(doc []byte, props map[string]schema, fixed []string, name string) ([]byte, error) {
	if n := bytes.Count(doc, []byte(errorResponseMarker)); n != 1 {
		return nil, fmt.Errorf("%s: the ErrorResponse definition appears %d times, want 1", name, n)
	}
	start := bytes.Index(doc, []byte(errorResponseMarker)) + len(errorResponseMarker) - 1
	dec := json.NewDecoder(bytes.NewReader(doc[start:]))
	var def json.RawMessage
	if err := dec.Decode(&def); err != nil {
		return nil, fmt.Errorf("%s: the ErrorResponse definition: %w", name, err)
	}
	end := start + int(dec.InputOffset())

	// The definition's keys, in order.
	kd := json.NewDecoder(bytes.NewReader(def))
	if _, err := kd.Token(); err != nil {
		return nil, err
	}
	var keys []string
	vals := map[string]json.RawMessage{}
	for kd.More() {
		tok, err := kd.Token()
		if err != nil {
			return nil, err
		}
		k := tok.(string)
		var v json.RawMessage
		if err := kd.Decode(&v); err != nil {
			return nil, err
		}
		keys = append(keys, k)
		vals[k] = v
	}
	if vals["properties"] == nil {
		return nil, fmt.Errorf("%s: the ErrorResponse definition has no properties", name)
	}
	var have map[string]json.RawMessage
	if err := json.Unmarshal(vals["properties"], &have); err != nil {
		return nil, fmt.Errorf("%s: ErrorResponse properties: %w", name, err)
	}
	for k := range props {
		delete(have, k)
	}
	if got := slices.Sorted(maps.Keys(have)); !slices.Equal(got, fixed) {
		return nil, fmt.Errorf("%s: ErrorResponse lists properties %v; the envelope's fixed fields are %v — run swag (make api-docs) first", name, got, fixed)
	}
	for k, s := range props {
		b, err := json.Marshal(s)
		if err != nil {
			return nil, err
		}
		have[k] = b
	}
	// map keys marshal sorted, as swag writes properties.
	merged, err := json.Marshal(have)
	if err != nil {
		return nil, err
	}
	vals["properties"] = merged

	var compact bytes.Buffer
	compact.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			compact.WriteByte(',')
		}
		kb, _ := json.Marshal(k)
		compact.Write(kb)
		compact.WriteByte(':')
		if err := json.Compact(&compact, vals[k]); err != nil {
			return nil, err
		}
	}
	compact.WriteByte('}')
	var indented bytes.Buffer
	if err := json.Indent(&indented, compact.Bytes(), "        ", "    "); err != nil {
		return nil, err
	}
	out := make([]byte, 0, len(doc)+indented.Len())
	out = append(out, doc[:start]...)
	out = append(out, indented.Bytes()...)
	out = append(out, doc[end:]...)
	return out, nil
}

// patchedDocs is the three swag files with the Extra properties, from their
// current contents (jsonDoc, goDoc): swagger.json and docs.go patched alike,
// swagger.yaml converted from the patched swagger.json.
func patchedDocs(jsonDoc, goDoc []byte, fields []errcode.ExtraField, codes []errcode.Spec, fixed []string) (map[string][]byte, error) {
	if err := checkRefs(jsonDoc, fields); err != nil {
		return nil, err
	}
	props, err := extraProperties(fields, codes)
	if err != nil {
		return nil, err
	}
	j, err := patchErrorResponse(jsonDoc, props, fixed, swaggerJSON)
	if err != nil {
		return nil, err
	}
	g, err := patchErrorResponse(goDoc, props, fixed, swaggerGo)
	if err != nil {
		return nil, err
	}
	y, err := yaml.JSONToYAML(j)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", swaggerYAML, err)
	}
	return map[string][]byte{swaggerJSON: j, swaggerGo: g, swaggerYAML: y}, nil
}
