package federation

// #1698: the _federation/ directory is a conf.d-shaped directory, so a
// tenant's subset file may be spelled `<id>.yaml`, `<id>.yml` or with an
// upper-case extension — the orphan detector's scan (confd.TenantIDFromFile)
// has always accepted all of them. Before the shared resolver, the read side
// and the write side both joined a hardcoded `<id>.yaml`:
//
//   - GET on a tenant stored as `<id>.yml` read a file that did not exist and
//     returned 200 with an EMPTY subset — indistinguishable from "this tenant
//     federates nothing";
//   - PUT on that tenant created a SECOND file `<id>.yaml` beside the real one,
//     leaving the old file with different content and no reader.
//
// These tests pin the tenant-plane (#1673) semantics onto _federation/: the
// tenant's ACTUAL file is read and rewritten in place; only a brand-new subset
// gets `<id>.yaml`; two files claiming one tenant are refused with the same
// 409 the tenant plane returns for confd.ErrAmbiguousTenantFile.

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/handler"
)

// spellingTenant is the tenant the spelling tests address. It must be the one
// scopedAdminRBAC grants admin on, so the PUT rows get past authorization.
const spellingTenant = "db-a"

// plantSubsetFile writes a subset file under _federation/ with an explicit
// file name — deliberately NOT via federationSubsetPath, whose whole point is
// the default spelling; these tests exist for the other spellings.
func plantSubsetFile(t *testing.T, configDir, name, metric string) string {
	t.Helper()
	dir := confd.FederationSubsetDir(configDir)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir _federation: %v", err)
	}
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte("metrics:\n  - "+metric+"\n"), 0o644); err != nil {
		t.Fatalf("plant %s: %v", path, err)
	}
	return path
}

// subsetDirNames lists _federation/ sorted; a missing directory lists empty.
func subsetDirNames(t *testing.T, configDir string) []string {
	t.Helper()
	entries, err := os.ReadDir(confd.FederationSubsetDir(configDir))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		t.Fatalf("read _federation: %v", err)
	}
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	sort.Strings(names)
	return names
}

func TestReadFederationSubset_ResolvesStoredSpelling(t *testing.T) {
	t.Parallel()
	for _, name := range []string{
		spellingTenant + ".yaml",
		spellingTenant + ".yml",
		spellingTenant + ".YAML",
		spellingTenant + ".Yml",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			configDir := t.TempDir()
			plantSubsetFile(t, configDir, name, "stored_metric")
			d := &handler.Deps{ConfigDir: configDir}

			subset, err := readFederationSubset(d, spellingTenant)
			if err != nil {
				t.Fatalf("readFederationSubset: %v", err)
			}
			if !reflect.DeepEqual(subset.Metrics, []string{"stored_metric"}) {
				t.Errorf("metrics = %v, want [stored_metric] — the subset stored as %s was not read", subset.Metrics, name)
			}
		})
	}
}

func TestReadFederationSubset_AmbiguousSpellingIsRefused(t *testing.T) {
	t.Parallel()
	configDir := t.TempDir()
	plantSubsetFile(t, configDir, spellingTenant+".yaml", "from_yaml")
	plantSubsetFile(t, configDir, spellingTenant+".yml", "from_yml")
	d := &handler.Deps{ConfigDir: configDir}

	subset, err := readFederationSubset(d, spellingTenant)
	if !errors.Is(err, confd.ErrAmbiguousTenantFile) {
		t.Fatalf("err = %v (subset %+v), want confd.ErrAmbiguousTenantFile", err, subset)
	}
}

// TestReadFederationSubset_StemCaseIsExact: the id match is exact, as on the
// tenant plane (confd.ResolveTenantFile). `DB-A.yaml` is not db-a's subset,
// so db-a reads as having none — and it is not an ambiguity either.
func TestReadFederationSubset_StemCaseIsExact(t *testing.T) {
	t.Parallel()
	configDir := t.TempDir()
	plantSubsetFile(t, configDir, strings.ToUpper(spellingTenant)+".yaml", "other_case")
	d := &handler.Deps{ConfigDir: configDir}

	subset, err := readFederationSubset(d, spellingTenant)
	if err != nil {
		t.Fatalf("readFederationSubset: %v", err)
	}
	if len(subset.Metrics) != 0 {
		t.Errorf("metrics = %v, want empty — a differently-cased stem is another id", subset.Metrics)
	}
}

