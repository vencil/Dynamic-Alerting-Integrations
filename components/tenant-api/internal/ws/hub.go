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
	"sync/atomic"
	"time"
)

// Event represents a config change notification.
type Event struct {
	Type      string    `json:"type"`              // "config_change" | "task_update" | "reload" | "connected" | "close" | "server_shutdown"
	TenantID  string    `json:"tenant_id,omitempty"`
	TaskID    string    `json:"task_id,omitempty"`
	Timestamp time.Time `json:"timestamp"`
	Detail    string    `json:"detail,omitempty"`
	// ReconnectDelayMs is a client hint, emitted only on "server_shutdown" (#675):
	// the server is going down (SIGTERM / rolling update), so a well-behaved client
	// should wait at least this long — plus its own random jitter — before
	// reconnecting, to spread the reconnect load away from the not-yet-ready new
	// pod instead of stampeding it. omitempty so every other event type stays byte-
	// identical on the wire.
	ReconnectDelayMs int `json:"reconnect_delay_ms,omitempty"`
}

// Config tunes SSE per-client liveness (#143). All three are independently
// optional (0 = disabled); see the field docs for what disabling each costs.
type Config struct {
	// HeartbeatInterval is how often a `: keepalive` SSE comment is written to
	// each client. It serves TWO purposes: (1) keeps intermediary proxies/LBs
	// from reaping an idle connection, and (2) — load-bearing — guarantees a
	// periodic write attempt so the per-write WriteTimeout can actually trip on
	// an idle TCP-half-open client (a goroutine blocked on <-ch with no events
	// has no in-flight write, so the deadline is dormant until the heartbeat
	// fires). MUST stay below the smallest downstream proxy idle timeout
	// (commonly 30s). 0 disables heartbeats — which RE-OPENS the idle-stuck-
	// client goroutine leak this whole feature exists to close, so only set 0
	// when something else guarantees periodic writes.
	HeartbeatInterval time.Duration
	// WriteTimeout bounds each individual write (heartbeat or event) via
	// http.ResponseController.SetWriteDeadline. A stuck/half-open client whose
	// socket buffer is full blocks at most this long, then the write errors and
	// the serving goroutine returns (resource reclaimed). 0 disables the
	// per-write deadline — relying solely on the server's global WriteTimeout /
	// TCP FIN, which does NOT cover the half-open zero-traffic case.
	//
	// NOTE (backpressure buffering): the worst-case reclaim time of
	// ~HeartbeatInterval+WriteTimeout (~35s with defaults) is a FLOOR, not a
	// ceiling, when an intermediary proxy/LB (Nginx, HAProxy, an Ingress
	// controller) sits in front. Those have their own response buffers (tens to
	// hundreds of KB), so after a client TCP-half-opens, the exporter's writes
	// flow into the OS + proxy buffers and don't block until THOSE fill and TCP
	// backpressure propagates back. The goroutine is still reclaimed — just
	// later than ~35s. If tenant_api_sse_clients declines more slowly than
	// expected after disconnects, this buffering (not a leak) is why.
	WriteTimeout time.Duration
	// MaxLifetime is an optional hard cap on a single connection's duration
	// (defense-in-depth). On expiry the server sends a {"type":"close"} event
	// and closes; well-behaved clients reconnect. 0 (default) disables it — the
	// heartbeat + WriteTimeout pair is the primary liveness mechanism, this is
	// just a backstop.
	MaxLifetime time.Duration
}

// DefaultConfig returns the production defaults. Heartbeat 25s sits under a
// common 30s proxy idle timeout on purpose; WriteTimeout 10s gives a generous
// window for a healthy-but-slow client while still reclaiming a dead one
// quickly; MaxLifetime disabled (the heartbeat+deadline pair handles liveness).
func DefaultConfig() Config {
	return Config{
		HeartbeatInterval: 25 * time.Second,
		WriteTimeout:      10 * time.Second,
		MaxLifetime:       0,
	}
}

// Hub manages SSE client connections and broadcasts events.
type Hub struct {
	mu      sync.RWMutex
	clients map[chan Event]struct{}
	cfg     Config
}

// activeHub holds the most-recently constructed Hub so the free-function
// MetricsHandler can read ClientCount() without threading the hub through Deps
// — mirroring handler.activeLimiter and platform's breaker registry. tenant-api
// constructs exactly one hub per process, so the "last writer wins" property is
// a non-issue in production; tests that need isolation assert on their own *Hub
// directly, not via ClientCountSnapshot.
var activeHub atomic.Pointer[Hub]

// NewHub creates a Hub with production-default liveness config (DefaultConfig).
// Retained for tests and callers that don't tune the knobs.
func NewHub() *Hub {
	return NewHubWithConfig(DefaultConfig())
}

// NewHubWithConfig creates a Hub with explicit liveness config (#143). main.go
// builds the Config from TA_SSE_* env vars and calls this.
func NewHubWithConfig(cfg Config) *Hub {
	h := &Hub{
		clients: make(map[chan Event]struct{}),
		cfg:     cfg,
	}
	activeHub.Store(h)
	return h
}

