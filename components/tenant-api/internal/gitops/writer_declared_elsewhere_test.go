package gitops

// #2078: a tenant write must not CREATE `<id>.yaml` for an id another conf.d
// file already declares. confd.TenantFilePathForWrite only looks at the top
// level for `<id>.yaml|.yml`, so without the guard in tenantFilePath the write
// "succeeds" and leaves one id in two files — the exporter keeps the old value
// while running and refuses the tree (`mixed-mode duplicate tenant`) on the
// next restart. Every write entry point (Write, WriteMerged, WritePR,
// WritePRBatch) resolves through tenantFilePath, so each is exercised here.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// tenantBody is a minimal valid single-tenant document for id.
func tenantBody(id string) string {
	return "tenants:\n  " + id + ":\n    _silent_mode: \"warning\"\n"
}

// seedTreeRepo writes files (slash-relative path → content) into a fresh repo
// and commits them on "main", with a bare origin so a PR-mode push that is
// (wrongly) reached would actually land somewhere observable.
func seedTreeRepo(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := initRepoOnMain(t)
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	gitRun(t, dir, "add", "-A")
	gitRun(t, dir, "commit", "-m", "seed tree")
	addBareRemote(t, dir)
	return dir
}

// writeVia runs one write entry point for tenantID with a valid body.
func writeVia(t *testing.T, mode string, w *Writer, tenantID string) error {
	t.Helper()
	ctx := context.Background()
	body := tenantBody(tenantID)
	merge := func([]byte) (string, error) { return body, nil }
	var err error
	switch mode {
	case "Write":
		_, err = w.Write(ctx, tenantID, "op@example.com", body)
	case "WriteMerged":
		_, err = w.WriteMerged(ctx, tenantID, "op@example.com", merge)
	case "WritePR":
		_, err = w.WritePR(ctx, tenantID, "op@example.com", body)
	case "WritePRBatch":
		_, err = w.WritePRBatch(ctx, []PRBatchOp{{TenantID: tenantID, Merge: merge}}, "op@example.com")
	default:
		t.Fatalf("unknown mode %q", mode)
	}
	return err
}

var allWriteModes = []string{"Write", "WriteMerged", "WritePR", "WritePRBatch"}

func TestTenantWrite_RefusesIDDeclaredElsewhere(t *testing.T) {
	cases := []struct {
		name   string
		files  map[string]string
		tenant string
		// wantDup: the id is already declared by ≥2 files, so the refusal
		// must also carry the walker's *DuplicateTenantError.
		wantDup bool
	}{
		{
			name:   "subdirectory file",
			files:  map[string]string{"team/sub-t.yaml": tenantBody("sub-t")},
			tenant: "sub-t",
		},
		{
			name: "top-level shared file",
			files: map[string]string{"shared.yaml": "tenants:\n" +
				"  shared-x:\n    _silent_mode: \"warning\"\n" +
				"  shared-y:\n    _silent_mode: \"critical\"\n"},
			tenant: "shared-x",
		},
		{
			name: "already declared by two other files",
			files: map[string]string{
				"team-a/dup-t.yaml": tenantBody("dup-t"),
				"team-b/dup-t.yaml": tenantBody("dup-t"),
			},
			tenant:  "dup-t",
			wantDup: true,
		},
	}
	for _, tc := range cases {
		for _, mode := range allWriteModes {
			t.Run(tc.name+"/"+mode, func(t *testing.T) {
				dir := seedTreeRepo(t, tc.files)
				w := NewWriter(dir, dir)
				mainHead := gitOut(t, dir, "rev-parse", "main")

				err := writeVia(t, mode, w, tc.tenant)
				if !errors.Is(err, ErrTenantDeclaredElsewhere) {
					t.Fatalf("%s(%s) err = %v, want ErrTenantDeclaredElsewhere", mode, tc.tenant, err)
				}
				var dup *cfg.DuplicateTenantError
				if got := errors.As(err, &dup); got != tc.wantDup {
					t.Errorf("errors.As(*DuplicateTenantError) = %v, want %v (err=%v)", got, tc.wantDup, err)
				}
				if _, serr := os.Stat(filepath.Join(dir, tc.tenant+".yaml")); !os.IsNotExist(serr) {
					t.Errorf("%s.yaml was created (stat err=%v) — the duplicate #2078 exists to prevent", tc.tenant, serr)
				}
				if got := gitOut(t, dir, "rev-parse", "main"); got != mainHead {
					t.Errorf("main moved %s → %s on a refused write", mainHead, got)
				}
				assertCleanOnBase(t, dir, "main", "tenant-api/")
				if remote := gitOut(t, dir, "ls-remote", "--heads", "origin"); remote != "" {
					t.Errorf("a refused write pushed to origin:\n%s", remote)
				}
				// The tree the refusal protected is still one the exporter loads.
				if _, _, lerr := cfg.LoadDir(dir, nil); lerr != nil && !tc.wantDup {
					t.Errorf("LoadDir after refused write: %v", lerr)
				}
			})
		}
	}
}

