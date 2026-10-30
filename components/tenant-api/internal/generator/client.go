// Package generator is tenant-api's side of the generator container
// (ADR-036 §3–§6, #2903): the `da-tools generator-service` that judges every
// content-changing conf.d write with `generate-routes --validate --strict`,
// over HTTP/1.1 on a pod-internal Unix socket.
//
// Client speaks the service's three endpoints (POST /v1/validate,
// GET /v1/status, GET /metrics). Gate is the gitops.WriteValidator built on
// it: deadline propagation, the "judged = written" reconciliation, findings
// ordering and the pre-existing comparison, and the error each outcome maps
// to (errors.go). The endpoint contract is docs/cli-reference.md
// §generator-service.
package generator

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"
)

// ScrapeTimeout bounds every call made while /metrics is being scraped
// (GET /v1/status for tenant_api_policy_available, GET /metrics for the
// da_generator_* families). The one place it is written.
const ScrapeTimeout = 2 * time.Second

// maxResponseBytes caps what is read of one response: the service's own
// --max-result-bytes default is 16 MiB, and findings plus the envelope stay
// under twice that.
const maxResponseBytes = 32 << 20

// Client talks to the generator service on its Unix socket.
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
		// The service answers every request with Connection: close.
		DisableKeepAlives: true,
	}
	return &Client{socket: socket, hc: &http.Client{Transport: tr}}
}

// Socket is the path the client dials.
func (c *Client) Socket() string { return c.socket }

// Write is one entry of a validate request's `writes`.
type Write struct {
	Path    string
	Content []byte
}

// Validated is what the service judged (the response's `validated`).
type Validated struct {
	BaseCommit string          `json:"base_commit"`
	Files      []ValidatedFile `json:"files"`
	// PolicySHA256 is nil when the service runs without --policy.
	PolicySHA256 *string `json:"policy_sha256"`
}

// ValidatedFile is one judged write.
type ValidatedFile struct {
	Path    string `json:"path"`
	SHA256  string `json:"sha256"`
	Deleted bool   `json:"deleted"`
}

// Finding is one entry of the generator's `--findings-json` document
// (schema da-tools.findings/v1), plus the two marks Gate adds.
type Finding struct {
	Kind     string  `json:"kind"`
	Severity string  `json:"severity"`
	Blocks   string  `json:"blocks"`
	Tenant   *string `json:"tenant"`
	Policy   *string `json:"policy"`
	File     *string `json:"file"`
	Field    *string `json:"field"`
	Message  string  `json:"message"`
	// Related: the finding names a file or a tenant this write changes.
	Related bool `json:"related"`
	// PreExisting: the same finding is reported on the base commit without
	// this write. nil when the base was not judged (see
	// FindingsError.ComparedWithBase).
	PreExisting *bool `json:"pre_existing,omitempty"`
}

// FindingsDoc is the `findings` document of a verdict.
type FindingsDoc struct {
	Schema    string    `json:"schema"`
	ConfigDir string    `json:"config_dir"`
	ExitCode  int       `json:"exit_code"`
	Findings  []Finding `json:"findings"`
}

// Verdict is a 200 answer of POST /v1/validate.
type Verdict struct {
	Result     string       `json:"result"` // pass | findings | rejected
	Reason     string       `json:"reason"` // when rejected
	Message    string       `json:"message"`
	RC         *int         `json:"rc"`
	Findings   *FindingsDoc `json:"findings"`
	StderrTail string       `json:"stderr_tail"`
	Validated  *Validated   `json:"validated"`
	// KeyVerdict is the key verdict on a pass (ADR-038 decision 5); nil
	// from a service that predates it.
	KeyVerdict *KeyVerdict `json:"key_verdict"`
}

// KeyVerdict is a pass's `key_verdict`: status complete or not_completed
// (with reason and message), findings when complete, truncated the
// findings not sent. Message is the service's own text, never shown to a
// caller (it can carry a platform path or a program's stderr): it is
// logged.
type KeyVerdict struct {
	Status    string       `json:"status"`
	Reason    string       `json:"reason"`
	Message   string       `json:"message"`
	Tenants   int          `json:"tenants"`
	Seconds   float64      `json:"seconds"`
	Findings  []KeyFinding `json:"findings"`
	Truncated int          `json:"truncated"`
}

// KeyFinding is one finding of a KeyVerdict.
type KeyFinding struct {
	Severity string   `json:"severity"`
	Kind     string   `json:"kind"`
	Key      string   `json:"key"`
	File     *string  `json:"file"`
	Tenants  []string `json:"tenants"`
	Reason   *string  `json:"reason"`
}

// errorBody is the service's non-200 JSON body.
type errorBody struct {
	Error   string `json:"error"`
	Reason  string `json:"reason"`
	Message string `json:"message"`
}

