package gitops

// #1718: the PRE-FLIGHT validator must not refuse a write on evidence read from
// a tree the write will not land on. See validateBodyOnly's doc comment for the
// split line; these tests pin both halves — that the refusals stop, and that the
// gate they were confused with still refuses.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// --- the split, at the unit level ---------------------------------------------

// TestValidateBodyOnlyIsTheBodyHalfOfValidate is the ANTI-DRIFT pin.
//
// The split's one real hazard is divergence: validate and validateBodyOnly each
// call ValidateTenantCustomAlerts, so deleting it from either leaves the other
// looking correct. This asserts the relationship instead of the implementation —
// every body-shaped defect must be caught by BOTH, and every tree-derived defect
// by validate ALONE.
func TestValidateBodyOnlyIsTheBodyHalfOfValidate(t *testing.T) {
	dir := t.TempDir()
	// A base file declaring only db-a, so `other` in a body is genuinely ADDED.
	if err := os.WriteFile(filepath.Join(dir, "db-a.yaml"), []byte(ownOnly), 0o644); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "db-a.yaml")

	bodyShaped := []struct{ name, body string }{
		{"invalid YAML", "tenants:\n  db-a:\n   - ]["},
		{"non-tenants root key", "defaults:\n  x: 1\ntenants:\n  db-a:\n    _silent_mode: \"warning\"\n"},
		{"body does not declare the URL id", "tenants:\n  someone-else:\n    _silent_mode: \"warning\"\n"},
		{"content after the first document", ownOnly + "---\ntenants:\n  sneaky:\n    _silent_mode: \"all\"\n"},
		// ⛔ THE ONE THAT ACTUALLY GUARDS THE DUPLICATION. The two validators each
		// call cfg.ValidateTenantCustomAlerts on their own line; every other row
		// here rides on the shared validateShape and so cannot drift. Without this
		// row the pin does not cover the only check that exists in two places —
		// which is exactly the check the split put at risk.
		{"duplicate custom-alert names", "tenants:\n  db-a:\n    _custom_alerts:\n" +
			"      - recipe: threshold\n        name: dup\n        metric: m\n        op: \">\"\n" +
			"        window: 5m\n        threshold: \"1:warning\"\n        mode: page\n" +
			"      - recipe: threshold\n        name: dup\n        metric: m2\n        op: \">\"\n" +
			"        window: 5m\n        threshold: \"2:warning\"\n        mode: page\n"},
	}
	for _, tc := range bodyShaped {
		t.Run("both reject: "+tc.name, func(t *testing.T) {
			if errs := validateBodyOnly("db-a", tc.body); len(errs) == 0 {
				t.Error("validateBodyOnly accepted a body-shaped defect — the pre-flight " +
					"has stopped catching something it is the only cheap chance to catch")
			}
			if errs, _ := validate(dir, "db-a", path, tc.body); len(errs) == 0 {
				t.Error("validate accepted a body-shaped defect — the split dropped a check " +
					"from the AUTHORITATIVE path, which is the dangerous direction")
			}
		})
	}

	// The tree-derived half: validateBodyOnly must stay silent (it cannot see the
	// base file and must not pretend to), validate must refuse.
	t.Run("only validate rejects an added foreign section", func(t *testing.T) {
		if errs := validateBodyOnly("db-a", smuggled); len(errs) > 0 {
			t.Errorf("validateBodyOnly refused on tree-derived evidence it does not have: %v", errs)
		}
		errs, _ := validate(dir, "db-a", path, smuggled)
		if len(errs) == 0 {
			t.Fatal("validate accepted a smuggled tenant section — the #1681 gate is gone")
		}
		if !strings.Contains(strings.Join(errs, "; "), "other") {
			t.Errorf("refusal does not name the added section: %v", errs)
		}
	})
}

// --- the defect this change exists to fix ------------------------------------

// seedStaleAndFresh builds a bare remote, an "author" clone that advances it,
// and a Writer clone that is left holding the PRE-advance tree — the long-lived
// pod whose local base went stale (TRK-318), which is the condition that made
// the pre-flight judge on the wrong tree.
func seedStaleAndFresh(t *testing.T, stale, fresh map[string]string) string {
	t.Helper()
	remoteDir := initBareRemoteOnMain(t)
	authorDir := t.TempDir()
	gitClone(t, remoteDir, authorDir)
	gitRun(t, authorDir, "config", "user.email", "a@a.com")
	gitRun(t, authorDir, "config", "user.name", "A")
	for name, body := range stale {
		writeFileInDir(t, authorDir, name, body)
	}
	gitRun(t, authorDir, "add", "-A")
	gitRun(t, authorDir, "commit", "-m", "stale state")
	gitRun(t, authorDir, "push", "origin", "main")

	dir := t.TempDir()
	gitClone(t, remoteDir, dir)
	gitRun(t, dir, "config", "user.email", "t@t.com")
	gitRun(t, dir, "config", "user.name", "T")

	for name, body := range fresh {
		writeFileInDir(t, authorDir, name, body)
	}
	gitRun(t, authorDir, "add", "-A")
	gitRun(t, authorDir, "commit", "-m", "remote advances")
	gitRun(t, authorDir, "push", "origin", "main")
	return dir
}

