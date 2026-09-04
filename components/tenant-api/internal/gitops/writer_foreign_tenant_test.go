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
	if errs, _ := validate("", "db-a", "", smuggled); len(errs) != 1 ||
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
//
// The `unparseable` column is the one that fails CLOSED, and the rows that
// exercise it are the hole this table used to have: it pinned `...` with
// NOTHING after it (legitimate, must stay legal) and never asked what happens
// when content follows it. `...` ends a document without starting one, so a
// bare block after it is a parse error rather than a second document — which
// the old single-int signature reported as "nothing after the first document".
func TestExtraDocumentsWithContent(t *testing.T) {
	for _, tc := range []struct {
		name            string
		body            string
		wantExtra       int
		wantUnparseable bool
	}{
		{"single document", ownOnly, 0, false},
		{"leading marker", "---\n" + ownOnly, 0, false},
		{"trailing marker, no content", ownOnly + "---\n", 0, false},
		{"trailing end-of-document marker", ownOnly + "...\n", 0, false},
		{"trailing comment-only document", ownOnly + "---\n# nothing here\n", 0, false},
		{"end-of-document marker then a comment", ownOnly + "...\n# nothing here\n", 0, false},
		{"second document with a tenant", smuggledDoc2, 1, false},
		{"second document with a root key", rootKeyDoc2, 1, false},
		{"two extra documents", smuggledDoc2 + "---\ndefaults:\n  cpu_critical: 1\n", 2, false},
		// ⛔ The fail-open rows. The last four are the ones the FIRST version of
		// this fix still let through: it kept "a failure on document 0 is the
		// caller's Unmarshal to report", and Unmarshal into ThresholdConfig
		// never decodes the subtree of a root key the struct does not know — so
		// a bad tag parked there is invisible to it and fatal here.
		{"unparseable from the very first byte", "{{not yaml", 0, true},
		{"end-of-document marker then a tenants block", ownOnly + "...\ntenants:\n  other:\n    _silent_mode: \"warning\"\n", 0, true},
		{"end-of-document marker then a root key", ownOnly + "...\ndefaults:\n  cpu_critical: 1\n", 0, true},
		{"end-of-document marker then a proper second document", ownOnly + "...\n---\ntenants:\n  other:\n    _silent_mode: \"warning\"\n", 1, false},
		{"bad tag under an unknown root key, then a second document", ownOnly + "zzz: !!int \"abc\"\n---\ntenants:\n  other:\n    _silent_mode: \"warning\"\n", 0, true},
		{"bad binary tag under an unknown root key", ownOnly + "zzz: !!binary \"@@@\"\n---\ntenants:\n  other:\n    _silent_mode: \"warning\"\n", 0, true},
		{"duplicate key under an unknown root key", ownOnly + "zzz:\n  k: 1\n  k: 2\n---\ntenants:\n  other:\n    _silent_mode: \"warning\"\n", 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			extra, unparseable := extraDocumentsWithContent(tc.body)
			if extra != tc.wantExtra || unparseable != tc.wantUnparseable {
				t.Errorf("got (extra=%d, unparseable=%v) want (extra=%d, unparseable=%v)",
					extra, unparseable, tc.wantExtra, tc.wantUnparseable)
			}
		})
	}
}

// TestWritePRRefusesATrailerItCannotParse is the end-to-end half: the unit
// table above would still pass if validateStateless ignored the new second
// return value, and the body is committed VERBATIM, so the property that
// matters is that the foreign section never reaches origin.
func TestWritePRRefusesATrailerItCannotParse(t *testing.T) {
	second := "---\ntenants:\n  other:\n    _silent_mode: \"critical\"\n"
	for _, tc := range []struct{ name, body string }{
		// A document-END marker ends document 1 without starting document 2, so
		// the block after it is a parse error rather than a second document.
		{"end-of-document marker then a bare block",
			ownOnly + "...\ntenants:\n  other:\n    _silent_mode: \"critical\"\n"},
		// ⛔ These decode CLEAN into cfg.ThresholdConfig — struct decoding never
		// walks the subtree of a root key the struct does not declare, so the bad
		// tag is invisible to the caller's Unmarshal and only the generic decoder
		// ever sees it. The body is committed verbatim, so "only the generic
		// decoder sees it" is the whole vulnerability.
		{"bad int tag under an unknown root key", ownOnly + "zzz: !!int \"abc\"\n" + second},
		{"bad binary tag under an unknown root key", ownOnly + "zzz: !!binary \"@@@\"\n" + second},
		{"duplicate key under an unknown root key", ownOnly + "zzz:\n  k: 1\n  k: 2\n" + second},
		{"unknown root key beside a platform-scoped one",
			ownOnly + "zzz: !!int \"abc\"\ndefaults:\n  cpu_critical: 1\n" + second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := initRepoOnMain(t)
			_, err := newW(dir).WritePR(context.Background(), "db-a", "a@example.com", tc.body)
			if !errors.Is(err, ErrValidation) {
				t.Fatalf("WritePR err = %v, want ErrValidation — the body carried an "+
					"unvalidated tenants block into the commit", err)
			}
		})
	}
}

