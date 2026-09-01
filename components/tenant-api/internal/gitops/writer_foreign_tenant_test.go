package gitops

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

const (
	// ownOnly and smuggled share a URL id of db-a; smuggled also declares other.
	ownOnly  = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	smuggled = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  other:\n    _silent_mode: \"warning\"\n"
	// grandfathered is what an operator-authored flat file looks like on disk.
	grandfathered = "tenants:\n  db-a:\n    _silent_mode: \"false\"\n  other:\n    _silent_mode: \"false\"\n"
	// plusThird edits a grandfathered file AND adds a section it did not have.
	plusThird = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  other:\n    _silent_mode: \"warning\"\n  third:\n    _silent_mode: \"warning\"\n"
)

func seedBase(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestWriteRefusesTenantSectionsItWouldAdd drives the real Write, not validate:
// the gate has to hold at the call site that reaches disk.
func TestWriteRefusesTenantSectionsItWouldAdd(t *testing.T) {
	for _, tc := range []struct {
		name string
		seed func(t *testing.T, dir string)
		body string
		// wantNamed is the section the error must name, so a caller can fix it.
		wantNamed string
	}{
		// The exporter's duplicate-tenant guard only fires when the smuggled id
		// also owns a file, so the arm WITHOUT one — a "ghost" tenant the
		// exporter would serve and GET /tenants could not see — is the arm with
		// no downstream backstop at all.
		{"smuggled id owns a file", func(t *testing.T, dir string) {
			seedBase(t, dir, "other.yaml", "tenants:\n  other:\n    _silent_mode: \"false\"\n")
		}, smuggled, "other"},
		{"smuggled id owns no file", nil, smuggled, "other"},
		{"base file exists but declares only the id itself", func(t *testing.T, dir string) {
			seedBase(t, dir, "db-a.yaml", ownOnly)
		}, smuggled, "other"},
		{"grandfathered file, but the write adds a further section", func(t *testing.T, dir string) {
			seedBase(t, dir, "db-a.yaml", grandfathered)
		}, plusThird, "third"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := initRepoOnMain(t)
			if tc.seed != nil {
				tc.seed(t, dir)
			}
			before, _ := os.ReadFile(filepath.Join(dir, "db-a.yaml"))

			_, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", tc.body)
			if err == nil {
				t.Fatal("Write accepted a body adding a tenant section it does not address")
			}
			if !errors.Is(err, ErrValidation) {
				t.Errorf("want ErrValidation, got %v", err)
			}
			if !strings.Contains(err.Error(), tc.wantNamed) {
				t.Errorf("error must name %q so the caller can fix it, got %v", tc.wantNamed, err)
			}
			after, _ := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
			if string(before) != string(after) {
				t.Errorf("rejected write still changed disk:\nbefore=%q\nafter=%q", before, after)
			}
		})
	}
}

// The delta half of the rule: a flat file an operator already wrote stays
// editable. Absolute rejection would turn a configuration the exporter
// supports into a permanent write failure for that tenant.
func TestWriteStillAcceptsAGrandfatheredSection(t *testing.T) {
	dir := initRepoOnMain(t)
	seedBase(t, dir, "db-a.yaml", grandfathered)
	if _, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", smuggled); err != nil {
		t.Fatalf("gate rejected an edit to a section the file already declared: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), "warning") {
		t.Errorf("accepted write did not reach disk: %q", raw)
	}
}

func TestWriteStillAcceptsABodyThatOnlyDeclaresItsOwnID(t *testing.T) {
	dir := initRepoOnMain(t)
	if _, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", ownOnly); err != nil {
		t.Fatalf("gate rejected a legitimate single-tenant write: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "db-a.yaml")); err != nil {
		t.Errorf("accepted write did not reach disk: %v", err)
	}
}

