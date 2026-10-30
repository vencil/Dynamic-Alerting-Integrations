package handler

// #2983 / ADR-038 §11: a `_routing`-prefixed tenant key the route generator
// does not read — a misspelling (`_routing_profle`) or the flat
// `_routing_receiver_type` — is a key no reader reads, and the shared key
// verdict is what refuses it (TestGeneratorGate_KeyVerdictErrorIs400). The
// handler's batch-patch check and the gitops key check that used to refuse
// it as well are gone: with a validator that passes, these writes land, so
// no second judgement of the same key is left behind.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

// A batch patch writing a `_routing` key the generator does not read is no
// body violation any more: nothing but the key verdict judges it.
func TestBatchTenants_UnknownRoutingKeyIsTheKeyVerdicts(t *testing.T) {
	for _, patch := range []string{`{"_routing_receiver_type":"slack"}`, `{"_routing_profle":"domain-ok"}`} {
		t.Run(patch, func(t *testing.T) {
			configDir := seedGitTree(t, offTree(offDisabledOK))
			d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect}
			req := httptest.NewRequest("POST", "/api/v1/tenants/batch",
				bytes.NewBufferString(`{"operations":[{"tenant_id":"t-off","patch":`+patch+`}]}`))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("X-Forwarded-Email", "alice@example.com")
			req.Header.Set("X-Forwarded-Groups", "admins")
			w := httptest.NewRecorder()
			d.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
			if w.Code != http.StatusOK {
				t.Fatalf("status %d body %s: want the write to land — the key verdict, not this check, refuses it", w.Code, w.Body.String())
			}
		})
	}
}

// A tenant file that already carries the flat key (tenant-api used to write
// it): another write is not refused for it by the key check (the key verdict
// reports it), and a batch `unset` still removes it.
func TestBatchTenants_LegacyRoutingReceiverTypeOnDisk(t *testing.T) {
	const legacy = offDisabledOK + "    _routing_receiver_type: pagerduty\n"
	for _, mode := range bothModes {
		t.Run(string(mode), func(t *testing.T) {
			results, _ := runOffBatch(t, mode, legacy, `[{"tenant_id":"t-off","patch":{"cpu_usage_percent":"70"}}]`)
			if got := statuses(results); got != okStatus(mode) {
				t.Fatalf("statuses = %s: %+v", got, results)
			}
			results, file := runOffBatch(t, mode, legacy,
				`[{"tenant_id":"t-off","unset":["_routing_receiver_type"]},{"tenant_id":"t-off","patch":{"cpu_usage_percent":"70"}}]`)
			if got := statuses(results); got != okStatus(mode)+","+okStatus(mode) {
				t.Fatalf("statuses = %s: %+v", got, results)
			}
			if file != "" && (strings.Contains(file, "_routing_receiver_type") || !strings.Contains(file, `cpu_usage_percent: "70"`)) {
				t.Errorf("unset + patch must drop the key and land the write:\n%s", file)
			}
		})
	}
}
