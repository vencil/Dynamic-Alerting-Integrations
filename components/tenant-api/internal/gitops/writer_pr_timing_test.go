package gitops

// #1339 — WHEN the PR-mode write validates, relative to the in-lock fetch.
//
// The stateful half of validate() reads conf.d: the added-section gate's
// baseline, the `_defaults.yaml` merge behind ValidateTenantKeys, and the
// eol-expansion guard's current-alerts read. Running it before
// resolveFreshBaseRef judged the tree as it was BEFORE the fetch while the
// commit landed on the tree AFTER it — wrong in both directions, and both
// directions are exercised below on a real repo + real bare remote.
//
// The scenario every test here builds: a long-lived pod whose local base was
// synced once at startup, and an origin that has moved since. That is not a
// contrived shape — it is the exact hazard resolveFreshBaseRef's own comment
// describes and the reason TRK-318 fetches in-lock at all.

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

const (
	// A flat conf.d file legitimately declaring two tenants — the shape the
	// delta gate exists to keep editable (a tenant may edit a file it shares).
	flatDeclaringBoth = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n" +
		"  db-shared:\n    _silent_mode: \"critical\"\n"
	flatDeclaringOnlyA = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
)

// stalePodAndOrigin seeds a bare remote with db-a.yaml (plus an optional
// _defaults.yaml), clones it twice, and returns (podDir, authorDir). The pod
// clone is the tenant-api's long-lived working copy: fetched once here, never
// again until a write's in-lock fetch. The author clone is how the test moves
// origin/main behind the pod's back.
func stalePodAndOrigin(t testing.TB, tenantFile, defaultsFile string) (podDir, authorDir string) {
	t.Helper()
	remoteDir := initBareRemoteOnMain(t)
	authorDir = t.TempDir()
	gitClone(t, remoteDir, authorDir)
	gitRun(t, authorDir, "config", "user.email", "author@example.com")
	gitRun(t, authorDir, "config", "user.name", "Author")
	writeFileInDir(t, authorDir, "db-a.yaml", tenantFile)
	if defaultsFile != "" {
		writeFileInDir(t, authorDir, "_defaults.yaml", defaultsFile)
	}
	gitRun(t, authorDir, "add", "-A")
	gitRun(t, authorDir, "commit", "-m", "seed conf.d")
	gitRun(t, authorDir, "push", "origin", "main")

	podDir = t.TempDir()
	gitClone(t, remoteDir, podDir)
	gitRun(t, podDir, "config", "user.email", "pod@example.com")
	gitRun(t, podDir, "config", "user.name", "Pod")
	return podDir, authorDir
}

// advanceOrigin commits the given files on the author clone and pushes, leaving
// the pod's local base behind.
func advanceOrigin(t testing.TB, authorDir, msg string, files map[string]string) {
	t.Helper()
	for name, content := range files {
		writeFileInDir(t, authorDir, name, content)
	}
	gitRun(t, authorDir, "add", "-A")
	gitRun(t, authorDir, "commit", "-m", msg)
	gitRun(t, authorDir, "push", "origin", "main")
}

// TestWritePR_RefusesForeignSectionOnlyTheStaleBaseDeclares is the containment
// regression. db-shared was off-boarded from the shared flat file on origin;
// the pod's stale base still shows it. A body re-adding tenants.db-shared must
// be refused — grandfathering it off the pre-fetch tree writes a section the
// tree being committed to does not declare, which is #1681's gate defeated by
// timing alone.
func TestWritePR_RefusesForeignSectionOnlyTheStaleBaseDeclares(t *testing.T) {
	podDir, authorDir := stalePodAndOrigin(t, flatDeclaringBoth, "")
	advanceOrigin(t, authorDir, "off-board db-shared from the shared file",
		map[string]string{"db-a.yaml": flatDeclaringOnlyA})

	w := NewWriter(podDir, podDir)
	res, err := w.WritePR(context.Background(), "db-a", "attacker@example.com", flatDeclaringBoth)
	if !errors.Is(err, ErrValidation) {
		// Report what actually reached origin — "it returned nil" is not the
		// finding, "origin gained a section for another tenant" is.
		if err == nil {
			t.Fatalf("WritePR accepted a body adding tenants.db-shared, which the "+
				"freshly-fetched base no longer declares; pushed branch diff vs origin/main:\n%s",
				gitOut(t, podDir, "diff", "origin/main", "refs/remotes/origin/"+res.BranchName))
		}
		t.Fatalf("WritePR err = %v, want ErrValidation", err)
	}
	if !strings.Contains(err.Error(), "db-shared") {
		t.Errorf("error must name the refused section, got %v", err)
	}
	// The refusal must not leak the feature branch it cut to find out.
	if branches := gitOut(t, podDir, "branch", "--list", "tenant-api/*"); branches != "" {
		t.Errorf("feature branch leaked after a refused write: %q", branches)
	}
}

