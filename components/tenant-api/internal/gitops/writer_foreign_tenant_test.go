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
	"github.com/vencil/threshold-exporter/pkg/parsetree"
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

// msgOwnOnly is the part of the #3064 body refusal a test looks for.
const msgOwnOnly = "tenants.db-a and no other tenant"

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
		// wantNamed is what the error must say, so a caller can fix it. Since
		// #3064 every row is the body-shape refusal: a body declares its own
		// tenant and no other.
		wantNamed string
	}{
		// The exporter's duplicate-tenant guard only fires when the smuggled id
		// also owns a file, so the arm WITHOUT one — a "ghost" tenant the
		// exporter would serve and GET /tenants could not see — is the arm with
		// no downstream backstop at all.
		{"smuggled id owns a file", func(t *testing.T, dir string) {
			seedBase(t, dir, "other.yaml", "tenants:\n  other:\n    _silent_mode: \"false\"\n")
		}, smuggled, msgOwnOnly},
		{"smuggled id owns no file", nil, smuggled, msgOwnOnly},
		{"base file exists but declares only the id itself", func(t *testing.T, dir string) {
			seedBase(t, dir, "db-a.yaml", ownOnly)
		}, smuggled, msgOwnOnly},
		{"grandfathered file, but the write adds a further section", func(t *testing.T, dir string) {
			seedBase(t, dir, "db-a.yaml", grandfathered)
		}, plusThird, msgOwnOnly},
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

// #3064: a flat file an operator wrote — db-a.yaml also declaring `other` —
// is git-managed. Until #3064 it stayed editable through the API (the delta
// rule grandfathered its sections); now no write reaches it, not even one
// whose body declares db-a alone, so `other` cannot be overwritten.
func TestWriteRefusesAGrandfatheredSharedFile(t *testing.T) {
	dir := initRepoOnMain(t)
	// Committed (seedCommitted): a direct write refuses a tenant file that
	// differs from HEAD (ErrTenantFileDirty, #1535 item 6) first.
	seedCommitted(t, dir, "db-a.yaml", grandfathered)
	_, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", ownOnly)
	if !errors.Is(err, ErrTenantGitManaged) {
		t.Fatalf("err = %v, want the git-managed refusal", err)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if err != nil || string(raw) != grandfathered {
		t.Errorf("db-a.yaml = %q (%v), want unchanged", raw, err)
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

// validate() with no configDir reads no current file; since #3064 the
// body-shape check refuses a body declaring another tenant before anything
// is read, so that path cannot be where the refusal stops applying.
func TestValidateWithoutConfigDirRefusesAForeignSection(t *testing.T) {
	if errs, _ := validate("", "db-a", "", smuggled); len(errs) != 1 ||
		!strings.Contains(errs[0], msgOwnOnly) {
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

func newW(dir string) *Writer { return newTestWriter(dir, dir) }

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

// TestValidateShape_ParseTreeVerdict: the body gate is parse-tree's verdict
// (ADR-036 §7 step 7, #3206), not a shape rule of tenant-api's own. want is
// the parse-tree kind the refusal must name ("" = accepted; "!" = refused by
// a later check of validateShape, not by parse-tree). The rows that the
// removed tenant-api gate let through and parse-tree refuses are marked
// CHANGED.
func TestValidateShape_ParseTreeVerdict(t *testing.T) {
	for _, tc := range []struct {
		name string
		body string
		want string
	}{
		{"single document", ownOnly, ""},
		{"leading marker", "---\n" + ownOnly, ""},
		{"trailing end-of-document marker", ownOnly + "...\n", ""},
		{"merge key inside a tenant section", "tenants:\n  db-a:\n    <<: {_silent_mode: \"warning\"}\n", ""},
		// CHANGED: an empty trailing document used to be legal here, while
		// parse-tree (and so the exporter since step 6) refuses the file.
		{"trailing marker, no content", ownOnly + "---\n", "multiple_documents"},
		{"trailing comment-only document", ownOnly + "---\n# nothing here\n", "multiple_documents"},
		{"trailing null document", ownOnly + "---\n~\n", "multiple_documents"},
		{"second document with a tenant", smuggledDoc2, "multiple_documents"},
		{"second document with a root key", rootKeyDoc2, "multiple_documents"},
		{"invalid YAML in the first document", "{{not yaml", "syntax_error"},
		{"invalid YAML after an end-of-document marker",
			ownOnly + "...\ntenants:\n  other:\n    _silent_mode: \"true\"\n", "syntax_error"},
		{"bad tag under an unknown root key", ownOnly + "zzz: !!int \"abc\"\n", "decode_error"},
		{"null root key", ownOnly + "~: {tenants: {other: {}}}\n", "null_key"},
		{"complex root key", ownOnly + "? [a, b]\n: {tenants: {other: {}}}\n", "non_scalar_key"},
		{"unconstructable !!int value in the own section",
			"tenants:\n  db-a:\n    _silent_mode: !!int \"abc\"\n", "decode_error"},
		{"unconstructable !!bool value in the own section",
			"tenants:\n  db-a:\n    _silent_mode: !!bool \"warning\"\n", "decode_error"},
		// CHANGED: tags the struct and generic decodes read as strings.
		{"!!binary value", "tenants:\n  db-a:\n    _silent_mode: !!binary d2FybmluZw==\n", "tag_not_allowed"},
		{"!!set value", "tenants:\n  db-a:\n    _metadata: !!set {a: null}\n", "tag_not_allowed"},
		{"local tag", "tenants:\n  db-a:\n    _silent_mode: !foo warning\n", "tag_not_allowed"},
		{"non-specific tag", "tenants:\n  db-a:\n    _silent_mode: ! \"warning\"\n", "non_specific_tag"},
		// ADR-036 step 8: with the vendored yaml.v3 patch gone, `! "<<"` is a
		// literal key to every decode below (TenantWritesStringMetadata's merge
		// walk included); the verdict refuses it before any of them runs.
		{"non-specific merge key bringing a string _metadata",
			"tenants:\n  db-a:\n    ! \"<<\": {_metadata: \"owner: x\"}\n", "non_specific_tag"},
		{"non-specific tag on an SLO objective",
			"tenants:\n  db-a:\n    _custom_alerts:\n    - name: x\n      slo: {objective: ! \"99.9\"}\n", "non_specific_tag"},
		{"byte order mark past the start", "tenants:\n  db-a:\n    _silent_mode: \"warn\ufeffing\"\n", "bom_not_at_start"},
		{"repeated key", "tenants:\n  db-a:\n    _silent_mode: warning\n    _silent_mode: critical\n", "duplicate_key"},
		{"top level not a mapping", "- tenants\n", "top_level_not_mapping"},
		// Read by parse-tree; refused by the checks after it.
		{"root merge key", "<<: {tenants: {other: {}}}\n" + ownOnly, "!"},
		{"integer root key", ownOnly + "1: {tenants: {other: {}}}\n", "!"},
		{"unknown string root key", ownOnly + "defaults: {}\n", "!"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, errs := validateShape("db-a", tc.body)
			switch tc.want {
			case "":
				if len(errs) > 0 {
					t.Fatalf("legal body refused: %v", errs)
				}
			case "!":
				if len(errs) == 0 {
					t.Fatal("body accepted; want a refusal from a check after parse-tree")
				}
				if strings.Contains(errs[0], "parse-tree refuses") {
					t.Fatalf("parse-tree refused a body it reads: %v", errs)
				}
			default:
				if len(errs) != 1 || !strings.Contains(errs[0], "parse-tree refuses it (kind "+tc.want) {
					t.Fatalf("got %v, want one parse-tree refusal of kind %s", errs, tc.want)
				}
				if bad := parsetree.Judge([]byte(tc.body)); bad == nil || string(bad.Kind) != tc.want {
					t.Fatalf("parse-tree's own verdict is %v, the table says %s", bad, tc.want)
				}
			}
		})
	}
}

// TestValidateShape_RefusalNamesNoValue: the refusal names the kind and the
// line, never parse-tree's message — that quotes what it refuses, and on a
// batch patch the bytes are a file other tenants share (#2903).
func TestValidateShape_RefusalNamesNoValue(t *testing.T) {
	body := "tenants:\n  db-a:\n    _silent_mode: warning\n  other:\n    secret_key_xyz: 1\n    secret_key_xyz: 2\n"
	_, errs := validateShape("db-a", body)
	if len(errs) != 1 || !strings.Contains(errs[0], "kind duplicate_key, line 6") {
		t.Fatalf("got %v, want a duplicate_key refusal naming line 6", errs)
	}
	if strings.Contains(errs[0], "secret_key_xyz") {
		t.Fatalf("the refusal quotes another tenant's key: %v", errs)
	}
}

// An empty trailing document was legal until #3206: tenant-api's own gate
// counted content, not documents. parse-tree refuses the file (and so the
// exporter, since ADR-036 step 6, serves none of it), so the write does too —
// at the call site that reaches git, leaving disk and HEAD untouched.
func TestWriteRefusesAnEmptyTrailingDocument(t *testing.T) {
	dir := initRepoOnMain(t)
	before := gitHead(t, dir)
	_, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", ownOnly+"---\n")
	if !errors.Is(err, ErrValidation) || !strings.Contains(err.Error(), "kind multiple_documents") {
		t.Fatalf("want a multiple_documents ErrValidation, got %v", err)
	}
	if _, serr := os.Stat(filepath.Join(dir, "db-a.yaml")); !os.IsNotExist(serr) {
		t.Errorf("refused write reached disk")
	}
	if after := gitHead(t, dir); after != before {
		t.Errorf("refused write moved HEAD: %s -> %s", before, after)
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

// TestSharedFileIsGitManagedInEitherSpelling is the #1673 × #3064 seam: the
// git-managed verdict is taken on the file the write would actually land on,
// not a synthesised `<id>.yaml` — a shared file spelled `.yml` would
// otherwise read as absent, and the write would create a second file beside
// it.
func TestSharedFileIsGitManagedInEitherSpelling(t *testing.T) {
	for _, spelling := range []string{"db-a.yaml", "db-a.yml"} {
		t.Run(spelling, func(t *testing.T) {
			dir := initRepoOnMain(t)
			seedCommitted(t, dir, spelling, grandfathered)

			if _, err := newW(dir).Write(context.Background(), "db-a", "a@example.com", ownOnly); !errors.Is(err, ErrTenantGitManaged) {
				t.Fatalf("err = %v, want the git-managed refusal for %s", err, spelling)
			}
			raw, err := os.ReadFile(filepath.Join(dir, spelling))
			if err != nil || string(raw) != grandfathered {
				t.Errorf("%s = %q (%v), want unchanged", spelling, raw, err)
			}
			if _, err := os.Stat(filepath.Join(dir, "db-a.yaml")); spelling == "db-a.yml" && err == nil {
				t.Error("the refused write created db-a.yaml beside db-a.yml")
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

	// #3064: a body declaring `other` is refused whatever the baseline says
	// (the body-shape check runs first), so neither arm can open the gate.
	// The control arm that once accepted it — the grandfathered edit — is
	// gone with the grandfathering.
	for _, configDir := range []string{dir, ""} {
		errs, _ := validate(configDir, "db-a", path, body)
		if len(errs) == 0 {
			t.Fatalf("configDir=%q accepted a body adding a foreign tenant section", configDir)
		}
		if !strings.Contains(errs[0], msgOwnOnly) {
			t.Fatalf("configDir=%q: rejected for the wrong reason: %v", configDir, errs)
		}
	}
}

// #3039: validate judged every section of the file, so a section the write
// kept (grandfathered) could carry another tenant's problems, named only when
// TenantNames showed that tenant. #3064: such a body is refused before any
// section is judged, and the refusal names nothing of the other section,
// whatever TenantNames says.
func TestValidateNamesAnotherTenantOnlyWhenShown(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "db-a.yaml")
	base := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  other:\n    secret_key_b: \"1\"\n"
	if err := os.WriteFile(path, []byte(base), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, names := range map[string]TenantNames{"nil": nil, "shown": func(id string) bool { return id == "other" }} {
		errs, _ := validateReplacing(dir, "db-a", path, base, false, names)
		if got := strings.Join(errs, "; "); len(errs) != 1 || !strings.Contains(got, msgOwnOnly) ||
			strings.Contains(got, "tenant=other") || strings.Contains(got, "secret_key_b") {
			t.Errorf("%s TenantNames: errs = %q, want the #3064 refusal naming nothing of the other section", name, errs)
		}
	}
}

// #3039: DiscloseKeyValidation asks names only about a tenant with something
// to say. Each question is an RBAC org-read check that logs a deny and counts
// it into the scope_would_deny soak count the enforce flip is judged by, so a
// question about a quiet tenant counts a deny no message depended on — one
// per tenant in a large flat file, under the writer lock.
func TestDiscloseKeyValidationAsksOnlyAboutTenantsWithMessages(t *testing.T) {
	t.Parallel()
	kv := cfg.KeyValidationByTenant{Tenants: map[string]cfg.KeyValidation{
		"db-a":   {Errors: []string{"own"}},
		"quiet":  {},
		"loud":   {Errors: []string{"tenant=loud: bad"}},
		"notice": {Notices: []string{"tenant=notice: renamed"}},
	}}
	var asked []string
	errs, notices := DiscloseKeyValidation("db-a", kv, func(id string) bool {
		asked = append(asked, id)
		return false
	})
	sort.Strings(asked)
	if strings.Join(asked, ",") != "loud,notice" {
		t.Errorf("names asked about %v, want only the tenants with messages [loud notice]", asked)
	}
	// The answer itself is unchanged by the order of the checks.
	if strings.Join(errs, "|") != "own|"+msgOtherPlaceErrors || strings.Join(notices, "|") != msgOtherPlaceNotices {
		t.Errorf("errs=%q notices=%q", errs, notices)
	}
}
