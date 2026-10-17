package gitops

import (
	"strings"
	"testing"
)

// #2859: the write side refuses `_metadata` written as a string — the schema
// says mapping, and after the exporter's decode the two forms are the same
// value, so the write is where the form can still be told apart.
func TestValidateShapeRefusesStringMetadata(t *testing.T) {
	refused := map[string]string{
		"quoted":              "tenants:\n  db-a:\n    _metadata: \"environment: production\"\n",
		"block":               "tenants:\n  db-a:\n    _metadata: |\n      environment: production\n",
		"plain":               "tenants:\n  db-a:\n    _metadata: production\n",
		"through an alias":    "tenants:\n  db-a:\n    x_note: &m \"owner: a\"\n    _metadata: *m\n",
		"through a merge key": "tenants:\n  db-a:\n    <<: {_metadata: \"owner: a\"}\n",
	}
	for name, body := range refused {
		t.Run("refused/"+name, func(t *testing.T) {
			_, errs := validateShape("db-a", body)
			if len(errs) != 1 || !strings.Contains(errs[0], "tenants.db-a._metadata must be a mapping") {
				t.Errorf("errs = %q, want the string-_metadata refusal", errs)
			}
		})
	}
	accepted := map[string]string{
		"mapping": "tenants:\n  db-a:\n    _metadata:\n      environment: production\n",
		"null":    "tenants:\n  db-a:\n    _metadata:\n",
		"absent":  "tenants:\n  db-a:\n    _silent_mode: warning\n",
		// A flat file's other section used to be accepted here (not this
		// write's to judge); since #3064 such a body is refused whole.
	}
	for name, body := range accepted {
		t.Run("accepted/"+name, func(t *testing.T) {
			if _, errs := validateShape("db-a", body); len(errs) != 0 {
				t.Errorf("errs = %q, want none", errs)
			}
		})
	}
}