func TestWritePR_StaleTreeNoLongerOverRejects(t *testing.T) {
	const ownOnlyFalse = "tenants:\n  db-a:\n    _silent_mode: \"false\"\n"
	const withOther = "tenants:\n  db-a:\n    _silent_mode: \"false\"\n  other:\n    _silent_mode: \"false\"\n"

	t.Run("operator grandfathered a section this pod has not seen", func(t *testing.T) {
		dir := seedStaleAndFresh(t,
			map[string]string{"db-a.yaml": ownOnlyFalse},
			map[string]string{"db-a.yaml": withOther})
		// db-a edits its OWN value. Against the FRESH base this ADDS nothing.
		body := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  other:\n    _silent_mode: \"false\"\n"
		if _, err := NewWriter(dir, dir).WritePR(context.Background(), "db-a", "b@example.com", body); err != nil {
			t.Fatalf("legitimate write refused on the STALE tree: %v", err)
		}
	})

	t.Run("platform added a defaults key this pod has not seen", func(t *testing.T) {
		// ⛔ This arm is why the split line is "reads the tree", not "sounds
		// static": ValidateTenantKeys merges <configDir>/_defaults.yaml off disk.
		dir := seedStaleAndFresh(t,
			map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 100\n",
				"db-a.yaml":      "tenants:\n  db-a:\n    mysql_connections: \"120\"\n",
			},
			map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 100\n  mysql_threads_running: 30\n",
			})
		body := "tenants:\n  db-a:\n    mysql_connections: \"120\"\n    mysql_threads_running: \"45\"\n"
		if _, err := NewWriter(dir, dir).WritePR(context.Background(), "db-a", "b@example.com", body); err != nil {
			t.Fatalf("write using a key the FRESH defaults declare was refused: %v", err)
		}
	})
}

func TestWritePRBatch_StaleTreeNoLongerOverRejects(t *testing.T) {
	const ownOnlyFalse = "tenants:\n  db-a:\n    _silent_mode: \"false\"\n"
	const withOther = "tenants:\n  db-a:\n    _silent_mode: \"false\"\n  other:\n    _silent_mode: \"false\"\n"

	dir := seedStaleAndFresh(t,
		map[string]string{"db-a.yaml": ownOnlyFalse},
		map[string]string{"db-a.yaml": withOther})
	merge := func([]byte) (string, error) {
		return "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  other:\n    _silent_mode: \"false\"\n", nil
	}
	if _, err := NewWriter(dir, dir).WritePRBatch(context.Background(),
		[]PRBatchOp{{TenantID: "db-a", Merge: merge}}, "op@example.com"); err != nil {
		t.Fatalf("legitimate batch write refused on the STALE tree: %v", err)
	}
}

// --- what the pre-flight used to buy, and must still buy ----------------------

// TestTreeDependentRefusalStillLeavesNoDanglingBranch pins the cost side of the
// change. The pre-flight's second job (per WritePRBatch's own comment) was to
// stop an invalid write from cutting a branch. A tree-dependent refusal now
// happens AFTER the branch exists, so the rollback — not the pre-flight — is
// what keeps that promise. If abortFeatureBranch ever stops running on this
// path, the branch leaks silently and only this test says so.
//
// ⛔ IT ALSO PINS SOMETHING THIS CHANGE MADE LOAD-BEARING. Before the split, the
// pre-flight ALSO ran addedTenantKeys, so deleting the post-checkout validate
// would still have left the #1681 gate standing. It no longer would: for these
// two entry points the authoritative pass is now the ONLY place the gate runs.
// That is why this asserts the refusal itself and not just the branch cleanup.
func TestTreeDependentRefusalStillLeavesNoDanglingBranch(t *testing.T) {
	for _, tc := range []struct {
		name string
		call func(w *Writer) error
	}{
		{"WritePR", func(w *Writer) error {
			_, err := w.WritePR(context.Background(), "db-a", "b@example.com", smuggled)
			return err
		}},
		{"WritePRBatch", func(w *Writer) error {
			_, err := w.WritePRBatch(context.Background(),
				[]PRBatchOp{{TenantID: "db-a", Merge: func([]byte) (string, error) { return smuggled, nil }}},
				"op@example.com")
			return err
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := initRepoOnMain(t)
			seedBase(t, dir, "db-a.yaml", ownOnly)
			gitRun(t, dir, "add", "-A")
			gitRun(t, dir, "commit", "-m", "seed")

			err := tc.call(NewWriter(dir, dir))
			if err == nil {
				t.Fatal("a smuggled tenant section was accepted — the #1681 gate is gone")
			}
			if !errors.Is(err, ErrValidation) {
				t.Errorf("want ErrValidation, got %v", err)
			}
			if b := gitOut(t, dir, "branch", "--format=%(refname:short)"); strings.Contains(b, "tenant-api/") {
				t.Errorf("refused write left a dangling feature branch:\n%s", b)
			}
		})
	}
}
