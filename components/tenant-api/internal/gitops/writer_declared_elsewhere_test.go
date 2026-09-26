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
	"os/exec"
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
		// Round 2 (M1): `<id>.yaml` EXISTS but does not declare the id, while
		// another file does. "The target exists" is not "the id lives there".
		{
			name: "own file is a tenants:{} shell",
			files: map[string]string{
				"shell-t.yaml":      "tenants: {}\n",
				"team/shell-t.yaml": tenantBody("shell-t"),
			},
			tenant: "shell-t",
		},
		{
			name: "own file does not parse",
			files: map[string]string{
				"broken-t.yaml":      "tenants: [not, a, map\n",
				"team/broken-t.yaml": tenantBody("broken-t"),
			},
			tenant: "broken-t",
		},
		{
			name: "own file declares another key",
			files: map[string]string{
				"key-t.yaml":      tenantBody("key-other"),
				"team/key-t.yaml": tenantBody("key-t"),
			},
			tenant: "key-t",
		},
		// …and when it DOES declare the id, a second declaration elsewhere is
		// a duplicate the exporter already rejects: refused even as an update.
		{
			name: "own file plus a declaration elsewhere",
			files: map[string]string{
				"both-t.yaml":      tenantBody("both-t"),
				"team/both-t.yaml": tenantBody("both-t"),
			},
			tenant:  "both-t",
			wantDup: true,
		},
	}
	for _, tc := range cases {
		for _, mode := range allWriteModes {
			t.Run(tc.name+"/"+mode, func(t *testing.T) {
				dir := seedTreeRepo(t, tc.files)
				w := NewWriter(dir, dir)
				mainHead := gitOut(t, dir, "rev-parse", "main")
				ownPath := filepath.Join(dir, tc.tenant+".yaml")
				ownBefore, ownErrBefore := os.ReadFile(ownPath)

				err := writeVia(t, mode, w, tc.tenant)
				if !errors.Is(err, ErrTenantDeclaredElsewhere) {
					t.Fatalf("%s(%s) err = %v, want ErrTenantDeclaredElsewhere", mode, tc.tenant, err)
				}
				var dup *cfg.DuplicateTenantError
				if got := errors.As(err, &dup); got != tc.wantDup {
					t.Errorf("errors.As(*DuplicateTenantError) = %v, want %v (err=%v)", got, tc.wantDup, err)
				}
				// The top-level `<id>.yaml` is exactly as it was: still absent,
				// or byte-identical.
				ownAfter, ownErrAfter := os.ReadFile(ownPath)
				if os.IsNotExist(ownErrBefore) != os.IsNotExist(ownErrAfter) || string(ownBefore) != string(ownAfter) {
					t.Errorf("%s.yaml changed on a refused write: before=%q (%v) after=%q (%v)",
						tc.tenant, ownBefore, ownErrBefore, ownAfter, ownErrAfter)
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
	// An existing `<id>.yaml` that declares the id (and nothing else does) is
	// the ordinary update: the walker attributes the id to the target itself.
	// The "existing file plus a declaration elsewhere" row of
	// TestTenantWrite_RefusesIDDeclaredElsewhere is the refused counterpart.
	t.Run("existing file is updated", func(t *testing.T) {
		for _, mode := range []string{"Write", "WriteMerged"} {
			t.Run(mode, func(t *testing.T) {
				dir := seedTreeRepo(t, map[string]string{
					"upd-t.yaml":            tenantBody("upd-t"),
					"team/neighbour-t.yaml": tenantBody("neighbour-t"),
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
	// An existing `<id>.yaml` that does NOT declare the id, with nothing else
	// declaring it either, is still writable: after the write the id lives in
	// exactly one file.
	t.Run("existing shell file is filled in", func(t *testing.T) {
		for _, mode := range allWriteModes {
			t.Run(mode, func(t *testing.T) {
				dir := seedTreeRepo(t, map[string]string{"fill-t.yaml": "tenants: {}\n"})
				if err := writeVia(t, mode, NewWriter(dir, dir), "fill-t"); err != nil {
					t.Fatalf("%s(fill-t): %v", mode, err)
				}
			})
		}
	})
}

// Each op of a PR batch is judged on the tree as the ops before it left it.
// op1 rewrites mv-x.yaml (which also declared mv-y) without mv-y; op2 then
// creates mv-y.yaml. Judged on the tree before op1, op2 would be refused
// (mv-y "lives in" mv-x.yaml); on the tree it actually lands on, mv-y is
// declared nowhere, and the branch loads cleanly.
func TestWritePRBatch_OpSeesEarlierOpsWrites(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"mv-x.yaml": "tenants:\n" +
		"  mv-x:\n    _silent_mode: \"warning\"\n" +
		"  mv-y:\n    _silent_mode: \"warning\"\n"})
	w := NewWriter(dir, dir)
	res, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		{TenantID: "mv-x", Merge: func([]byte) (string, error) { return tenantBody("mv-x"), nil }},
		{TenantID: "mv-y", Merge: func([]byte) (string, error) { return tenantBody("mv-y"), nil }},
	}, "op@example.com")
	if err != nil {
		t.Fatalf("WritePRBatch: %v", err)
	}
	gitRun(t, dir, "fetch", "origin", res.BranchName)
	gitRun(t, dir, "checkout", "-q", "FETCH_HEAD")
	if _, _, err := cfg.LoadDir(dir, nil); err != nil {
		t.Fatalf("LoadDir on the PR branch: %v", err)
	}
	// op2 must have landed, not silently become a no-op (mv-y would then be
	// declared nowhere and still load cleanly).
	if _, err := os.Stat(filepath.Join(dir, "mv-y.yaml")); err != nil {
		t.Fatalf("mv-y.yaml missing on the PR branch: %v", err)
	}
}

// WritePRBatch decides "declared elsewhere" only after checking out the fresh
// base, never on the local tree. Here the local base still declares fix-t in
// a subdirectory file, but origin/main has already removed that file: the
// batch branches from origin/main, where the id is declared nowhere, so the
// write must go through.
func TestWritePRBatch_PreflightDefersDeclaredElsewhereToFreshBase(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"team/fix-t.yaml": tenantBody("fix-t")})
	gitRun(t, dir, "push", "-q", "origin", "main")
	remote := gitOut(t, dir, "remote", "get-url", "origin")

	// Another clone removes the declaration upstream; the writer's local main
	// is left stale on purpose.
	other := t.TempDir()
	gitRun(t, other, "clone", "-q", "-b", "main", remote, ".")
	gitRun(t, other, "config", "user.email", "o@o.com")
	gitRun(t, other, "config", "user.name", "O")
	gitRun(t, other, "rm", "-q", "team/fix-t.yaml")
	gitRun(t, other, "commit", "-q", "-m", "drop the subdirectory declaration")
	gitRun(t, other, "push", "-q", "origin", "HEAD:main")

	if _, err := os.Stat(filepath.Join(dir, "team", "fix-t.yaml")); err != nil {
		t.Fatalf("fixture: local tree must still declare fix-t elsewhere: %v", err)
	}
	w := NewWriter(dir, dir)
	res, err := w.WritePRBatch(context.Background(), []PRBatchOp{{TenantID: "fix-t",
		Merge: func([]byte) (string, error) { return tenantBody("fix-t"), nil }}}, "op@example.com")
	if err != nil {
		t.Fatalf("WritePRBatch against a fresh base without the other declaration: %v", err)
	}
	gitRun(t, dir, "fetch", "origin", res.BranchName)
	gitOut(t, dir, "show", "FETCH_HEAD:fix-t.yaml")
	if out, err := exec.Command("git", "-C", dir, "cat-file", "-e", "FETCH_HEAD:team/fix-t.yaml").CombinedOutput(); err == nil {
		t.Errorf("the PR branch still carries team/fix-t.yaml — it was not cut from origin/main (%s)", out)
	}
}

// The guard fails CLOSED when the walk itself cannot run.
func TestEnsureNotDeclaredElsewhere_ScanFailureFailsClosed(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "no-such-conf.d")
	err := (&Writer{configDir: missing}).ensureNotDeclaredElsewhere("fresh-t", filepath.Join(missing, "fresh-t.yaml"))
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
	if err := (&Writer{configDir: link}).ensureNotDeclaredElsewhere("own-t", filepath.Join(link, "own-t.yaml")); err != nil {
		t.Errorf("symlinked configDir: %v", err)
	}
	t.Chdir(filepath.Dir(realDir))
	rel := filepath.Base(realDir)
	if err := (&Writer{configDir: rel}).ensureNotDeclaredElsewhere("own-t", filepath.Join(rel, "own-t.yaml")); err != nil {
		t.Errorf("relative configDir: %v", err)
	}
}
