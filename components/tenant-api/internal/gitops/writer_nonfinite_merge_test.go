package gitops

// #3140: the merge paths (WriteMerged / WriteBatch's readMergeValidate and
// WritePRBatch's pre-flight, readMergeBodyOnly) run the exporter's
// non-finite predicate on what the merge WRITES — a value the file already
// holds is carried over, not written, and does not refuse the write.
// #3267: the predicate is every value the exporter refuses
// (the #3267 threshold value finding, RefusedValueAt's classifier), not only a
// non-finite one.

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
	// #3267: and every other value the exporter refuses.
	refusals := map[string]string{"NaN": "is not a finite number", "-Inf": "is not a finite number",
		"+Inf:critical": "is not a finite number", "infinity": "is not a finite number",
		"abc": "is not a threshold the exporter reads", "70:high": "is not a threshold the exporter reads",
		"1e999": "is not a threshold the exporter reads", "0x1p1024": "is not a threshold the exporter reads",
		// #3457: outside the grammar of a threshold number.
		"1_000": "is not a threshold the exporter reads", "0x1p4": "is not a threshold the exporter reads",
		"123456789012345678901": "is not a threshold the exporter reads",
		"":                      "is not a threshold the exporter reads"}
	for value, want := range refusals {
		t.Run(value, func(t *testing.T) {
			dir := seedTenantRepo(t)
			w := newTestWriter(dir, dir)
			head := gitOut(t, dir, "rev-parse", "HEAD")
			_, err := w.WriteMerged(context.Background(), "db-a", "op@example.com", setKey(value))
			if !errors.Is(err, ErrValidation) || !strings.Contains(err.Error(), want) {
				t.Fatalf("WriteMerged %q: err = %v, want the ErrValidation %q", value, err, want)
			}
			if got := gitOut(t, dir, "rev-parse", "HEAD"); got != head {
				t.Errorf("WriteMerged %q committed %s", value, got)
			}
			// The pre-flight refuses a non-finite number (#3140, strict); the
			// other refused values are judged against the file the write
			// replaces (#3267's ratchet), which the pre-flight does not read.
			path := filepath.Join(dir, "db-a.yaml")
			err = w.readMergeBodyOnly("db-a", path, setKey(value))
			if strict := want == "is not a finite number"; errors.Is(err, ErrValidation) != strict {
				t.Errorf("readMergeBodyOnly %q: err = %v, want ErrValidation = %v", value, err, strict)
			}
		})
	}
}

// The values the exporter reads go through the merge paths.
func TestMergePaths_TakeAThresholdTheExporterReads(t *testing.T) {
	for _, value := range []string{"70", "1e3", "+.5", "12345678901234567890", "70:critical", "disable"} {
		t.Run(value, func(t *testing.T) {
			dir := seedTenantRepo(t)
			w := newTestWriter(dir, dir)
			if err := w.readMergeBodyOnly("db-a", filepath.Join(dir, "db-a.yaml"), setKey(value)); err != nil {
				t.Errorf("readMergeBodyOnly %q: %v", value, err)
			}
			if _, err := w.WriteMerged(context.Background(), "db-a", "op@example.com", setKey(value)); err != nil {
				t.Errorf("WriteMerged %q: %v", value, err)
			}
		})
	}
}

func TestMergePaths_DoNotRefuseANonFiniteThresholdTheyCarry(t *testing.T) {
	for _, held := range []string{"NaN", "abc"} {
		t.Run(held, func(t *testing.T) { mergeCarries(t, held) })
	}
}

func mergeCarries(t *testing.T, held string) {
	dir := seedTenantRepo(t)
	path := filepath.Join(dir, "db-a.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	carried := strings.Replace(string(raw), `mysql_connections: "50"`, `mysql_connections: "`+held+`"`, 1)
	if err := os.WriteFile(path, []byte(carried), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "commit", "-am", "the file already holds "+held)
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
