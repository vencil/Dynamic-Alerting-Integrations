package main

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	"github.com/vencil/tenant-api/internal/testutil"
	"github.com/vencil/tenant-api/internal/ws"
)

// #3123 item 1, end to end: GET /api/v1/events as main.go serves it (the
// route's RBAC middleware in front of handler.EventStream over a real hub,
// events built by the constructors main.go broadcasts). An org-scoped
// `tenants: ["*"]` caller passes the route's "*" gate; the stream must still
// carry only its own org's tenants.
func TestEventsRoute_FiltersPerSubscriber(t *testing.T) {
	t.Parallel()
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", `groups:
  - name: org-users
    tenants: ["*"]
    permissions: [read]
    org-scope: org
`)
	mgr, err := rbac.NewManager(rbacFile, map[string]string{"org": "X-Auth-Request-Org"})
	if err != nil {
		t.Fatal(err)
	}
	torg := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		"tenant-ev-in":  {"ORG-ALPHA"},
		"tenant-ev-out": {"ORG-BETA"},
	}})
	mgr.SetOrgResolver(func(id string) []string { o, _ := torg.OrgsForTenant(id); return o })
	d := &handler.Deps{RBAC: mgr, TenantOrg: torg}
	hub := ws.NewHubWithConfig(ws.Config{})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req := httptest.NewRequest("GET", "/api/v1/events", nil).WithContext(ctx)
	req.Header.Set("X-Forwarded-Email", "alpha@example.com")
	req.Header.Set("X-Forwarded-Groups", "org-users")
	req.Header.Set("X-Auth-Request-Org", "ORG-ALPHA")
	w := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		mgr.Middleware(rbac.PermRead, nil)(handler.EventStream(d, hub)).ServeHTTP(w, req)
		close(done)
	}()
	deadline := time.Now().Add(2 * time.Second)
	for hub.ClientCount() != 1 {
		if time.Now().After(deadline) {
			t.Fatalf("no subscriber; status=%d body=%s", w.Code, w.Body.String())
		}
		time.Sleep(time.Millisecond)
	}
	hub.Broadcast(handler.ConfigChangeEvent("tenant-ev-out"))
	hub.Broadcast(handler.StateChangeEvent("federation/tenant-ev-out"))
	hub.Broadcast(handler.StateChangeEvent("views"))
	hub.Broadcast(handler.ConfigChangeEvent("tenant-ev-in"))
	cancel()
	<-done

	body := w.Body.String()
	if strings.Contains(body, "tenant-ev-out") || strings.Contains(body, `"views"`) {
		t.Errorf("stream carried events the caller may not see: %s", body)
	}
	if !strings.Contains(body, `"tenant_id":"tenant-ev-in"`) {
		t.Errorf("stream lost the caller's own tenant's event: %s", body)
	}
}
