// Package recipepreview is tenant-api's side of the recipe-preview container
// (ADR-027 §5): the would-fire preview service that runs in tenant-api's pod
// and answers only tenant-api, over HTTP/1.1 on a pod-internal Unix socket
// (POST /v1/preview), like the generator container (ADR-036 §3).
//
// tenant-api authorizes the caller before it calls; the service reads no
// identity. What crosses the socket is the recipe, the tenant it is previewed
// for, the scenario and the caller's deadline:
//
//   - Every call is bounded by its context's deadline, which the request
//     timeout middleware sets. The service is sent the time left
//     (deadline_ms) and stops waiting for an evaluation slot, bounds each
//     da-guard / promtool run, and refuses to answer a verdict past it.
//   - A context with less than MinBudget left is not sent at all (BusyError
//     no_time): a worst-case evaluation would not fit, and a run cut short
//     only produces an `error` state that reads like a verdict.
//   - The service's own refusals map to typed errors: no verdict this time
//     (BusyError: busy, deadline), the tenant's preview rate limit
//     (RateLimitedError), a request it refuses (RejectedError), and no
//     usable answer at all (UnavailableError).
package recipepreview

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"

	"github.com/vencil/tenant-api/internal/errkind"
)

// MinBudget is the least time left on a call's context for it to be sent:
// below it the service could not judge even a quick recipe before tenant-api
// must answer.
const MinBudget = 2 * time.Second

// maxResponseBytes caps what is read of one answer: a verdict is a few
// hundred bytes.
const maxResponseBytes = 1 << 20

// The reasons a BusyError or an UnavailableError carries, as sent to the
// client in the envelope's `reason`.
const (
	// ReasonBusy: every evaluation slot stayed taken for the service's queue
	// limit.
	ReasonBusy = "busy"
	// ReasonDeadline: the caller's deadline passed while waiting for a slot
	// or during the evaluation.
	ReasonDeadline = "deadline"
	// ReasonNoTime: too little of the request's time was left to ask.
	ReasonNoTime = "no_time"
	// ReasonUnreachable: nobody answered on the socket.
	ReasonUnreachable = "unreachable"
	// ReasonBadResponse: an answer this client cannot read.
	ReasonBadResponse = "bad_response"
	// ReasonInternalError: the service's own 500.
	ReasonInternalError = "internal_error"
)

// BusyError: no verdict this time; the same request may get one later.
type BusyError struct{ Reason string }

func (e *BusyError) Error() string { return "recipe preview gave no verdict: " + e.Reason }

// ErrorKind: errkind.PreviewBusy.
func (e *BusyError) ErrorKind() errkind.Kind { return errkind.PreviewBusy }

// RateLimitedError: the tenant's preview rate limit (per minute) is used up.
type RateLimitedError struct{ Message string }

func (e *RateLimitedError) Error() string { return "recipe preview: " + e.Message }

// ErrorKind: errkind.PreviewRateLimited.
func (e *RateLimitedError) ErrorKind() errkind.Kind { return errkind.PreviewRateLimited }

// RejectedError: the service refuses the request itself (400 / 413); resending
// it unchanged does not help. Message is the service's own text, which names
// the field at fault and never the caller's data.
type RejectedError struct {
	Status  int
	Message string
}

func (e *RejectedError) Error() string { return "recipe preview refused the request: " + e.Message }

// ErrorKind: errkind.PreviewRejected.
func (e *RejectedError) ErrorKind() errkind.Kind { return errkind.PreviewRejected }

// UnavailableError: the service gave no usable answer. Detail is for the
// server log only.
type UnavailableError struct {
	Reason string
	Detail string
}

func (e *UnavailableError) Error() string {
	return fmt.Sprintf("recipe preview unavailable (%s): %s", e.Reason, e.Detail)
}

// ErrorKind: errkind.PreviewUnavailable.
func (e *UnavailableError) ErrorKind() errkind.Kind { return errkind.PreviewUnavailable }

