package confd

import (
	"errors"
	"io/fs"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

// wantProblem is the classification each filesystem state must get (#1680).
// ok=false means the entry is not a tenant file at all (ListTenantFiles skips
// it). invalid-config is USABLE here on purpose: confd judges YAML syntax
// only, never the threshold-exporter schema — the list handler decides that.
var wantProblem = map[testutil.ConfdFileState]struct {
	listed  bool
	problem FileProblem
}{
	testutil.ConfdGood:            {true, ProblemNone},
	testutil.ConfdMalformedYAML:   {true, ProblemMalformedYAML},
	testutil.ConfdInvalidConfig:   {true, ProblemNone},
	testutil.ConfdEmptyFile:       {true, ProblemNone},
	testutil.ConfdDanglingSymlink: {true, ProblemUnreadable},
	testutil.ConfdSymlinkToDir:    {true, ProblemNotRegularFile},
	testutil.ConfdRealDir:         {false, ProblemNone},
	testutil.ConfdUnreadable:      {true, ProblemUnreadable},
}

func TestListAndReadTenantFiles_StateMatrix(t *testing.T) {
	t.Parallel()
	for _, state := range testutil.ConfdFileStates {
		t.Run(string(state), func(t *testing.T) {
			t.Parallel()
			want, ok := wantProblem[state]
			if !ok {
				t.Fatalf("no expectation for state %q — add one", state)
			}
			dir := t.TempDir()
			if !testutil.BuildConfdEntry(t, dir, "acme", state) {
				t.Skipf("state %q cannot be built in this process (euid 0 ignores permission bits)", state)
			}
			// A healthy sibling proves the listing itself did not fail.
			testutil.BuildConfdEntry(t, dir, "zeta", testutil.ConfdGood)

			files, err := ListTenantFiles(dir)
			if err != nil {
				t.Fatalf("ListTenantFiles: %v", err)
			}
			wantFiles := []TenantFile{{ID: "zeta", Name: "zeta.yaml"}}
			if want.listed {
				wantFiles = append([]TenantFile{{ID: "acme", Name: "acme.yaml"}}, wantFiles...)
			}
			if !reflect.DeepEqual(files, wantFiles) {
				t.Fatalf("ListTenantFiles = %+v, want %+v", files, wantFiles)
			}
			if !want.listed {
				return
			}

			data, problem := ReadTenantFile(dir, "acme.yaml")
			if problem != want.problem {
				t.Errorf("ReadTenantFile problem = %q, want %q", problem, want.problem)
			}
			if problem == ProblemNone && data == nil && state != testutil.ConfdEmptyFile {
				t.Errorf("usable file returned nil data")
			}
			if problem != ProblemNone && data != nil {
				t.Errorf("unusable file returned data %q; want nil", data)
			}
		})
	}
}

// ListTenantFiles keeps every caller's pre-helper shape: reserved and non-YAML
// names are skipped, ReadDir order is kept, and duplicate stems are NOT folded
// (each caller owns its stance on that).
func TestListTenantFiles_NamesOrderAndDuplicates(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	for _, n := range []string{"b.yml", "a.yaml", "a.yml", "_defaults.yaml", ".hidden.yaml", "README.md", "Upper.YAML"} {
		testutil.WriteYAML(t, dir, n, "")
	}
	got, err := ListTenantFiles(dir)
	if err != nil {
		t.Fatalf("ListTenantFiles: %v", err)
	}
	want := []TenantFile{
		{ID: "Upper", Name: "Upper.YAML"},
		{ID: "a", Name: "a.yaml"},
		{ID: "a", Name: "a.yml"},
		{ID: "b", Name: "b.yml"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("ListTenantFiles = %+v, want %+v", got, want)
	}
}

func TestListTenantFiles_MissingDirIsNotExist(t *testing.T) {
	t.Parallel()
	_, err := ListTenantFiles(filepath.Join(t.TempDir(), "absent"))
	if !errors.Is(err, fs.ErrNotExist) {
		t.Errorf("err = %v, want fs.ErrNotExist so callers can keep their missing-dir handling", err)
	}
}

// ReadTenantFile uses only the base name, so a caller cannot reach outside dir.
func TestReadTenantFile_OnlyBaseName(t *testing.T) {
	t.Parallel()
	parent := t.TempDir()
	dir := filepath.Join(parent, "conf.d")
	testutil.WriteFile(t, filepath.Join(parent, "outside.yaml"), "tenants: {}\n")
	testutil.WriteFile(t, filepath.Join(dir, "inside.yaml"), "tenants: {}\n")
	if _, problem := ReadTenantFile(dir, "../outside.yaml"); problem != ProblemUnreadable {
		t.Errorf("../outside.yaml problem = %q, want %q (base name only → absent in dir)", problem, ProblemUnreadable)
	}
	if _, problem := ReadTenantFile(dir, "inside.yaml"); problem != ProblemNone {
		t.Errorf("inside.yaml problem = %q, want usable", problem)
	}
}

// The reason strings are an API contract (TenantSummary.config_error).
func TestFileProblem_StableValues(t *testing.T) {
	t.Parallel()
	got := []FileProblem{ProblemNone, ProblemUnreadable, ProblemNotRegularFile, ProblemMalformedYAML}
	want := []FileProblem{"", "unreadable", "not_regular_file", "malformed_yaml"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("FileProblem values = %q, want %q", got, want)
	}
}
