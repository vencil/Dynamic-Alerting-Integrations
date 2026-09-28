// Package receiverspec is the Go copy of the tenant receiver contract: which
// receiver types exist, which fields each one requires, and which values
// Alertmanager would refuse to load (#2295).
//
// One Go copy, three consumers: the da-guard routing checks
// (internal/guard), the routing resolver in pkg/config, and the tenant-api
// PUT handler, which cannot import internal/guard. It depends on the standard
// library only, so every one of them can import it (import_direction_test.go
// and depguard's `logic-no-nethttp` keep net/http out).
//
// The hub of the contract is docs/schemas/tenant-config.schema.json. Two
// tests pin the other copies to it, each reading the schema as JSON:
// TestSpecs_MatchSchema (this package) and
// tests/shared/test_receiver_spec_parity.py (Python RECEIVER_TYPES,
// scripts/tools/_lib_constants.py). Behaviour is pinned by the shared case
// table testdata/receiver_presence_cases.json: this package, the guard,
// pytest (schema + route generator) and tests/alertmanager-inhibit
// (Alertmanager config.Load, the `am` column) all assert it.
//
// Check reports problems in neutral terms (a Kind, a field path relative to
// the receiver, a message without tenant context); each caller renders them
// in its own contract — da-guard findings, tenant-api 400 violations.
package receiverspec

import (
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"
)

// Spec is the contract of one receiver type.
//
//   - Required:     every field must be set ("" and null count as unset).
//   - ExactlyOneOf: per group, exactly one field must be set.
//   - Patterns:     the format a Required string field must match (a copy
//     of the schema `pattern` the property references).
//   - StringLists:  Required fields the schema types as a list of non-empty
//     strings (email `to`); a plain string is still taken.
//   - Bools:        optional fields the schema types as boolean. When the key
//     is present its value must be true or false — Alertmanager refuses the
//     whole config over `send_resolved: maybe`.
//   - HTTPConfig:   the type accepts `http_config` (checkHTTPConfig).
type Spec struct {
	Required     []string
	ExactlyOneOf [][]string
	Patterns     map[string]string
	StringLists  []string
	Bools        []string
	HTTPConfig   bool
}

// Copies of the `pattern`s in tenant-config.schema.json
// (definitions.receiverHttpUrl / receiverSmtpHostPort / receiverProxyUrl),
// which hold the only authored copy and the reasoning; the Go binaries cannot
// read the schema at run time. TestSpecs_MatchSchema fails on any difference.
const (
	HTTPURLPattern      = `^[Hh][Tt][Tt][Pp][Ss]?://(([A-Za-z0-9._~!$&'()*+,;=:-]|%[0-9A-Fa-f]{2})+@)?(([A-Za-z0-9._~!$&'()*+,;=<>"-]|[^\x00-\x7f]|%(25|[89A-Fa-f][0-9A-Fa-f]))+|\[[0-9A-Fa-f:.]+\])(:[0-9]*)?(/([^\x00-\x20\x7f%?#]|%[0-9A-Fa-f]{2})*)?(\?[^\x00-\x20\x7f#]*)?(#([^\x00-\x20\x7f%]|%[0-9A-Fa-f]{2})*)?$`
	SMTPHostPortPattern = `^([^\x00-\x20\x7f:/?#@\[\]\\]+|\[[0-9A-Fa-f:.]+\]):[0-9]+$`
	ProxyURLPattern     = `^([Hh][Tt][Tt][Pp][Ss]?|[Ss][Oo][Cc][Kk][Ss]5[Hh]?)://(([A-Za-z0-9._~!$&'()*+,;=:-]|%[0-9A-Fa-f]{2})+@)?(([A-Za-z0-9._~!$&'()*+,;=<>"-]|[^\x00-\x7f]|%(25|[89A-Fa-f][0-9A-Fa-f]))+|\[[0-9A-Fa-f:.]+\])(:[0-9]*)?(/([^\x00-\x20\x7f%?#]|%[0-9A-Fa-f]{2})*)?(\?[^\x00-\x20\x7f#]*)?(#([^\x00-\x20\x7f%]|%[0-9A-Fa-f]{2})*)?$`
)

