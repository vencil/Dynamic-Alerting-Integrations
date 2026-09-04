package gitops

import (
	"context"
	"errors"
	"sort"
	"testing"

	"gopkg.in/yaml.v3"
)

// declaredTenants is a DELIBERATELY INDEPENDENT oracle. It must not call
// addedTenantKeys: that is the production helper under test here, and an oracle
// borrowed from the subject goes green the moment the subject breaks — the
// mutation would be unkillable. Ten lines of yaml is cheap enough to own.
func declaredTenants(t *testing.T, body string) []string {
	t.Helper()
	var doc struct {
		Tenants map[string]yaml.Node `yaml:"tenants"`
	}
	if err := yaml.Unmarshal([]byte(body), &doc); err != nil {
		t.Fatalf("oracle could not parse body: %v\n%s", err, body)
	}
	out := make([]string, 0, len(doc.Tenants))
	for id := range doc.Tenants {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

func notIn(a, b []string) []string {
	have := make(map[string]struct{}, len(b))
	for _, x := range b {
		have[x] = struct{}{}
	}
	var extra []string
	for _, x := range a {
		if _, ok := have[x]; !ok {
			extra = append(extra, x)
		}
	}
	sort.Strings(extra)
	return extra
}

// TestWritePR_ReValidatesAgainstTheFreshBase is the #1681 timing half.
//
// ⛔ THE GATE AND THE WRITE READ DIFFERENT TREES. `WritePR` validates at Step 1
// against the tree as it is BEFORE `checkoutBaseClean` + `resolveFreshBaseRef`,
// and `addedTenantKeys` takes its baseline from the tenant file on THAT tree.
// Step 3 then swaps the tree for the fresh origin/<base> and Step 4 writes the
// body there. Step 3b already re-resolves the PATH for exactly this reason
// (#1673) — but re-resolving the path and never re-running validate leaves the
// decision itself anchored to a tree the write does not land on.
//
// The reachable shape is not contrived: `resolveFreshBaseRef` exists because a
// long-lived pod's local base goes stale after startup (TRK-318). Let the stale
// base carry a grandfathered `other:` section and the fresh base not carry it,
// and the gate grandfathers a section the write ADDS.
//
// The assertion is the INVARIANT, not the mechanism: an accepted write may never
// declare a tenant the base it lands on did not. A fix that re-validates and a
// fix that re-computes the baseline both satisfy it; a fix that only re-resolves
// the path does not.
func TestWritePR_ReValidatesAgainstTheFreshBase(t *testing.T) {
	// ownOnlyFalse mirrors `grandfathered` minus the foreign section, so the two
	// trees differ ONLY in whether `other` is declared inside db-a.yaml.
	const ownOnlyFalse = "tenants:\n  db-a:\n    _silent_mode: \"false\"\n"
	const otherOwnFile = "tenants:\n  other:\n    _silent_mode: \"false\"\n"
	// body is what the tenant PUTs, and it is IDENTICAL in both arms — the two
	// arms differ only in the fresh base. It edits db-a's own value so the write
	// is a real change: a body byte-identical to the base takes WritePR's no-op
	// short-circuit and never reaches the gate at all, which would make the
	// control arm pass without exercising anything (measured).
	const body = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  other:\n    _silent_mode: \"false\"\n"

	for _, tc := range []struct {
		name string
		// freshBase is db-a.yaml as it exists on origin/main by the time the
		// write lands. The tenant-api's clone never sees it (stale since clone).
		freshBase string
		// splitOut seeds other.yaml alongside, mirroring a real "give `other`
		// its own file" migration.
		splitOut bool
		wantErr  bool
	}{
		// THE DEFECT. Local (stale) db-a.yaml still declares `other`; the fresh
		// base does not. Step 1 grandfathers `other`; Step 4 writes it onto a
		// base that never had it.
		{name: "fresh base dropped the section the stale base grandfathered",
			freshBase: ownOnlyFalse, splitOut: true, wantErr: true},

		// ⛔ MANDATORY CONTROL. Same body, same code path, fresh base UNCHANGED —
		// `other` is still grandfathered where the write lands, so this write is
		// legitimate and must still be accepted. Without this arm, "reject
		// everything" passes the arm above; #1681 recorded over-rejection as a
		// real failure mode (a tenant locked out of its own section until the
		// pod restarts), so it is pinned here rather than assumed.
		{name: "fresh base still declares it — legitimate edit, must be accepted",
			freshBase: grandfathered, splitOut: false, wantErr: false},

		// ⚠️ THE MIRROR DIRECTION IS NOT COVERED HERE, AND IS NOT FIXED.
		// Stale base LACKS a section the fresh base grandfathered → Step 1's
		// pre-flight has no baseline for it and refuses BEFORE the checkout ever
		// happens, so deciding on the fresh base cannot rescue it. Measured on
		// this branch: that arm stayed red with this fix applied. It is a
		// liveness defect, not the security one, and the same pre-flight shape
		// exists in WritePRBatch — so it is tracked separately rather than
		// widened into this change.
	} {
		t.Run(tc.name, func(t *testing.T) {
			remoteDir := initBareRemoteOnMain(t)

			authorDir := t.TempDir()
			gitClone(t, remoteDir, authorDir)
			gitRun(t, authorDir, "config", "user.email", "a@a.com")
			gitRun(t, authorDir, "config", "user.name", "A")
			// Seed: db-a.yaml carries a grandfathered `other:` section. This is
			// the tree the tenant-api's clone will still be holding.
			writeFileInDir(t, authorDir, "db-a.yaml", grandfathered)
			gitRun(t, authorDir, "add", "-A")
			gitRun(t, authorDir, "commit", "-m", "seed grandfathered db-a.yaml")
			gitRun(t, authorDir, "push", "origin", "main")

			// The tenant-api's long-lived clone: fetched once ("pod startup"),
			// never again until the in-lock fetch inside WritePR.
			dir := t.TempDir()
			gitClone(t, remoteDir, dir)
			gitRun(t, dir, "config", "user.email", "t@t.com")
			gitRun(t, dir, "config", "user.name", "T")

			// The remote moves on. The Writer's clone does not have this.
			//
			// The unrelated marker keeps this commit non-empty in BOTH arms: the
			// control arm leaves db-a.yaml byte-identical, and `git commit` on an
			// empty tree fails, which would make the control pass for the wrong
			// reason (no fresh base to re-validate against at all). With it, the
			// two arms differ in exactly one thing — whether the fresh db-a.yaml
			// still declares `other`.
			writeFileInDir(t, authorDir, "_unrelated.yaml", "marker: "+tc.name+"\n")
			writeFileInDir(t, authorDir, "db-a.yaml", tc.freshBase)
			if tc.splitOut {
				writeFileInDir(t, authorDir, "other.yaml", otherOwnFile)
			}
			gitRun(t, authorDir, "add", "-A")
			gitRun(t, authorDir, "commit", "-m", "remote advances")
			gitRun(t, authorDir, "push", "origin", "main")

			// The body still declares `other` — accepted against the stale base.
			res, err := NewWriter(dir, dir).WritePR(
				context.Background(), "db-a", "bob@example.com", body)

			if tc.wantErr {
				if err == nil {
					// Don't stop at "no error": say what actually landed, so a
					// failure names the widening rather than only the verdict.
					branch := gitOut(t, dir, "show",
						"refs/remotes/origin/"+res.BranchName+":db-a.yaml")
					base := gitOut(t, dir, "show", "origin/main:db-a.yaml")
					added := notIn(declaredTenants(t, branch), declaredTenants(t, base))
					t.Fatalf("WritePR accepted a body that ADDS tenant section(s) %v to the "+
						"base it landed on — the gate read the pre-checkout tree (#1681 timing half).\n"+
						"branch db-a.yaml:\n%s\nfresh base db-a.yaml:\n%s", added, branch, base)
				}
				if !errors.Is(err, ErrValidation) {
					t.Errorf("want ErrValidation, got %v", err)
				}
				return
			}

			if err != nil {
				t.Fatalf("legitimate write rejected — the fix over-rejects: %v", err)
			}
			branch := gitOut(t, dir, "show",
				"refs/remotes/origin/"+res.BranchName+":db-a.yaml")
			base := gitOut(t, dir, "show", "origin/main:db-a.yaml")
			if added := notIn(declaredTenants(t, branch), declaredTenants(t, base)); len(added) > 0 {
				t.Errorf("accepted write widened the content plane by %v", added)
			}
		})
	}
}
