package handler

// #3829: a retired threshold key on disk refuses every write to its tenant
// (the shared key verdict, from the generator), naming the key that replaces
// it; a batch `unset` of it is the way out, and writes pass again after it.
// The generator is the fake one: its key verdict retires retiredGateKey
// wherever the tree after the write holds it, as `da-guard key-verdict`
// does. The committed registry retires no key, so the unset admission is
// Deps.RetiredThresholdKeys (the seam), not the generated table. Tenant ids
// and the retired key are fixture names (CLAUDE.md #9).

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

const (
	retiredGateKey  = "fixture_old_conns"
	retiredGateHint = "retired threshold key (#3829): write `mysql_connections` instead; fixture"
)

// retiredVerdict answers as the generator does for a tree whose t-a.yaml
// (the write's, else the one on disk) holds retiredGateKey: an unread error,
// reason retired, with the hint.
func retiredVerdict(dir string) func(int, testutil.FakeValidateRequest) (int, any) {
	return func(_ int, req testutil.FakeValidateRequest) (int, any) {
		content, _ := os.ReadFile(filepath.Join(dir, "t-a.yaml"))
		for _, w := range req.Writes {
			if filepath.Base(w.Path) == "t-a.yaml" {
				content = w.Content
			}
		}
		v := testutil.PassVerdict(req, nil)
		if strings.Contains(string(content), retiredGateKey+":") {
			v["key_verdict"] = map[string]any{"status": "complete", "tenants": 1, "seconds": 0.1, "truncated": 0,
				"findings": []map[string]any{{"severity": "error", "kind": "unread", "key": retiredGateKey,
					"file": "t-a.yaml", "tenants": []string{"t-a"}, "reason": "retired", "hint": retiredGateHint,
					"blocks": true}}}
		}
		return 200, v
	}
}

func TestRetiredKeyOnDisk_RefusesWritesUntilABatchUnsetRemovesIt(t *testing.T) {
	t.Parallel()
	var dir string // set before the first request
	f, _ := generatorDirectFixture(t, func(n int, req testutil.FakeValidateRequest) (int, any) {
		return retiredVerdict(dir)(n, req)
	})
	dir = f.dir
	if err := os.WriteFile(filepath.Join(f.dir, "t-a.yaml"),
		[]byte("tenants:\n  t-a:\n    mysql_connections: \"70\"\n    "+retiredGateKey+": \"70\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	f.git(t, "commit", "-qam", "a retired key on disk")
	patch := `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`
	unset := `{"operations":[{"tenant_id":"t-a","unset":["` + retiredGateKey + `"]}]}`

	// (a) an ordinary write to the tenant is refused, naming the replacement
	// and not claiming that no reader reads the key.
	code, res, _ := f.run(t, "", patch, false)
	if code == http.StatusOK || len(res) != 1 || res[0].Status != "error" ||
		!strings.Contains(res[0].Message, "write `mysql_connections` instead") ||
		!strings.Contains(res[0].Message, "batch `unset`") ||
		strings.Contains(res[0].Message, "(retired)") ||
		strings.Contains(res[0].Message, "is read by no reader") {
		t.Fatalf("patch with the retired key on disk: %d %+v", code, res)
	}
	w := putTenantAs(t, f.deps, "t-a", "tenants:\n  t-a:\n    mysql_connections: \"72\"\n    "+retiredGateKey+": \"70\"\n",
		"admins", nil)
	if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "write `mysql_connections` instead") {
		t.Fatalf("PUT writing the retired key: %d %s", w.Code, w.Body.String())
	}

	// Without the key admitted, the unset is no way out.
	if code, _, _ := f.run(t, "", unset, false); code != http.StatusBadRequest {
		t.Fatalf("unset of a key BatchUnsettable does not admit: %d", code)
	}

	// (b) the batch unset removes it, and the tenant can be written again.
	f.deps.RetiredThresholdKeys = []string{retiredGateKey}
	if code, res, _ := f.run(t, "", unset, false); code != http.StatusOK || res[0].Status != "ok" {
		t.Fatalf("unset: %d %+v", code, res)
	}
	if b, _ := os.ReadFile(filepath.Join(f.dir, "t-a.yaml")); strings.Contains(string(b), retiredGateKey) {
		t.Fatalf("the unset left the key:\n%s", b)
	}
	if code, res, _ := f.run(t, "", patch, false); code != http.StatusOK || res[0].Status != "ok" {
		t.Fatalf("patch after the unset: %d %+v", code, res)
	}
}
