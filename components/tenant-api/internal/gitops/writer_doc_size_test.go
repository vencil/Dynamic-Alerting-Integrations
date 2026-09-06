package gitops

// #1722: the pre-parse size gate on a single tenant document.
//
// The gate exists because since #1718 the authoritative validate() runs INSIDE
// the single-writer token, which turned "this body is expensive to parse" from
// a cost the sender pays into cross-tenant head-of-line blocking. These tests
// pin the three properties that make the gate worth having — it refuses BEFORE
// parsing, it refuses on the MERGED document (the semantics chosen for #1722),
// and it does not refuse anything a tenant legitimately writes.

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// oversizeDoc builds a tenant document just over the cap that is wrong in
// EXACTLY ONE WAY: its size.
//
// ⛔ THE KEYS MUST BE UNIQUE AND LEGAL, and that is not cosmetic. An earlier
// version of this helper repeated one key, which made the document invalid YAML
// (duplicate mapping key) — so every test using it would have failed for a
// reason that was not the size gate, and a counterfactual that moved the gate
// after the parse looked like it killed four tests when it should kill one.
// Version-labelled metric keys (ADR-024) are the natural way to get many
// distinct keys that all resolve against the platform defaults, so a document
// built here is one the validator would ACCEPT if the gate were removed.
func oversizeDoc(t *testing.T, tenantID string) string {
	t.Helper()
	var b strings.Builder
	b.WriteString("tenants:\n  " + tenantID + ":\n")
	for i := 0; int64(b.Len()) <= maxTenantDocBytes; i++ {
		fmt.Fprintf(&b, "    container_cpu{version=\"v%d\"}: 70\n", i)
	}
	return b.String()
}

// --- the gate itself ----------------------------------------------------------

// TestValidateShapeRefusesOversizeDocument pins that the refusal happens and
// that its message is actionable. An operator who hits a byte cap can do
// nothing with "too large"; the three facts they need are how big the document
// actually was, what the limit is, and which knob moves it.
func TestValidateShapeRefusesOversizeDocument(t *testing.T) {
	t.Parallel()
	body := oversizeDoc(t, "db-a")
	_, errs := validateShape("db-a", body)
	if len(errs) == 0 {
		t.Fatal("oversize document accepted — the #1722 gate is gone")
	}
	msg := strings.Join(errs, "; ")
	for _, want := range []string{"TA_MAX_TENANT_DOC_BYTES"} {
		if !strings.Contains(msg, want) {
			t.Errorf("refusal does not name %q, so an operator cannot act on it: %s", want, msg)
		}
	}
	if !strings.Contains(msg, strconv.Itoa(len(body))) {
		t.Errorf("refusal does not name the ACTUAL size %d: %s", len(body), msg)
	}
	if !strings.Contains(msg, strconv.FormatInt(maxTenantDocBytes, 10)) {
		t.Errorf("refusal does not name the LIMIT %d: %s", maxTenantDocBytes, msg)
	}
}

// TestOversizeGateRunsBeforeTheParse is the load-bearing ordering proof.
//
// ⛔ A timing assertion would be flaky, so this proves the ordering by
// CONSEQUENCE instead: the body is oversize AND unparseable. If the gate ran
// after yaml.Unmarshal the answer would be "invalid YAML"; getting the size
// message back is only possible if nothing parsed it. That is the whole claim —
// the gate's value is that refusing is cheap.
func TestOversizeGateRunsBeforeTheParse(t *testing.T) {
	t.Parallel()
	broken := "tenants:\n  db-a:\n   - ][\n" + strings.Repeat("#x\n", int(maxTenantDocBytes/3)+1)
	if int64(len(broken)) <= maxTenantDocBytes {
		t.Fatalf("fixture is not oversize (%d <= %d)", len(broken), maxTenantDocBytes)
	}
	_, errs := validateShape("db-a", broken)
	if len(errs) == 0 {
		t.Fatal("accepted")
	}
	msg := strings.Join(errs, "; ")
	if strings.Contains(msg, "invalid YAML") {
		t.Errorf("got the PARSER's error, so the gate ran after the parse — it "+
			"bounds nothing: %s", msg)
	}
	if !strings.Contains(msg, "TA_MAX_TENANT_DOC_BYTES") {
		t.Errorf("expected the size refusal, got: %s", msg)
	}
}

