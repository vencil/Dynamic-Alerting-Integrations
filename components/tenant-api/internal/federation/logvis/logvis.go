// Package logvis holds a tenant's log visibility selection — the second tier
// of ADR-021's two-tier log visibility (§租戶自選的可見度子集).
//
// Tier one is the platform's: which log classes reach a tenant's VictoriaLogs
// partition at all (only federation_audit) and which fields a projected row
// keeps (helm/vector `tenantProjectionKeepFields`). Tier two is the tenant's:
// a subset of those fields it chooses to keep in its own copy.
//
// Where it is enforced: at write time, by Vector's tenant projection. The
// operator copies the selection into the tenant's `tenantProjections` entry
// (`visibleFields`), next to the AccountID it already copies from the
// registry; the projection then keeps only the always-visible fields plus the
// selected ones. tenant-api only stores and validates the selection: it never
// sits on the query path, and nothing here can make a field visible that the
// platform's allowlist does not already carry (Validate refuses any field
// outside SelectableFields, and the chart refuses any `visibleFields` entry
// outside `tenantProjectionKeepFields`).
//
// No selection (no record, or `fields: null`) means the platform default:
// every field the platform curates, including fields the platform adds later.
package logvis

import (
	"fmt"

	"github.com/vencil/tenant-api/internal/confd"
	"gopkg.in/yaml.v3"
)

// AlwaysVisibleFields are kept in every tenant row whatever the tenant
// selects: the partition's own identity (tenant_id, log_type), the event
// time, and log_event_id — the key the platform uses to find the full row in
// its own copy when a tenant reports a problem. They mirror helm/vector
// `tenantProjectionAlwaysVisibleFields` (pinned by TestVocabularyMatchesVectorChart).
var AlwaysVisibleFields = []string{"tenant_id", "log_type", "log_event_id", "timestamp"}

// SelectableFields are the platform-curated fields a tenant may keep or drop.
// AlwaysVisibleFields ∪ SelectableFields is exactly helm/vector
// `tenantProjectionKeepFields` (pinned by TestVocabularyMatchesVectorChart).
var SelectableFields = []string{"status", "method", "path", "query", "token_id", "duration_ms", "response_flags"}

// Selection is one tenant's log visibility record. Fields is nil when the
// tenant has made no selection (the platform default); a non-nil empty list
// keeps only AlwaysVisibleFields.
type Selection struct {
	Fields *[]string `yaml:"fields" json:"fields"`
}

// Violation is a single validation failure, shaped like the API's
// validation-error entries.
type Violation struct {
	Field  string `json:"field"`
	Reason string `json:"reason"`
}

// Parse decodes a selection record. An empty document is the platform
// default. parse-tree's verdict comes first, as for every state record.
func Parse(data []byte) (*Selection, error) {
	if err := confd.Judge(data); err != nil {
		return nil, err
	}
	var s Selection
	if err := yaml.Unmarshal(data, &s); err != nil {
		return nil, err
	}
	return &s, nil
}

// Validate checks a proposed selection: every field must be one the platform
// curates as selectable, and none may repeat. This is the containment rule —
// a selection can only narrow the platform's set, never add to it. A nil
// Fields (back to the default) is always valid.
func Validate(s *Selection) []Violation {
	if s.Fields == nil {
		return nil
	}
	selectable := toSet(SelectableFields)
	always := toSet(AlwaysVisibleFields)
	var v []Violation
	seen := make(map[string]bool, len(*s.Fields))
	for i, f := range *s.Fields {
		field := fmt.Sprintf("fields[%d]", i)
		switch {
		case seen[f]:
			v = append(v, Violation{field, fmt.Sprintf("duplicate field %q", f)})
		case always[f]:
			v = append(v, Violation{field, fmt.Sprintf("%q is always visible and cannot be selected or dropped", f)})
		case !selectable[f]:
			v = append(v, Violation{field, fmt.Sprintf("%q is not a field the platform makes visible to tenants", f)})
		}
		seen[f] = true
	}
	return v
}

// Effective is the selection as it applies under the current vocabulary:
// nil Fields stays nil (the default), and a stored list keeps only fields
// still selectable — read-repair for a record written against an older,
// larger vocabulary, mirroring fedpolicy.EffectiveSubset.
func Effective(s *Selection) *Selection {
	if s.Fields == nil {
		return &Selection{}
	}
	selectable := toSet(SelectableFields)
	out := []string{}
	for _, f := range *s.Fields {
		if selectable[f] {
			out = append(out, f)
		}
	}
	return &Selection{Fields: &out}
}

// VisibleFields is the full set of fields a tenant row keeps under s:
// AlwaysVisibleFields, then either every selectable field (no selection) or
// the selected ones, in vocabulary order.
func VisibleFields(s *Selection) []string {
	out := append([]string{}, AlwaysVisibleFields...)
	if s.Fields == nil {
		return append(out, SelectableFields...)
	}
	chosen := toSet(*s.Fields)
	for _, f := range SelectableFields {
		if chosen[f] {
			out = append(out, f)
		}
	}
	return out
}

func toSet(ss []string) map[string]bool {
	m := make(map[string]bool, len(ss))
	for _, s := range ss {
		m[s] = true
	}
	return m
}
