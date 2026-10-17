package confd

import (
	"sort"
	"strings"
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

// The node walk alone, held against the exporter shape by shape (#3064
// review). TenantFileManagement unions cfg.ParseTenantFile's ids into the
// walk's, so a property stated on it agrees with the exporter by
// construction and cannot fail; shapeVerdict leaves the union out, and this
// table is where the walk has to be right on its own.
//
// exporter is what cfg.ParseTenantFile reads: its tenant ids, sorted and
// comma-joined ("" for none), or "!err" when it rejects the file (it then
// serves no tenant of it: config_error). walk is shapeVerdict's verdict.
var shapeCases = []struct {
	name, stem, data, walk, exporter string
}{
	{"BOM", "db-a", "\ufefftenants:\n  db-a: {x: \"1\"}\n", "", "db-a"},
	{"CRLF", "db-a", "tenants:\r\n  db-a:\r\n    x: \"1\"\r\n", "", "db-a"},
	{"... end marker", "db-a", "tenants:\n  db-a: {}\n...\n", "", "db-a"},
	{"... end marker then another document", "db-a", "tenants:\n  db-a: {}\n...\n---\ntenants:\n  db-b: {}\n", GitManagedSeveralDocuments, "db-a"},
	{"empty document then content", "db-a", "---\n---\ntenants:\n  db-b: {}\n", GitManagedSeveralDocuments, ""},
	{"duplicate tenants key", "db-a", "tenants:\n  db-a: {}\ntenants:\n  db-b: {}\n", GitManagedNotTenantDocument, "!err"},
	{"duplicate tenant key", "db-a", "tenants:\n  db-a: {x: \"1\"}\n  db-a: {x: \"2\"}\n", "", "!err"},
	{"quoted keys", "db-a", "\"tenants\":\n  'db-a': {}\n", "", "db-a"},
	{"quoted key escaping to another id", "db-a", "tenants:\n  \"db\\x2Db\": {}\n", GitManagedIDMismatch, "db-b"},
	{"? complex keys", "db-a", "? tenants\n: ? db-a\n  : {}\n", "", "db-a"},
	{"!!binary key decoding to the stem", "db-a", "tenants:\n  !!binary ZGItYQ==: {}\n", GitManagedYAMLIndirection, "db-a"},
	// The stem is the key's TEXT: a walk that read k.Value would agree
	// with the stem while the exporter reads db-b.
	{"!!binary key decoding to another id", "ZGItYg==", "tenants:\n  !!binary ZGItYg==: {}\n", GitManagedYAMLIndirection, "db-b"},
	{"!!binary co-tenant key", "db-a", "tenants:\n  db-a: {}\n  ? !!binary ZGItYg==\n  : {}\n", GitManagedYAMLIndirection, "db-a,db-b"},
	{"!!binary tenants key", "db-a", "? !!binary dGVuYW50cw==\n: {db-b: {}}\n", GitManagedYAMLIndirection, "db-b"},
	{"literal block key", "db-a", "tenants:\n  ? |-\n    db-a\n  : {}\n", GitManagedYAMLIndirection, "db-a"},
	{"folded block key to another id", "db-a", "tenants:\n  ? >-\n    db-b\n  : {}\n", GitManagedYAMLIndirection, "db-b"},
	{"tag on the root", "db-a", "!!map\ntenants:\n  db-a: {}\n", "", "db-a"},
	{"local tag on the root", "db-a", "!foo\ntenants:\n  db-a: {}\n", "", "db-a"},
	{"tag on tenants", "db-a", "tenants: !!map\n  db-a: {}\n", "", "db-a"},
	{"local tag on tenants", "db-a", "tenants: !foo\n  db-a: {}\n", "", "db-a"},
	{"tag on the section", "db-a", "tenants:\n  db-a: !!map {x: \"1\"}\n", "", "db-a"},
	{"!!null tenants", "db-a", "tenants: !!null\n", "", ""},
	{"!!str tenants", "db-a", "tenants: !!str x\n", GitManagedNotTenantDocument, "!err"},
	{"%TAG shorthand key", "db-a", "%TAG !e! tag:yaml.org,2002:\n---\ntenants:\n  !e!str db-a: {}\n", GitManagedYAMLIndirection, "db-a"},
	{"<< at the top level", "db-a", "<<: {tenants: {db-b: {}}}\n", GitManagedYAMLIndirection, "db-b"},
	{"<< under tenants", "db-a", "tenants:\n  db-a: {}\n  <<: {db-b: {}}\n", GitManagedYAMLIndirection, "db-a,db-b"},
	{"quoted \"<<\" under tenants", "db-a", "tenants:\n  db-a: {}\n  \"<<\": {db-b: {}}\n", GitManagedYAMLIndirection, "<<,db-a"},
	{"!!merge <<", "db-a", "tenants:\n  db-a: {}\n  !!merge <<: {db-b: {}}\n", GitManagedYAMLIndirection, "db-a,db-b"},
	{"!!merge on another key", "db-a", "tenants:\n  db-a: {}\n  !!merge x: {db-b: {}}\n", GitManagedYAMLIndirection, "db-a,x"},
	{"int key", "123", "tenants:\n  123: {}\n", "", "123"},
	{"hex int key", "0x1F", "tenants:\n  0x1F: {}\n", "", "0x1F"},
	{"float key", "1.50", "tenants:\n  1.50: {}\n", "", "1.50"},
	{"bool key", "true", "tenants:\n  true: {}\n", "", "true"},
	{"yes key", "yes", "tenants:\n  yes: {}\n", "", "yes"},
	{"null key", "db-a", "tenants:\n  ~: {}\n", GitManagedIDMismatch, ""},
	{"null key beside the stem", "db-a", "tenants:\n  db-a: {}\n  null: {}\n", GitManagedTenantCount, "db-a"},
	{"flow-style root", "db-a", "{tenants: {db-a: {x: \"1\"}}}\n", "", "db-a"},
	{"flow-style root, two tenants", "db-a", "{tenants: {db-a: {}, db-b: {}}}\n", GitManagedTenantCount, "db-a,db-b"},
	{"%YAML directive", "db-a", "%YAML 1.1\n---\ntenants:\n  db-a: {}\n", "", "db-a"},
	{"tab indentation", "db-a", "tenants:\n\tdb-a: {}\n", GitManagedNotTenantDocument, "!err"},
	{"tab as separation", "db-a", "tenants:\t{db-a: {}}\n", "", "db-a"},
	{"invalid UTF-8 in a key", "db-a", "tenants:\n  db-\xffa: {}\n", GitManagedNotTenantDocument, "!err"},
	{"invalid UTF-8 in a value", "db-a", "tenants:\n  db-a: {x: \"\xff\"}\n", GitManagedNotTenantDocument, "!err"},
	{"escape decoding to another id", "db-a", "tenants:\n  \"db-\\xff\": {}\n", GitManagedIDMismatch, "db-\u00ff"},
	{"anchor on the tenant key", "db-a", "tenants:\n  &k db-a: {}\n", GitManagedYAMLIndirection, "db-a"},
	{"empty shell", "db-a", "tenants: {}\n", "", ""},
	{"body the exporter rejects", "db-a", "tenants:\n  db-a: {x: !!int abc}\n", "", "!err"},
}

// exporterReads is cfg.ParseTenantFile's reading of data in shapeCases'
// exporter notation, plus its id set.
func exporterReads(data []byte) (string, map[string]bool) {
	c, err := cfg.ParseTenantFile(data)
	if err != nil {
		return "!err", nil
	}
	set := map[string]bool{}
	ids := make([]string, 0, len(c.Tenants))
	for id := range c.Tenants {
		set[id] = true
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return strings.Join(ids, ","), set
}

// Pins both columns: the walk's verdict, and that the exporter really reads
// what the row says (a git-managed row is one where it reads another id, or
// would have to be trusted to).
func TestShapeVerdict(t *testing.T) {
	t.Parallel()
	for _, c := range shapeCases {
		if got := shapeVerdict([]byte(c.data), c.stem); got != c.walk {
			t.Errorf("%s: shapeVerdict = %q, want %q", c.name, got, c.walk)
		}
		if got := TenantFileManagement([]byte(c.data), c.stem); got != c.walk {
			t.Errorf("%s: TenantFileManagement = %q, want %q", c.name, got, c.walk)
		}
		if got, _ := exporterReads([]byte(c.data)); got != c.exporter {
			t.Errorf("%s: exporter reads %q, table says %q", c.name, got, c.exporter)
		}
	}
}

// The differential: on every shape (both tables), when the node walk ALONE
// calls a file API-shaped, the exporter reads exactly the ids the walk read
// — {stem}, or nothing for an empty shell — or rejects the file (serving no
// tenant of it). It does not look at the walk column, so a weakened walk
// fails here by what the exporter reads, not by a pinned verdict.
func TestShapeVerdict_AgreesWithTheExporter(t *testing.T) {
	t.Parallel()
	type row struct{ name, stem, data string }
	var rows []row
	for _, c := range shapeCases {
		rows = append(rows, row{c.name, c.stem, c.data})
	}
	for _, c := range managedCases {
		rows = append(rows, row{c.name, "db-a", c.data})
	}
	apiShaped := 0
	for _, c := range rows {
		if shapeVerdict([]byte(c.data), c.stem) != "" {
			continue
		}
		apiShaped++
		ids, reason := walkDeclaredIDs([]byte(c.data))
		if reason != "" {
			t.Fatalf("%s: walk reason %q under an API-shaped verdict", c.name, reason)
		}
		want := map[string]bool{}
		for _, id := range ids {
			want[id] = true
		}
		got, set := exporterReads([]byte(c.data))
		if got == "!err" {
			continue // the exporter serves no tenant of it
		}
		for id := range set {
			if id != c.stem {
				t.Errorf("%s: the walk calls it API-shaped for %q, but the exporter declares %q", c.name, c.stem, id)
			}
		}
		if len(set) != len(want) || (len(want) == 1 && !set[c.stem]) {
			t.Errorf("%s: the walk read %v, the exporter reads %q", c.name, ids, got)
		}
	}
	if apiShaped < 20 {
		t.Fatalf("only %d API-shaped rows: the differential checks too little", apiShaped)
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