// TestAtTheCapIsStillAccepted is the off-by-one guard: the limit is inclusive,
// so a document of exactly maxTenantDocBytes must pass. Without this the gate
// could tighten by one byte and no other test would notice.
func TestAtTheCapIsStillAccepted(t *testing.T) {
	t.Parallel()
	// ⛔ The ENFORCED value, not the compiled-in default. validateShape checks
	// against maxTenantDocBytes, so sizing the fixture from
	// DefaultTenantDocBytes() tests a boundary the code does not enforce
	// whenever TA_MAX_TENANT_DOC_BYTES is set: measured at 32768 the old form
	// went RED on an inclusive limit, and at 131072 it passed with a fixture far
	// under the cap, detecting nothing. Reading the enforced value keeps the
	// test on the real boundary at any setting; a setting too small for that
	// boundary to exist is the Fatal below, never a skip.
	cap := MaxTenantDocBytes()
	head := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	pad := int(cap) - len(head)
	if pad < 0 {
		t.Fatalf("cap %d is smaller than a minimal tenant document (%d bytes) — "+
			"the boundary this test guards does not exist at that setting", cap, len(head))
	}
	body := head + strings.Repeat("#", pad)
	if int64(len(body)) != cap {
		t.Fatalf("fixture is %d bytes, want exactly %d", len(body), cap)
	}
	if _, errs := validateShape("db-a", body); len(errs) > 0 {
		t.Errorf("a document of exactly the cap was refused — the limit is not "+
			"inclusive: %v", errs)
	}
}

// TestRealTenantFilesAreNowhereNearTheCap keeps the number honest against the
// evidence it was chosen from.
//
// ⛔ IT MEASURES THE REPO, NOT A LITERAL. The first version hard-coded the
// largest file's size, which guards only the direction nobody takes (someone
// lowering the cap) and is blind to the one that actually causes a production
// 400: someone ADDING a tenant config that approaches it. Walking the tree makes
// the test fail on either move.
// realTenantConfigs returns every real tenant config in the repo, relative to the
// repo root, so the cap's two justifications share one definition of the term.
func realTenantConfigs(t *testing.T) (root string, rels []string) {
	t.Helper()
	root = testutil.RepoRoot(t)
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // unreadable subtree: skip, do not fail the suite
		}
		if d.IsDir() {
			if name := d.Name(); name == ".git" || name == "node_modules" {
				return fs.SkipDir
			}
			return nil
		}
		if ext := filepath.Ext(path); ext != ".yaml" && ext != ".yml" {
			return nil
		}
		raw, rerr := os.ReadFile(path)
		// ⛔ NOT HasPrefix: every real tenant config here opens with a comment, so
		// a byte-0 test matches only golden fixtures.
		if rerr != nil || !hasTopLevelTenants(raw) {
			return nil
		}
		if rel, rerr := filepath.Rel(root, path); rerr == nil {
			rels = append(rels, filepath.ToSlash(rel))
		}
		return nil
	})
	// ⛔ Fatal, not Skip: an unwalkable repo means these tests measured nothing,
	// and a skip reports that as a green run.
	if err != nil {
		t.Fatalf("could not walk the repo (err=%v)", err)
	}
	// ⛔ Zero matches is a FAILURE, not a skip: a filter matching nothing would
	// otherwise sail past every assertion below and report PASS.
	if len(rels) == 0 {
		t.Fatal("the walk matched no tenant config at all — the filter is wrong " +
			"and these tests are guarding nothing")
	}
	sort.Strings(rels)
	return root, rels
}