// ClientCountSnapshot returns (current SSE client count, true) for the
// most-recently-constructed hub, or (0, false) when no hub exists (e.g. the
// metrics handler running before NewHub, or a binary with SSE disabled). The
// /metrics handler omits the gauge line entirely when ok is false.
func ClientCountSnapshot() (int, bool) {
	if h := activeHub.Load(); h != nil {
		return h.ClientCount(), true
	}
	return 0, false
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

// Shutdown performs a graceful SSE teardown for process shutdown (SIGTERM /
// rolling update, #675). It (1) broadcasts a single "server_shutdown" control
// event carrying a reconnect-delay hint so well-behaved clients back off with
// jitter instead of stampeding the not-yet-ready replacement pod, then (2)
// closes every client channel so each ServeHTTP goroutine returns promptly.
//
// Why this exists: SSE streams are never idle, so http.Server.Shutdown would
// otherwise block for its full grace period waiting for them to drain, then
// sever them abruptly. Closing the channels here lets Shutdown complete in
// milliseconds and gives clients a clean, actionable signal instead of an
// abrupt connection reset.
//
// Ordering guarantee: the buffered event is delivered before the channel-close
// is observed (Go drains buffered values before a closed receive yields
// ok=false), so a client with buffer room sees server_shutdown then EOF. A
// client whose buffer is already full skips the hint (best-effort) but is still
// closed. Callers MUST invoke this BEFORE http.Server.Shutdown.
func (h *Hub) Shutdown(reconnectDelay time.Duration) {
	evt := Event{
		Type:             "server_shutdown",
		Timestamp:        time.Now(),
		Detail:           "server shutting down — reconnect after the hinted delay plus client-side jitter",
		ReconnectDelayMs: int(reconnectDelay.Milliseconds()),
	}

	h.mu.Lock()
	defer h.mu.Unlock()
	for ch := range h.clients {
		// Non-blocking send: deliver the hint if there's buffer room, else skip
		// it — the close below unblocks the serving goroutine either way.
		select {
		case ch <- evt:
		default:
		}
		delete(h.clients, ch)
		close(ch)
	}
}

// ServeHTTP implements http.Handler for the SSE endpoint.
// GET /api/v1/events — clients connect and receive streaming JSON events.
func (h *Hub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Set SSE headers
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	if _, ok := w.(http.Flusher); !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}

	// #143: ResponseController lets us (a) clear the server's global
	// WriteTimeout for this long-lived stream — otherwise SSE is severed the
	// first time a write happens after TA_WRITE_TIMEOUT (~30s) elapses since
	// connect — and (b) set a per-write deadline so a stuck/half-open client's
	// write unblocks the goroutine instead of leaking it. Clearing the deadline
	// (zero time) is best-effort: an exotic ResponseWriter that doesn't support
	// it just keeps the global timeout, and the per-write SetWriteDeadline below
	// also no-ops — no worse than the pre-#143 behavior.
	rc := http.NewResponseController(w)
	_ = rc.SetWriteDeadline(time.Time{})

	// write applies the per-write deadline, writes the raw SSE payload, and
	// flushes. Returns the first error (deadline-exceeded on a stuck client,
	// broken-pipe on a disconnected one) so the caller can return and let the
	// deferred Unsubscribe reclaim the goroutine. This error check is the whole
	// point of #143 — the pre-#143 code ignored write/flush errors, so a dead
	// client was only noticed when its buffer overflowed or r.Context cancelled.
	write := func(payload string) error {
		if h.cfg.WriteTimeout > 0 {
			_ = rc.SetWriteDeadline(time.Now().Add(h.cfg.WriteTimeout))
		}
		if _, err := fmt.Fprint(w, payload); err != nil {
			return err
		}
		return rc.Flush()
	}
	writeEvent := func(evt Event) error {
		data, _ := json.Marshal(evt)
		return write(fmt.Sprintf("data: %s\n\n", data))
	}

	ch := h.Subscribe()
	defer h.Unsubscribe(ch)

	// Send initial connection event. A failure here means the client is already
	// gone — bail (deferred Unsubscribe cleans up).
	if err := writeEvent(Event{
		Type:      "connected",
		Timestamp: time.Now(),
		Detail:    "SSE stream established",
	}); err != nil {
		return
	}

	// Heartbeat ticker (load-bearing for stuck-client detection — see Config
	// docs). nil channel when disabled (HeartbeatInterval <= 0): a receive on a
	// nil channel blocks forever, so the select arm is simply never taken.
	var heartbeat <-chan time.Time
	if h.cfg.HeartbeatInterval > 0 {
		t := time.NewTicker(h.cfg.HeartbeatInterval)
		defer t.Stop()
		heartbeat = t.C
	}

	// Optional hard max-connection-lifetime cap (defense-in-depth, default off).
	var maxLifetime <-chan time.Time
	if h.cfg.MaxLifetime > 0 {
		timer := time.NewTimer(h.cfg.MaxLifetime)
		defer timer.Stop()
		maxLifetime = timer.C
	}

	// Stream events until the client closes, the context is done, a write
	// fails, or the max-lifetime cap fires.
	//
	// On context cancel we drain any events that Broadcast has already pushed
	// into ch but the main select hasn't picked up yet. Without this drain,
	// races between Broadcast and cancel can drop in-flight events on the floor
	// — flaky tests that "fix" themselves with a time.Sleep before cancel (the
	// anti-pattern TRK-219 removes).
	for {
		select {
		case <-r.Context().Done():
			for {
				select {
				case evt, ok := <-ch:
					if !ok {
						return
					}
					if err := writeEvent(evt); err != nil {
						return
					}
				default:
					return
				}
			}
		case <-maxLifetime:
			// Best-effort clean close so well-behaved clients reconnect.
			_ = writeEvent(Event{
				Type:      "close",
				Timestamp: time.Now(),
				Detail:    "max connection lifetime reached",
			})
			return
		case <-heartbeat:
			if err := write(": keepalive\n\n"); err != nil {
				return // stuck/dead client — write hit the deadline or broke
			}
		case evt, ok := <-ch:
			if !ok {
				return
			}
			if err := writeEvent(evt); err != nil {
				return
			}
		}
	}
}
