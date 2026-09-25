package testutil

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// gitEnvKeys registers every variable DisableGitAutoMaintenance may touch with
// t.Setenv, so the writes it makes through os.Setenv are rolled back after the
// test instead of leaking into the rest of this test binary.
func gitEnvKeys(t *testing.T, count string) {
	t.Helper()
	for i := 0; i < 4; i++ {
		t.Setenv("GIT_CONFIG_KEY_"+strconv.Itoa(i), "")
		t.Setenv("GIT_CONFIG_VALUE_"+strconv.Itoa(i), "")
	}
	t.Setenv("GIT_CONFIG_COUNT", count)
}

func TestDisableGitAutoMaintenance_StartsAtZeroWhenUnset(t *testing.T) {
	gitEnvKeys(t, "")
	if err := DisableGitAutoMaintenance(); err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, k := range []string{"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"} {
		got[k] = os.Getenv(k)
	}
	want := map[string]string{
		"GIT_CONFIG_COUNT": "2",
		"GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false",
		"GIT_CONFIG_KEY_1": "gc.auto", "GIT_CONFIG_VALUE_1": "0",
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %q, want %q", k, got[k], v)
		}
	}
}

// An entry the environment already carries must survive: overwriting
// GIT_CONFIG_KEY_0 would silently drop a developer's or CI's own config.
func TestDisableGitAutoMaintenance_AppendsAfterExistingEntries(t *testing.T) {
	gitEnvKeys(t, "1")
	t.Setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
	t.Setenv("GIT_CONFIG_VALUE_0", "false")
	if err := DisableGitAutoMaintenance(); err != nil {
		t.Fatal(err)
	}
	if got := os.Getenv("GIT_CONFIG_COUNT"); got != "3" {
		t.Fatalf("GIT_CONFIG_COUNT = %q, want 3", got)
	}
	if got := os.Getenv("GIT_CONFIG_KEY_0"); got != "core.autocrlf" {
		t.Errorf("existing GIT_CONFIG_KEY_0 was overwritten: %q", got)
	}
	if got := os.Getenv("GIT_CONFIG_KEY_1"); got != "maintenance.auto" {
		t.Errorf("GIT_CONFIG_KEY_1 = %q, want maintenance.auto", got)
	}
	if got := os.Getenv("GIT_CONFIG_KEY_2"); got != "gc.auto" {
		t.Errorf("GIT_CONFIG_KEY_2 = %q, want gc.auto", got)
	}
}

func TestDisableGitAutoMaintenance_RejectsAMalformedCount(t *testing.T) {
	for _, bad := range []string{"abc", "-1"} {
		gitEnvKeys(t, bad)
		if err := DisableGitAutoMaintenance(); err == nil {
			t.Errorf("GIT_CONFIG_COUNT=%q: want an error, got nil", bad)
		}
	}
}

// The unit tests above only check the variables we write. This one checks that
// git itself reads them, which is what actually stops the background process.
func TestDisableGitAutoMaintenance_GitReadsTheSettings(t *testing.T) {
	gitEnvKeys(t, "")
	if err := DisableGitAutoMaintenance(); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if out, err := exec.Command("git", "-C", dir, "init", "-q").CombinedOutput(); err != nil {
		t.Fatalf("git init: %v: %s", err, out)
	}
	for key, want := range map[string]string{"maintenance.auto": "false", "gc.auto": "0"} {
		out, err := exec.Command("git", "-C", dir, "config", "--get", key).Output()
		if err != nil {
			t.Fatalf("git config --get %s: %v", key, err)
		}
		if got := strings.TrimSpace(string(out)); got != want {
			t.Errorf("git sees %s = %q, want %q", key, got, want)
		}
	}
}

// runsGit matches the two ways a test in this module reaches git: passing the
// "git" binary name to exec, or constructing the real gitops writer.
var runsGit = regexp.MustCompile(`"git"|\bNewWriter\(`)

// Every package whose tests run git must call DisableGitAutoMaintenance from
// its TestMain; a new package that forgets it gets the flaky TempDir cleanup
// back. The detection is textual, so it is anchored by a floor: if it stops
// finding the packages it knows about, it fails instead of passing on nothing.
func TestEveryPackageThatRunsGitDisablesAutoMaintenance(t *testing.T) {
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Fatalf("module root not found at %s: %v", root, err)
	}
	runs := map[string]bool{}
	calls := map[string]bool{}
	err = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, "_test.go") {
			return nil
		}
		dir, _ := filepath.Rel(root, filepath.Dir(path))
		if filepath.Base(dir) == "testutil" {
			return nil // this package's own tests run git on purpose
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if runsGit.Match(b) {
			runs[dir] = true
		}
		if strings.Contains(string(b), "testutil.DisableGitAutoMaintenance()") {
			calls[dir] = true
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	var missing []string
	for dir := range runs {
		if !calls[dir] {
			missing = append(missing, dir)
		}
	}
	sort.Strings(missing)
	if len(runs) < 3 {
		t.Fatalf("found only %d packages whose tests run git (%v); expected at least gitops, handler and handler/federation — the detection regexp stopped matching", len(runs), runs)
	}
	if len(missing) > 0 {
		t.Errorf("these packages run git in tests but their TestMain does not call testutil.DisableGitAutoMaintenance(): %v", missing)
	}
}
