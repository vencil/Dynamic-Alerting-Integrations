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
		// A body with no foreign key has the same answer whatever the base
		// says, so the baseline must not be consulted at all — pinned here as
		// behavior (an unparseable base cannot change the answer) rather than
		// as a timing assertion.
		{"own id only, unparseable base", "{{not yaml", ownOnly, nil},
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
			baseRaw, _ := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
			got := addedTenantKeys(baseRaw, tcfg, "db-a")
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

// TestAddedTenantKeysFailsClosedWithoutABaseFile pins the shapes that produce
// no baseline bytes — no configDir (the unit-test shape) and an unreadable or
// missing file both arrive as nil. Neither may be the one path where the gate
// silently stops applying.
func TestAddedTenantKeysFailsClosedWithoutABaseFile(t *testing.T) {
	var tcfg cfg.ThresholdConfig
	if err := yaml.Unmarshal([]byte(smuggled), &tcfg); err != nil {
		t.Fatal(err)
	}
	for _, baseRaw := range [][]byte{nil, {}} {
		if got := addedTenantKeys(baseRaw, tcfg, "db-a"); len(got) != 1 || got[0] != "other" {
			t.Errorf("baseRaw %v must yield an empty baseline, got %v", baseRaw, got)
		}
	}
	// validate() is the caller that turns "no configDir" into nil baseRaw, so
	// assert that end of the wiring too rather than assuming it.
	if errs, _ := validate("", "db-a", smuggled); len(errs) != 1 ||
		!strings.Contains(errs[0], "adds tenant section") {
		t.Errorf("validate with no configDir must still refuse an added section, got %v", errs)
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

// --- multi-document bodies (#1681, found blind-reviewing the gate above) ---

const (
	// smuggledDoc2 hides the section in a SECOND document: yaml.Unmarshal reads
	// only the first and reports no error, while the write path commits the
	// body verbatim.
	smuggledDoc2 = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n---\ntenants:\n  other:\n    _silent_mode: \"true\"\n"
	// rootKeyDoc2 is the same trick carrying a ROOT key, which the added-keys
	// gate cannot see even if it unioned every document's tenant keys.
	rootKeyDoc2 = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n---\ndefaults:\n  cpu_critical: 1\n"
)

func TestWriteRefusesContentAfterTheFirstDocument(t *testing.T) {
	for _, tc := range []struct{ name, body string }{
		{"second document declares a tenant", smuggledDoc2},
		{"second document declares a root key", rootKeyDoc2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := initRepoOnMain(t)
			_, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", tc.body)
			if err == nil {
				t.Fatal("Write committed a document nothing validated")
			}
			if !errors.Is(err, ErrValidation) {
				t.Errorf("want ErrValidation, got %v", err)
			}
			if _, serr := os.Stat(filepath.Join(dir, "db-a.yaml")); !os.IsNotExist(serr) {
				raw, _ := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
				t.Errorf("rejected write still reached disk: %q", raw)
			}
		})
	}
}

func TestWritePRRefusesContentAfterTheFirstDocument(t *testing.T) {
	dir := initRepoOnMain(t)
	if _, err := newW(dir).WritePR(context.Background(), "db-a", "a@example.com", smuggledDoc2); err == nil {
		t.Fatal("WritePR committed a document nothing validated")
	} else if !errors.Is(err, ErrValidation) {
		t.Errorf("want ErrValidation, got %v", err)
	}
}

// An empty trailer is not a smuggling channel, and refusing it would reject
// bodies a YAML emitter may legitimately produce.
func TestExtraDocumentsWithContent(t *testing.T) {
	for _, tc := range []struct {
		name string
		body string
		want int
	}{
		{"single document", ownOnly, 0},
		{"leading marker", "---\n" + ownOnly, 0},
		{"trailing marker, no content", ownOnly + "---\n", 0},
		{"trailing end-of-document marker", ownOnly + "...\n", 0},
		{"trailing comment-only document", ownOnly + "---\n# nothing here\n", 0},
		{"second document with a tenant", smuggledDoc2, 1},
		{"second document with a root key", rootKeyDoc2, 1},
		{"two extra documents", smuggledDoc2 + "---\ndefaults:\n  cpu_critical: 1\n", 2},
		{"invalid YAML is the earlier check's business", "{{not yaml", 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := extraDocumentsWithContent(tc.body); got != tc.want {
				t.Errorf("got %d want %d", got, tc.want)
			}
		})
	}
}

func TestWriteStillAcceptsAnEmptyTrailingDocument(t *testing.T) {
	dir := initRepoOnMain(t)
	if _, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", ownOnly+"---\n"); err != nil {
		t.Fatalf("gate rejected an empty trailing document: %v", err)
	}
}

// TestValidateRefusesAnIDThatWouldLeaveConfigDir pins the containment check in
// the same function as the path it protects: validate joins tenantID into a
// path, and is reachable from callers that have not run the id past the
// handler's own validator (#1681, CodeQL "uncontrolled data in path
// expression"). IsTenantConfigFile alone says yes to every id below.
func TestValidateRefusesAnIDThatWouldLeaveConfigDir(t *testing.T) {
	dir := initRepoOnMain(t)
	for _, id := range []string{"a/b", `a\b`, "a/../../b", "/abs", "..", "../x"} {
		// ⛔ The body must DECLARE this id. With any other body the earlier
		// "must contain tenants.<id>" check rejects it and the test passes even
		// with the containment guard deleted — measured: the first version of
		// this test survived both mutants below.
		body := "tenants:\n  " + id + ":\n    _silent_mode: \"warning\"\n"
		errs, _ := validate(dir, id, body)
		if len(errs) == 0 {
			t.Errorf("validate accepted id %q", id)
			continue
		}
		if !strings.Contains(errs[0], "reserved tenant id") {
			t.Errorf("id %q rejected for the wrong reason: %v", id, errs)
		}
	}
	if errs, _ := validate(dir, "", ownOnly); len(errs) == 0 {
		t.Error("validate accepted an empty id")
	}
	if errs, _ := validate(dir, "db-a", ownOnly); len(errs) != 0 {
		t.Errorf("containment check rejected a legitimate id: %v", errs)
	}
}