// TestCheckTenantRootKeysAgreesWithTheTrailerGate records WHY the gate above has
// to be the one that fails closed. CheckTenantRootKeys — the #705 root-key gate —
// decodes into map[string]any and returns NO violations when that decode fails,
// so on these bodies it reports a clean root map while `zzz:` (and a smuggled
// `defaults:`) sit right there. Nothing here asks CheckTenantRootKeys to change;
// this pins that the trailer gate covers what it misses, so a future edit that
// relaxes the trailer gate has to confront this.
func TestCheckTenantRootKeysAgreesWithTheTrailerGate(t *testing.T) {
	body := ownOnly + "zzz: !!int \"abc\"\ndefaults:\n  cpu_critical: 1\n"
	if got := cfg.CheckTenantRootKeys([]byte(body)); len(got) != 0 {
		t.Logf("CheckTenantRootKeys now reports %v — it no longer fails open here; "+
			"the trailer gate below is then belt-and-braces rather than the only cover", got)
	}
	if _, unparseable := extraDocumentsWithContent(body); !unparseable {
		t.Fatal("the trailer gate must refuse a body whose generic decode fails, " +
			"because the root-key gate answers 'no violations' on exactly these")
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
		errs, _ := validate(dir, id, filepath.Join(dir, id+".yaml"), body)
		if len(errs) == 0 {
			t.Errorf("validate accepted id %q", id)
			continue
		}
		if !strings.Contains(errs[0], "reserved tenant id") {
			t.Errorf("id %q rejected for the wrong reason: %v", id, errs)
		}
	}
	if errs, _ := validate(dir, "", filepath.Join(dir, ".yaml"), ownOnly); len(errs) == 0 {
		t.Error("validate accepted an empty id")
	}
	if errs, _ := validate(dir, "db-a", filepath.Join(dir, "db-a.yaml"), ownOnly); len(errs) != 0 {
		t.Errorf("containment check rejected a legitimate id: %v", errs)
	}
}

// TestGrandfatheredSectionSurvivesAYmlSpelling is the #1673 × #1681 seam: the
// added-section baseline must be read from the file the write will actually
// land on, not from a synthesised `<id>.yaml`. A tenant whose file is spelled
// `.yml` would otherwise get an EMPTY baseline, and since the baseline fails
// closed that turns every edit of a flat file it legitimately shares into a
// refusal — the gate would lock the tenant out of its own config.
func TestGrandfatheredSectionSurvivesAYmlSpelling(t *testing.T) {
	for _, spelling := range []string{"db-a.yaml", "db-a.yml"} {
		t.Run(spelling, func(t *testing.T) {
			dir := initRepoOnMain(t)
			seedBase(t, dir, spelling, grandfathered)

			if _, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", smuggled); err != nil {
				t.Fatalf("gate refused an edit to a section %s already declared: %v", spelling, err)
			}
			raw, err := os.ReadFile(filepath.Join(dir, spelling))
			if err != nil {
				t.Fatalf("write did not land on %s: %v", spelling, err)
			}
			if !strings.Contains(string(raw), "warning") {
				t.Errorf("%s did not receive the write: %q", spelling, raw)
			}
		})
	}
}

// An empty configDir must not open the added-section gate. The path resolver
// still returns a NON-EMPTY relative name ("db-a.yaml") in that case, so an
// unconditional baseline read would resolve it against the process CWD and
// could grandfather foreign sections open. main.go:349 feeds configDir straight
// from `--config-dir`, which has no emptiness guard, so this pairing is
// reachable by misconfiguration rather than test-only.
//
// Counterfactual (measured, not asserted from reading): deleting the
// `if configDir != ""` guard in validate() flips this test from reject to
// accept.
func TestValidateFailsClosedWhenConfigDirIsEmpty(t *testing.T) {
	dir := t.TempDir()
	// A real file that DOES grandfather `other` — so the only thing that can
	// keep the gate shut is refusing to read a baseline at all.
	path := filepath.Join(dir, "db-a.yaml")
	if err := os.WriteFile(path, []byte("tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  other:\n    _silent_mode: \"warning\"\n"), 0644); err != nil {
		t.Fatal(err)
	}
	body := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  other:\n    _silent_mode: \"all\"\n"

	// Control: with a real configDir the baseline IS read and `other` is
	// legitimately grandfathered, so this same body is accepted. Without this
	// arm the test could pass because the body is bad rather than because the
	// guard held.
	if errs, _ := validate(dir, "db-a", path, body); len(errs) != 0 {
		t.Fatalf("control arm: grandfathered body should be accepted with a real configDir, got %v", errs)
	}

	errs, _ := validate("", "db-a", path, body)
	if len(errs) == 0 {
		t.Fatal("configDir=\"\" accepted a body adding a foreign tenant section — the baseline read was not skipped, so the gate failed OPEN")
	}
	if !strings.Contains(errs[0], "adds tenant section") {
		t.Fatalf("rejected for the wrong reason: %v", errs)
	}
}
