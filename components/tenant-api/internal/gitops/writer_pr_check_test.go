package gitops

// #2486: WritePRChecked runs the caller's check on the FRESH base — the
// working tree checked out at the feature branch cut from origin/<base> —
// before anything of the write is on it. PUT uses it for the domain-policy
// check, whose first pass read the pod's local tree. A refusal drops the
// branch: nothing written, committed or pushed.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// staleClone is a writer clone of a remote that moved on after the clone:
// origin/main carries _marker.yaml, the clone's tree does not.
func staleClone(t *testing.T) (dir, remote string) {
	t.Helper()
	remote = initBareRemoteOnMain(t)
	author := t.TempDir()
	gitClone(t, remote, author)
	gitRun(t, author, "config", "user.email", "a@a.com")
	gitRun(t, author, "config", "user.name", "A")
	writeFileInDir(t, author, "db-a.yaml", "tenants:\n  db-a:\n    _silent_mode: \"false\"\n")
	gitRun(t, author, "add", "-A")
	gitRun(t, author, "commit", "-m", "seed")
	gitRun(t, author, "push", "origin", "main")

	dir = t.TempDir()
	gitClone(t, remote, dir)
	gitRun(t, dir, "config", "user.email", "t@t.com")
	gitRun(t, dir, "config", "user.name", "T")

	writeFileInDir(t, author, "_marker.yaml", "marker: fresh\n")
	gitRun(t, author, "add", "-A")
	gitRun(t, author, "commit", "-m", "remote advances")
	gitRun(t, author, "push", "origin", "main")
	return dir, remote
}

func tenantAPIBranches(t *testing.T, dirs ...string) string {
	t.Helper()
	var out []string
	for _, d := range dirs {
		for _, line := range strings.Split(gitOut(t, d, "branch", "--format=%(refname:short)"), "\n") {
			if strings.HasPrefix(strings.TrimSpace(line), "tenant-api/") {
				out = append(out, line)
			}
		}
	}
	return strings.Join(out, ",")
}

func TestWritePRChecked_CheckSeesFreshBaseAndRefusalDropsBranch(t *testing.T) {
	dir, remote := staleClone(t)
	if _, err := os.Stat(filepath.Join(dir, "_marker.yaml")); !os.IsNotExist(err) {
		t.Fatalf("fixture: the clone already has the remote's new file (err=%v)", err)
	}
	mainBefore := gitOut(t, remote, "rev-parse", "main")
	refused := errors.New("refused by check")
	var sawMarker, sawBody bool
	_, err := NewWriter(dir, dir).WritePRChecked(context.Background(), "db-a", "bob@example.com", validTenantYAML,
		func(configDir string) error {
			_, statErr := os.Stat(filepath.Join(configDir, "_marker.yaml"))
			sawMarker = statErr == nil
			cur, _ := os.ReadFile(filepath.Join(configDir, "db-a.yaml"))
			sawBody = string(cur) == validTenantYAML
			return refused
		})
	if !errors.Is(err, refused) {
		t.Fatalf("err = %v, want the check's error unchanged", err)
	}
	if !sawMarker {
		t.Error("the check did not see the fresh base (origin's _marker.yaml absent)")
	}
	if sawBody {
		t.Error("the check ran after the body was written")
	}
	if b := tenantAPIBranches(t, dir, remote); b != "" {
		t.Errorf("refused write left branch(es): %s", b)
	}
	if got := gitOut(t, remote, "rev-parse", "main"); got != mainBefore {
		t.Errorf("origin main moved: %s → %s", mainBefore, got)
	}
	if head := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); head != "main" {
		t.Errorf("tree left on %q, want main", head)
	}
	if st := gitOut(t, dir, "status", "--porcelain"); st != "" {
		t.Errorf("tree not clean after refusal:\n%s", st)
	}
}

func TestWritePRChecked_PassingCheckWrites(t *testing.T) {
	dir, remote := staleClone(t)
	calls := 0
	res, err := NewWriter(dir, dir).WritePRChecked(context.Background(), "db-a", "bob@example.com", validTenantYAML,
		func(string) error { calls++; return nil })
	if err != nil || calls != 1 {
		t.Fatalf("err = %v, check calls = %d; want nil and 1", err, calls)
	}
	if got := gitOut(t, remote, "show", res.BranchName+":db-a.yaml"); got != strings.TrimSuffix(validTenantYAML, "\n") {
		t.Errorf("pushed db-a.yaml = %q, want the body", got)
	}
}
