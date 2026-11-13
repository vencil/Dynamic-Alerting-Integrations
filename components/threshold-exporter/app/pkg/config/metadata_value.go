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
	read := fmt.Sprintf("%q|%q|%q|%q|%q|%q|%q|%q|%q", tm.RunbookURL, tm.Owner, tm.Tier, tm.Environment,
		tm.Region, tm.Domain, tm.DBType, tm.Tags, tm.Groups)
	if metadataProblem(k, sv) != "" {
		return read + "|raw=" + sv.Default
	}
	return read
}