// TestWritePR_AcceptsForeignSectionTheFreshBaseDeclares is the availability
// half, and it is the direction a containment fix is most likely to break.
// db-shared was legitimately on-boarded into the shared flat file on origin.
// The pod has not seen that yet — but the write lands on the tree that HAS it,
// so the body must be accepted. Judging off the stale base bricks the tenant's
// own edits to its own file until the pod restarts.
func TestWritePR_AcceptsForeignSectionTheFreshBaseDeclares(t *testing.T) {
	podDir, authorDir := stalePodAndOrigin(t, flatDeclaringOnlyA, "")
	advanceOrigin(t, authorDir, "on-board db-shared into the shared file",
		map[string]string{"db-a.yaml": flatDeclaringBoth})

	body := "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n" +
		"  db-shared:\n    _silent_mode: \"critical\"\n"
	w := NewWriter(podDir, podDir)
	res, err := w.WritePR(context.Background(), "db-a", "ops@example.com", body)
	if err != nil {
		t.Fatalf("WritePR refused a body whose db-shared section the freshly-fetched "+
			"base declares: %v\n(pod local base still reads:\n%s)",
			err, gitOut(t, podDir, "show", "main:db-a.yaml"))
	}
	// And the write is the tenant's own edit, not a rollback of the shared file.
	got := gitOut(t, podDir, "show", "refs/remotes/origin/"+res.BranchName+":db-a.yaml")
	if !strings.Contains(got, "db-shared") {
		t.Errorf("pushed db-a.yaml dropped the shared tenant's section:\n%s", got)
	}
}

// TestWritePR_AcceptsKeyOnlyTheFreshDefaultsDeclare is the same availability
// bug through the OTHER stateful reader: ValidateTenantKeys judges "unknown
// key" against the merged _defaults.yaml, so a platform metric added on origin
// is invisible to a stale pod and every tenant adopting it gets a 400.
func TestWritePR_AcceptsKeyOnlyTheFreshDefaultsDeclare(t *testing.T) {
	podDir, authorDir := stalePodAndOrigin(t,
		"tenants:\n  db-a:\n    cpu_usage: 70\n",
		"defaults:\n  cpu_usage: 80\n")
	advanceOrigin(t, authorDir, "platform declares disk_usage",
		map[string]string{"_defaults.yaml": "defaults:\n  cpu_usage: 80\n  disk_usage: 90\n"})

	w := NewWriter(podDir, podDir)
	_, err := w.WritePR(context.Background(), "db-a", "ops@example.com",
		"tenants:\n  db-a:\n    cpu_usage: 70\n    disk_usage: 85\n")
	if err != nil {
		t.Fatalf("WritePR refused a key the freshly-fetched _defaults.yaml declares: %v\n"+
			"(pod local _defaults.yaml still reads:\n%s)",
			err, gitOut(t, podDir, "show", "main:_defaults.yaml"))
	}
}

