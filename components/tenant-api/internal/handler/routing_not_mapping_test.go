package handler

// #2341 R5: PUT /api/v1/tenants/{id} refuses a `_routing` the body writes that
// is neither a mapping nor a disabling string, as the route generator's PyYAML
// reads it (an unquoted `off` is a boolean), with 400 INVALID_BODY and nothing
// written. A disabling string and a mapping go through.

import (
	"net/http"
	"strings"
	"testing"
)

func TestPutTenant_RoutingNotMapping(t *testing.T) {
	cases := []struct {
		name, routing string
		refused, hint bool
	}{
		{"string", `"slack"`, true, false},
		{"list", "[x]", true, false},
		{"null", "~", true, false},
		{"unquoted false", "false", true, true},
		{"unquoted off", "off", true, true},
		{"unquoted no", "no", true, true},
		{"quoted off disables", `"off"`, false, false},
		{"disable", "disable", false, false},
		{"empty mapping", "{}", false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			code, resp, written := putReceiverDoc(t, "tenants:\n  rs-t:\n    _routing: "+tc.routing+"\n", false)
			if !tc.refused {
				if code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body: %s", code, resp)
				}
				return
			}
			if code != http.StatusBadRequest || !strings.Contains(resp, "INVALID_BODY") ||
				!strings.Contains(resp, "tenants.rs-t._routing") ||
				!strings.Contains(resp, "_routing must be a mapping or a disabling string") {
				t.Fatalf("status = %d, want 400 INVALID_BODY on tenants.rs-t._routing; body: %s", code, resp)
			}
			if got := strings.Contains(resp, "quoted string ('off')"); got != tc.hint {
				t.Errorf("boolean hint present = %v, want %v; body: %s", got, tc.hint, resp)
			}
			if written != "" {
				t.Errorf("refused PUT wrote the tenant file:\n%s", written)
			}
		})
	}
}
