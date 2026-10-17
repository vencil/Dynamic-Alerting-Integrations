package ws

import (
	"context"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// #3123: each subscriber receives only what its filter admits, and the filter
// is consulted at broadcast time (so a change in what it reads — an RBAC
// hot-reload — applies to the next event, not to the next connection).
func TestBroadcast_PerSubscriberFilterAtSendTime(t *testing.T) {
	t.Parallel()
	h := NewHub()
	var allowB atomic.Bool
	chA := h.Subscribe(func(e Event) bool { return e.Tenant == "tenant-a" })
	chB := h.Subscribe(func(e Event) bool { return allowB.Load() })
	chNil := h.Subscribe(nil)

	h.Broadcast(Event{Type: "config_change", Tenant: "tenant-b"})
	allowB.Store(true)
	h.Broadcast(Event{Type: "config_change", Tenant: "tenant-a"})

	if n := len(chA); n != 1 {
		t.Errorf("subscriber A queued %d events, want 1 (tenant-a only)", n)
	}
	if n := len(chB); n != 1 {
		t.Errorf("subscriber B queued %d events, want 1 (the one broadcast after its filter changed)", n)
	}
	if n := len(chNil); n != 0 {
		t.Errorf("subscriber without a filter queued %d events, want 0", n)
	}
}

// #3123: ServeHTTP without a filter streams its own "connected" event and no
// broadcast event (fail closed).
func TestServeHTTP_UnfilteredStreamsNoBroadcast(t *testing.T) {
	t.Parallel()
	h := NewHub()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req := httptest.NewRequest("GET", "/api/v1/events", nil).WithContext(ctx)
	w := httptest.NewRecorder()
	done := make(chan struct{})
	go func() { h.ServeHTTP(w, req); close(done) }()
	waitForClientCount(t, h, 1, time.Second)
	h.Broadcast(Event{Type: "config_change", TenantID: "tenant-secret", Tenant: "tenant-secret"})
	cancel()
	<-done
	body := w.Body.String()
	if !strings.Contains(body, `"connected"`) {
		t.Errorf("no connected event: %s", body)
	}
	if strings.Contains(body, "tenant-secret") {
		t.Errorf("unfiltered stream carried a broadcast event: %s", body)
	}
}
