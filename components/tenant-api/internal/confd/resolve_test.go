package confd

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func mkdir(t *testing.T, names ...string) string {
	t.Helper()
	dir := t.TempDir()
	for _, n := range names {
		if err := os.WriteFile(filepath.Join(dir, n), []byte("tenants: {}\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestResolveTenantFile(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		files    []string
		tenantID string
		wantBase string // "" when an error is expected
		wantErr  error
	}{
		{name: "yaml", files: []string{"db-a.yaml"}, tenantID: "db-a", wantBase: "db-a.yaml"},
		{
			// The #1673 case: the enumerating plane already accepted this
			// spelling while every per-tenant site joined `<id>.yaml`.
			name: "yml is a tenant file too", files: []string{"db-a.yml"},
			tenantID: "db-a", wantBase: "db-a.yml",
		},
		{
			name: "extension case is folded", files: []string{"db-a.YAML"},
			tenantID: "db-a", wantBase: "db-a.YAML",
		},
		{
			// Refused, not resolved by precedence — the two files can disagree
			// on _metadata and on threshold values.
			name: "both spellings present", files: []string{"db-a.yaml", "db-a.yml"},
			tenantID: "db-a", wantErr: ErrAmbiguousTenantFile,
		},
		{name: "absent", files: []string{"db-b.yaml"}, tenantID: "db-a", wantErr: ErrTenantFileNotFound},
		{
			name: "reserved control files are never tenants", files: []string{"_defaults.yaml"},
			tenantID: "_defaults", wantErr: ErrTenantFileNotFound,
		},
		{
			name: "non-yaml is ignored", files: []string{"db-a.json"},
			tenantID: "db-a", wantErr: ErrTenantFileNotFound,
		},
		{
			// The STEM keeps its case (confd.go's #1537 invariant), so an id
			// that differs only in case must not resolve.
			name: "stem case is significant", files: []string{"db-a.yaml"},
			tenantID: "DB-A", wantErr: ErrTenantFileNotFound,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			dir := mkdir(t, tt.files...)
			got, err := ResolveTenantFile(dir, tt.tenantID)
			if tt.wantErr != nil {
				if !errors.Is(err, tt.wantErr) {
					t.Fatalf("err = %v, want %v", err, tt.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected err: %v", err)
			}
			if got != filepath.Join(dir, tt.wantBase) {
				t.Errorf("got %q, want base %q", got, tt.wantBase)
			}
		})
	}
}

func TestResolveTenantFile_MissingDirIsNotFound(t *testing.T) {
	t.Parallel()
	_, err := ResolveTenantFile(filepath.Join(t.TempDir(), "nope"), "db-a")
	if !errors.Is(err, ErrTenantFileNotFound) {
		t.Fatalf("err = %v, want ErrTenantFileNotFound", err)
	}
}

func TestResolveTenantFile_DirectoryEntryIsNotATenant(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "db-a.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := ResolveTenantFile(dir, "db-a"); !errors.Is(err, ErrTenantFileNotFound) {
		t.Fatalf("err = %v, want ErrTenantFileNotFound", err)
	}
}

func TestTenantFilePathForWrite(t *testing.T) {
	t.Parallel()

	t.Run("existing file wins over the default spelling", func(t *testing.T) {
		t.Parallel()
		dir := mkdir(t, "db-a.yml")
		got, err := TenantFilePathForWrite(dir, "db-a")
		if err != nil {
			t.Fatal(err)
		}
		if got != filepath.Join(dir, "db-a.yml") {
			t.Errorf("got %q, want the existing .yml file", got)
		}
	})

	t.Run("new tenant gets the default spelling", func(t *testing.T) {
		t.Parallel()
		dir := mkdir(t)
		got, err := TenantFilePathForWrite(dir, "db-a")
		if err != nil {
			t.Fatal(err)
		}
		if got != filepath.Join(dir, "db-a.yaml") {
			t.Errorf("got %q, want the default .yaml path", got)
		}
	})

	t.Run("ambiguous is refused", func(t *testing.T) {
		t.Parallel()
		dir := mkdir(t, "db-a.yaml", "db-a.yml")
		if _, err := TenantFilePathForWrite(dir, "db-a"); !errors.Is(err, ErrAmbiguousTenantFile) {
			t.Fatalf("err = %v, want ErrAmbiguousTenantFile", err)
		}
	})
}

// The sink-side backstop (CodeQL "uncontrolled data used in path expression",
// PR #1682): an id that is not a bare filename must never be joined onto
// configDir. Measured before the guard existed: `foo/../../etc/passwd`
// escaped the directory AND satisfied IsTenantConfigFile, so guardTenantID —
// which gates on the reserved-NAME predicate alone — let it through.
func TestTenantIDMustBeABareFilename(t *testing.T) {
	t.Parallel()
	unsafe := []string{
		"",
		"../../etc/passwd",
		"foo/../../etc/passwd", // the one guardTenantID does NOT catch
		"sub/nested",
		`windows\path`,
		"..",
		"a/..",
	}
	for _, id := range unsafe {
		t.Run(id, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()

			if _, err := ResolveTenantFile(dir, id); !errors.Is(err, ErrUnsafeTenantID) {
				t.Errorf("ResolveTenantFile(%q) err = %v, want ErrUnsafeTenantID", id, err)
			}
			got, err := TenantFilePathForWrite(dir, id)
			if !errors.Is(err, ErrUnsafeTenantID) {
				t.Fatalf("TenantFilePathForWrite(%q) err = %v, want ErrUnsafeTenantID", id, err)
			}
			if got != "" {
				t.Errorf("TenantFilePathForWrite(%q) leaked a path on rejection: %q", id, got)
			}
		})
	}
}

// A plain id keeps working — the guard must not narrow the accepted namespace.
func TestBareFilenameIDsStillResolve(t *testing.T) {
	t.Parallel()
	dir := mkdir(t, "db-a.yaml")
	if _, err := ResolveTenantFile(dir, "db-a"); err != nil {
		t.Fatalf("ResolveTenantFile: %v", err)
	}
	if _, err := TenantFilePathForWrite(dir, "db-a"); err != nil {
		t.Fatalf("TenantFilePathForWrite: %v", err)
	}
}

// The filepath.Base applied at the join must not change any accepted id — the
// guard already rejects every id for which Base would differ, so the two must
// agree on the whole accepted namespace.
func TestBaseAtTheJoinIsANoOp(t *testing.T) {
	t.Parallel()
	for _, id := range []string{"db-a", "db-a-1", "Upper", "a.b", "x_y-1", "tenant.with.dots"} {
		t.Run(id, func(t *testing.T) {
			t.Parallel()
			if err := guardBareTenantID(id); err != nil {
				t.Fatalf("guard rejected a legitimate id %q: %v", id, err)
			}
			if filepath.Base(id) != id {
				t.Fatalf("filepath.Base(%q) = %q — the guard let through an id it changes", id, filepath.Base(id))
			}
			dir := mkdir(t)
			got, err := TenantFilePathForWrite(dir, id)
			if err != nil {
				t.Fatal(err)
			}
			if got != filepath.Join(dir, id+".yaml") {
				t.Errorf("TenantFilePathForWrite(%q) = %q, want the unchanged default path", id, got)
			}
		})
	}
}
