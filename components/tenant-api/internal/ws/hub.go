// Package ws implements Server-Sent Events (SSE) hub for real-time config change notifications.
//
// v2.6.0: WebSocket-like push notifications to Portal using standard HTTP SSE.
// This approach requires zero external dependencies and works with any HTTP client.
//
// Design:
//   - Hub maintains a set of client channels (buffered, 16 events per client)
//   - Broadcast sends events to all connected clients
//   - SSE endpoint (GET /api/v1/events) streams JSON events with proper headers
//   - Slow clients (full buffer) are disconnected to prevent blocking broadcasts
//   - Events include type, tenant_id, timestamp, and optional detail
package ws

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"
)

// Event represents a config change notification.
type Event struct {
	Type      string    `json:"type"`              // "config_change" | "task_update" | "reload" | "connected"
	TenantID  string    `json:"tenant_id,omitempty"`
	TaskID    string    `json:"task_id,omitempty"`
	Timestamp time.Time `json:"timestamp"`
	Detail    string    `json:"detail,omitempty"`
}

// Hub manages SSE client connections and broadcasts events.
type Hub struct {
	mu      sync.RWMutex
	clients map[chan Event]struct{}
}

// NewHub creates a new Hub.
func NewHub() *Hub {
	return &Hub{
		clients: make(map[chan Event]struct{}),
	}
}

// Broadcast sends an event to all connected clients.
// Slow clients whose channel buffer is full are disconnected silently.
func (h *Hub) Broadcast(evt Event) {
	h.mu.Lock()
	defer h.mu.Unlock()

	var slow []chan Event
	for ch := range h.clients {
		select {
		case ch <- evt:
			// Event sent successfully
		default:
			// Client buffer full — mark for disconnection
			slow = append(slow, ch)
		}
	}

	// Remove slow clients and close their channels
	for _, ch := range slow {
		delete(h.clients, ch)
		close(ch)
	}
}

// Subscribe adds a new client channel and returns it.
// The channel is buffered with a capacity of 16 events.
func (h *Hub) Subscribe() chan Event {
	ch := make(chan Event, 16)

	h.mu.Lock()
	h.clients[ch] = struct{}{}
	h.mu.Unlock()

	return ch
}

// Unsubscribe removes a client channel.
func (h *Hub) Unsubscribe(ch chan Event) {
	h.mu.Lock()
	delete(h.clients, ch)
	h.mu.Unlock()
}

// ClientCount returns the number of currently-subscribed clients.
// Useful for observability (metrics) and tests that need to wait for a
// subscriber to be registered before broadcasting.
func (h *Hub) ClientCount() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.clients)
}

// ServeHTTP implements http.Handler for the SSE endpoint.
// GET /api/v1/events — clients connect and receive streaming JSON events.
func (h *Hub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Set SSE headers
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}

	ch := h.Subscribe()
	defer h.Unsubscribe(ch)

	// Send initial connection event
	connEvt := Event{
		Type:      "connected",
		Timestamp: time.Now(),
		Detail:    "SSE stream established",
	}
	data, _ := json.Marshal(connEvt)
	fmt.Fprintf(w, "data: %s\n\n", string(data))
	flusher.Flush()

	// writeEvent marshals + flushes a single event. Factored so it can
	// be reused by the drain loop on context cancel.
	writeEvent := func(evt Event) {
		data, _ := json.Marshal(evt)
		fmt.Fprintf(w, "data: %s\n\n", string(data))
		flusher.Flush()
	}

	// Stream events until client closes or context is done.
	//
	// On context cancel we drain any events that Broadcast has already
	// pushed into ch but the main select hasn't picked up yet. Without
	// this drain, races between Broadcast and cancel can drop in-flight
	// events on the floor — which surfaces as flaky tests that "fix"
	// themselves with a time.Sleep before cancel (the anti-pattern that
	// TECH-DEBT-019 removes).
	for {
		select {
		case <-r.Context().Done():
			for {
				select {
				case evt, ok := <-ch:
					if !ok {
						return
					}
					writeEvent(evt)
				default:
					return
				}
			}
		case evt, ok := <-ch:
			if !ok {
				return
			}
			writeEvent(evt)
		}
	}
}
