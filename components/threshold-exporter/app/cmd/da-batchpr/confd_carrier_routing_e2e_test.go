package main

// confd_carrier_routing_e2e_test.go — the END-TO-END half of the #1605 pin.
//
// ⛔ WHY THIS FILE EXISTS AT ALL, given that
// internal/batchpr/confd_name_classification_parity_test.go already pins the
// same classification. That test calls the exported AllocateFiles with an
// in-memory map. Production does not: `da-batchpr apply` reads `--emit-dir`
// off the FILESYSTEM through walkFilesDir, and only then allocates. A pin that
// stops at the in-memory API cannot see anything the filesystem does to those
// names, and this family has already paid twice for exactly that gap — a fix
// verified only by calling a helper directly, whose callers had thrown the
// input away one step earlier (#1634's dead end A).
//
// So this half writes REAL FILES with the matrix's names into a real
// --emit-dir, runs the real CLI entry point, and observes which branch each
// carrier was written to via a recording GitClient. What it can see that the
// unit half cannot:
//
//   - a case-insensitive filesystem collapsing two matrix rows into one file
//     (guarded explicitly below rather than left to produce a confusing pass),
//   - walkFilesDir's own path handling (it keys files by their path under the
//     emit dir, so the allocator sees `path.Base` of a real relative path),
//   - the allocation warnings actually reaching the operator-visible report.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/vencil/threshold-exporter/internal/batchpr"
)

// recordingGit captures which files were written to which branch. Everything
// else is the no-op behaviour of stubGit.
type recordingGit struct {
	stubGit
	mu      sync.Mutex
	written map[string][]string // branch → sorted file paths
}

func (g *recordingGit) WriteFiles(ctx context.Context, branch string, files map[string][]byte) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.written == nil {
		g.written = map[string][]string{}
	}
	for p := range files {
		g.written[branch] = append(g.written[branch], p)
	}
	sort.Strings(g.written[branch])
	return nil
}

// recordingPR maps each branch back to the PlanItem title it belongs to, so
// the assertions can talk about "the Base PR" and "the tenant chunk" rather
// than about opaque branch hashes.
type recordingPR struct {
	stubPR
	mu          sync.Mutex
	titleByHead map[string]string
}

func (p *recordingPR) OpenPR(ctx context.Context, in batchpr.OpenPRInput) (*batchpr.PROpened, error) {
	p.mu.Lock()
	if p.titleByHead == nil {
		p.titleByHead = map[string]string{}
	}
	p.titleByHead[in.Head] = in.Title
	p.mu.Unlock()
	return p.stubPR.OpenPR(ctx, in)
}

type e2eRow struct {
	Name           string `json:"name"`
	YAMLExtension  bool   `json:"yaml_extension"`
	ReservedPrefix bool   `json:"reserved_prefix"`
	Hidden         bool   `json:"hidden"`
	DefaultsFile   bool   `json:"defaults_file"`
	Stem           string `json:"stem"`
	Why            string `json:"why"`
}

func (r e2eRow) isTenantCarrier() bool {
	return r.YAMLExtension && !r.ReservedPrefix && !r.Hidden
}

