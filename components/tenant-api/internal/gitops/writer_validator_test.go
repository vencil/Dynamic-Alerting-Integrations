package gitops

// #2903 (ADR-036 §4): the Writer asks its WriteValidator under the lock,
// with the bytes it is about to commit, on the commit it lands on — and a
// refusal commits nothing. The handler tests (internal/handler
// generator_gate_test.go) walk every HTTP write path; these cover the Writer
// methods directly: WriteMerged (no handler calls it) and the paths of a
// batch when conf.d is a subdirectory of the repository.

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

// recordingValidator records every call and answers with err.
type recordingValidator struct {
	mu    sync.Mutex
	calls []validatorCall
	err   error
}

type validatorCall struct {
	base  string
	files []ProposedFile
}

func (v *recordingValidator) ValidateWrites(_ context.Context, base string, files []ProposedFile) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.calls = append(v.calls, validatorCall{base, files})
	return v.err
}

var errFakeRefusal = errors.New("refused by the fake validator")

func TestWriteMerged_AsksTheValidator(t *testing.T) {
	t.Parallel()
	for _, refuse := range []bool{true, false} {
		dir := initRepoWithDefaults(t)
		w := NewWriter(dir, dir)
		v := &recordingValidator{}
		if refuse {
			v.err = errFakeRefusal
		}
		w.SetValidator(v)
		head := gitRevParse(t, dir, "HEAD")
		const body = "tenants:\n  db-a:\n    mysql_threads_running: \"70\"\n"
		_, err := w.WriteMerged(context.Background(), "db-a", "op@example.com", func([]byte) (string, error) { return body, nil })
		if refuse != errors.Is(err, errFakeRefusal) {
			t.Fatalf("refuse=%v: err = %v", refuse, err)
		}
		if len(v.calls) != 1 || v.calls[0].base != head || len(v.calls[0].files) != 1 ||
			v.calls[0].files[0].Path != "db-a.yaml" || string(v.calls[0].files[0].Content) != body ||
			v.calls[0].files[0].Tenants[0] != "db-a" {
			t.Fatalf("calls = %+v", v.calls)
		}
		moved := gitRevParse(t, dir, "HEAD") != head
		if moved == refuse {
			t.Errorf("refuse=%v: HEAD moved = %v", refuse, moved)
		}
		if _, statErr := os.Stat(filepath.Join(dir, "db-a.yaml")); refuse && statErr == nil {
			t.Error("a refused write left its file in the serving tree")
		}
	}
}

// conf.d below the repository root: the validator gets paths relative to
// conf.d (the generator's --config-subdir is that directory), in every
// path.
func TestValidator_PathsRelativeToConfDir(t *testing.T) {
	t.Parallel()
	repo := t.TempDir()
	conf := filepath.Join(repo, "conf.d")
	if err := os.MkdirAll(filepath.Join(conf, "team"), 0o755); err != nil {
		t.Fatal(err)
	}
	src, err := os.ReadFile(filepath.Join(pilotDefaultsDir(t), "_defaults.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(conf, "_defaults.yaml"), src, 0o644); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{{"init", "-b", "main"}, {"config", "user.email", "t@t.com"},
		{"config", "user.name", "T"}, {"add", "."}, {"commit", "-m", "initial"}} {
		if out, err := exec.Command("git", append([]string{"-C", repo}, args...)...).CombinedOutput(); err != nil {
			t.Skipf("git %v: %v\n%s", args, err, out)
		}
	}
	w := NewWriter(conf, repo)
	v := &recordingValidator{}
	w.SetValidator(v)
	const body = "tenants:\n  db-a:\n    mysql_threads_running: \"70\"\n"
	if _, err := w.Write(context.Background(), "db-a", "op@example.com", body); err != nil {
		t.Fatal(err)
	}
	merge := func(id string) MergeFunc {
		return func([]byte) (string, error) {
			return "tenants:\n  " + id + ":\n    mysql_threads_running: \"71\"\n", nil
		}
	}
	if _, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: "db-a", Merge: merge("db-a")},
		{TenantID: "db-b", Merge: merge("db-b")}}, "op@example.com"); err != nil {
		t.Fatal(err)
	}
	if len(v.calls) != 2 {
		t.Fatalf("calls = %d", len(v.calls))
	}
	var got []string
	for _, c := range v.calls {
		for _, f := range c.files {
			got = append(got, f.Path+"="+strings.Join(f.Tenants, ","))
		}
	}
	if strings.Join(got, " ") != "db-a.yaml=db-a db-a.yaml=db-a db-b.yaml=db-b" {
		t.Errorf("paths = %v, want conf.d-relative", got)
	}
	// A batch whose every op is a no-op makes no commit and asks nothing.
	if _, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: "db-a", Merge: merge("db-a")}}, "op@example.com"); err != nil {
		t.Fatal(err)
	}
	if len(v.calls) != 2 {
		t.Errorf("a no-op batch asked the validator: %d calls", len(v.calls))
	}
}
