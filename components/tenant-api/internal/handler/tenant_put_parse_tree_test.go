package handler

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
)

// ADR-036 §7 step 7 (#3206): a PUT body parse-tree refuses is the 400 every
// other body validation failure is, naming the kind, and nothing is written.
// Each row was accepted by tenant-api's own shape gate before this step (an
// empty trailing document, `!!binary`, `!`) or refused with that gate's own
// message (a second document).
func TestPutTenant_ParseTreeRefusedBodyIs400(t *testing.T) {
	t.Parallel()
	const own = "tenants:\n  t1:\n    _silent_mode: \"warning\"\n"
	for _, c := range []struct{ name, body, kind string }{
		{"second document", own + "---\ntenants:\n  t2: {}\n", "multiple_documents"},
		{"trailing empty document", own + "---\n", "multiple_documents"},
		{"!!binary value", "tenants:\n  t1:\n    _silent_mode: !!binary d2FybmluZw==\n", "tag_not_allowed"},
		{"non-specific tag", "tenants:\n  t1:\n    _silent_mode: ! \"warning\"\n", "non_specific_tag"},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			configDir := setupConfigDir(t, nil)
			h := PutTenant(&Deps{Writer: newTestWriter(configDir), WriteMode: WriteModeDirect})
			w := httptest.NewRecorder()
			h(w, newRequestWithChiParam("PUT", "/api/v1/tenants/t1", "id", "t1", bytes.NewBufferString(c.body)))
			if w.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400; body: %s", w.Code, w.Body.String())
			}
			var resp ErrorResponse
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatalf("decode: %v (%s)", err, w.Body.String())
			}
			if !strings.Contains(resp.Error, "kind "+c.kind) {
				t.Errorf("error = %q, want parse-tree's kind %s", resp.Error, c.kind)
			}
			if _, err := os.Stat(filepath.Join(configDir, "t1.yaml")); !os.IsNotExist(err) {
				t.Errorf("refused body reached disk (stat err %v)", err)
			}
		})
	}
}

// The batch metadata scope pre-check reads no base from a tenant file
// parse-tree refuses (#3206): its first document is not the tenant's
// metadata — nothing serves it — so the pre-check judges the patch alone, as
// it does for a file it cannot read; the write itself then refuses the base.
func TestScopeBlockOnDisk_ParseTreeRefusedFileIsNoBase(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ name, file string }{
		{"trailing empty document", "tenants:\n  t1:\n    _metadata:\n      environment: production\n---\n"},
		{"!!binary value", "tenants:\n  t1:\n    _metadata:\n      environment: !!binary cHJvZHVjdGlvbg==\n"},
	} {
		dir := t.TempDir()
		if err := os.WriteFile(filepath.Join(dir, "t1.yaml"), []byte(c.file), 0o600); err != nil {
			t.Fatal(err)
		}
		if block := scopeBlockOnDisk(dir, "t1"); len(block) != 0 {
			t.Errorf("%s: scope base = %v, want none", c.name, block)
		}
	}
	// Control: the same file without the refused shape is read.
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "t1.yaml"),
		[]byte("tenants:\n  t1:\n    _metadata:\n      environment: production\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if block := scopeBlockOnDisk(dir, "t1"); block["_metadata"] == nil {
		t.Errorf("control: scope base = %v, want the file's _metadata", block)
	}
}

// The 409 TENANT_CONFIG_NOT_LOADABLE text says who can repair the file
// through the API (#3206): for a file parse-tree refuses — whose whole-file
// PUT needs write permission on every tenant, since the end-of-life guard
// cannot read it — it names the kind and that permission, and what anyone
// else does; it does not promise any caller that a whole-file PUT replaces
// it. The envelope carries the kind as parse_tree_kind.
func TestTenantFileNotLoadable_SaysWhoCanRepair(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ name, base, reason, kind string }{
		{"trailing empty document", "tenants:\n  t1:\n    _silent_mode: warning\n---\n", "invalid_config", "multiple_documents"},
		{"!!binary value", "tenants:\n  t1:\n    _silent_mode: !!binary d2FybmluZw==\n", "invalid_config", "tag_not_allowed"},
		{"syntax error", "tenants:\n  t1: [unclosed\n", "malformed_yaml", "syntax_error"},
		// parse-tree reads it; only the typed decode refuses it.
		{"typed decode only", "tenants:\n  t1: oops\n", "invalid_config", ""},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			var e *tenantFileNotLoadableError
			if err := checkPartialWriteBase("t1", []byte(c.base)); !errors.As(err, &e) {
				t.Fatalf("checkPartialWriteBase = %v, want a tenantFileNotLoadableError", err)
			}
			if e.Reason != c.reason || string(e.Kind) != c.kind {
				t.Fatalf("reason %q kind %q, want %q %q", e.Reason, e.Kind, c.reason, c.kind)
			}
			msg := e.Error()
			for _, want := range []string{"write permission on all tenants", "ask a caller", "repair the file in git"} {
				if !strings.Contains(msg, want) {
					t.Errorf("%q lacks %q", msg, want)
				}
			}
			if c.kind != "" {
				if !strings.Contains(msg, "parse-tree kind "+c.kind) {
					t.Errorf("%q does not name kind %s", msg, c.kind)
				}
				if strings.Contains(msg, "replaces it, unless") {
					t.Errorf("%q promises a whole-file PUT to every caller", msg)
				}
			} else if strings.Contains(msg, "parse-tree") || !strings.Contains(msg, "replaces it, unless") {
				t.Errorf("%q, want the conditional whole-file PUT guidance and no parse-tree kind", msg)
			}

			w := httptest.NewRecorder()
			writeTenantFileNotLoadable(w, httptest.NewRequest("PUT", "/", nil), e)
			var env map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
				t.Fatal(err)
			}
			if w.Code != http.StatusConflict || env["code"] != string(errcode.TenantConfigNotLoadable) {
				t.Fatalf("status %d body %s", w.Code, w.Body.String())
			}
			if got, _ := env["parse_tree_kind"].(string); got != c.kind {
				t.Errorf("parse_tree_kind = %q, want %q (body %s)", got, c.kind, w.Body.String())
			}
		})
	}
}