var specs = map[string]Spec{
	"webhook": {
		Required:   []string{"url"},
		Patterns:   map[string]string{"url": HTTPURLPattern},
		Bools:      []string{"send_resolved"},
		HTTPConfig: true,
	},
	"email": {
		Required:    []string{"to", "smarthost", "from"},
		Patterns:    map[string]string{"smarthost": SMTPHostPortPattern},
		StringLists: []string{"to"},
		Bools:       []string{"require_tls", "send_resolved"},
	},
	"slack": {
		Required: []string{"api_url"},
		Patterns: map[string]string{"api_url": HTTPURLPattern},
		Bools:    []string{"send_resolved"},
	},
	"teams": {
		Required: []string{"webhook_url"},
		Patterns: map[string]string{"webhook_url": HTTPURLPattern},
		Bools:    []string{"send_resolved"},
	},
	"rocketchat": {
		Required: []string{"url"},
		Patterns: map[string]string{"url": HTTPURLPattern},
		Bools:    []string{"send_resolved"},
	},
	// Alertmanager accepts both keys at once but then uses the Events API
	// v1 (service_key) and silently ignores routing_key
	// (notify/pagerduty/pagerduty.go), so both-set is rejected too.
	"pagerduty": {
		ExactlyOneOf: [][]string{{"service_key", "routing_key"}},
		Bools:        []string{"send_resolved"},
	},
}

// HTTPConfigAuthFields are the http_config keys Alertmanager accepts at most
// one of: prometheus/common HTTPClientConfig.Validate rejects every pair of
// them (measured with amtool 0.34.1 and config.Load 0.33.1; rows in
// testdata/receiver_presence_cases.json). Python holds the same list as
// _lib_constants.HTTP_CONFIG_AUTH_FIELDS; the table pins both.
var HTTPConfigAuthFields = []string{"basic_auth", "oauth2", "authorization", "bearer_token", "bearer_token_file"}

// httpConfigMapFields are the auth keys whose value is a mapping; the others
// in HTTPConfigAuthFields are strings.
var httpConfigMapFields = []string{"basic_auth", "oauth2", "authorization"}

// Lookup returns the contract of a receiver type. The type must match
// exactly — no case folding or trimming (#2180).
func Lookup(rtype string) (Spec, bool) {
	s, ok := specs[rtype]
	return s, ok
}

// Known reports whether rtype is a supported receiver type.
func Known(rtype string) bool {
	_, ok := specs[rtype]
	return ok
}