// WritePR is a second, independent caller of validate (writer_pr.go), so the
// gate is asserted there rather than assumed to be shared.
func TestWritePRRefusesTenantSectionsItWouldAdd(t *testing.T) {
	dir := initRepoOnMain(t)
	if _, err := newW(dir).WritePR(context.Background(), "db-a", "a@example.com", smuggled); err == nil {
		t.Fatal("WritePR accepted a smuggled tenant section")
	} else if !errors.Is(err, ErrValidation) {
		t.Errorf("want ErrValidation, got %v", err)
	}
}

func TestAddedTenantKeys(t *testing.T) {
	for _, tc := range []struct {
		name string
		base string // "" = do not create a base file
		body string
		want []string
	}{
		{"own id only", "", ownOnly, nil},
		{"no baseline → foreign counts as added", "", smuggled, []string{"other"}},
		{"baseline declares it → grandfathered", grandfathered, smuggled, nil},
		{"baseline declares it, body adds one more", grandfathered, plusThird, []string{"third"}},
		{"baseline declares only the id itself", ownOnly, smuggled, []string{"other"}},
		{"unparseable baseline fails closed", "{{not yaml", smuggled, []string{"other"}},
		{"sorted", "", "tenants:\n  zz: {}\n  db-a: {}\n  aa: {}\n", []string{"aa", "zz"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			if tc.base != "" {
				seedBase(t, dir, "db-a.yaml", tc.base)
			}
			var tcfg cfg.ThresholdConfig
			if err := yaml.Unmarshal([]byte(tc.body), &tcfg); err != nil {
				t.Fatal(err)
			}
			got := addedTenantKeys(dir, tcfg, "db-a")
			if len(got) != len(tc.want) {
				t.Fatalf("got %v want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("got %v want %v", got, tc.want)
				}
			}
		})
	}
}

// TestAddedTenantKeysFailsClosedWithoutAConfigDir pins the unit-test shape
// (configDir "") separately: it has no file to read, and must not be the one
// path where the gate silently stops applying.
func TestAddedTenantKeysFailsClosedWithoutAConfigDir(t *testing.T) {
	var tcfg cfg.ThresholdConfig
	if err := yaml.Unmarshal([]byte(smuggled), &tcfg); err != nil {
		t.Fatal(err)
	}
	if got := addedTenantKeys("", tcfg, "db-a"); len(got) != 1 || got[0] != "other" {
		t.Errorf("configDir \"\" must yield an empty baseline, got %v", got)
	}
}

// TestAnAcceptedWriteNeverWidensTheContentPlane pins WHY the gate exists rather
// than what it does. tenant-api derives tenant ids from the FILENAME, the
// exporter from the body's `tenants:` KEYS. The invariant B buys is not that
// the two sets are equal — a grandfathered file breaks that — but that a write
// can never make the content plane wider than it already was.
func TestAnAcceptedWriteNeverWidensTheContentPlane(t *testing.T) {
	for _, base := range []string{"", ownOnly, grandfathered} {
		dir := initRepoOnMain(t)
		if base != "" {
			seedBase(t, dir, "db-a.yaml", base)
		}
		before := tenantKeysOnDisk(t, dir)

		for _, body := range []string{ownOnly, smuggled, plusThird} {
			if _, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", body); err != nil {
				continue // rejected: it never reaches disk, so it cannot widen anything
			}
			after := tenantKeysOnDisk(t, dir)
			for _, id := range after {
				if id != "db-a" && !contains(before, id) {
					t.Errorf("accepted write widened the content plane: base=%q before=%v after=%v",
						base, before, after)
				}
			}
		}
	}
}

func tenantKeysOnDisk(t *testing.T, dir string) []string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if err != nil {
		return nil
	}
	var tcfg cfg.ThresholdConfig
	if err := yaml.Unmarshal(raw, &tcfg); err != nil {
		return nil
	}
	out := make([]string, 0, len(tcfg.Tenants))
	for id := range tcfg.Tenants {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

func contains(hay []string, needle string) bool {
	for _, h := range hay {
		if h == needle {
			return true
		}
	}
	return false
}

func newW(dir string) *Writer { return NewWriter(dir, dir) }