// largestRealTenantConfig picks the biggest of realTenantConfigs, with the floor
// check that proves the walk saw real configs at all.
func largestRealTenantConfig(t *testing.T) (path string, size int64) {
	t.Helper()
	root, rels := realTenantConfigs(t)
	for _, rel := range rels {
		raw, err := os.ReadFile(filepath.Join(root, rel))
		if err != nil {
			continue
		}
		if n := int64(len(raw)); n > size {
			path, size = rel, n
		}
	}
	// ⛔ The walk must be proven to see REAL configs, not merely to run: the
	// headroom assertions are satisfied by any small number, so a walk that
	// matched only fixtures would pass while guarding nothing.
	const smallestPlausibleRealConfig = 1024
	if size < smallestPlausibleRealConfig {
		t.Fatalf("the walk's largest match is %s at %d bytes — under %d, so it is "+
			"matching test fixtures rather than real tenant configs and these "+
			"tests are guarding nothing", path, size, smallestPlausibleRealConfig)
	}
	return path, size
}

// worstBytesPerRecipe returns the DENSEST-per-recipe real config — the file that
// costs the most bytes per recipe — because that is the one that projects the
// largest legitimate body at the recipe ceiling.
//
// ⛔ IT IS NOT THE LARGEST FILE, WHICH IS WHAT THE FIRST VERSION USED. Dividing
// the largest file's size by its recipe count is a different quantity, and it
// moved for reasons unrelated to recipes: adding a large recipe-free example made
// the projection SKIP (largest file, zero recipes → nothing to divide) and so
// dropped half the cap's justification with no signal at all.
//
// ⚠️ THE CONSERVATIVE DIRECTION IS DELIBERATE AND IT DOES FIRE. Taking the WORST
// density means a single verbose example drives the projection: a real config at
// ~4,000 B/recipe makes this fail (20 x 4,000 = 80,000, outside the cap's 2x
// band) even though no tenant has authored such a body. That is the safe way to
// be wrong — it errs toward "the cap may refuse a legitimate write", the failure
// this whole change set exists to avoid — and the message names the file so a
// reader can judge whether to widen the cap or trim the example. It is NOT a
// claim that this test never raises a false alarm.
func worstBytesPerRecipe(t *testing.T) (path string, perRecipe int64) {
	t.Helper()
	root, rels := realTenantConfigs(t)
	for _, rel := range rels {
		raw, err := os.ReadFile(filepath.Join(root, rel))
		if err != nil {
			continue
		}
		n := bytes.Count(raw, []byte("recipe:"))
		if n == 0 {
			continue
		}
		if per := int64(len(raw)) / int64(n); per > perRecipe {
			path, perRecipe = rel, per
		}
	}
	// ⛔ FATAL, NOT SKIP. "No real config declares a recipe" would mean the
	// projection this test exists to make is unmakeable — silently passing then
	// is how the recipe half of the cap's justification disappears without a
	// signal, which is exactly what happened when this was a t.Skipf.
	if perRecipe == 0 {
		t.Fatal("no real tenant config in the repo declares a recipe — the " +
			"recipe-ceiling projection cannot be made, and passing anyway would " +
			"silently drop half the cap's justification")
	}
	return path, perRecipe
}