func TestGetTenantFederation_SpellingOutcomes(t *testing.T) {
	t.Parallel()
	whitelist := fedpolicy.NewManagerForTest(&fedpolicy.Config{
		Whitelist: []fedpolicy.WhitelistEntry{{Metric: "from_yaml"}, {Metric: "from_yml"}},
	})
	cases := []struct {
		name       string
		files      map[string]string // file name → metric
		wantStatus int
		wantBody   []string // metrics, when 200
	}{
		{"no subset file", nil, http.StatusOK, []string{}},
		{"stored as .yml", map[string]string{spellingTenant + ".yml": "from_yml"}, http.StatusOK, []string{"from_yml"}},
		{"stored as .YAML", map[string]string{spellingTenant + ".YAML": "from_yaml"}, http.StatusOK, []string{"from_yaml"}},
		{
			"two spellings",
			map[string]string{spellingTenant + ".yaml": "from_yaml", spellingTenant + ".yml": "from_yml"},
			http.StatusConflict, nil,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			configDir := t.TempDir()
			for name, metric := range tc.files {
				plantSubsetFile(t, configDir, name, metric)
			}
			d := &handler.Deps{ConfigDir: configDir, FederationPolicy: whitelist, RBAC: newRBACManager(t, "")}

			w := executeWithRBAC(t, GetTenantFederation(d),
				fedReq(t, "GET", "/api/v1/tenants/"+spellingTenant+"/federation", "id", spellingTenant, ""))
			if w.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d, body: %s", w.Code, tc.wantStatus, w.Body.String())
			}
			if tc.wantStatus != http.StatusOK {
				var er handler.ErrorResponse
				if err := json.Unmarshal(w.Body.Bytes(), &er); err != nil {
					t.Fatalf("error body is not an ErrorResponse: %v (%s)", err, w.Body.String())
				}
				if !strings.Contains(er.Error, confd.ErrAmbiguousTenantFile.Error()) {
					t.Errorf("error = %q, want it to carry %q", er.Error, confd.ErrAmbiguousTenantFile.Error())
				}
				return
			}
			var got fedpolicy.Subset
			if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			if !reflect.DeepEqual(got.Metrics, tc.wantBody) {
				t.Errorf("metrics = %v, want %v", got.Metrics, tc.wantBody)
			}
		})
	}
}

func TestPutTenantFederation_SpellingOutcomes(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name       string
		existing   []string // subset files on disk (and committed) before the PUT
		wantStatus int
		wantFiles  []string // _federation/ listing afterwards
		wantNewIn  string   // file that must carry the PUT's content ("" = none)
	}{
		{"brand-new subset gets .yaml", nil, http.StatusOK, []string{spellingTenant + ".yaml"}, spellingTenant + ".yaml"},
		{"existing .yaml rewritten", []string{spellingTenant + ".yaml"}, http.StatusOK, []string{spellingTenant + ".yaml"}, spellingTenant + ".yaml"},
		{"existing .yml rewritten in place", []string{spellingTenant + ".yml"}, http.StatusOK, []string{spellingTenant + ".yml"}, spellingTenant + ".yml"},
		{"existing .YAML rewritten in place", []string{spellingTenant + ".YAML"}, http.StatusOK, []string{spellingTenant + ".YAML"}, spellingTenant + ".YAML"},
		{
			"two spellings refused",
			[]string{spellingTenant + ".yaml", spellingTenant + ".yml"},
			http.StatusConflict,
			[]string{spellingTenant + ".yaml", spellingTenant + ".yml"},
			"",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			configDir := t.TempDir()
			before := map[string][]byte{}
			for _, name := range tc.existing {
				path := plantSubsetFile(t, configDir, name, "old_metric")
				b, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				before[name] = b
			}
			initGitRepo(t, configDir) // commits the planted files
			d := &handler.Deps{
				ConfigDir: configDir,
				Writer:    newTestWriter(configDir),
				FederationPolicy: fedpolicy.NewManagerForTest(&fedpolicy.Config{
					Whitelist: []fedpolicy.WhitelistEntry{{Metric: "old_metric"}, {Metric: "new_metric"}},
				}),
				RBAC: newRBACManager(t, scopedAdminRBAC),
			}

			w := executeWithRBAC(t, PutTenantFederation(d),
				fedReq(t, "PUT", "/api/v1/tenants/"+spellingTenant+"/federation", "id", spellingTenant, `{"metrics":["new_metric"]}`))
			if w.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d, body: %s", w.Code, tc.wantStatus, w.Body.String())
			}
			if got := subsetDirNames(t, configDir); !reflect.DeepEqual(got, tc.wantFiles) {
				t.Errorf("_federation/ = %v, want %v — a PUT must not leave a second file claiming the tenant", got, tc.wantFiles)
			}
			if tc.wantNewIn == "" {
				// Refused: every pre-existing file is byte-identical.
				for name, b := range before {
					got, err := os.ReadFile(filepath.Join(confd.FederationSubsetDir(configDir), name))
					if err != nil || string(got) != string(b) {
						t.Errorf("%s changed on a refused PUT: err=%v, got %q, want %q", name, err, got, b)
					}
				}
				return
			}
			got, err := os.ReadFile(filepath.Join(confd.FederationSubsetDir(configDir), tc.wantNewIn))
			if err != nil {
				t.Fatalf("read %s: %v", tc.wantNewIn, err)
			}
			if !strings.Contains(string(got), "new_metric") || strings.Contains(string(got), "old_metric") {
				t.Errorf("%s = %q, want the PUT's subset [new_metric]", tc.wantNewIn, got)
			}
			// And the reader sees what was written.
			subset, err := readFederationSubset(d, spellingTenant)
			if err != nil {
				t.Fatalf("readFederationSubset after PUT: %v", err)
			}
			if !reflect.DeepEqual(subset.Metrics, []string{"new_metric"}) {
				t.Errorf("read-back metrics = %v, want [new_metric]", subset.Metrics)
			}
		})
	}
}
