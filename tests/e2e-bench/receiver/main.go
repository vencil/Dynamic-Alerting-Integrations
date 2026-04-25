// Webhook receiver for v2.8.0 B-1 Phase 2 e2e harness.
//
// Provides three endpoints:
//
//	POST /hook              — Alertmanager webhook target (send_resolved=true).
//	                          Stamps received_unix_ns and stores into ring buffer.
//	                          Per-alert (each alert in alerts[] array) becomes a
//	                          separate Post with that alert's tenant_id label.
//	GET  /posts             — Returns the full ring buffer as JSON array.
//	GET  /posts?since=<ns>  — Returns posts with received_unix_ns >= since.
//	                          Optional &tenant_id=<id>&status=<firing|resolved>
//	                          filters narrow the result set.
//	GET  /healthz           — 200 OK for compose healthcheck.
//
// Ring buffer is bounded (DEFAULT_CAPACITY = 200). Sized for n=30 runs ×
// (fire + resolve) × ~3 lifecycle events per alert ~= 200 max in flight.
// Older posts are silently dropped on overwrite — the harness driver
// always queries with `since` >= a recent T0 so dropped older posts are
// never queried.
//
// Why not third-party webhook-logger images: commonly suggested options
// (e.g. webhook-logger / json-logger) have unclear maintenance + no
// pinned versions; ~80 lines of stdlib Go is the lowest-risk option that
// also lets us emit NDJSON to stdout for debug.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"sync"
	"time"
)

const defaultCapacity = 200

// Post is a single alert-status webhook stored in the ring buffer.
type Post struct {
	ReceivedUnixNs int64           `json:"received_unix_ns"`
	Status         string          `json:"status"`    // "firing" or "resolved"
	TenantID       string          `json:"tenant_id"` // from alert labels.tenant or labels.tenant_id
	AlertName      string          `json:"alert_name"`
	Body           json.RawMessage `json:"body"`
}

// alertmanagerPayload mirrors the Alertmanager v0.27 webhook schema —
// only the fields we need for indexing.
type alertmanagerPayload struct {
	Status string `json:"status"`
	Alerts []struct {
		Status string            `json:"status"`
		Labels map[string]string `json:"labels"`
	} `json:"alerts"`
}

// ringBuffer is a fixed-capacity cyclic buffer of Posts, FIFO-ordered
// and thread-safe.
type ringBuffer struct {
	mu       sync.RWMutex
	posts    []Post
	capacity int
	next     int
	wrapped  bool
}

func newRingBuffer(capacity int) *ringBuffer {
	return &ringBuffer{
		posts:    make([]Post, capacity),
		capacity: capacity,
	}
}

func (rb *ringBuffer) add(p Post) {
	rb.mu.Lock()
	defer rb.mu.Unlock()
	rb.posts[rb.next] = p
	rb.next = (rb.next + 1) % rb.capacity
	if rb.next == 0 {
		rb.wrapped = true
	}
}

// snapshot returns posts in chronological order (oldest first).
func (rb *ringBuffer) snapshot() []Post {
	rb.mu.RLock()
	defer rb.mu.RUnlock()
	if !rb.wrapped {
		out := make([]Post, rb.next)
		copy(out, rb.posts[:rb.next])
		return out
	}
	out := make([]Post, 0, rb.capacity)
	out = append(out, rb.posts[rb.next:]...)
	out = append(out, rb.posts[:rb.next]...)
	return out
}

// queryFilter holds optional GET /posts query params.
type queryFilter struct {
	since    int64
	tenantID string
	status   string
}

func parseQueryFilter(r *http.Request) (queryFilter, error) {
	f := queryFilter{}
	if s := r.URL.Query().Get("since"); s != "" {
		v, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			return f, fmt.Errorf("since: %w", err)
		}
		f.since = v
	}
	f.tenantID = r.URL.Query().Get("tenant_id")
	f.status = r.URL.Query().Get("status")
	return f, nil
}

func (rb *ringBuffer) query(f queryFilter) []Post {
	all := rb.snapshot()
	out := make([]Post, 0, len(all))
	for _, p := range all {
		if p.ReceivedUnixNs < f.since {
			continue
		}
		if f.tenantID != "" && p.TenantID != f.tenantID {
			continue
		}
		if f.status != "" && p.Status != f.status {
			continue
		}
		out = append(out, p)
	}
	return out
}

// extractTenantID reads `tenant` first (the threshold-exporter convention),
// falling back to `tenant_id` (some external rules may use this name).
func extractTenantID(labels map[string]string) string {
	if v, ok := labels["tenant"]; ok {
		return v
	}
	if v, ok := labels["tenant_id"]; ok {
		return v
	}
	return ""
}

func extractAlertName(labels map[string]string) string {
	if v, ok := labels["alertname"]; ok {
		return v
	}
	return ""
}

func newServer(rb *ringBuffer) http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	mux.HandleFunc("/hook", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		recvNs := time.Now().UnixNano()
		var body json.RawMessage
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
			return
		}
		// Re-parse for indexing — we keep raw body for downstream debug.
		var payload alertmanagerPayload
		parseErr := json.Unmarshal(body, &payload)
		// Tolerate non-Alertmanager payloads (e.g. healthcheck pings,
		// invalid AM-shaped JSON, or AM payloads with no alerts) — store
		// a single placeholder post with empty fields so operators can
		// confirm the receiver actually got the request. Without this,
		// "0 posts stored" is ambiguous (no request? or request without
		// alerts?).
		if parseErr != nil || len(payload.Alerts) == 0 {
			rb.add(Post{ReceivedUnixNs: recvNs, Body: body})
			w.WriteHeader(http.StatusOK)
			return
		}
		// Each alert in the payload becomes its own Post — driver
		// queries by tenant_id, so flattening here is the natural shape.
		for _, a := range payload.Alerts {
			rb.add(Post{
				ReceivedUnixNs: recvNs,
				Status:         a.Status,
				TenantID:       extractTenantID(a.Labels),
				AlertName:      extractAlertName(a.Labels),
				Body:           body,
			})
			// NDJSON to stdout for debug stream.
			fmt.Printf(`{"recv":%d,"status":%q,"tenant":%q,"alert":%q}`+"\n",
				recvNs, a.Status, extractTenantID(a.Labels), extractAlertName(a.Labels))
		}
		w.WriteHeader(http.StatusOK)
	})

	mux.HandleFunc("/posts", func(w http.ResponseWriter, r *http.Request) {
		f, err := parseQueryFilter(r)
		if err != nil {
			http.Error(w, "bad query: "+err.Error(), http.StatusBadRequest)
			return
		}
		out := rb.query(f)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(out)
	})

	return mux
}

func main() {
	addr := flag.String("listen", ":5001", "HTTP listen address")
	cap := flag.Int("capacity", defaultCapacity, "Ring buffer capacity")
	flag.Parse()

	rb := newRingBuffer(*cap)
	log.Printf("e2e-bench receiver listening on %s (ring capacity %d)", *addr, *cap)
	if err := http.ListenAndServe(*addr, newServer(rb)); err != nil {
		log.Fatalf("listen: %v", err)
	}
}