// TestRealTenantFilesAreNowhereNearTheCap is the MECHANICAL SSOT for the
// legitimate-side half of the cap's justification.
//
// ⛔ IT EXISTS BECAUSE THE PROSE VERSION KEPT ROTTING. The same evidence lived
// as literals in a doc comment, the CHANGELOG, the README and a helm values
// comment; three separate blind-review rounds found one or another of them
// stale or simply wrong. Numbers a test derives cannot drift out of step with
// the repo — so the literals are gone from all four surfaces and this is where
// the claim lives.
func TestRealTenantFilesAreNowhereNearTheCap(t *testing.T) {
	t.Parallel()
	largestPath, largest := largestRealTenantConfig(t)
	cap := DefaultTenantDocBytes()
	t.Logf("largest tenant config in the repo (examples and fixtures included): "+
		"%s = %d bytes; cap = %d (%.1fx)",
		largestPath, largest, cap, float64(cap)/float64(largest))

	// LOWER bound — refuse nothing anyone legitimately writes.
	if cap < 2*largest {
		t.Errorf("cap %d leaves under 2x headroom over the largest tenant config in "+
			"this repo (%s, %d bytes). The repo's configs — examples included — are "+
			"the only evidence of what tenants author, so either raise the cap or "+
			"establish that this file is not representative", cap, largestPath, largest)
	}
	// UPPER bound — the half that went missing when the cost evidence moved out
	// of prose.
	//
	// ⛔ WITHOUT THIS THE CAP HAS NO CEILING AT ALL: raising it fourfold left the
	// whole suite green, and the prose that a reader could once have used to tell
	// 64 KiB from 64 MiB had been deleted. Parse cost grows faster than size and
	// is paid inside the single-writer token, so an unbounded cap is latency one
	// tenant can hand another.
	//
	// ⚠️ maxLegitimateMultiple IS A POLICY RATIO, NOT A MEASUREMENT — it says "no
	// tenant needs an order of magnitude more than the largest thing anyone has
	// written", and it is deliberately expressed against repo-derived evidence so
	// it moves when that evidence does. For seconds, run BenchmarkValidateAtCap;
	// a wall-clock threshold here would be flaky and is why none is asserted.
	const maxLegitimateMultiple = 16
	if cap > maxLegitimateMultiple*largest {
		t.Errorf("cap %d is more than %dx the largest real tenant config (%s, %d "+
			"bytes) — nothing legitimate needs that, and every byte of it is parse "+
			"work one tenant can force inside the write lock; lower the cap or "+
			"justify the new band here", cap, maxLegitimateMultiple, largestPath, largest)
	}
}

// TestCapClearsTheRecipeCeiling derives the OTHER legitimate-side bound: a
// tenant is capped at cfg.MaxCustomRecipesDefault recipes, so the largest body
// anyone can legally author is roughly that many recipes' worth of bytes.
//
// The projection is computed from the repo's own densest real file rather than
// asserted as a literal — the literal ("roughly 12 KiB") was written once and
// never re-derived, and nothing would have caught it drifting.
func TestCapClearsTheRecipeCeiling(t *testing.T) {
	t.Parallel()
	path, perRecipe := worstBytesPerRecipe(t)
	projected := perRecipe * int64(cfg.MaxCustomRecipesDefault)
	cap := DefaultTenantDocBytes()
	t.Logf("densest-per-recipe real config %s: %d B/recipe; at the %d-recipe "+
		"ceiling that projects to %d bytes; cap = %d (%.1fx)",
		path, perRecipe, cfg.MaxCustomRecipesDefault, projected, cap,
		float64(cap)/float64(projected))
	// 2x is the same floor the sibling test uses: enough that a legitimate
	// maximal body is nowhere near the refusal, without pinning a ratio that
	// would fail on any ordinary edit to the example files.
	if cap < 2*projected {
		t.Errorf("cap %d leaves under 2x headroom over a full %d-recipe tenant "+
			"(projected %d bytes from %s) — a tenant using its recipe budget "+
			"would be refused", cap, cfg.MaxCustomRecipesDefault, projected, path)
	}
}

// --- env parsing --------------------------------------------------------------

func TestTenantDocBytesFromEnv(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name, env     string
		want          int64
		wantMalformed bool
	}{
		{"unset", "", defaultMaxTenantDocBytes, false},
		{"valid", "4096", 4096, false},
		{"zero is a fat-finger, not a valid 'reject everything'", "0", defaultMaxTenantDocBytes, true},
		{"negative", "-1", defaultMaxTenantDocBytes, true},
		{"unparseable", "abc", defaultMaxTenantDocBytes, true},
		// #795 F4: a numeric PREFIX must not be accepted as the number.
		{"numeric prefix", "65536x", defaultMaxTenantDocBytes, true},
		{"whitespace trimmed", "  8192  ", 8192, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got, malformed := TenantDocBytesFromEnv(tc.env)
			if got != tc.want || malformed != tc.wantMalformed {
				t.Errorf("got (%d, %v), want (%d, %v)", got, malformed, tc.want, tc.wantMalformed)
			}
		})
	}
}

// --- end to end ---------------------------------------------------------------

