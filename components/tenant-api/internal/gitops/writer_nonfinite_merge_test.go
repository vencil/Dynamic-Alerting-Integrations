package gitops

// #3140: the merge paths (WriteMerged / WriteBatch's readMergeValidate and
// WritePRBatch's pre-flight, readMergeBodyOnly) run the exporter's
// non-finite predicate on what the merge WRITES — a value the file already
// holds is carried over, not written, and does not refuse the write.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// setKey is a MergeFunc that rewrites db-a's mysql_connections line to value
// (seedTenantRepo's file carries it as "50").
func setKey(value string) MergeFunc {
	return func(existing []byte) (string, error) {
		return strings.Replace(string(existing), `mysql_connections: "50"`, `mysql_connections: "`+value+`"`, 1), nil
	}
}

// addSilentMode is a MergeFunc that writes another key and keeps the rest.
func addSilentMode(existing []byte) (string, error) {
	return string(existing) + "    _silent_mode: warning\n", nil
}

func TestMergePaths_RefuseANonFiniteThresholdTheyWrite(t *testing.T) {
	for _, value := range []string{"NaN", "-Inf", "+Inf:critical", "infinity"} {
		t.Run(value, func(t *testing.T) {
			dir := seedTenantRepo(t)
			w := newTestWriter(dir, dir)
			head := gitOut(t, dir, "rev-parse", "HEAD")
			_, err := w.WriteMerged(context.Background(), "db-a", "op@example.com", setKey(value))
			if !errors.Is(err, ErrValidation) || !strings.Contains(err.Error(), "is not a finite number") {
				t.Fatalf("WriteMerged %q: err = %v, want the non-finite ErrValidation", value, err)
			}
			if got := gitOut(t, dir, "rev-parse", "HEAD"); got != head {
				t.Errorf("WriteMerged %q committed %s", value, got)
			}
			path := filepath.Join(dir, "db-a.yaml")
			if err := w.readMergeBodyOnly("db-a", path, setKey(value)); !errors.Is(err, ErrValidation) {
				t.Errorf("readMergeBodyOnly %q: err = %v, want ErrValidation", value, err)
			}
		})
	}
}

func TestMergePaths_DoNotRefuseANonFiniteThresholdTheyCarry(t *testing.T) {
	dir := seedTenantRepo(t)
	path := filepath.Join(dir, "db-a.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	carried := strings.Replace(string(raw), `mysql_connections: "50"`, `mysql_connections: "NaN"`, 1)
	if err := os.WriteFile(path, []byte(carried), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "commit", "-am", "the file already holds NaN")
	w := newTestWriter(dir, dir)
	if err := w.readMergeBodyOnly("db-a", path, addSilentMode); err != nil {
		t.Errorf("readMergeBodyOnly refused a value the file already holds: %v", err)
	}
	if _, err := w.WriteMerged(context.Background(), "db-a", "op@example.com", addSilentMode); err != nil {
		t.Fatalf("WriteMerged refused a value the file already holds: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "_silent_mode") {
		t.Errorf("the write did not land:\n%s", got)
	}
}
