package ws

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// waitForClientCount polls h.ClientCount() until it matches want or the
// timeout expires. Mirrors the ticker-with-deadline pattern in
// async/taskmanager_test.go to replace blind time.Sleep — see
// TECH-DEBT-019.
func waitForClientCount(t *testing.T, h *Hub, want int, timeout time.Duration) {
	t.Helper()
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	tick := time.NewTicker(time.Millisecond)
	defer tick.Stop()

	for {
		if h.ClientCount() == want {
			return
		}
		select {
		case <-deadline.C:
			t.Fatalf("waitForClientCount: want %d clients within %v, got %d", want, timeout, h.ClientCount())
		case <-tick.C:
		}
	}
}

func TestSubscribeUnsubscribe(t *testing.T) {
	h := NewHub()

	// Subscribe a client
	ch := h.Subscribe()
	if ch == nil {
		t.Fatal("Subscribe returned nil channel")
	}

	// Verify client is in the hub
	h.mu.RLock()
	if len(h.clients) != 1 {
		t.Errorf("expected 1 client, got %d", len(h.clients))
	}
	h.mu.RUnlock()

	// Unsubscribe the client
	h.Unsubscribe(ch)

	// Verify client is removed
	h.mu.RLock()
	if len(h.clients) != 0 {
		t.Errorf("expected 0 clients after unsubscribe, got %d", len(h.clients))
	}
	h.mu.RUnlock()
}

func TestBroadcast(t *testing.T) {
	h := NewHub()

	// Subscribe two clients
	ch1 := h.Subscribe()
	ch2 := h.Subscribe()

	evt := Event{
		Type:      "config_change",
		TenantID:  "tenant-a",
		Timestamp: time.Now(),
		Detail:    "test update",
	}

	// Broadcast event
	h.Broadcast(evt)

	// Both clients should receive the event
	select {
	case received := <-ch1:
		if received.Type != evt.Type {
			t.Errorf("ch1: expected type %q, got %q", evt.Type, received.Type)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("ch1: timeout waiting for event")
	}

	select {
	case received := <-ch2:
		if received.Type != evt.Type {
			t.Errorf("ch2: expected type %q, got %q", evt.Type, received.Type)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("ch2: timeout waiting for event")
	}
}

func TestConcurrentBroadcasts(t *testing.T) {
	h := NewHub()

	// Subscribe multiple clients
	numClients := 10
	clients := make([]chan Event, numClients)
	for i := 0; i < numClients; i++ {
		clients[i] = h.Subscribe()
	}

	// Broadcast multiple events concurrently
	numEvents := 5
	var wg sync.WaitGroup
	for i := 0; i < numEvents; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			evt := Event{
				Type:      "config_change",
				TenantID:  "tenant-x",
				Timestamp: time.Now(),
				Detail:    "concurrent event",
			}
			h.Broadcast(evt)
		}(i)
	}
	wg.Wait()

	// Each client should receive all events
	for cidx, ch := range clients {
		received := 0
		timeout := time.After(5 * time.Second)
		for received < numEvents {
			select {
			case <-ch:
				received++
			case <-timeout:
				t.Errorf("client %d: only received %d/%d events before timeout", cidx, received, numEvents)
				break
			}
		}
	}
}

