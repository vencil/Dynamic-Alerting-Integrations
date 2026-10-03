// Package tenantid is the Go reader of the tenant-id rule (ADR-035).
//
// The rule is written once, as definitions.tenantId of
// docs/schemas/tenant-config.schema.json. A Go binary cannot read the repo's
// docs/ at runtime, so tenant_id.json — generated from that definition by
// scripts/tools/dx/gen_tenant_id_json.py and drift-gated by the
// tenant-id-json-check pre-commit hook and CI — is embedded here. Edit the
// schema, then run `make tenant-id-json`; never edit tenant_id.json by hand.
//
// It depends on the standard library only, so da-guard, the routing checks
// and tenant-api can all import it.
package tenantid

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
)

//go:embed tenant_id.json
var ruleJSON []byte

var (
	// Pattern is the tenant-id pattern, anchored with ^…$. Go's RE2 `$` does
	// not match before a trailing "\n", so a MatchString is a full match.
	Pattern string
	// Description states the rule in plain words; error messages cite it.
	Description string

	re *regexp.Regexp
)

func init() {
	var rule struct {
		Pattern     string `json:"pattern"`
		Description string `json:"description"`
	}
	// Build-time data: a malformed copy is a generator bug, not an input error.
	if err := json.Unmarshal(ruleJSON, &rule); err != nil {
		panic(fmt.Sprintf("tenantid: embedded tenant_id.json: %v", err))
	}
	if rule.Pattern == "" || rule.Description == "" {
		panic("tenantid: embedded tenant_id.json lacks pattern or description")
	}
	Pattern, Description = rule.Pattern, rule.Description
	re = regexp.MustCompile(Pattern)
}

// Valid reports whether id satisfies the tenant-id rule.
func Valid(id string) bool {
	return re.MatchString(id)
}