// SupportedTypes returns the receiver types in alphabetical order.
func SupportedTypes() []string {
	out := make([]string, 0, len(specs))
	for k := range specs {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Kind classifies a Problem. Callers map it onto their own vocabulary
// (da-guard FindingKind, tenant-api violations).
type Kind string

const (
	// KindNotObject: the receiver itself is missing or not a mapping.
	KindNotObject Kind = "not_object"
	// KindMissingType: `type` is absent, empty or not a string.
	KindMissingType Kind = "missing_type"
	// KindUnknownType: `type` is not a supported receiver type.
	KindUnknownType Kind = "unknown_type"
	// KindMissing: a required field (or every field of an exactly-one
	// group) is unset.
	KindMissing Kind = "missing"
	// KindInvalid: a value of the wrong type or format.
	KindInvalid Kind = "invalid"
	// KindConflicting: more than one field of an exactly-one or at-most-one
	// group is set.
	KindConflicting Kind = "conflicting"
)

// Problem is one reason a receiver would not load. Field is relative to the
// receiver ("" for the receiver itself, "type", "url",
// "http_config.proxy_url"). Message carries no tenant or path context.
type Problem struct {
	Kind    Kind
	Field   string
	Message string
}

// Check returns every problem of one receiver, or nil. An unusable receiver
// (not a mapping, no type, unknown type) yields exactly one problem, since
// the field checks need a type to be meaningful.
func Check(receiver any) []Problem {
	m, ok := receiver.(map[string]any)
	if !ok || m == nil {
		return []Problem{{Kind: KindNotObject, Message: "receiver is missing or not an object; routing requires a receiver dict with `type`"}}
	}
	rtype, _ := m["type"].(string)
	if rtype == "" {
		return []Problem{{Kind: KindMissingType, Field: "type", Message: "receiver type is missing or empty; receivers must declare a type"}}
	}
	spec, known := specs[rtype]
	if !known {
		return []Problem{{Kind: KindUnknownType, Field: "type", Message: fmt.Sprintf(
			"receiver type %q is not a supported receiver type (supported: %s)", rtype, strings.Join(SupportedTypes(), ", "))}}
	}
	var out []Problem
	for _, field := range spec.Required {
		if p, bad := requiredProblem(rtype, spec, m, field); bad {
			out = append(out, p)
		}
	}
	for _, group := range spec.ExactlyOneOf {
		if p, bad := exactlyOneProblem(rtype, m, group); bad {
			out = append(out, p)
		}
	}
	for _, field := range spec.Bools {
		v, present := m[field]
		if _, isBool := v.(bool); present && !isBool {
			out = append(out, Problem{Kind: KindInvalid, Field: field, Message: fmt.Sprintf(
				"receiver type %q field %q must be true or false, got %s", rtype, field, describe(v))})
		}
	}
	if v, present := m["http_config"]; present && spec.HTTPConfig {
		out = append(out, checkHTTPConfig(rtype, v)...)
	}
	return out
}

// compiled holds every pattern, compiled once (MustCompile: a bad copy fails
// at init, and TestSpecs_MatchSchema pins the copies to the schema).
var compiled = func() map[string]*regexp.Regexp {
	out := map[string]*regexp.Regexp{ProxyURLPattern: regexp.MustCompile(ProxyURLPattern)}
	for _, spec := range specs {
		for _, p := range spec.Patterns {
			if _, ok := out[p]; !ok {
				out[p] = regexp.MustCompile(p)
			}
		}
	}
	return out
}()

// requiredProblem checks one Required field by the schema's type (#2180),
// the same rule as _lib_validation.receiver_required_problem on the Python
// side.
//
//   - nil (absent, or a YAML key with no value) or "" is missing, as in
//     Alertmanager, whose config decodes both to the zero value.
//   - A string must match the field's Patterns entry, if any.
//   - A StringLists field given as a list needs at least one item, each a
//     non-empty string: the pipeline joins it into Alertmanager's `to`
//     string, where [""] reads as no address. A plain string is still
//     taken — Alertmanager's own `to` is a string.
//   - Any other type is an error. For some (email from: 0) that is
//     stricter than Alertmanager, which renders them as text; for most
//     (a list, or url: 0) Alertmanager rejects the config.
func requiredProblem(rtype string, spec Spec, receiver map[string]any, field string) (Problem, bool) {
	missing := func(format string, args ...any) (Problem, bool) {
		return Problem{Kind: KindMissing, Field: field,
			Message: fmt.Sprintf("receiver type %q ", rtype) + fmt.Sprintf(format, args...)}, true
	}
	invalid := func(format string, args ...any) (Problem, bool) {
		return Problem{Kind: KindInvalid, Field: field,
			Message: fmt.Sprintf("receiver type %q field %q ", rtype, field) + fmt.Sprintf(format, args...)}, true
	}
	switch v := receiver[field].(type) {
	case nil:
		return missing("requires field %q", field)
	case string:
		if v == "" {
			return missing("field %q is present but empty string", field)
		}
		if p, ok := spec.Patterns[field]; ok && !compiled[p].MatchString(v) {
			return invalid("value %q is not in the format tenant-config.schema.json requires", v)
		}
		return Problem{}, false
	case []any:
		if !slices.Contains(spec.StringLists, field) {
			return invalid("must be a string, got %s", describe(v))
		}
		if len(v) == 0 {
			return missing("field %q is present but an empty list", field)
		}
		for i, item := range v {
			if s, ok := item.(string); !ok || s == "" {
				return invalid("item %d must be a non-empty string, got %#v", i, item)
			}
		}
		return Problem{}, false
	default:
		return invalid("must be a string, got %s", describe(v))
	}
}

// exactlyOneProblem checks one ExactlyOneOf group, by the schema's type rule
// (`type: ["string", "null"]`), the same rule as
// _lib_validation.receiver_field_state on the Python side. A non-string
// value is an error — stricter than Alertmanager on purpose: it renders 0 as
// "0" but fails to load a list, so no "counts as given" rule fits all of
// them. "Exactly one" is judged only once every field's type is valid.
func exactlyOneProblem(rtype string, receiver map[string]any, group []string) (Problem, bool) {
	var set []string
	for _, field := range group {
		switch v := receiver[field].(type) {
		case nil:
		case string:
			if v != "" {
				set = append(set, field)
			}
		default:
			return Problem{Kind: KindInvalid, Field: field, Message: fmt.Sprintf(
				"receiver type %q field %q must be a string, got %s", rtype, field, describe(v))}, true
		}
	}
	if len(set) == 1 {
		return Problem{}, false
	}
	names := quoteAll(group)
	if len(set) == 0 {
		return Problem{Kind: KindMissing, Field: group[0], Message: fmt.Sprintf(
			"receiver type %q requires exactly one of %s; none is set", rtype, names)}, true
	}
	msg := fmt.Sprintf("receiver type %q requires exactly one of %s; %d are set", rtype, names, len(set))
	if rtype == "pagerduty" {
		msg += " (Alertmanager would use the Events API v1 via service_key and silently ignore routing_key)"
	}
	return Problem{Kind: KindConflicting, Field: set[len(set)-1], Message: msg}, true
}

// checkHTTPConfig checks the parts of `http_config` that make Alertmanager
// refuse the whole config (#2295), by the schema's types:
//
//   - http_config itself must be a mapping (null included: the schema types
//     it as an object; Alertmanager would take null as absent).
//   - basic_auth / oauth2 / authorization must be mappings and
//     bearer_token / bearer_token_file strings when present; "" counts as
//     unset. At most one of HTTPConfigAuthFields may be set — an empty
//     mapping counts as set, as it does for Alertmanager.
//   - proxy_url must be a string matching ProxyURLPattern: an http, https,
//     socks5 or socks5h URL with a host. Stricter than Alertmanager, which
//     takes anything net/url parses (`foo`, `ftp://h`, `http://`); every
//     string the pattern accepts also parses there.
//
// Not modelled: the fields inside the auth mappings (oauth2 needs client_id
// and token_url), tls_config, and the other proxy keys.
func checkHTTPConfig(rtype string, v any) []Problem {
	hc, ok := v.(map[string]any)
	if !ok || hc == nil {
		return []Problem{{Kind: KindInvalid, Field: "http_config", Message: fmt.Sprintf(
			"receiver type %q field \"http_config\" must be a mapping, got %s", rtype, describe(v))}}
	}
	var out []Problem
	var set []string
	for _, key := range HTTPConfigAuthFields {
		val, present := hc[key]
		if !present {
			continue
		}
		field := "http_config." + key
		if slices.Contains(httpConfigMapFields, key) {
			if m, isMap := val.(map[string]any); !isMap || m == nil {
				out = append(out, Problem{Kind: KindInvalid, Field: field, Message: fmt.Sprintf(
					"receiver type %q field %q must be a mapping, got %s", rtype, field, describe(val))})
				continue
			}
			set = append(set, key)
			continue
		}
		s, isString := val.(string)
		if !isString {
			out = append(out, Problem{Kind: KindInvalid, Field: field, Message: fmt.Sprintf(
				"receiver type %q field %q must be a string, got %s", rtype, field, describe(val))})
			continue
		}
		if s != "" {
			set = append(set, key)
		}
	}
	if len(set) > 1 {
		out = append(out, Problem{Kind: KindConflicting, Field: "http_config." + set[len(set)-1], Message: fmt.Sprintf(
			"receiver type %q http_config sets %s; Alertmanager accepts at most one of %s",
			rtype, quoteAll(set), quoteAll(HTTPConfigAuthFields))})
	}
	if val, present := hc["proxy_url"]; present {
		s, isString := val.(string)
		switch {
		case !isString:
			out = append(out, Problem{Kind: KindInvalid, Field: "http_config.proxy_url", Message: fmt.Sprintf(
				"receiver type %q field \"http_config.proxy_url\" must be a string, got %s", rtype, describe(val))})
		case !compiled[ProxyURLPattern].MatchString(s):
			out = append(out, Problem{Kind: KindInvalid, Field: "http_config.proxy_url", Message: fmt.Sprintf(
				"receiver type %q field \"http_config.proxy_url\" value %q is not an http, https, socks5 or socks5h URL with a host (tenant-config.schema.json receiverProxyUrl)",
				rtype, s)})
		}
	}
	return out
}

func quoteAll(fields []string) string {
	quoted := make([]string, len(fields))
	for i, f := range fields {
		quoted[i] = fmt.Sprintf("%q", f)
	}
	return strings.Join(quoted, ", ")
}

// describe names a decoded YAML/JSON value's kind for messages.
func describe(v any) string {
	switch x := v.(type) {
	case nil:
		return "null"
	case string:
		return fmt.Sprintf("string %q", x)
	case bool:
		return fmt.Sprintf("boolean %v", x)
	case int, int64, uint64, float64:
		return fmt.Sprintf("number %v", x)
	case []any:
		return "a list"
	case map[string]any:
		return "a mapping"
	default:
		return fmt.Sprintf("%T", v)
	}
}