func TestSlowClientDisconnected(t *testing.T) {
	h := NewHub()

	// Subscribe a slow client (not reading)
	_ = h.Subscribe()

	// Fill the slow client's buffer (capacity 16)
	for i := 0; i < 16; i++ {
		h.Broadcast(Event{
			Type:      "config_change",
			TenantID:  "tenant-slow",
			Timestamp: time.Now(),
		})
	}

	// Now subscribe a normal client (after slow buffer is full)
	normalCh := h.Subscribe()

	// Broadcast one more — should trigger disconnection of slow client
	// but normal client just subscribed and has an empty buffer
	triggerEvt := Event{
		Type:      "config_change",
		TenantID:  "tenant-trigger",
		Timestamp: time.Now(),
	}
	h.Broadcast(triggerEvt)

	// Normal client should still be connected (slow client removed synchronously)
	h.mu.RLock()
	numClients := len(h.clients)
	h.mu.RUnlock()

	// Should have 1 client (the normal one) — slow client removed
	if numClients != 1 {
		t.Errorf("expected 1 client (slow disconnected), got %d", numClients)
	}

	// Normal client should receive the trigger event
	select {
	case received := <-normalCh:
		if received.TenantID != "tenant-trigger" {
			t.Errorf("normalCh: expected 'tenant-trigger', got %q", received.TenantID)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("normalCh: timeout waiting for event")
	}
}

func TestSSEEndpoint(t *testing.T) {
	h := NewHub()

	// Use a cancelable context so we can stop the handler
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Create test request with cancelable context
	req := httptest.NewRequest("GET", "/api/v1/events", nil).WithContext(ctx)
	w := httptest.NewRecorder()

	// Start the handler in a goroutine; wait for it to finish before reading body
	done := make(chan struct{})
	go func() {
		h.ServeHTTP(w, req)
		close(done)
	}()

	// Wait until the handler has subscribed (no time.Sleep — see TECH-DEBT-019).
	waitForClientCount(t, h, 1, time.Second)

	// Broadcast an event
	evt := Event{
		Type:      "config_change",
		TenantID:  "tenant-portal",
		Timestamp: time.Now(),
		Detail:    "config updated",
	}
	h.Broadcast(evt)

	// Cancel the context to stop the handler. ServeHTTP drains any pending
	// events from the channel before returning (see hub.go), so the body
	// is guaranteed to contain the broadcast event by the time done fires.
	cancel()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("handler did not finish in time")
	}

	// Read the response body (safe: handler goroutine has exited)
	body := w.Body.String()

	// Verify SSE headers are set
	headers := w.Header()
	if contentType := headers.Get("Content-Type"); contentType != "text/event-stream" {
		t.Errorf("expected Content-Type: text/event-stream, got %q", contentType)
	}

	if cacheControl := headers.Get("Cache-Control"); cacheControl != "no-cache" {
		t.Errorf("expected Cache-Control: no-cache, got %q", cacheControl)
	}

	// Verify response contains SSE formatted events
	lines := strings.Split(body, "\n")
	if len(lines) == 0 {
		t.Fatal("no response data")
	}

	// Look for the "connected" event and the broadcast event
	foundConnected := false
	foundBroadcast := false

	for _, line := range lines {
		if !strings.HasPrefix(line, "data: ") {
			continue
		}

		jsonData := strings.TrimPrefix(line, "data: ")
		if jsonData == "" {
			continue
		}

		var receivedEvt Event
		if err := json.Unmarshal([]byte(jsonData), &receivedEvt); err != nil {
			continue
		}

		if receivedEvt.Type == "connected" {
			foundConnected = true
		}
		if receivedEvt.Type == "config_change" && receivedEvt.TenantID == "tenant-portal" {
			foundBroadcast = true
		}
	}

	if !foundConnected {
		t.Error("did not receive 'connected' event")
	}
	if !foundBroadcast {
		t.Error("did not receive broadcast event")
	}
}

func TestSSEStreamingFormat(t *testing.T) {
	h := NewHub()

	// Use a cancelable context so we can stop the handler
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	req := httptest.NewRequest("GET", "/api/v1/events", nil).WithContext(ctx)
	w := httptest.NewRecorder()

	// Run handler in goroutine
	done := make(chan struct{})
	go func() {
		h.ServeHTTP(w, req)
		close(done)
	}()

	// Wait until the handler has subscribed (no time.Sleep — see TECH-DEBT-019).
	waitForClientCount(t, h, 1, time.Second)

	// Broadcast an event
	evt := Event{
		Type:      "task_update",
		TaskID:    "task-123",
		Timestamp: time.Now(),
	}
	h.Broadcast(evt)

	// Cancel the context to stop the handler. ServeHTTP drains pending
	// events before returning, so the body is complete by the time done fires.
	cancel()

	// Wait for handler to finish
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("handler did not finish in time")
	}

	// Verify streaming format
	body := w.Body.String()
	if !strings.Contains(body, "data: {") {
		t.Error("response does not contain SSE formatted data")
	}

	// Verify each event ends with double newline
	if !strings.Contains(body, "\n\n") {
		t.Error("response does not contain proper SSE event separator (\\n\\n)")
	}

	// Try to parse events from response
	records := strings.Split(body, "\n\n")
	eventCount := 0
	for _, record := range records {
		if !strings.HasPrefix(record, "data: ") {
			continue
		}
		eventCount++
	}

	// Should have at least 2 events: connected + broadcast
	if eventCount < 2 {
		t.Errorf("expected at least 2 events, parsed %d", eventCount)
	}
}

func BenchmarkBroadcast(b *testing.B) {
	h := NewHub()

	// Subscribe 10 clients
	for i := 0; i < 10; i++ {
		h.Subscribe()
	}

	evt := Event{
		Type:      "config_change",
		TenantID:  "tenant-bench",
		Timestamp: time.Now(),
	}

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		h.Broadcast(evt)
	}
}

func BenchmarkSubscribeUnsubscribe(b *testing.B) {
	h := NewHub()

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		ch := h.Subscribe()
		h.Unsubscribe(ch)
	}
}
