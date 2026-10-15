package gitops

// #2903 (ADR-036 §4 "驗的和寫的必須是同一份"): what the generator judged
// is what is committed —
//
//   - on the commit judged: a direct single-file write commits only while
//     HEAD is still the base the validator was given; a commit made from
//     outside the process while the generator judged refuses the write
//     (ErrConflict) and nothing of it is committed;
//   - with the bytes judged: a .gitattributes rule that rewrites a file as
//     it is staged refuses the write, single file and batch alike, and the
//     serving tree is left as HEAD has it;
//   - a single write whose body equals the file is a no-op and asks
//     nothing, as WriteMerged's.
//
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

const bindTenant = "tn-bind"

// externalCommitValidator makes a commit from outside the Writer while it
// "judges" — the shape of a concurrent writer outside the process — and
// then passes.
type externalCommitValidator struct {
	t    *testing.T
	dir  string
	mu   sync.Mutex
	made []string
}

func (v *externalCommitValidator) ValidateWrites(_ context.Context, base string, _ []ProposedFile) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	if err := os.WriteFile(filepath.Join(v.dir, "outside.txt"), []byte(base), 0o644); err != nil {
		v.t.Errorf("write outside.txt: %v", err)
	}
	for _, args := range [][]string{{"add", "outside.txt"}, {"commit", "-q", "-m", "outside the writer"}} {
		if out, err := exec.Command("git", append([]string{"-C", v.dir}, args...)...).CombinedOutput(); err != nil {
			v.t.Errorf("git %v: %v\n%s", args, err, out)
		}
	}
	v.made = append(v.made, gitRevParse(v.t, v.dir, "HEAD"))
	return nil
}

func gitLog(t *testing.T, dir string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "log", "--format=%s").Output()
	if err != nil {
		t.Fatalf("git log: %v", err)
	}
	return string(out)
}

func TestDirectWrite_RefusedWhenHEADMovesWhileJudged(t *testing.T) {
	t.Parallel()
	const body = "tenants:\n  " + bindTenant + ":\n    mysql_threads_running: \"70\"\n"
	for name, run := range map[string]func(*Writer) error{
		"Write": func(w *Writer) error {
			_, err := w.Write(context.Background(), bindTenant, "op@example.com", body)
			return err
		},
		"WriteMerged": func(w *Writer) error {
			_, err := w.WriteMerged(context.Background(), bindTenant, "op@example.com",
				func([]byte) (string, error) { return body, nil })
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := initRepoWithDefaults(t)
			w := NewWriter(dir, dir)
			v := &externalCommitValidator{t: t, dir: dir}
			w.SetValidator(v)
			base := gitRevParse(t, dir, "HEAD")

			if err := run(w); !errors.Is(err, ErrConflict) {
				t.Fatalf("err = %v, want ErrConflict", err)
			}
			if len(v.made) != 1 {
				t.Fatalf("validator calls = %d", len(v.made))
			}
			if head := gitRevParse(t, dir, "HEAD"); head != v.made[0] {
				t.Errorf("HEAD = %s, want the outside commit %s (nothing of the write on top)", head, v.made[0])
			}
			if parent := gitRevParse(t, dir, "HEAD~1"); parent != base {
				t.Errorf("HEAD~1 = %s, want the judged base %s", parent, base)
			}
			if log := gitLog(t, dir); strings.Contains(log, "tenant/"+bindTenant) {
				t.Errorf("a tenant-api commit is in the history:\n%s", log)
			}
			if _, err := os.Stat(filepath.Join(dir, bindTenant+".yaml")); err == nil {
				t.Error("the refused write left its file in the serving tree")
			}
		})
	}
}

// initRepoEOL is initRepoWithDefaults with `*.yaml text eol=lf` committed.
func initRepoEOL(t *testing.T) string {
	t.Helper()
	dir := pilotDefaultsDir(t)
	if err := os.WriteFile(filepath.Join(dir, ".gitattributes"), []byte("*.yaml text eol=lf\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{{"init", "-b", "main"}, {"config", "user.email", "t@t.com"},
		{"config", "user.name", "T"}, {"add", "."}, {"commit", "-m", "initial"}} {
		if out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput(); err != nil {
			t.Skipf("git %v: %v\n%s", args, err, out)
		}
	}
	return dir
}

// refuserValidator passes every judgement and renders the staged-bytes
// refusal itself (NormalizationRefuser), as the generator gate does.
type refuserValidator struct {
	recordingValidator
	refused [][]string
}

var errRefusedNormalized = errors.New("refused: normalized")

func (v *refuserValidator) RefuseNormalized(paths []string) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.refused = append(v.refused, append([]string(nil), paths...))
	return errRefusedNormalized
}

const crlfBody = "tenants:\r\n  " + bindTenant + ":\r\n    mysql_threads_running: \"70\"\r\n"