// TestWritePRBatch_AcceptsForeignSectionTheFreshBaseDeclares pins the same
// availability property on the batch path. Its authoritative loop already ran
// after the checkout, but its PRE-FLIGHT ran the full validate against the
// pre-fetch tree and rejected there — so the batch path had the false refusal
// too, and fixing only WritePR would have left it.
func TestWritePRBatch_AcceptsForeignSectionTheFreshBaseDeclares(t *testing.T) {
	podDir, authorDir := stalePodAndOrigin(t, flatDeclaringOnlyA, "")
	advanceOrigin(t, authorDir, "on-board db-shared into the shared file",
		map[string]string{"db-a.yaml": flatDeclaringBoth})

	body := "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n" +
		"  db-shared:\n    _silent_mode: \"critical\"\n"
	w := NewWriter(podDir, podDir)
	_, err := w.WritePRBatch(context.Background(), []PRBatchOp{{
		TenantID: "db-a",
		Merge:    func([]byte) (string, error) { return body, nil },
	}}, "ops@example.com")
	if err != nil {
		t.Fatalf("WritePRBatch refused a body whose db-shared section the "+
			"freshly-fetched base declares: %v", err)
	}
}

// TestWritePR_MalformedBodyTouchesNoGit is the cost guard on the split above.
// Moving the STATEFUL checks past the checkout is only affordable because the
// stateless ones still run first: a body that is malformed rather than merely
// wrong must be a 400 that costs no fetch and no branch. Deleting the
// pre-flight would still satisfy every other test in this file.
//
// The oracle is the HEAD reflog, not `git branch`: a refused write rolls its
// branch back, so refs are identical either way — only the reflog records that
// a checkout happened at all.
func TestWritePR_MalformedBodyTouchesNoGit(t *testing.T) {
	podDir, _ := stalePodAndOrigin(t, flatDeclaringOnlyA, "")
	before := gitOut(t, podDir, "reflog", "--format=%gd %gs")

	w := NewWriter(podDir, podDir)
	for _, tc := range []struct{ name, body string }{
		{"unparseable", "tenants:\n  db-a:\n   : ::\n"},
		{"non-tenants root key", "defaults:\n  cpu_usage: 80\ntenants:\n  db-a:\n    _silent_mode: \"warning\"\n"},
		{"missing the target tenant", "tenants:\n  db-other:\n    _silent_mode: \"warning\"\n"},
		{"content after the first document", flatDeclaringOnlyA + "---\ntenants:\n  db-x:\n    _silent_mode: \"warning\"\n"},
	} {
		_, err := w.WritePR(context.Background(), "db-a", "x@example.com", tc.body)
		if !errors.Is(err, ErrValidation) {
			t.Errorf("%s: err = %v, want ErrValidation", tc.name, err)
		}
	}

	after := gitOut(t, podDir, "reflog", "--format=%gd %gs")
	if after != before {
		t.Errorf("a malformed body reached git — the stateless pre-flight is gone.\n"+
			"reflog before:\n%s\nreflog after:\n%s", before, after)
	}
}

// TestWritePR_MalformedBodyTakesNoAdmissionToken pins the OTHER half, and it is
// the half the cost argument actually rests on: the pre-flight must run before
// acquireWrite, not merely before the git commands.
//
// The reflog oracle above cannot see this. Moving the pre-flight to just after
// `w.mu.Lock()` — past admission control, still before every git command —
// leaves the reflog byte-identical and that test green, while a burst of
// malformed bodies would then take the single execution token and serialise
// behind each other. Measured: with the pre-flight moved there, this test fails
// and the reflog one still passes.
//
// Oracle: hold the writer's only execution token, then call with a context that
// expires almost immediately. A pre-flight that runs FIRST returns ErrValidation
// without ever looking at the token. One that runs after acquireWrite queues on
// the token instead and comes back with the context's error.
func TestWritePR_MalformedBodyTakesNoAdmissionToken(t *testing.T) {
	podDir, _ := stalePodAndOrigin(t, flatDeclaringOnlyA, "")
	w := NewWriter(podDir, podDir)

	<-w.writeExec // hold the single execution token; never returned in this test
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	_, err := w.WritePR(ctx, "db-a", "x@example.com", "tenants:\n  db-a:\n   : ::\n")
	if !errors.Is(err, ErrValidation) {
		t.Fatalf("WritePR err = %v, want ErrValidation — a malformed body queued for "+
			"the admission token instead of being refused before it", err)
	}
}
