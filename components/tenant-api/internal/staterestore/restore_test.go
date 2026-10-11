package staterestore

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/statestore"
)

func newStore(t *testing.T) statestore.Store {
	t.Helper()
	s, err := statestore.NewDir(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func get(t *testing.T, s statestore.Store, key string) []byte {
	t.Helper()
	b, _, err := s.Get(context.Background(), key)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func put(t *testing.T, s statestore.Store, key, val string) {
	t.Helper()
	if _, err := s.Mutate(context.Background(), key, statestore.Meta{Entity: "test"},
		func([]byte) ([]byte, error) { return []byte(val), nil }); err != nil {
		t.Fatal(err)
	}
}

// #2884 regression: t-a allocated → backup → t-b allocated → restore the
// backup → allocate t-c. Overwriting the registry with the backup (what
// `kubectl apply` of the backup does) handed t-c t-b's 1001.
func TestRestore_OlderBackupDoesNotReissue(t *testing.T) {
	ctx := context.Background()
	s := newStore(t)
	put(t, s, statestore.KeyAccountRegistry, "schema_version: v1\nnext_account_id: 1000\nallocations: {}\n")
	alloc := account.NewAllocator(s, nil)
	if id, err := alloc.EnsureAccountID(ctx, "t-a", "op"); err != nil || id != 1000 {
		t.Fatalf("t-a = %d, %v", id, err)
	}
	backup := Backup{statestore.KeyAccountRegistry: get(t, s, statestore.KeyAccountRegistry)}
	if id, err := alloc.EnsureAccountID(ctx, "t-b", "op"); err != nil || id != 1001 {
		t.Fatalf("t-b = %d, %v", id, err)
	}

	var out bytes.Buffer
	if err := Run(ctx, s, backup, Options{Author: "op"}, &out); err != nil {
		t.Fatalf("restore: %v\n%s", err, out.String())
	}
	id, err := alloc.EnsureAccountID(ctx, "t-c", "op")
	if err != nil {
		t.Fatal(err)
	}
	if id == 1001 {
		t.Fatalf("t-c was given t-b's id 1001 after the restore")
	}
	if id != 1002 {
		t.Errorf("t-c = %d, want 1002", id)
	}
}

// A conflict refuses the whole restore: neither the registry nor a record
// named in --keys is written.
func TestRestore_ConflictWritesNothing(t *testing.T) {
	ctx := context.Background()
	s := newStore(t)
	reg := "schema_version: v1\nnext_account_id: 1001\nallocations:\n  t-a: 1000\n"
	put(t, s, statestore.KeyAccountRegistry, reg)
	put(t, s, statestore.KeyGroups, "groups: {}\n")
	backup := Backup{
		statestore.KeyAccountRegistry: []byte("schema_version: v1\nnext_account_id: 1006\nallocations:\n  t-a: 1005\n"),
		statestore.KeyGroups:          []byte("groups:\n  g: {members: [t-a]}\n"),
	}
	var out bytes.Buffer
	err := Run(ctx, s, backup, Options{Keys: []string{statestore.KeyGroups}}, &out)
	var ce *account.RestoreConflictError
	if !errors.As(err, &ce) {
		t.Fatalf("err = %v, want a RestoreConflictError", err)
	}
	if got := string(get(t, s, statestore.KeyAccountRegistry)); got != reg {
		t.Errorf("registry written on a conflict:\n%s", got)
	}
	if got := string(get(t, s, statestore.KeyGroups)); got != "groups: {}\n" {
		t.Errorf("%s written on a conflict:\n%s", statestore.KeyGroups, got)
	}
}

func TestRestore_KeysAndDryRun(t *testing.T) {
	ctx := context.Background()
	s := newStore(t)
	put(t, s, statestore.KeyGroups, "groups: {}\n")
	put(t, s, statestore.KeyViews, "views: {}\n")
	backup := Backup{
		statestore.KeyGroups: []byte("groups:\n  g: {members: [t-a]}\n"),
		statestore.KeyViews:  []byte("views:\n  v: {}\n"),
	}

	var out bytes.Buffer
	if err := Run(ctx, s, backup, Options{Keys: []string{statestore.KeyGroups}, DryRun: true}, &out); err != nil {
		t.Fatal(err)
	}
	if got := string(get(t, s, statestore.KeyGroups)); got != "groups: {}\n" {
		t.Errorf("dry run wrote %s:\n%s", statestore.KeyGroups, got)
	}
	for _, want := range []string{"_groups.yaml: replace", "  - groups: {}", "  + groups:", "not restored (not named in --keys): _views.yaml"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("dry-run output lacks %q:\n%s", want, out.String())
		}
	}

	out.Reset()
	if err := Run(ctx, s, backup, Options{Keys: []string{statestore.KeyGroups}}, &out); err != nil {
		t.Fatal(err)
	}
	if got := string(get(t, s, statestore.KeyGroups)); got != string(backup[statestore.KeyGroups]) {
		t.Errorf("%s not replaced:\n%s", statestore.KeyGroups, got)
	}
	if got := string(get(t, s, statestore.KeyViews)); got != "views: {}\n" {
		t.Errorf("%s replaced without being named in --keys:\n%s", statestore.KeyViews, got)
	}
}

func TestRestore_KeysUsage(t *testing.T) {
	s := newStore(t)
	backup := Backup{statestore.KeyGroups: []byte("groups: {}\n")}
	for _, keys := range [][]string{
		{statestore.KeyAccountRegistry},
		{statestore.KeyViews}, // not in the backup
		{"../etc/passwd"},
		{".lock"},
	} {
		err := Run(context.Background(), s, backup, Options{Keys: keys}, &bytes.Buffer{})
		if !errors.Is(err, ErrUsage) {
			t.Errorf("keys %v: err = %v, want ErrUsage", keys, err)
		}
	}
}

func TestLoad(t *testing.T) {
	dir := t.TempDir()
	yamlCM := "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: tenant-api-state}\ndata:\n  _groups.yaml: |\n    groups: {}\n"
	jsonCM := `{"apiVersion":"v1","kind":"ConfigMap","data":{"_views.yaml":"views: {}\n"}}`
	write := func(name, body string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	if b, err := Load(write("cm.yaml", yamlCM), nil); err != nil || string(b["_groups.yaml"]) != "groups: {}\n" {
		t.Errorf("yaml: %v %q", err, b)
	}
	if b, err := Load(write("cm.json", jsonCM), nil); err != nil || string(b["_views.yaml"]) != "views: {}\n" {
		t.Errorf("json: %v %q", err, b)
	}
	if b, err := Load("-", strings.NewReader(yamlCM)); err != nil || len(b) != 1 {
		t.Errorf("stdin: %v %q", err, b)
	}
	if _, err := Load(write("list.yaml", "kind: List\nitems: []\n"), nil); err == nil {
		t.Error("a List was read as a backup")
	}
	sd := filepath.Join(dir, "state")
	if err := os.MkdirAll(sd, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, body := range map[string]string{"_groups.yaml": "groups: {}\n", ".lock": "", ".tmp-1": "x"} {
		if err := os.WriteFile(filepath.Join(sd, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if b, err := Load(sd, nil); err != nil || len(b) != 1 || b["_groups.yaml"] == nil {
		t.Errorf("dir: %v %q", err, b)
	}
}