// Request is what POST /v1/preview takes, less the deadline (the client adds
// it from the context).
type Request struct {
	Recipe   json.RawMessage `json:"recipe"`
	Tenant   string          `json:"tenant"`
	Scenario json.RawMessage `json:"scenario,omitempty"`
}

// Client talks to the recipe-preview service on its Unix socket.
type Client struct {
	socket string
	hc     *http.Client
}

// NewClient returns a Client for the service listening on socket.
func NewClient(socket string) *Client {
	tr := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", socket)
		},
		// The service closes every connection after its answer.
		DisableKeepAlives: true,
	}
	return &Client{socket: socket, hc: &http.Client{Transport: tr}}
}

// Socket is the path the client dials.
func (c *Client) Socket() string { return c.socket }

type serviceError struct {
	Error  string `json:"error"`
	Reason string `json:"reason"`
}

// Preview asks for the would-fire verdict of req and returns it as the
// service wrote it (a JSON object: alertname, supported, states, warnings).
func (c *Client) Preview(ctx context.Context, req Request) (json.RawMessage, error) {
	deadline, ok := ctx.Deadline()
	if !ok {
		return nil, &BusyError{Reason: ReasonNoTime}
	}
	left := time.Until(deadline)
	if left < MinBudget {
		return nil, &BusyError{Reason: ReasonNoTime}
	}
	body, err := json.Marshal(struct {
		Request
		DeadlineMS int64 `json:"deadline_ms"`
	}{req, left.Milliseconds()})
	if err != nil {
		return nil, &UnavailableError{Reason: ReasonInternalError, Detail: "encode the request: " + err.Error()}
	}
	hreq, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://recipe-preview/v1/preview", bytes.NewReader(body))
	if err != nil {
		return nil, &UnavailableError{Reason: ReasonInternalError, Detail: err.Error()}
	}
	hreq.Header.Set("Content-Type", "application/json")
	resp, err := c.hc.Do(hreq)
	if err != nil {
		if ctx.Err() != nil {
			return nil, &BusyError{Reason: ReasonDeadline}
		}
		return nil, &UnavailableError{Reason: ReasonUnreachable, Detail: err.Error()}
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		if ctx.Err() != nil {
			return nil, &BusyError{Reason: ReasonDeadline}
		}
		return nil, &UnavailableError{Reason: ReasonBadResponse, Detail: "read the answer: " + err.Error()}
	}
	return decode(resp.StatusCode, raw)
}

func decode(status int, raw []byte) (json.RawMessage, error) {
	var se serviceError
	switch status {
	case http.StatusOK:
		var obj map[string]json.RawMessage
		if err := json.Unmarshal(raw, &obj); err != nil {
			return nil, &UnavailableError{Reason: ReasonBadResponse, Detail: "the verdict is not a JSON object"}
		}
		return json.RawMessage(raw), nil
	case http.StatusServiceUnavailable:
		_ = json.Unmarshal(raw, &se)
		switch se.Reason {
		case ReasonBusy, ReasonDeadline:
			return nil, &BusyError{Reason: se.Reason}
		}
		return nil, &UnavailableError{Reason: ReasonBadResponse, Detail: fmt.Sprintf("503 with reason %q", se.Reason)}
	case http.StatusTooManyRequests:
		_ = json.Unmarshal(raw, &se)
		return nil, &RateLimitedError{Message: nonEmpty(se.Error, "rate limit exceeded for this tenant")}
	case http.StatusBadRequest, http.StatusRequestEntityTooLarge:
		_ = json.Unmarshal(raw, &se)
		return nil, &RejectedError{Status: status, Message: nonEmpty(se.Error, http.StatusText(status))}
	case http.StatusInternalServerError:
		return nil, &UnavailableError{Reason: ReasonInternalError, Detail: "the service answered 500"}
	}
	return nil, &UnavailableError{Reason: ReasonBadResponse, Detail: fmt.Sprintf("HTTP status %d", status)}
}

func nonEmpty(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}