func TestWrite_NormalizedByGitAttributesIsRefused(t *testing.T) {
	t.Parallel()
	for _, existing := range []bool{false, true} {
		dir := initRepoEOL(t)
		w := NewWriter(dir, dir)
		file := filepath.Join(dir, bindTenant+".yaml")
		const lfBody = "tenants:\n  " + bindTenant + ":\n    mysql_threads_running: \"60\"\n"
		if existing {
			if _, err := w.Write(context.Background(), bindTenant, "op@example.com", lfBody); err != nil {
				t.Fatal(err)
			}
		}
		v := &refuserValidator{}
		w.SetValidator(v)
		head := gitRevParse(t, dir, "HEAD")

		_, err := w.Write(context.Background(), bindTenant, "op@example.com", crlfBody)
		if !errors.Is(err, errRefusedNormalized) || !w.stagedRefusal(err) {
			t.Fatalf("existing=%v: err = %v, want the validator's normalized refusal", existing, err)
		}
		if len(v.calls) != 1 || len(v.refused) != 1 || strings.Join(v.refused[0], ",") != bindTenant+".yaml" {
			t.Fatalf("existing=%v: calls = %d, refused = %v", existing, len(v.calls), v.refused)
		}
		if got := gitRevParse(t, dir, "HEAD"); got != head {
			t.Errorf("existing=%v: HEAD moved to %s", existing, got)
		}
		st, _ := exec.Command("git", "-C", dir, "status", "--porcelain").Output()
		if len(st) != 0 {
			t.Errorf("existing=%v: the serving tree is not HEAD's:\n%s", existing, st)
		}
		got, rerr := os.ReadFile(file)
		switch {
		case existing && (rerr != nil || string(got) != lfBody):
			t.Errorf("the file is %q (%v), want HEAD's %q", got, rerr, lfBody)
		case !existing && rerr == nil:
			t.Error("the refused new file was left in the serving tree")
		}
	}
}

// Without a NormalizationRefuser the refusal is ErrContentNormalized.
func TestWrite_NormalizedFallbackError(t *testing.T) {
	t.Parallel()
	dir := initRepoEOL(t)
	w := NewWriter(dir, dir)
	w.SetValidator(&recordingValidator{})
	_, err := w.Write(context.Background(), bindTenant, "op@example.com", crlfBody)
	if !errors.Is(err, ErrContentNormalized) || !strings.Contains(err.Error(), bindTenant+".yaml") {
		t.Fatalf("err = %v", err)
	}
}

func TestWriteBatch_NormalizedByGitAttributesIsRefused(t *testing.T) {
	t.Parallel()
	dir := initRepoEOL(t)
	w := NewWriter(dir, dir)
	v := &refuserValidator{}
	w.SetValidator(v)
	head := gitRevParse(t, dir, "HEAD")
	const other = "tn-bind-lf"
	ops := []BatchOp{
		{TenantID: other, Merge: func([]byte) (string, error) {
			return "tenants:\n  " + other + ":\n    mysql_threads_running: \"70\"\n", nil
		}},
		{TenantID: bindTenant, Merge: func([]byte) (string, error) { return crlfBody, nil }},
	}
	_, err := w.WriteBatch(context.Background(), ops, "op@example.com")
	if !errors.Is(err, errRefusedNormalized) {
		t.Fatalf("err = %v, want the validator's normalized refusal", err)
	}
	if len(v.refused) != 1 || strings.Join(v.refused[0], ",") != bindTenant+".yaml" {
		t.Errorf("refused = %v, want only the CRLF file", v.refused)
	}
	if got := gitRevParse(t, dir, "HEAD"); got != head {
		t.Errorf("HEAD moved to %s", got)
	}
	for _, id := range []string{other, bindTenant} {
		if _, err := os.Stat(filepath.Join(dir, id+".yaml")); err == nil {
			t.Errorf("%s.yaml of a refused batch is in the serving tree", id)
		}
	}
}

// An LF body under the same attributes commits: the check refuses only
// bytes git would change.
func TestWrite_UnchangedByGitAttributesCommits(t *testing.T) {
	t.Parallel()
	dir := initRepoEOL(t)
	w := NewWriter(dir, dir)
	v := &refuserValidator{}
	w.SetValidator(v)
	head := gitRevParse(t, dir, "HEAD")
	if _, err := w.Write(context.Background(), bindTenant, "op@example.com",
		"tenants:\n  "+bindTenant+":\n    mysql_threads_running: \"70\"\n"); err != nil {
		t.Fatal(err)
	}
	if gitRevParse(t, dir, "HEAD~1") != head || len(v.refused) != 0 {
		t.Errorf("an LF body was not committed on the judged base (refused = %v)", v.refused)
	}
}

// F8b: a body byte-identical to the file is success without asking.
func TestWrite_IdenticalBodyAsksNothing(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	w := NewWriter(dir, dir)
	const body = "tenants:\n  " + bindTenant + ":\n    mysql_threads_running: \"70\"\n"
	if _, err := w.Write(context.Background(), bindTenant, "op@example.com", body); err != nil {
		t.Fatal(err)
	}
	v := &recordingValidator{err: errFakeRefusal}
	w.SetValidator(v)
	head := gitRevParse(t, dir, "HEAD")
	if _, err := w.Write(context.Background(), bindTenant, "op@example.com", body); err != nil {
		t.Fatalf("identical body: %v", err)
	}
	if len(v.calls) != 0 {
		t.Errorf("an identical body asked the validator %d time(s)", len(v.calls))
	}
	if gitRevParse(t, dir, "HEAD") != head {
		t.Error("an identical body moved HEAD")
	}
	// A different body still asks (and here is refused).
	if _, err := w.Write(context.Background(), bindTenant, "op@example.com", body+"# changed\n"); !errors.Is(err, errFakeRefusal) {
		t.Fatalf("changed body: err = %v", err)
	}
	if len(v.calls) != 1 {
		t.Errorf("a changed body asked the validator %d time(s)", len(v.calls))
	}
}
