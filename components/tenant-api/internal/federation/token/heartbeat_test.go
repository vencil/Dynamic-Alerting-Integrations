package token

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"
)

// heartbeatRecords returns the JSON log records in buf whose "event" field is
// the ADR-028 canary event. Mirrors decodeRevocationEvents in
// configmap_store_test.go — same capture technique, same per-instance logger
// seam (no global slog swap).
func heartbeatRecords(t *testing.T, buf *bytes.Buffer) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		if line == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("log line is not JSON: %q: %v", line, err)
		}
		if rec["event"] == heartbeatEvent {
			out = append(out, rec)
		}
	}
	return out
}

// TestHeartbeater_EmitsCanaryEvent: one emission produces exactly one record
// carrying the event name the Vector allowlist and the reconciler's LogsQL
// query both key on. A rename on this side alone silently empties the evidence
// channel, which is the failure mode the canary exists to make visible.
func TestHeartbeater_EmitsCanaryEvent(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	h := &Heartbeater{logger: slog.New(slog.NewJSONHandler(&buf, nil))}

	h.emit()

	recs := heartbeatRecords(t, &buf)
	if len(recs) != 1 {
		t.Fatalf("want exactly 1 heartbeat record, got %d: %s", len(recs), buf.String())
	}
	if got := recs[0]["event"]; got != "federation_revocation_channel_heartbeat" {
		t.Errorf("event = %v, want federation_revocation_channel_heartbeat", got)
	}
	// slog's JSON handler supplies `time` itself — the canary must not add a
	// second, hand-rolled timestamp field (the revocation event does not either).
	if _, ok := recs[0]["ts"]; ok {
		t.Errorf("heartbeat carries its own `ts` field; slog already emits `time`: %v", recs[0])
	}
	if _, ok := recs[0]["time"]; !ok {
		t.Errorf("heartbeat record has no `time` field — the slog JSON handler should supply it: %v", recs[0])
	}
}

// TestHeartbeater_CarriesNoTenantIdentifier pins ADR-028 §D3 (PII
// minimisation): this event lands in a 30-day audit store, so it must not carry
// any customer identifier. Asserted on the KEY SET rather than against a
// specific field name, so a future `tenant`, `tenant_id` or `tenantID` all trip
// it.
func TestHeartbeater_CarriesNoTenantIdentifier(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	h := &Heartbeater{logger: slog.New(slog.NewJSONHandler(&buf, nil))}

	h.emit()

	recs := heartbeatRecords(t, &buf)
	if len(recs) != 1 {
		t.Fatalf("want exactly 1 heartbeat record, got %d: %s", len(recs), buf.String())
	}
	for k, v := range recs[0] {
		if strings.Contains(strings.ToLower(k), "tenant") {
			t.Errorf("heartbeat must not carry a tenant identifier: %s=%v", k, v)
		}
	}
	// The whole rendered line must be free of it too — a tenant id embedded in
	// the message string would ride into the store just as effectively as a
	// structured field.
	if strings.Contains(strings.ToLower(buf.String()), "tenant") {
		t.Errorf("heartbeat line mentions a tenant: %s", buf.String())
	}
}

// TestHeartbeater_RunEmitsImmediatelyThenStops: Run must emit once BEFORE the
// first tick (else every restart costs a full interval of apparent downtime) and
// must return when stopCh closes. Driven with a short interval — never a real
// 5m sleep.
func TestHeartbeater_RunEmitsImmediatelyThenStops(t *testing.T) {
	t.Parallel()
	var buf syncBuffer
	h := &Heartbeater{logger: slog.New(slog.NewJSONHandler(&buf, nil))}

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		h.Run(50*time.Millisecond, stopCh)
		close(done)
	}()

	// The immediate emission lands without waiting for a tick.
	deadline := time.Now().Add(2 * time.Second)
	for buf.count(heartbeatEvent) == 0 {
		if time.Now().After(deadline) {
			t.Fatal("Run did not emit immediately (before the first tick)")
		}
		time.Sleep(5 * time.Millisecond)
	}

	// And it keeps ticking.
	deadline = time.Now().Add(2 * time.Second)
	for buf.count(heartbeatEvent) < 2 {
		if time.Now().After(deadline) {
			t.Fatalf("Run stopped ticking: only %d emission(s)", buf.count(heartbeatEvent))
		}
		time.Sleep(5 * time.Millisecond)
	}

	close(stopCh)
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return after stopCh closed")
	}

	// No emission after the return (the ticker was stopped, not leaked).
	after := buf.count(heartbeatEvent)
	time.Sleep(150 * time.Millisecond)
	if got := buf.count(heartbeatEvent); got != after {
		t.Errorf("Run emitted %d more time(s) after returning (leaked ticker)", got-after)
	}
}

// TestHeartbeater_RunRejectsNonPositiveInterval: a 0 / negative interval must
// NOT reach time.NewTicker (which panics) and must NOT silently disable the
// canary — a `--federation-heartbeat-interval=0` typo would otherwise either
// crash tenant-api at startup or leave on-call chasing a "severed evidence
// channel" that is really a values mistake.
func TestHeartbeater_RunRejectsNonPositiveInterval(t *testing.T) {
	t.Parallel()
	for _, bad := range []time.Duration{0, -1 * time.Second} {
		var buf syncBuffer
		h := &Heartbeater{logger: slog.New(slog.NewJSONHandler(&buf, nil))}
		stopCh := make(chan struct{})
		done := make(chan struct{})
		go func() {
			defer close(done)
			h.Run(bad, stopCh) // must not panic
		}()

		deadline := time.Now().Add(2 * time.Second)
		for buf.count(heartbeatEvent) == 0 {
			if time.Now().After(deadline) {
				t.Fatalf("interval %v: Run emitted nothing — the canary was silently disabled", bad)
			}
			time.Sleep(5 * time.Millisecond)
		}
		if buf.count("not positive") == 0 {
			t.Errorf("interval %v: no warning logged about the rejected interval: %s", bad, buf.dump())
		}

		close(stopCh)
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Fatalf("interval %v: Run did not return after stopCh closed", bad)
		}
	}
}

// syncBuffer is a bytes.Buffer guarded for the concurrent write (the Run
// goroutine) + read (the assertions) in the test above. Without it the race
// detector flags the shared buffer.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.Write(p)
}

func (s *syncBuffer) count(substr string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return strings.Count(s.buf.String(), substr)
}

func (s *syncBuffer) dump() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.String()
}
