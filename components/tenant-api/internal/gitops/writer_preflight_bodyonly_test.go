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

	"github.com/vencil/tenant-api/internal/confd"
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

// TestWritePR_AmbiguousSpelling covers the resolution that moved out of the
// pre-flight with the same reasoning as the validators.
//
// tenantFilePath walks configDir (confd.ResolveTenantFile → os.ReadDir), so its
// errors are tree-derived too — which is why Step 1 no longer calls it. The two
// arms are the two halves of that: the refusal must survive, and the refusal
// must stop being issued on evidence from a tree the write does not land on.
//
// ⚠️ Nothing pinned WritePR's ambiguity refusal before this: the pre-existing
// TestWrite_AmbiguousSpellingIsRefused drives Write, not WritePR. So "no test
// broke when Step 1 stopped resolving" was NOT evidence that nothing changed —
// this is what makes it evidence.
func TestWritePR_AmbiguousSpelling(t *testing.T) {
	const body = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	const seed = "tenants:\n  db-a:\n    _silent_mode: \"false\"\n"

	t.Run("still refused when the FRESH base is ambiguous", func(t *testing.T) {
		dir := initRepoOnMain(t)
		seedBase(t, dir, "db-a.yaml", seed)
		seedBase(t, dir, "db-a.yml", seed)
		gitRun(t, dir, "add", "-A")
		gitRun(t, dir, "commit", "-m", "two spellings")

		_, err := NewWriter(dir, dir).WritePR(context.Background(), "db-a", "b@example.com", body)
		if err == nil {
			t.Fatal("WritePR accepted a tenant claimed by two files — it would pick one arbitrarily")
		}
		if b := gitOut(t, dir, "branch", "--format=%(refname:short)"); strings.Contains(b, "tenant-api/") {
			t.Errorf("refused write left a dangling feature branch:\n%s", b)
		}
	})

	t.Run("no longer refused when only the STALE tree was ambiguous", func(t *testing.T) {
		// Built inline rather than through seedStaleAndFresh: the remote advance
		// here is a DELETION, and that helper only writes files.
		remoteDir := initBareRemoteOnMain(t)
		authorDir := t.TempDir()
		gitClone(t, remoteDir, authorDir)
		gitRun(t, authorDir, "config", "user.email", "a@a.com")
		gitRun(t, authorDir, "config", "user.name", "A")
		writeFileInDir(t, authorDir, "db-a.yaml", seed)
		writeFileInDir(t, authorDir, "db-a.yml", seed)
		gitRun(t, authorDir, "add", "-A")
		gitRun(t, authorDir, "commit", "-m", "two spellings")
		gitRun(t, authorDir, "push", "origin", "main")

		// The pod clones while the tree is still ambiguous, and never fetches.
		dir := t.TempDir()
		gitClone(t, remoteDir, dir)
		gitRun(t, dir, "config", "user.email", "t@t.com")
		gitRun(t, dir, "config", "user.name", "T")

		// An operator cleans the duplicate up remotely.
		gitRun(t, authorDir, "rm", "-q", "db-a.yml")
		gitRun(t, authorDir, "commit", "-m", "clean up the duplicate spelling")
		gitRun(t, authorDir, "push", "origin", "main")

		if _, err := NewWriter(dir, dir).WritePR(
			context.Background(), "db-a", "b@example.com", body); err != nil {
			t.Fatalf("write refused over an ambiguity the FRESH base no longer has: %v", err)
		}
	})
}

// TestWritePRBatch_AmbiguousSpelling is the batch twin of the test above.
//
// Batch cannot drop the pre-flight resolution the way WritePR did — MergeFunc
// needs the existing body — so it drops only the refusal. Without these arms
// the two entry points disagree about the same tenant: WritePR accepts a write
// whose duplicate spelling the fresh base already cleaned up, WritePRBatch
// refuses it. Nothing detected that asymmetry before.
func TestWritePRBatch_AmbiguousSpelling(t *testing.T) {
	const seed = "tenants:\n  db-a:\n    _silent_mode: \"false\"\n"
	merge := func([]byte) (string, error) {
		return "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n", nil
	}
	batch := func(dir string) error {
		_, err := NewWriter(dir, dir).WritePRBatch(context.Background(),
			[]PRBatchOp{{TenantID: "db-a", Merge: merge}}, "op@example.com")
		return err
	}

	t.Run("still refused when the FRESH base is ambiguous", func(t *testing.T) {
		dir := initRepoOnMain(t)
		seedBase(t, dir, "db-a.yaml", seed)
		seedBase(t, dir, "db-a.yml", seed)
		gitRun(t, dir, "add", "-A")
		gitRun(t, dir, "commit", "-m", "two spellings")

		err := batch(dir)
		if !errors.Is(err, confd.ErrAmbiguousTenantFile) {
			t.Fatalf("err = %v, want confd.ErrAmbiguousTenantFile", err)
		}
		if b := gitOut(t, dir, "branch", "--format=%(refname:short)"); strings.Contains(b, "tenant-api/") {
			t.Errorf("refused batch left a dangling feature branch:\n%s", b)
		}
	})

	t.Run("no longer refused when only the STALE tree was ambiguous", func(t *testing.T) {
		remoteDir := initBareRemoteOnMain(t)
		authorDir := t.TempDir()
		gitClone(t, remoteDir, authorDir)
		gitRun(t, authorDir, "config", "user.email", "a@a.com")
		gitRun(t, authorDir, "config", "user.name", "A")
		writeFileInDir(t, authorDir, "db-a.yaml", seed)
		writeFileInDir(t, authorDir, "db-a.yml", seed)
		gitRun(t, authorDir, "add", "-A")
		gitRun(t, authorDir, "commit", "-m", "two spellings")
		gitRun(t, authorDir, "push", "origin", "main")

		dir := t.TempDir()
		gitClone(t, remoteDir, dir)
		gitRun(t, dir, "config", "user.email", "t@t.com")
		gitRun(t, dir, "config", "user.name", "T")

		gitRun(t, authorDir, "rm", "-q", "db-a.yml")
		gitRun(t, authorDir, "commit", "-m", "clean up the duplicate spelling")
		gitRun(t, authorDir, "push", "origin", "main")

		if err := batch(dir); err != nil {
			t.Fatalf("batch refused over an ambiguity the FRESH base no longer has: %v", err)
		}
	})
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
