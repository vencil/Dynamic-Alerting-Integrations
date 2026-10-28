package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/staterestore"
	"github.com/vencil/tenant-api/internal/tenantorg"
	"github.com/vencil/tenant-api/internal/views"
)

// #2884: `tenant-api state restore` against a dir store, end to end through
// the flags. Settings come from flags here; the pod passes them as TA_* env.
func TestStateRestoreCmd_DirStore(t *testing.T) {
	root := t.TempDir()
	stateDir := filepath.Join(root, "state")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	cur := "schema_version: v1\nnext_account_id: 1002\nallocations:\n  t-a: 1000\n  t-b: 1001\n"
	if err := os.WriteFile(filepath.Join(stateDir, "_account_registry.yaml"), []byte(cur), 0o644); err != nil {
		t.Fatal(err)
	}
	backup := "kind: ConfigMap\ndata:\n  _account_registry.yaml: |\n    schema_version: v1\n    next_account_id: 1001\n    allocations:\n      t-a: 1000\n  _groups.yaml: |\n    groups: {}\n"
	base := []string{"restore", "--from", "-", "--state-store", "dir", "--state-dir", stateDir,
		"--config-dir", filepath.Join(root, "conf.d"), "--federation-key", "", "--account-id-margin", "0"}

	run := func(extra ...string) (int, string, string) {
		var out, errb bytes.Buffer
		code := runStateCmd(append(append([]string{}, base...), extra...), strings.NewReader(backup), &out, &errb)
		return code, out.String(), errb.String()
	}

	code, out, errOut := run("--dry-run", "--keys", "_groups.yaml")
	if code != 0 || !strings.Contains(out, "_groups.yaml: replace") || !strings.Contains(out, "_account_registry.yaml: unchanged") {
		t.Fatalf("dry run: code %d\nstdout:\n%s\nstderr:\n%s", code, out, errOut)
	}
	if _, err := os.Stat(filepath.Join(stateDir, "_groups.yaml")); err == nil {
		t.Error("dry run wrote _groups.yaml")
	}

	if code, out, errOut := run("--keys", "_groups.yaml"); code != 0 {
		t.Fatalf("restore: code %d\nstdout:\n%s\nstderr:\n%s", code, out, errOut)
	}
	if b, _ := os.ReadFile(filepath.Join(stateDir, "_groups.yaml")); string(b) != "groups: {}\n" {
		t.Errorf("_groups.yaml = %q", b)
	}
	if b, _ := os.ReadFile(filepath.Join(stateDir, "_account_registry.yaml")); string(b) != cur {
		t.Errorf("registry changed by restoring an older backup:\n%s", b)
	}

	if code, _, _ := run("--keys", "_account_registry.yaml"); code != 2 {
		t.Errorf("--keys naming the registry: code %d, want 2", code)
	}
	var errb bytes.Buffer
	if code := runStateCmd([]string{"export"}, nil, &bytes.Buffer{}, &errb); code != 2 || !strings.Contains(errb.String(), "usage:") {
		t.Errorf("unknown verb: code %d, stderr %q", code, errb.String())
	}
}