// Validate asks the service to judge base with writes, giving it remaining
// as the caller's time left (`deadline_ms`). A 200 is a Verdict; every other
// answer is an error from errors.go: 503 → *UnavailableError with the
// service's code and reason, 413 → *RejectedError (ReasonTooLarge), 400 →
// *RejectedError (ReasonInvalidRequest), anything unreadable or unreachable
// → *UnavailableError (VALIDATOR_UNAVAILABLE). A ctx that ends first is
// returned as ctx's error, wrapped.
func (c *Client) Validate(ctx context.Context, base string, writes []Write, remaining time.Duration) (*Verdict, error) {
	type wireWrite struct {
		Path       string `json:"path"`
		ContentB64 string `json:"content_b64"`
	}
	req := struct {
		BaseCommit string      `json:"base_commit"`
		Writes     []wireWrite `json:"writes"`
		DeadlineMS int64       `json:"deadline_ms"`
	}{BaseCommit: base, Writes: make([]wireWrite, 0, len(writes)), DeadlineMS: remaining.Milliseconds()}
	if req.DeadlineMS < 0 {
		req.DeadlineMS = 0
	}
	for _, w := range writes {
		req.Writes = append(req.Writes, wireWrite{Path: w.Path, ContentB64: base64.StdEncoding.EncodeToString(w.Content)})
	}
	body, err := json.Marshal(req)
	if err != nil {
		return nil, unavailable(ReasonInternal, "encode the validate request: "+err.Error())
	}
	status, raw, err := c.do(ctx, http.MethodPost, "/v1/validate", body)
	if err != nil {
		return nil, err
	}
	switch status {
	case http.StatusOK:
		var v Verdict
		if err := json.Unmarshal(raw, &v); err != nil {
			return nil, unavailable(ReasonBadResponse, "the validate answer is not JSON: "+err.Error())
		}
		switch v.Result {
		case ResultPass, ResultFindings, ResultRejected:
			return &v, nil
		}
		return nil, unavailable(ReasonBadResponse, fmt.Sprintf("the validate answer has an unknown result %q", v.Result))
	case http.StatusServiceUnavailable:
		var e errorBody
		_ = json.Unmarshal(raw, &e)
		code := e.Error
		if code != CodePolicyUnavailable {
			code = CodeValidatorUnavailable
		}
		reason := e.Reason
		if reason == "" {
			reason = ReasonUnknown
		}
		return nil, &UnavailableError{Code: code, Reason: reason, Message: e.Message}
	case http.StatusRequestEntityTooLarge:
		var e errorBody
		_ = json.Unmarshal(raw, &e)
		return nil, &RejectedError{Reason: ReasonTooLarge, Message: e.Message}
	case http.StatusBadRequest:
		var e errorBody
		_ = json.Unmarshal(raw, &e)
		return nil, &RejectedError{Reason: ReasonInvalidRequest, Message: e.Message}
	}
	return nil, unavailable(ReasonBadResponse, fmt.Sprintf("the validate answer has HTTP status %d", status))
}

// Status is GET /v1/status, as far as tenant-api reads it.
type Status struct {
	Policy struct {
		Configured bool    `json:"configured"`
		Available  bool    `json:"available"`
		SHA256     *string `json:"sha256"`
		Error      *string `json:"error"`
	} `json:"policy"`
	GeneratorVersion *string `json:"generator_version"`
	GeneratorTooOld  *string `json:"generator_too_old"`
	DAGuardVersion   *string `json:"da_guard_version"`
	AmtoolAvailable  bool    `json:"amtool_available"`
	// DAGuardAvailable is nil from an image older than the shared key
	// verdict tenant-api refuses on (ADR-038 decision 5): such an image
	// passes writes without one, so it cannot judge them (ReadyForVerdicts).
	DAGuardAvailable *bool `json:"da_guard_available"`
	Ready            bool  `json:"ready"`
}

// ReadyForVerdicts is whether the container can judge writes as tenant-api
// takes them: ready, and an image that gives the key verdict
// (DAGuardAvailable present). An older image answers ready, yet every write
// it passes is refused 503 generator_too_old (Gate.refuseUnjudged).
func (s *Status) ReadyForVerdicts() bool { return s.Ready && s.DAGuardAvailable != nil }

// Status reads GET /v1/status.
func (c *Client) Status(ctx context.Context) (*Status, error) {
	status, raw, err := c.do(ctx, http.MethodGet, "/v1/status", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, unavailable(ReasonBadResponse, fmt.Sprintf("the status answer has HTTP status %d", status))
	}
	var s Status
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, unavailable(ReasonBadResponse, "the status answer is not JSON: "+err.Error())
	}
	return &s, nil
}

// Metrics reads GET /metrics (Prometheus text format), as is.
func (c *Client) Metrics(ctx context.Context) ([]byte, error) {
	status, raw, err := c.do(ctx, http.MethodGet, "/metrics", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, unavailable(ReasonBadResponse, fmt.Sprintf("the metrics answer has HTTP status %d", status))
	}
	return raw, nil
}

// do sends one request and reads the whole answer. A transport failure is
// *UnavailableError (ReasonUnreachable), unless ctx ended first: then ctx's
// error, wrapped, so a caller tells "no time left" from "nobody answered".
func (c *Client) do(ctx context.Context, method, path string, body []byte) (int, []byte, error) {
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://generator"+path, rd)
	if err != nil {
		return 0, nil, unavailable(ReasonInternal, err.Error())
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return 0, nil, fmt.Errorf("generator %s %s: %w", method, path, ctx.Err())
		}
		return 0, nil, unavailable(ReasonUnreachable, fmt.Sprintf("%s %s on %s: %v", method, path, c.socket, err))
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
	if err != nil {
		if ctx.Err() != nil {
			return 0, nil, fmt.Errorf("generator %s %s: %w", method, path, ctx.Err())
		}
		return 0, nil, unavailable(ReasonUnreachable, fmt.Sprintf("read the answer to %s %s: %v", method, path, err))
	}
	if len(raw) > maxResponseBytes {
		return 0, nil, unavailable(ReasonBadResponse, fmt.Sprintf("the answer to %s %s is over %d bytes", method, path, maxResponseBytes))
	}
	return resp.StatusCode, raw, nil
}

// isCtxErr reports whether err is (wraps) a context's end.
func isCtxErr(err error) bool {
	return errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled)
}