// TestWritePR_OversizeRefusedWithoutTouchingGit proves the gate is reached on
// the real write path AND that it fires in the pre-flight — before the branch
// is cut, and therefore before the single-writer token is taken. A gate that
// only fired at Step 3c would still be inside the lock, i.e. would not bound
// the thing it exists to bound.
func TestWritePR_OversizeRefusedWithoutTouchingGit(t *testing.T) {
	work := probeSizeRepo(t)
	w := NewWriter(work, work)

	_, err := w.WritePR(context.Background(), "db-a", "p@p", oversizeDoc(t, "db-a"))
	if err == nil {
		t.Fatal("oversize body accepted by WritePR")
	}
	if !errors.Is(err, ErrValidation) {
		t.Fatalf("want ErrValidation (→400), got %T %v", err, err)
	}
	if !strings.Contains(err.Error(), "TA_MAX_TENANT_DOC_BYTES") {
		t.Errorf("refusal is not the size gate: %v", err)
	}
	// No feature branch may exist: reaching `checkout -b` means the refusal came
	// from inside the lock, which is the case this change exists to prevent.
	if out := gitOut(t, work, "branch", "--list", "tenant-api/*"); out != "" {
		t.Errorf("a feature branch was cut before the refusal — the gate ran "+
			"inside the lock, not in the pre-flight: %q", out)
	}
}

// TestWritePRBatch_CapMeasuresTheMergedDocument pins the #1722 semantics the
// owner chose: the cap measures the document the write path PARSES, which on
// the merged paths is the whole merged file, not the patch that arrived.
//
// ⚠️ The accepted consequence is asserted here on purpose, not just tolerated:
// a small patch onto an already-large file is REFUSED. If that is ever changed
// to measure the request instead, this test must be the thing that says so.
func TestWritePRBatch_CapMeasuresTheMergedDocument(t *testing.T) {
	work := probeSizeRepo(t)
	w := NewWriter(work, work)

	// A tiny merge whose RESULT is over the cap.
	big := oversizeDoc(t, "db-a")
	ops := []PRBatchOp{{
		TenantID: "db-a",
		Merge:    func(existing []byte) (string, error) { return big, nil },
	}}
	_, err := w.WritePRBatch(context.Background(), ops, "p@p")
	if err == nil {
		t.Fatal("batch accepted an op whose merged document is over the cap")
	}
	if !errors.Is(err, ErrValidation) {
		t.Fatalf("want ErrValidation (→400), got %T %v", err, err)
	}
	if !strings.Contains(err.Error(), "TA_MAX_TENANT_DOC_BYTES") {
		t.Errorf("refusal is not the size gate: %v", err)
	}
}

// --- fixtures -----------------------------------------------------------------

// probeSizeRepo builds the bare-remote + working-clone pair the PR paths need,
// on the package's existing git helpers rather than a second set of its own.
func probeSizeRepo(t *testing.T) string {
	t.Helper()
	remote := initBareRemoteOnMain(t)

	seed := t.TempDir()
	gitClone(t, remote, seed)
	gitRun(t, seed, "config", "user.email", "p@p")
	gitRun(t, seed, "config", "user.name", "P")
	writeFileInDir(t, seed, "_defaults.yaml", "defaults:\n  container_cpu: 80\n")
	writeFileInDir(t, seed, "db-a.yaml", "tenants:\n  db-a:\n    container_cpu: 70\n")
	gitRun(t, seed, "add", "-A")
	gitRun(t, seed, "commit", "-m", "seed")
	gitRun(t, seed, "push", "origin", "main")

	work := t.TempDir()
	gitClone(t, remote, work)
	gitRun(t, work, "config", "user.email", "p@p")
	gitRun(t, work, "config", "user.name", "P")
	return work
}

// hasTopLevelTenants reports whether raw declares a column-0 `tenants:` key.
func hasTopLevelTenants(raw []byte) bool {
	for _, line := range bytes.Split(raw, []byte("\n")) {
		if bytes.HasPrefix(line, []byte("tenants:")) {
			return true
		}
	}
	return false
}
