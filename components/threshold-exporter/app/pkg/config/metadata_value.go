package config

// The `metadata` value check (#3591, ADR-038 decision 10): a `_metadata`
// value as the exporter reads it — decodeTenantMetadata, the decode
// tenantMetadataOf serves tenant_metadata_info from — and the part of it the
// exporter does not read. TenantMetadata is the reader, so it is the
// validator: a field is known exactly when TenantMetadata has a yaml tag for
// it (TenantMetadataFields), and the schema's `definitions.metadata` is
// pinned to that set in both directions by
// TestMetadataSchemaPropertiesMatchReader — never generated from it.

import (
	"errors"
	"fmt"
	"reflect"
	"sort"
	"strings"
)

// decodeTenantMetadata is the exporter's one decode of a `_metadata` value:
// the mapping it was written as (structureOf, #3638) — a non-string scalar
// field is read as its text, a field it does not know is dropped, and a
// value that does not decode into TenantMetadata is an error; every field
// then reads empty (tenantMetadataOf). A value written as a string is no
// mapping, whatever its text reads like: an error (#2859 refused writing the
// form; #3638 stops reading it).
func decodeTenantMetadata(sv ScheduledValue) (TenantMetadata, error) {
	var tm TenantMetadata
	node, structured, err := structureOf(sv)
	if !structured {
		if sv.Default == "" {
			return tm, nil
		}
		return tm, errMetadataString
	}
	if err == nil {
		err = node.Decode(&tm)
	}
	return tm, err
}

// errMetadataString is decodeTenantMetadata's error for `_metadata` written
// as a string.
var errMetadataString = fmt.Errorf("_metadata is written as a string, not a mapping (#2859, #3638): rewrite it as a mapping (owner: …, tier: …)")

// tenantMetadataFields is TenantMetadata's yaml tags, computed once.
var tenantMetadataFields = func() map[string]bool {
	out := map[string]bool{}
	t := reflect.TypeOf(TenantMetadata{})
	for i := 0; i < t.NumField(); i++ {
		tag := strings.Split(t.Field(i).Tag.Get("yaml"), ",")[0]
		if tag != "" && tag != "-" {
			out[tag] = true
		}
	}
	return out
}()

// TenantMetadataFields is the `_metadata` fields the exporter reads
// (TenantMetadata's yaml tags), sorted.
func TenantMetadataFields() []string {
	out := make([]string, 0, len(tenantMetadataFields))
	for f := range tenantMetadataFields {
		out = append(out, f)
	}
	sort.Strings(out)
	return out
}

// unknownMetadataFields is the top-level fields of a `_metadata` mapping
// TenantMetadata has no field for, sorted; nil when the value is not a
// mapping or has none.
func unknownMetadataFields(sv ScheduledValue) []string {
	m, ok := decodeMetadataValue(sv).(map[string]any)
	if !ok {
		return nil
	}
	var out []string
	for k := range m {
		if !tenantMetadataFields[k] {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// metadataProblem is the `metadata` check's problem: "" when the exporter
// reads all of the value — a null or empty value (no metadata), or one that
// decodes into TenantMetadata with no field it does not know. Constants and
// a count only (#3703): never a field name the author wrote, nor the
// decoder's error, which quotes the value.
func metadataProblem(_ string, sv ScheduledValue) string {
	if sv.deleted || sv.Default == "" {
		return ""
	}
	if _, err := decodeTenantMetadata(sv); err != nil {
		if errors.Is(err, errMetadataString) {
			return metadataUndecodedProblem + ": " + errMetadataString.Error()
		}
		return metadataUndecodedProblem
	}
	if p := retiredMetadataFieldProblem(sv); p != "" {
		return p
	}
	if unknown := unknownMetadataFields(sv); len(unknown) > 0 {
		return fmt.Sprintf("%d unknown field(s) (known: %s)",
			len(unknown), strings.Join(TenantMetadataFields(), ", "))
	}
	return ""
}

const metadataUndecodedProblem = "it does not decode into tenant metadata, so every field reads empty"

// metadataReading is the `metadata` check's reading: the fields as decoded,
// and for a value with a part the exporter does not read the text itself —
// two such values are alike only when they are the same text
// (stateFilterValueReading's rule), or a write could replace one unread
// field with another and get a NOTICE.
func metadataReading(k string, sv ScheduledValue) any {
	if sv.deleted || sv.Default == "" {
		return "none"
	}
	tm, _ := decodeTenantMetadata(sv)
	read := fmt.Sprintf("%q|%q|%q|%q|%q|%q|%q|%q", tm.RunbookURL, tm.Owner, tm.Tier, tm.Environment,
		tm.Region, tm.Domain, tm.Tags, tm.Groups)
	if metadataProblem(k, sv) != "" {
		return read + "|raw=" + sv.Default
	}
	return read
}

// retiredDBTypeField is the one `_metadata` field that was removed rather
// than never known (#4097): until v2.9 `db_type` was a grouping string, then
// (unreleased) the list tenant_expected_exporter was served from. Its job is
// the top-level `_expected_exporters` key's now (expected_exporters.go).
const retiredDBTypeField = "db_type"

// metadataWritesRetiredField reports whether sv is a `_metadata` mapping
// that still writes `db_type`. Such a value is an ERROR in every layer that
// writes it (valueCheck.everyLayer), not the notice an unknown field is
// outside a tenant file: a tree migrated half-way would otherwise load
// clean while the declaration it used to make — and the absence alerts it
// switched on — is gone.
func metadataWritesRetiredField(sv ScheduledValue) bool {
	m, ok := decodeMetadataValue(sv).(map[string]any)
	if !ok {
		return false
	}
	_, writes := m[retiredDBTypeField]
	return writes
}

// retiredMetadataFieldProblem is the migration instruction for a `_metadata`
// that still writes `db_type`; "" when it does not. The line to write is
// printed only when every value it held is an exporter kind — then it is
// built from the kind table, not from the value (#3703); otherwise the
// sentence carries a count and the allowed kinds.
func retiredMetadataFieldProblem(sv ScheduledValue) string {
	m, ok := decodeMetadataValue(sv).(map[string]any)
	if !ok {
		return ""
	}
	raw, writes := m[retiredDBTypeField]
	if !writes {
		return ""
	}
	var values []any
	switch v := raw.(type) {
	case nil:
	case []any:
		values = v
	default:
		values = []any{v}
	}
	var kinds []string
	seen := map[string]bool{}
	notKind := 0
	for _, v := range values {
		s, isString := v.(string)
		switch {
		case isString && s == "":
		case isString && IsExpectedExporterKind(s):
			if !seen[s] {
				seen[s] = true
				kinds = append(kinds, s)
			}
		default:
			notKind++
		}
	}
	const head = "`_metadata.db_type` is removed (#4097): what a tenant runs is declared in the top-level key `_expected_exporters`"
	const where = " beside `_metadata` (the top level of this tenant or profile entry)"
	const effect = "; a declared kind gets the absence alert TenantExporterAbsent, so declare only the exporters that are really scraped"
	switch {
	case notKind > 0:
		return fmt.Sprintf("%s. %d of its value(s) is not an exporter kind, so there is no line to copy: delete `db_type` and write "+
			"`_expected_exporters: [<kind>, …]`%s with the kinds of the exporters this tenant runs (%s)%s",
			head, notKind, where, allowedKindsText(), effect)
	case len(kinds) == 0:
		return head + ". It declares nothing here: delete `db_type`" + effect
	default:
		return fmt.Sprintf("%s. Delete `db_type` and write `_expected_exporters: [%s]`%s%s",
			head, strings.Join(kinds, ", "), where, effect)
	}
}
