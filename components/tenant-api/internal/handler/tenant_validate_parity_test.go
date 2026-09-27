package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

// TestValidateTenant_VerdictMatchesWrite pins #2124: for the SAME body against
// the SAME tree, POST /{id}/validate says valid exactly when Writer.Write
// accepts it — and carries the same advisory notices.
//
// The dry-run used to hand-copy the write path's checks, so every refusal it
// had not copied answered `valid: true` for a body PUT then refused: invalid
// YAML (a duplicate mapping key, a syntax error), a second document, an added
// foreign section, and the #1673/#2078 placement refusals. The table covers
// the two the ticket names plus one case per other refusal class, both
// directions, so a future hand-copied check that drifts shows up here.
//
// Each case gets its own git repo: Write commits, and a committed body would
// change the tree the next case is judged against.
func TestValidateTenant_VerdictMatchesWrite(t *testing.T) {
	t.Parallel()
	const tid = "parity-t"
	const defaults = "defaults:\n  container_cpu: 80\n  mysql_connections: 80\n  mysql_threads_running: 80\n"
	body := func(s string) string { return "tenants:\n  " + tid + ":\n" + s }

	cases := []struct {
		name      string
		files     map[string]string // extra conf.d files besides _defaults.yaml
		body      string
		wantValid bool
	}{
		// The two inputs #2124 was filed with.
		{name: "duplicate_metric_key", body: body("    mysql_connections: \"70\"\n    mysql_connections: \"90\"\n")},
		{name: "yaml_syntax_error", body: body("    mysql_connections: [\n")},
		// One per remaining refusal class of the write path.
		{name: "root_key_violation", body: "defaults:\n  container_cpu: 80\n" + body("    container_cpu: \"70\"\n")},
		{name: "missing_tenant_section", body: "tenants:\n  other-t:\n    container_cpu: \"70\"\n"},
		{name: "second_document", body: body("    container_cpu: \"70\"\n") + "---\ntenants:\n  other-t:\n    container_cpu: \"1\"\n"},
		{name: "adds_foreign_section", body: body("    container_cpu: \"70\"\n") + "  other-t:\n    container_cpu: \"1\"\n"},
		{name: "unknown_key", body: body("    unknown_key_xyz: \"70\"\n")},
		{name: "declared_elsewhere",
			files: map[string]string{"shared.yaml": body("    container_cpu: \"1\"\n")},
			body:  body("    container_cpu: \"70\"\n")},
		{name: "ambiguous_file",
			files: map[string]string{tid + ".yaml": body("    container_cpu: \"1\"\n"), tid + ".yml": body("    container_cpu: \"2\"\n")},
			body:  body("    container_cpu: \"70\"\n")},
		// Accepted bodies: a plain one, and a deprecated alias that must stay
		// valid while carrying the same notice the write returns.
		{name: "valid_body", body: body("    container_cpu: \"70\"\n"), wantValid: true},
		{name: "deprecated_alias_notice", body: body("    mysql_cpu: \"70\"\n"), wantValid: true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := initGitConfigDir(t)
			testutil.WriteYAML(t, dir, "_defaults.yaml", defaults)
			for name, content := range c.files {
				testutil.WriteYAML(t, dir, name, content)
			}
			wr := newTestWriter(dir)

			// Both handler wirings: the production one (Deps.Writer) and the
			// ConfigDir-only fallback the older handler tests use.
			var got []ValidateResponse
			for _, d := range []*Deps{{Writer: wr, ConfigDir: dir}, {ConfigDir: dir}} {
				rec := httptest.NewRecorder()
				ValidateTenant(d)(rec, newRequestWithChiParam("POST",
					"/api/v1/tenants/"+tid+"/validate", "id", tid, bytes.NewBufferString(c.body)))
				if rec.Code != http.StatusOK {
					t.Fatalf("validate status = %d, body: %s", rec.Code, rec.Body.String())
				}
				var resp ValidateResponse
				if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
					t.Fatalf("unmarshal validate response: %v", err)
				}
				got = append(got, resp)
			}
			if !reflect.DeepEqual(got[0], got[1]) {
				t.Fatalf("the two handler wirings disagree:\n writer:    %+v\n configDir: %+v", got[0], got[1])
			}
			vresp := got[0]

			notices, werr := wr.Write(context.Background(), tid, "op@example.com", c.body)
			if vresp.Valid != (werr == nil) {
				t.Fatalf("dry-run and write disagree: validate.valid=%v warnings=%q, write err=%v",
					vresp.Valid, vresp.Warnings, werr)
			}
			if vresp.Valid != c.wantValid {
				t.Fatalf("validate.valid = %v, want %v (warnings=%q, write err=%v)",
					vresp.Valid, c.wantValid, vresp.Warnings, werr)
			}
			if !vresp.Valid && len(vresp.Warnings) == 0 {
				t.Error("valid=false with no warnings: the caller cannot tell why")
			}
			if werr == nil && !reflect.DeepEqual(vresp.Notices, notices) {
				t.Errorf("notices differ: validate %q, write %q", vresp.Notices, notices)
			}
			if c.name == "deprecated_alias_notice" && len(vresp.Notices) == 0 {
				t.Error("deprecated alias produced no notice — the case no longer exercises the advisory channel")
			}
		})
	}
}