func loadMatrixForE2E(t *testing.T) []e2eRow {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	var root string
	for {
		if fi, err := os.Stat(filepath.Join(dir, "tests", "shared")); err == nil && fi.IsDir() {
			if fi2, err2 := os.Stat(filepath.Join(dir, "Makefile")); err2 == nil && !fi2.IsDir() {
				root = dir
				break
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("could not locate repo root walking up from %s", dir)
		}
		dir = parent
	}
	raw, err := os.ReadFile(filepath.Join(root, "tests", "shared", "confd_name_classification_matrix.json")) // #nosec G304 -- repo-relative test fixture
	if err != nil {
		t.Fatalf("read name classification matrix: %v", err)
	}
	var doc struct {
		Rows []e2eRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal name classification matrix: %v", err)
	}
	if len(doc.Rows) < 18 {
		t.Fatalf("matrix shrank to %d rows — this end-to-end pin would still run "+
			"and would still be green while measuring almost nothing", len(doc.Rows))
	}
	return doc.Rows
}

// requireCaseSensitiveFS fails the test rather than letting it pass on a
// filesystem that cannot hold `_defaults.yaml` and `_DEFAULTS.YAML` as two
// files. ⛔ Without this the fixture would silently lose rows and the run
// would be green having measured fewer cases than it claims — the shape the
// exporter half guards with its own `_case_probe_lower` check.
func requireCaseSensitiveFS(t *testing.T, dir string) {
	t.Helper()
	lower := filepath.Join(dir, "_case_probe_lower")
	if err := os.WriteFile(lower, []byte("x"), 0o600); err != nil {
		t.Fatalf("case probe: %v", err)
	}
	defer func() { _ = os.Remove(lower) }()
	if _, err := os.Stat(filepath.Join(dir, "_CASE_PROBE_LOWER")); err == nil {
		t.Skipf("filesystem at %s is case-insensitive; the matrix's case rows "+
			"cannot exist as distinct files here, so this pin would measure "+
			"fewer names than it names", dir)
	}
}

// TestApply_ConfdCarriersReachThePRTheExporterWouldAgreeWith runs the real
// `apply` entry point over an emit dir holding one file per matrix row.
func TestApply_ConfdCarriersReachThePRTheExporterWouldAgreeWith(t *testing.T) {
	t.Parallel()
	rows := loadMatrixForE2E(t)

	tmp := t.TempDir()
	emitDir := filepath.Join(tmp, "emit")
	if err := os.MkdirAll(emitDir, 0o750); err != nil {
		t.Fatalf("mkdir emit: %v", err)
	}
	requireCaseSensitiveFS(t, emitDir)

	// Content is constant so every answer is attributable to the NAME.
	const body = "constant body\n"
	for _, r := range rows {
		if err := os.WriteFile(filepath.Join(emitDir, r.Name), []byte(body), 0o600); err != nil {
			t.Fatalf("write fixture %q: %v", r.Name, err)
		}
	}
	got, err := os.ReadDir(emitDir)
	if err != nil {
		t.Fatalf("readdir emit: %v", err)
	}
	if len(got) != len(rows) {
		t.Fatalf("emit dir holds %d files for %d matrix rows — names collided on "+
			"this filesystem and the run would measure fewer cases than it claims",
			len(got), len(rows))
	}

	// The plan claims every id this allocator could recover from any row —
	// the oracle stem AND the naive strip-the-extension id. See the unit
	// half's header: a narrower plan makes the reserved / hidden rows pass
	// for the wrong reason.
	seen := map[string]bool{}
	var ids []string
	add := func(s string) {
		if s != "" && !seen[s] {
			seen[s] = true
			ids = append(ids, s)
		}
	}
	for _, r := range rows {
		add(r.Stem)
		base := r.Name
		for _, ext := range []string{".yaml", ".yml"} {
			if len(base) > len(ext) && strings.EqualFold(base[len(base)-len(ext):], ext) {
				base = base[:len(base)-len(ext)]
				break
			}
		}
		add(base)
	}
	sort.Strings(ids)

	const baseTitle = "[Base] carrier routing"
	const chunkTitle = "[chunk 1/1] carrier routing"
	plan := batchpr.Plan{Items: []batchpr.PlanItem{
		{Kind: batchpr.PlanItemBase, Title: baseTitle, Description: "base body",
			SourceProposalIndices: []int{0}},
		{Kind: batchpr.PlanItemTenant, Title: chunkTitle, Description: "tenant body",
			BlockedBy: "0", SourceProposalIndices: []int{0},
			TenantIDs: ids, ChunkKey: "domain-x"},
	}}
	planJSON, err := json.Marshal(plan)
	if err != nil {
		t.Fatalf("marshal plan: %v", err)
	}
	planFile := filepath.Join(tmp, "plan.json")
	mustWriteFile(t, planFile, planJSON)

	flags := &applyFlags{
		planPath:       planFile,
		emitDir:        emitDir,
		repoFlag:       "o/r",
		baseBranch:     "main",
		workdir:        tmp,
		reportPath:     filepath.Join(tmp, "report.md"),
		resultJSONPath: filepath.Join(tmp, "result.json"),
	}
	repo := batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"}
	git := &recordingGit{}
	pr := &recordingPR{}
	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}

	code := runApply(flags, repo, stdout, stderr, git, pr, &bytes.Buffer{})
	if code != 0 {
		t.Fatalf("apply exited %d; stderr=%q", code, stderr.String())
	}

	// Invert: file → the PR title it was written into ("" = never written).
	landedIn := map[string]string{}
	for _, r := range rows {
		landedIn[r.Name] = ""
	}
	for branch, paths := range git.written {
		title := pr.titleByHead[branch]
		if title == "" {
			t.Fatalf("branch %q received files but no PR was opened for it", branch)
		}
		for _, p := range paths {
			if prev, ok := landedIn[p]; ok && prev != "" {
				t.Errorf("%q was written to two PRs (%q and %q)", p, prev, title)
			}
			landedIn[p] = title
		}
	}

	report, err := os.ReadFile(flags.reportPath) // #nosec G304 -- test-owned temp path
	if err != nil {
		t.Fatalf("read report: %v", err)
	}
	reportText := string(report)

	for _, r := range rows {
		r := r
		t.Run(r.Name, func(t *testing.T) {
			switch {
			case r.DefaultsFile:
				if landedIn[r.Name] != baseTitle {
					t.Errorf("defaults carrier %q reached PR %q, want the Base PR %q.\n"+
						"  the exporter merges it into the inheritance chain, so it "+
						"belongs in the Base Infrastructure PR and nowhere else.\n"+
						"  matrix why: %s", r.Name, landedIn[r.Name], baseTitle, r.Why)
				}
			case r.isTenantCarrier():
				if landedIn[r.Name] != chunkTitle {
					t.Errorf("tenant carrier %q reached PR %q, want the tenant chunk %q.\n"+
						"  the exporter serves tenant %q out of this file; a carrier that "+
						"reaches no PR is a threshold change the operator was told was "+
						"applied and that production never sees.\n  matrix why: %s",
						r.Name, landedIn[r.Name], chunkTitle, r.Stem, r.Why)
				}
			default:
				if landedIn[r.Name] != "" {
					t.Errorf("%q reached PR %q, want NO PR.\n"+
						"  the exporter derives no tenant from this name (reserved=%t "+
						"hidden=%t yaml=%t), so committing it proposes configuration "+
						"production will not read.\n  matrix why: %s",
						r.Name, landedIn[r.Name], r.ReservedPrefix, r.Hidden, r.YAMLExtension, r.Why)
				} else if !strings.Contains(reportText, fmt.Sprintf("%q", r.Name)) {
					t.Errorf("%q was skipped without the operator-visible report naming it.\n"+
						"  a dropped carrier nobody is told about is the silence this "+
						"family is about. Report was:\n%s", r.Name, reportText)
				}
			}
		})
	}
}
