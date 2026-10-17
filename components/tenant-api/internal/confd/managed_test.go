package confd

import (
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// #3064: which tenant files are API-shaped. The stem is always "db-a"; the
// ids are what the exporter's decoder reads, so `ZGItYQ==` is "db-a" and
// `ZGItYg==` is "db-b" once a !!binary tag decodes them.
var managedCases = []struct {
	name, data, want string
}{
	// API-shaped.
	{"one tenant", "tenants:\n  db-a:\n    mysql_connections: \"70\"\n", ""},
	{"flow style", "tenants: {db-a: {mysql_connections: \"70\"}}\n", ""},
	{"anchor and alias inside own section", "tenants:\n  db-a:\n    _metadata: &m\n      owner: x\n    _routing:\n      meta: *m\n", ""},
	{"merge key inside own section", "tenants:\n  db-a:\n    base: &b {x: \"1\"}\n    more:\n      <<: *b\n      y: \"2\"\n", ""},
	{"other plain top-level key", "x: 1\ntenants:\n  db-a: {}\n", ""},
	{"trailing empty document", "tenants:\n  db-a: {}\n---\n", ""},
	{"trailing null document", "tenants:\n  db-a: {}\n---\n~\n", ""},
	{"trailing comment-only document", "tenants:\n  db-a: {}\n---\n# nothing\n", ""},
	{"same key twice is still the one tenant", "tenants:\n  db-a: {x: \"1\"}\n  db-a: {x: \"2\"}\n", ""},
	{"empty file", "", ""},
	{"whitespace only", " \n\n", ""},
	{"comments only", "# nothing yet\n", ""},
	{"null root", "~\n", ""},
	{"no tenants key", "x: 1\n", ""},
	{"null tenants", "tenants:\n", ""},
	{"empty tenants", "tenants: {}\n", ""},

	// Git-managed.
	{"two tenants", "tenants:\n  db-a: {}\n  db-b: {}\n", GitManagedTenantCount},
	{"stem mismatch", "tenants:\n  db-b: {}\n", GitManagedIDMismatch},
	{"!!binary co-tenant key decoding to own id",
		"tenants:\n  db-a: {x: \"1\"}\n  ? !!binary ZGItYQ==\n  : {secret: \"2\"}\n", GitManagedYAMLIndirection},
	{"!!binary key decoding to another id", "tenants:\n  !!binary ZGItYg==: {}\n", GitManagedYAMLIndirection},
	{"!!binary tenants key", "? !!binary dGVuYW50cw==\n: {db-a: {}, db-b: {}}\n", GitManagedYAMLIndirection},
	{"!!str tenant key", "tenants:\n  !!str db-a: {}\n", GitManagedYAMLIndirection},
	{"!!str tenants key", "!!str tenants:\n  db-a: {}\n", GitManagedYAMLIndirection},
	{"quoted tenant key", "tenants:\n  \"db-a\": {}\n", ""},
	{"single-quoted keys", "'tenants':\n  'db-a': {}\n", ""},
	{"quoted key with an escape decodes to its text", "tenants:\n  \"db\\x2Da\": {}\n", ""},
	{"quoted key decoding to another id", "tenants:\n  \"db\\x2Db\": {}\n", GitManagedIDMismatch},
	{"block scalar key", "tenants:\n  ? |-\n    db-a\n  : {}\n", GitManagedYAMLIndirection},
	{"non-scalar tenant key", "tenants:\n  ? [db-a]\n  : {}\n", GitManagedYAMLIndirection},
	{"alias as tenant key", "tenants:\n  db-a: {k: &k db-b}\n  *k : {}\n", GitManagedYAMLIndirection},
	{"merge key at the top level", "x: &t\n  tenants: {db-b: {}}\n<<: *t\ntenants:\n  db-a: {}\n", GitManagedYAMLIndirection},
	{"merge key directly under tenants", "x: &t\n  db-b: {}\ntenants:\n  db-a: {}\n  <<: *t\n", GitManagedYAMLIndirection},
	{"anchor on the root", "&r\ntenants:\n  db-a: {}\n", GitManagedYAMLIndirection},
	{"anchor on a top-level value", "x: &v {y: 1}\ntenants:\n  db-a: {}\n", GitManagedYAMLIndirection},
	{"anchor on tenants", "tenants: &t\n  db-a: {}\n", GitManagedYAMLIndirection},
	{"anchor on the section", "tenants:\n  db-a: &s {x: \"1\"}\n", GitManagedYAMLIndirection},
	{"tenants is an alias", "x: &t\n  db-a: {}\ntenants: *t\n", GitManagedYAMLIndirection},
	{"section is an alias", "x: &s {y: \"1\"}\ntenants:\n  db-a: *s\n", GitManagedYAMLIndirection},
	{"two documents", "tenants:\n  db-a: {}\n---\ntenants:\n  db-b: {}\n", GitManagedSeveralDocuments},
	{"own tenant in a second document", "---\n---\ntenants:\n  db-a: {}\n", GitManagedSeveralDocuments},
	{"not YAML", "tenants:\n  db-a: {\n", GitManagedNotTenantDocument},
	{"root is a list", "- tenants\n", GitManagedNotTenantDocument},
	{"tenants is a list", "tenants:\n  - db-a\n", GitManagedNotTenantDocument},
	{"tenants twice", "tenants:\n  db-a: {}\ntenants:\n  db-a: {}\n", GitManagedNotTenantDocument},
}

func TestTenantFileManagement(t *testing.T) {
	t.Parallel()
	for _, c := range managedCases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			if got := TenantFileManagement([]byte(c.data), "db-a"); got != c.want {
				t.Errorf("TenantFileManagement(%q) = %q, want %q", c.data, got, c.want)
			}
		})
	}
}

// The verdict can never disagree with the exporter: on every case above, a
// file called API-shaped is one whose tenants, as cfg.ParseTenantFile
// decodes them, are the stem or none. (The `!!binary` co-tenant row is
// where a text-only reading would fail: its key reads as a base64 string.)
func TestTenantFileManagement_NeverDisagreesWithTheExporter(t *testing.T) {
	t.Parallel()
	for _, c := range managedCases {
		if TenantFileManagement([]byte(c.data), "db-a") != "" {
			continue
		}
		parsed, err := cfg.ParseTenantFile([]byte(c.data))
		if err != nil {
			continue // the exporter serves nothing from a file it rejects
		}
		for id := range parsed.Tenants {
			if id != "db-a" {
				t.Errorf("%s: API-shaped, but the exporter declares %q", c.name, id)
			}
		}
	}
}

// The exporter's own set is consulted, not only the node walk: an id the
// walk could not see is still counted.
func TestDeclaredVerdict(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		ids  []string
		want string
	}{
		{nil, ""},
		{[]string{"db-a"}, ""},
		{[]string{"db-a", "db-a"}, ""},
		{[]string{"db-b"}, GitManagedIDMismatch},
		{[]string{"db-a", "db-b"}, GitManagedTenantCount},
	} {
		if got := declaredVerdict(c.ids, "db-a"); got != c.want {
			t.Errorf("declaredVerdict(%v) = %q, want %q", c.ids, got, c.want)
		}
	}
}