// A `tenants:` block in a `_`-prefixed platform file is NOT a declaration —
// the walker never parses those files for tenants — so writing `<id>.yaml`
// for such an id is legitimate and the exporter loads the result.
func TestTenantWrite_PlatformFileTenantsBlockIsNotADeclaration(t *testing.T) {
	for _, mode := range allWriteModes {
		t.Run(mode, func(t *testing.T) {
			dir := seedTreeRepo(t, map[string]string{"_extra.yaml": tenantBody("pt")})
			w := NewWriter(dir, dir)
			if err := writeVia(t, mode, w, "pt"); err != nil {
				t.Fatalf("%s(pt): %v", mode, err)
			}
			if mode == "Write" || mode == "WriteMerged" {
				if _, err := os.Stat(filepath.Join(dir, "pt.yaml")); err != nil {
					t.Fatalf("pt.yaml not written: %v", err)
				}
			} else {
				// PR mode lands on the pushed branch; read it from there.
				heads := gitOut(t, dir, "ls-remote", "--heads", "origin")
				if !strings.Contains(heads, "refs/heads/tenant-api/") {
					t.Fatalf("no PR branch pushed:\n%s", heads)
				}
				branch := heads[strings.Index(heads, "refs/heads/")+len("refs/heads/"):]
				gitRun(t, dir, "fetch", "origin", branch)
				gitRun(t, dir, "checkout", "FETCH_HEAD")
			}
			if _, _, err := cfg.LoadDir(dir, nil); err != nil {
				t.Fatalf("LoadDir after writing pt.yaml beside _extra.yaml: %v", err)
			}
		})
	}
}

func TestTenantWrite_NewAndExistingTenantsStillWrite(t *testing.T) {
	t.Run("brand-new id", func(t *testing.T) {
		for _, mode := range allWriteModes {
			t.Run(mode, func(t *testing.T) {
				dir := seedTreeRepo(t, map[string]string{"team/sub-t.yaml": tenantBody("sub-t")})
				if err := writeVia(t, mode, NewWriter(dir, dir), "fresh-t"); err != nil {
					t.Fatalf("%s(fresh-t): %v", mode, err)
				}
			})
		}
	})
	// An existing top-level `<id>.yaml` is an ordinary update and is resolved
	// WITHOUT walking the tree. Pinned observably: the tree below already
	// declares upd-t twice (the exporter rejects it), and the update still
	// goes through — had the guard scanned, Locate would have reported the
	// duplicate and refused. This pins "no scan on update", not an
	// endorsement of the tree.
	t.Run("existing file is updated without a tree scan", func(t *testing.T) {
		for _, mode := range []string{"Write", "WriteMerged"} {
			t.Run(mode, func(t *testing.T) {
				dir := seedTreeRepo(t, map[string]string{
					"upd-t.yaml":      tenantBody("upd-t"),
					"team/upd-t.yaml": tenantBody("upd-t"),
				})
				w := NewWriter(dir, dir)
				body := "tenants:\n  upd-t:\n    _silent_mode: \"critical\"\n"
				var err error
				if mode == "Write" {
					_, err = w.Write(context.Background(), "upd-t", "op@example.com", body)
				} else {
					_, err = w.WriteMerged(context.Background(), "upd-t", "op@example.com",
						func([]byte) (string, error) { return body, nil })
				}
				if err != nil {
					t.Fatalf("%s(upd-t): %v", mode, err)
				}
				got, rerr := os.ReadFile(filepath.Join(dir, "upd-t.yaml"))
				if rerr != nil || string(got) != body {
					t.Fatalf("upd-t.yaml = %q (err=%v), want the new body", got, rerr)
				}
			})
		}
	})
}

// The guard fails CLOSED when the walk itself cannot run.
func TestEnsureNotDeclaredElsewhere_ScanFailureFailsClosed(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "no-such-conf.d")
	err := ensureNotDeclaredElsewhere(missing, "fresh-t", filepath.Join(missing, "fresh-t.yaml"))
	if !errors.Is(err, ErrTenantTreeScan) {
		t.Fatalf("err = %v, want ErrTenantTreeScan", err)
	}

	// End to end on the PR path: nothing written, no branch left behind.
	repo := initRepoOnMain(t)
	w := NewWriter(filepath.Join(repo, "conf.d-does-not-exist"), repo)
	if _, err := w.WritePR(context.Background(), "fresh-t", "op@example.com", tenantBody("fresh-t")); !errors.Is(err, ErrTenantTreeScan) {
		t.Fatalf("WritePR err = %v, want ErrTenantTreeScan", err)
	}
	assertCleanOnBase(t, repo, "main", "tenant-api/")
}

// Locate answers in the walker's form (absolute, symlinked root resolved);
// the target is configDir-relative. A relative or symlinked configDir must
// not make the id's own top-level file look like "another file".
func TestEnsureNotDeclaredElsewhere_PathFormDoesNotMisjudge(t *testing.T) {
	realDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(realDir, "own-t.yaml"), []byte(tenantBody("own-t")), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(t.TempDir(), "conf.d-link")
	if err := os.Symlink(realDir, link); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}
	if err := ensureNotDeclaredElsewhere(link, "own-t", filepath.Join(link, "own-t.yaml")); err != nil {
		t.Errorf("symlinked configDir: %v", err)
	}
	t.Chdir(filepath.Dir(realDir))
	rel := filepath.Base(realDir)
	if err := ensureNotDeclaredElsewhere(rel, "own-t", filepath.Join(rel, "own-t.yaml")); err != nil {
		t.Errorf("relative configDir: %v", err)
	}
}