// #2928: without --account-id-margin the counter goes twice the fleet (at
// least 10000) above the backup's, once — the same restore again changes
// nothing. Flag misuse is a usage error, including --no-backup without
// federation, where it would have nothing to merge.
func TestStateRestoreCmd_MarginAndNoBackup(t *testing.T) {
	root := t.TempDir()
	stateDir := filepath.Join(root, "state")
	confDir := filepath.Join(root, "conf.d")
	for _, d := range []string{stateDir, confDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for _, tn := range []string{"t-a", "t-b"} {
		if err := os.WriteFile(filepath.Join(confDir, tn+".yaml"), []byte("tenants: {}\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	reg := filepath.Join(stateDir, "_account_registry.yaml")
	if err := os.WriteFile(reg, []byte("schema_version: v1\nnext_account_id: 1001\nallocations:\n  t-a: 1000\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	backup := "kind: ConfigMap\ndata:\n  _account_registry.yaml: |\n    schema_version: v1\n    next_account_id: 1001\n    allocations:\n      t-a: 1000\n"
	common := []string{"--state-store", "dir", "--state-dir", stateDir, "--config-dir", confDir, "--federation-key", ""}
	run := func(args ...string) (int, string) {
		var out, errb bytes.Buffer
		code := runStateCmd(append(append([]string{"restore"}, args...), common...), strings.NewReader(backup), &out, &errb)
		return code, out.String() + errb.String()
	}

	for i := 0; i < 2; i++ {
		if code, out := run("--from", "-"); code != 0 {
			t.Fatalf("restore #%d: code %d\n%s", i+1, code, out)
		}
		if b, _ := os.ReadFile(reg); !strings.Contains(string(b), "next_account_id: 11001\n") {
			t.Fatalf("after restore #%d the registry is\n%s\nwant next_account_id 11001 (1001 + the 10000 floor)", i+1, b)
		}
	}

	for name, args := range map[string][]string{
		"neither --from nor --no-backup": {},
		"both":                           {"--from", "-", "--no-backup"},
		"--no-backup with --keys":        {"--no-backup", "--keys", "_groups.yaml"},
		"--no-backup with a margin":      {"--no-backup", "--account-id-margin", "5"},
		"--no-backup without federation": {"--no-backup"},
		"negative margin":                {"--from", "-", "--account-id-margin", "-5"},
	} {
		if code, out := run(args...); code != 2 {
			t.Errorf("%s: code %d, want 2\n%s", name, code, out)
		}
	}
}

// A backup without a registry record gets no margin, so the default margin's
// conf.d count is not needed: a missing conf.d does not fail the restore.
func TestStateRestoreCmd_NoRegistryInBackup_NeedsNoConfDir(t *testing.T) {
	stateDir := filepath.Join(t.TempDir(), "state")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	backup := "kind: ConfigMap\ndata:\n  _groups.yaml: |\n    groups: {}\n"
	var out, errb bytes.Buffer
	code := runStateCmd([]string{"restore", "--from", "-", "--keys", "_groups.yaml",
		"--state-store", "dir", "--state-dir", stateDir,
		"--config-dir", filepath.Join(t.TempDir(), "absent"), "--federation-key", ""},
		strings.NewReader(backup), &out, &errb)
	if code != 0 {
		t.Fatalf("code %d\nstdout:\n%s\nstderr:\n%s", code, out.String(), errb.String())
	}
}

// ADR-036 §12: `tenant-api state show` and `config check` print what the
// server's own parse functions make of the bytes.

type showOut struct {
	Schema           string `json:"schema"`
	TenantAPIVersion string `json:"tenant_api_version"`
	Records          []struct {
		Key     string          `json:"key"`
		Record  json.RawMessage `json:"record"`
		Problem *struct {
			Kind    string `json:"kind"`
			Message string `json:"message"`
		} `json:"problem"`
	} `json:"records"`
}

func runShow(t *testing.T, stdin string, args ...string) (int, showOut, string) {
	t.Helper()
	var out, errb bytes.Buffer
	code := runStateCmd(append([]string{"show"}, args...), strings.NewReader(stdin), &out, &errb)
	var doc showOut
	if code == 0 {
		if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
			t.Fatalf("output is not JSON: %v\n%s", err, out.String())
		}
	}
	return code, doc, errb.String()
}

func writeFiles(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	for n, c := range files {
		if err := os.WriteFile(filepath.Join(dir, n), []byte(c), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// goodState holds one record of every kind the store keeps.
var goodState = map[string]string{
	"_groups.yaml":            "groups:\n  g1:\n    label: G1\n    members: [t-a, 010]\n",
	"_views.yaml":             "views:\n  v1:\n    label: V1\n    filters:\n      tenant: t-a\n",
	"_federation_policy.yaml": "whitelist:\n  - metric: up\n",
	"_federation.t-a.yaml":    "metrics: [up]\n",
	"_account_registry.yaml":  "schema_version: v1\nnext_account_id: 1002\nallocations:\n  t-a: 1000\n  t-b: 1001\n",
}

// parsedJSON is what the parse function the server uses makes of a record,
// as JSON.
func parsedJSON(t *testing.T, key, data string) []byte {
	t.Helper()
	var (
		v   any
		err error
	)
	switch key {
	case "_groups.yaml":
		v, err = groups.ParseConfig([]byte(data))
	case "_views.yaml":
		v, err = views.ParseConfig([]byte(data))
	case "_federation_policy.yaml":
		v, err = fedpolicy.ParsePolicyConfig([]byte(data))
	case "_account_registry.yaml":
		v, err = account.Parse([]byte(data))
	default:
		v, err = fedpolicy.ParseSubset([]byte(data))
	}
	if err != nil {
		t.Fatalf("%s: %v", key, err)
	}
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func jsonEqual(t *testing.T, a, b []byte) bool {
	t.Helper()
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, &y); err != nil {
		t.Fatal(err)
	}
	return reflect.DeepEqual(x, y)
}

func TestStateShow_DirStore_KeysAndRecords(t *testing.T) {
	root := t.TempDir()
	stateDir := filepath.Join(root, "state")
	files := map[string]string{}
	for k, v := range goodState {
		files[k] = v
	}
	// Not records: a foreign file, the store's own dot files.
	files["notes.txt"] = "hello\n"
	files[".lock"] = ""
	files[".tmp-123"] = "groups: {}\n"
	writeFiles(t, stateDir, files)

	code, doc, errOut := runShow(t, "", "--state-store", "dir", "--state-dir", stateDir,
		"--config-dir", filepath.Join(root, "conf.d"))
	if code != 0 {
		t.Fatalf("code %d: %s", code, errOut)
	}
	if doc.Schema != "tenant-api.state-show/v1" || doc.TenantAPIVersion == "" {
		t.Errorf("schema %q, version %q", doc.Schema, doc.TenantAPIVersion)
	}
	var want []string
	for k := range files {
		if staterestore.ValidKey(k) {
			want = append(want, k)
		}
	}
	sort.Strings(want)
	var got []string
	for _, r := range doc.Records {
		got = append(got, r.Key)
		if r.Problem != nil {
			t.Errorf("%s: problem %+v", r.Key, r.Problem)
			continue
		}
		if !jsonEqual(t, r.Record, parsedJSON(t, r.Key, files[r.Key])) {
			t.Errorf("%s: record %s, want the parse function's %s", r.Key, r.Record, parsedJSON(t, r.Key, files[r.Key]))
		}
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("keys %v, want %v (sorted, state records only)", got, want)
	}
	// `members: [t-a, 010]` names tenant "010" (the text), not 8.
	if !strings.Contains(string(doc.Records[sort.SearchStrings(got, "_groups.yaml")].Record), `"010"`) {
		t.Errorf("groups record lost the tenant id's text: %s", doc.Records[sort.SearchStrings(got, "_groups.yaml")].Record)
	}
}

func TestStateShow_Problems(t *testing.T) {
	for _, c := range []struct {
		name, key, data, kind, record string
	}{
		{"members not a list", "_groups.yaml", "groups:\n  g1:\n    label: G\n    members: abc\n", "rejected", ""},
		{"group not a mapping", "_groups.yaml", "groups:\n  g1: just-text\n", "rejected", ""},
		{"registry id at next", "_account_registry.yaml", "schema_version: v1\nnext_account_id: 1001\nallocations:\n  t-a: 1001\n", "rejected", ""},
		{"duplicate key", "_views.yaml", "views:\n  v1:\n    label: a\n    label: b\n", "unreadable", ""},
		{"leading zero member is text", "_groups.yaml", "groups:\n  g1:\n    label: G\n    members: [010]\n", "", `"members":["010"]`},
	} {
		t.Run(c.name, func(t *testing.T) {
			dir := filepath.Join(t.TempDir(), "state")
			writeFiles(t, dir, map[string]string{c.key: c.data})
			code, doc, errOut := runShow(t, "", "--from", dir)
			if code != 0 {
				t.Fatalf("code %d (a problem is printed, exit 0): %s", code, errOut)
			}
			if len(doc.Records) != 1 || doc.Records[0].Key != c.key {
				t.Fatalf("records %+v", doc.Records)
			}
			r := doc.Records[0]
			isNull := len(r.Record) == 0 || string(r.Record) == "null"
			if c.kind != "" {
				if r.Problem == nil || r.Problem.Kind != c.kind || r.Problem.Message == "" || !isNull {
					t.Errorf("problem %+v record %s, want kind %s and a null record", r.Problem, r.Record, c.kind)
				}
				return
			}
			var compact bytes.Buffer
			if err := json.Compact(&compact, r.Record); err != nil {
				t.Fatal(err)
			}
			if r.Problem != nil || isNull || !strings.Contains(compact.String(), c.record) {
				t.Errorf("record %s problem %+v, want %s", r.Record, r.Problem, c.record)
			}
		})
	}
}

func TestStateShow_From(t *testing.T) {
	cm := "kind: ConfigMap\ndata:\n  _groups.yaml: |\n    groups:\n      g1:\n        label: G\n        members: [t-a]\n"
	dir := filepath.Join(t.TempDir(), "bak")
	writeFiles(t, dir, map[string]string{"_groups.yaml": "groups:\n  g1:\n    label: G\n    members: [t-a]\n"})
	file := filepath.Join(t.TempDir(), "cm.yaml")
	if err := os.WriteFile(file, []byte(cm), 0o644); err != nil {
		t.Fatal(err)
	}
	// No store settings at all: --from needs none.
	t.Setenv("TA_STATE_STORE", "")
	for name, args := range map[string][]string{
		"configmap file": {"--from", file},
		"directory":      {"--from", dir},
		"stdin":          {"--from", "-"},
	} {
		code, doc, errOut := runShow(t, cm, args...)
		if code != 0 || len(doc.Records) != 1 || doc.Records[0].Key != "_groups.yaml" || doc.Records[0].Problem != nil {
			t.Errorf("%s: code %d records %+v stderr %s", name, code, doc.Records, errOut)
		}
	}
	if code, _, _ := runShow(t, "", "--from", filepath.Join(t.TempDir(), "absent.yaml")); code != 1 {
		t.Errorf("unreadable backup: code %d, want 1", code)
	}
}

func TestStateShow_StoreCannotBeRead(t *testing.T) {
	root := t.TempDir()
	missing := filepath.Join(root, "no-such-state")
	code, _, errOut := runShow(t, "", "--state-store", "dir", "--state-dir", missing, "--config-dir", filepath.Join(root, "conf.d"))
	if code != 1 {
		t.Errorf("missing state dir: code %d, want 1 (%s)", code, errOut)
	}
	if _, err := os.Stat(missing); err == nil {
		t.Error("state show created the missing state directory; it must open the store read-only")
	}
	// Not in a cluster: the ConfigMap store cannot be reached.
	if code, _, errOut := runShow(t, "", "--state-store", "configmap", "--config-dir", filepath.Join(root, "conf.d")); code != 1 {
		t.Errorf("unreachable ConfigMap: code %d, want 1 (%s)", code, errOut)
	}
	if code, _, _ := runShow(t, "", "--state-store", "", "--config-dir", filepath.Join(root, "conf.d")); code != 1 {
		t.Errorf("no store kind: code %d, want 1", code)
	}
}

func TestStateShow_Usage(t *testing.T) {
	var errb bytes.Buffer
	for _, args := range [][]string{{"show", "extra"}, {"show", "--no-such-flag"}, {"export"}, {}} {
		errb.Reset()
		if code := runStateCmd(args, strings.NewReader(""), &bytes.Buffer{}, &errb); code != 2 || !strings.Contains(errb.String(), "usage:") {
			t.Errorf("%v: code %d, stderr %q", args, code, errb.String())
		}
	}
}

// The JSON field names of every record and config type are its YAML field
// names (ADR-036 §12): the Python readers address the decoded record by the
// names the record's YAML uses. Checked by reflection, so a tag changed on
// one side only fails here.
func TestRecordTypes_JSONNamesAreYAMLNames(t *testing.T) {
	seen := map[reflect.Type]bool{}
	var walk func(reflect.Type, string)
	walk = func(tp reflect.Type, path string) {
		for tp.Kind() == reflect.Pointer || tp.Kind() == reflect.Slice || tp.Kind() == reflect.Map {
			tp = tp.Elem()
		}
		if tp.Kind() != reflect.Struct || seen[tp] {
			return
		}
		seen[tp] = true
		for i := 0; i < tp.NumField(); i++ {
			f := tp.Field(i)
			if !f.IsExported() {
				continue
			}
			y, _, _ := strings.Cut(f.Tag.Get("yaml"), ",")
			j, _, _ := strings.Cut(f.Tag.Get("json"), ",")
			if y == "" || y != j {
				t.Errorf("%s.%s: yaml name %q, json name %q", path, f.Name, y, j)
			}
			walk(f.Type, path+"."+f.Name)
		}
	}
	for _, v := range []any{
		groups.GroupsConfig{}, views.ViewsConfig{}, fedpolicy.Config{}, fedpolicy.Subset{},
		account.Registry{}, rbac.RBACConfig{}, tenantorg.Config{},
	} {
		tp := reflect.TypeOf(v)
		walk(tp, tp.String())
	}
}

type checkOut struct {
	Schema  string          `json:"schema"`
	Kind    string          `json:"kind"`
	Config  json.RawMessage `json:"config"`
	Problem *struct {
		Kind    string `json:"kind"`
		Message string `json:"message"`
	} `json:"problem"`
}

func runCheck(t *testing.T, stdin string, args ...string) (int, checkOut, string) {
	t.Helper()
	var out, errb bytes.Buffer
	code := runConfigCmd(append([]string{"check"}, args...), strings.NewReader(stdin), &out, &errb)
	var doc checkOut
	if code == 0 || code == 1 {
		if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
			t.Fatalf("output is not JSON: %v\n%s", err, out.String())
		}
	}
	return code, doc, errb.String()
}

func TestConfigCheck(t *testing.T) {
	t.Setenv("TA_IDENTITY_CLAIM_HEADERS", "")
	for _, c := range []struct {
		name, kind, data string
		extra            []string
		code             int
		problem, config  string
	}{
		{"rbac good", "rbac", "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n", nil, 0, "", `"permissions":["read"]`},
		{"rbac not a list", "rbac", "groups: abc\n", nil, 1, "rejected", ""},
		{"rbac unknown field", "rbac", "groups:\n  - name: ops\n    permissons: [read]\n", nil, 1, "rejected", ""},
		{"rbac duplicate key", "rbac", "groups: []\ngroups: []\n", nil, 1, "unreadable", ""},
		{"rbac undeclared claim", "rbac", "groups:\n  - name: ops\n    match: {claims: {org: [a]}}\n    tenants: [t]\n    permissions: [read]\n", nil, 1, "rejected", ""},
		{"rbac declared claim", "rbac", "groups:\n  - name: ops\n    match: {claims: {org: [a]}}\n    tenants: [t]\n    permissions: [read]\n",
			[]string{"--identity-claim-headers", "org=X-Org"}, 0, "", `"claims":{"org":["a"]}`},
		{"rbac empty", "rbac", "", nil, 0, "", `{"groups":null}`},
		{"tenant-orgs good", "tenant-orgs", "tenant_orgs:\n  t-a: [ORG-1]\n  010: []\n", nil, 0, "", `"010":[]`},
		{"tenant-orgs not a list", "tenant-orgs", "tenant_orgs:\n  t-a: ORG-1\n", nil, 1, "rejected", ""},
		{"tenant-orgs unknown field", "tenant-orgs", "tenant_org:\n  t-a: [ORG-1]\n", nil, 1, "rejected", ""},
		{"tenant-orgs empty", "tenant-orgs", "# nothing\n", nil, 0, "", `{"tenant_orgs":{}}`},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := filepath.Join(t.TempDir(), "in.yaml")
			if err := os.WriteFile(f, []byte(c.data), 0o644); err != nil {
				t.Fatal(err)
			}
			for _, src := range []string{f, "-"} {
				args := append(append([]string{"--kind", c.kind}, c.extra...), src)
				code, doc, errOut := runCheck(t, c.data, args...)
				if code != c.code || doc.Schema != "tenant-api.config-check/v1" || doc.Kind != c.kind {
					t.Fatalf("%s: code %d (want %d) doc %+v stderr %s", src, code, c.code, doc, errOut)
				}
				isNull := len(doc.Config) == 0 || string(doc.Config) == "null"
				if c.problem != "" {
					if doc.Problem == nil || doc.Problem.Kind != c.problem || !isNull {
						t.Errorf("%s: problem %+v config %s, want kind %s", src, doc.Problem, doc.Config, c.problem)
					}
					continue
				}
				var compact bytes.Buffer
				if err := json.Compact(&compact, doc.Config); err != nil {
					t.Fatal(err)
				}
				if doc.Problem != nil || !strings.Contains(compact.String(), c.config) {
					t.Errorf("%s: config %s problem %+v, want %s", src, compact.String(), doc.Problem, c.config)
				}
			}
		})
	}
}

func TestConfigCheck_Usage(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "absent.yaml")
	for name, args := range map[string][]string{
		"no verb":           nil,
		"unknown verb":      {"lint"},
		"no kind":           {"check", "-"},
		"unknown kind":      {"check", "--kind", "groups", "-"},
		"no file":           {"check", "--kind", "rbac"},
		"two files":         {"check", "--kind", "rbac", "a", "b"},
		"cannot be opened":  {"check", "--kind", "rbac", missing},
		"is a directory":    {"check", "--kind", "tenant-orgs", t.TempDir()},
		"bad claim headers": {"check", "--kind", "rbac", "--identity-claim-headers", "org", "-"},
	} {
		var errb bytes.Buffer
		if code := runConfigCmd(args, strings.NewReader(""), &bytes.Buffer{}, &errb); code != 2 {
			t.Errorf("%s: code %d, want 2 (%s)", name, code, errb.String())
		}
	}
}

// The store is read as the server reads it (dirStore.Get follows a symlink):
// a record that is a symlink — every file of a ConfigMap volume is one, into
// `..data/` — is a record, not an absent one.
func TestStateShow_DirStore_Symlinks(t *testing.T) {
	root := t.TempDir()
	conf := filepath.Join(root, "conf.d")

	plain := filepath.Join(root, "plain")
	writeFiles(t, filepath.Join(root, "elsewhere"), map[string]string{"g.yaml": goodState["_groups.yaml"]})
	if err := os.MkdirAll(plain, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "elsewhere", "g.yaml"), filepath.Join(plain, "_groups.yaml")); err != nil {
		t.Fatal(err)
	}

	// A ConfigMap volume: the files are symlinks to ..data/<key>, ..data a
	// symlink to the timestamped directory holding the real files.
	cmv := filepath.Join(root, "cmvolume")
	writeFiles(t, filepath.Join(cmv, "..2026_10_28_00_00_00.1"), map[string]string{
		"_groups.yaml": goodState["_groups.yaml"], "_views.yaml": goodState["_views.yaml"]})
	if err := os.Symlink("..2026_10_28_00_00_00.1", filepath.Join(cmv, "..data")); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"_groups.yaml", "_views.yaml"} {
		if err := os.Symlink(filepath.Join("..data", k), filepath.Join(cmv, k)); err != nil {
			t.Fatal(err)
		}
	}

	for name, c := range map[string]struct {
		dir  string
		keys []string
	}{
		"symlinked record": {plain, []string{"_groups.yaml"}},
		"configmap volume": {cmv, []string{"_groups.yaml", "_views.yaml"}},
	} {
		code, doc, errOut := runShow(t, "", "--state-store", "dir", "--state-dir", c.dir, "--config-dir", conf)
		if code != 0 {
			t.Fatalf("%s: code %d: %s", name, code, errOut)
		}
		var got []string
		for _, r := range doc.Records {
			got = append(got, r.Key)
			if r.Problem != nil {
				t.Errorf("%s: %s: %+v", name, r.Key, r.Problem)
			}
		}
		if strings.Join(got, ",") != strings.Join(c.keys, ",") {
			t.Errorf("%s: keys %v, want %v", name, got, c.keys)
		}
	}
}

// A record that exists but cannot be read is not an absent record: the store
// cannot be read (exit 1). A directory where a record should be stands for
// any read error other than "does not exist" (a permission error would not
// stop a test running as root).
func TestStateShow_DirStore_UnreadableRecordIsExit1(t *testing.T) {
	root := t.TempDir()
	stateDir := filepath.Join(root, "state")
	writeFiles(t, stateDir, map[string]string{"_views.yaml": goodState["_views.yaml"]})
	if err := os.MkdirAll(filepath.Join(stateDir, "_groups.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	code, _, errOut := runShow(t, "", "--state-store", "dir", "--state-dir", stateDir, "--config-dir", filepath.Join(root, "conf.d"))
	if code != 1 || !strings.Contains(errOut, "_groups.yaml") {
		t.Fatalf("code %d, want 1 naming _groups.yaml: %s", code, errOut)
	}
}
