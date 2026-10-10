package handler

// #1977: /effective on the production wiring (Deps.Writer set) walks on the
// Writer's prior. Its answers — status and body, byte for byte — are the
// answers a cold walk of the same tree gives (Deps without a Writer, the
// pre-#1977 path), on the first request and on the warm ones after it; and a
// PUT that returned is in the next GET.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

// effectiveWarmTrees are the shapes /effective reports and the ways a tree
// can fail around the tenant asked for.
var effectiveWarmTrees = map[string]map[string]string{
	"chain": {
		"_defaults.yaml":       "defaults:\n  mysql_connections: 80\n",
		"a/_defaults.yaml":     "defaults:\n  mysql_connections: \"70\"\n  only_a: \"3\"\n",
		"a/b/_defaults.yaml":   "defaults:\n  mysql_connections: abc\n",
		"a/b/t1.yaml":          "tenants:\n  t1: {}\n",
		"a/t2.yaml":            "tenants:\n  t2:\n    mysql_connections: \"60\"\n",
		"c/_defaults.yaml":     "defaults: [\n",
		"c/t3.yaml":            "tenants:\n  t3: {}\n",
		"shared.yaml":          "tenants:\n  t4: {}\n  t5: {mysql_connections: \"5\"}\n",
		"broken.yaml":          "tenants: [\n",
		"z/deep/er/t6.yml":     "tenants:\n  t6: {}\n",
		"z/deep/_defaults.yml": "defaults:\n  mysql_connections: \"9\"\n",
	},
	"platform and profiles": {
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_x: 5\ntenants:\n  t1:\n    mysql_connections: \"60\"\n",
		"_profiles.yaml": "profiles:\n  strict:\n    redis_x: \"11\"\ntenants:\n  t2:\n    _profile: strict\n  ghost: {redis_x: \"1\"}\n",
		"t1.yaml":        "tenants:\n  t1:\n    redis_x: \"7\"\n    _profile: strict\n",
		"t2.yaml":        "tenants:\n  t2: {}\n",
	},
	"not served": {
		"_defaults.yaml":     "defaults:\n  mysql_connections: 30\n  redis_memory: null\n",
		"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n  pg_x: abc\n  sub_only: 4\n",
		"sub/t1.yaml":        "tenants:\n  t1:\n    mysql_connections:\n      <<: {default: \"55\"}\n",
		"t2.yaml":            "tenants:\n  t2:\n    redis_memory: 60\n",
	},
	"root unwrapped and decode error": {
		"_defaults.yaml": "pg_connections: 100\n",
		"t1.yaml":        "tenants:\n  t1: {}\n",
		"t2.yaml":        "extra:\n  k: 1\n  k: 2\ntenants:\n  t2:\n    cpu: \"80\"\n",
	},
	"duplicates": {
		"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
		"sub/_defaults.yaml": "defaults:\n  mysql_connections: \"40\"\n  sub_only: \"4\"\n",
		"sub/t1.yaml":        "tenants:\n  t1: {}\n",
		"t2.yaml":            "tenants:\n  t2: {}\n",
		"t2.yml":             "tenants:\n  t2: {}\n",
		"t3.yaml":            "tenants:\n  t3: {}\n",
		"other/t3.yaml":      "tenants:\n  t3: {}\n",
	},
}

func effectiveGET(h http.HandlerFunc, id string) (int, string) {
	req := newRequestWithChiParam("GET", "/api/v1/tenants/"+id+"/effective", "id", id, nil)
	w := httptest.NewRecorder()
	h(w, req)
	return w.Code, w.Body.String()
}

// ageConfDir puts every file past the exporter's mtime guard, so the
// Writer's second walk takes the fast-path for each.
func ageConfDir(t *testing.T, dir string) {
	t.Helper()
	past := time.Now().Add(-time.Hour)
	err := filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err
		}
		return os.Chtimes(p, past, past)
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestGetTenantEffective_WriterAnswersAsAColdWalk(t *testing.T) {
	t.Parallel()
	for name, files := range effectiveWarmTrees {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			for rel, body := range files {
				writeFile(t, filepath.Join(dir, filepath.FromSlash(rel)), body)
			}
			ageConfDir(t, dir)
			cold := GetTenantEffective(&Deps{ConfigDir: dir})
			warm := GetTenantEffective(&Deps{ConfigDir: dir, Writer: gitops.NewWriter(dir, "")})
			codes := map[int]bool{}
			for _, id := range []string{"t1", "t2", "t3", "t4", "t5", "t6", "t-absent"} {
				wantCode, wantBody := effectiveGET(cold, id)
				codes[wantCode] = true
				for i := 1; i <= 3; i++ {
					code, body := effectiveGET(warm, id)
					if code != wantCode || body != wantBody {
						t.Errorf("%s request %d: %d %s\nwant %d %s", id, i, code, body, wantCode, wantBody)
					}
				}
			}
			t.Logf("statuses covered: %v", codes)
		})
	}
}

// The corpus above reaches every status /effective answers with a tenant
// id that validates: 200, 404, 409 and 500 (a decode error).
func TestGetTenantEffective_WarmCorpusReachesEveryStatus(t *testing.T) {
	t.Parallel()
	seen := map[int]bool{}
	for _, files := range effectiveWarmTrees {
		dir := t.TempDir()
		for rel, body := range files {
			writeFile(t, filepath.Join(dir, filepath.FromSlash(rel)), body)
		}
		h := GetTenantEffective(&Deps{ConfigDir: dir})
		for _, id := range []string{"t1", "t2", "t3", "t4", "t5", "t6", "t-absent"} {
			code, _ := effectiveGET(h, id)
			seen[code] = true
		}
	}
	for _, c := range []int{http.StatusOK, http.StatusNotFound, http.StatusConflict, http.StatusInternalServerError} {
		if !seen[c] {
			t.Errorf("no tree answers %d", c)
		}
	}
}

// A PUT that returned is in the next GET — including a same-size rewrite
// within the same second, and back.
func TestGetTenantEffective_SeesThePutBeforeIt(t *testing.T) {
	t.Parallel()
	dir := shortTempDir(t)
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	writeFile(t, filepath.Join(dir, "t1.yaml"), "tenants:\n  t1:\n    mysql_connections: \"61\"\n")
	initGitRepo(t, dir)
	ageConfDir(t, dir)
	d := &Deps{ConfigDir: dir, Writer: gitops.NewWriter(dir, dir), Policy: policy.NewManager(dir), WriteMode: WriteModeDirect}
	get, put := GetTenantEffective(d), PutTenant(d)
	value := func() string {
		t.Helper()
		code, body := effectiveGET(get, "t1")
		if code != http.StatusOK {
			t.Fatalf("GET: %d %s", code, body)
		}
		return body
	}
	if b := value(); !bytes.Contains([]byte(b), []byte(`"mysql_connections":"61"`)) {
		t.Fatalf("before: %s", b)
	}
	for _, v := range []string{"62", "61", "63"} {
		req := newRequestWithChiParam("PUT", "/api/v1/tenants/t1", "id", "t1",
			bytes.NewBufferString("tenants:\n  t1:\n    mysql_connections: \""+v+"\"\n"))
		req.Header.Set("X-Forwarded-Email", "op@example.com")
		req.Header.Set("X-Forwarded-Groups", "ops")
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(put, newRBACManager(t, "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read, write]\n"), rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("PUT %s: %d %s", v, w.Code, w.Body.String())
		}
		if b := value(); !bytes.Contains([]byte(b), []byte(`"mysql_connections":"`+v+`"`)) {
			t.Fatalf("GET after PUT %s: %s", v, b)
		}
	}
}
