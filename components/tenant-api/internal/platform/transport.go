package platform

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
)

// JSONRoundTrip performs one authenticated JSON request against a forge REST API
// and returns the response body and headers. It centralizes the transport policy
// the GitHub and GitLab clients previously duplicated line-for-line in their own
// roundTrip methods:
//
//   - JSON body marshaling (nil body → no body, no Content-Type).
//   - The no-leak error contract: a non-2xx response becomes a *APIError carrying
//     ONLY the status code; the full upstream body is logged for debugging but is
//     never surfaced to callers, so forge error details don't leak through the API.
//   - TRK-319 rate-limit detection (a 403/429 secondary rate limit is flagged so
//     the circuit breaker treats it as forge degradation, not a permission error).
//
// setAuth applies the provider-specific auth/Accept headers; provider is the
// canonical forge name ("GitHub"/"GitLab") used in the APIError and the log line.
// Callers keep their own circuit-breaker wrapping around this call (see the
// clients' do/doRequest), so the breaker still sees ErrCircuitOpen vs APIError
// exactly as before.
func JSONRoundTrip(httpClient *http.Client, provider, baseURL, method, path string, body interface{}, setAuth func(http.Header)) ([]byte, http.Header, error) {
	var bodyReader io.Reader
	if body != nil {
		jsonBody, err := json.Marshal(body)
		if err != nil {
			return nil, nil, fmt.Errorf("marshal body: %w", err)
		}
		bodyReader = bytes.NewReader(jsonBody)
	}

	req, err := http.NewRequest(method, baseURL+path, bodyReader)
	if err != nil {
		return nil, nil, fmt.Errorf("create request: %w", err)
	}

	setAuth(req.Header)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, nil, fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode >= 400 {
		// Sanitize: log the full response for debugging but expose only the status
		// code to callers, so internal forge error details never leak to API consumers.
		slog.Warn(strings.ToLower(provider)+" API non-2xx",
			"method", method, "path", path, "status", resp.StatusCode, "body", string(respBody))
		apiErr := &APIError{
			Provider: provider, Method: method, Path: path, StatusCode: resp.StatusCode,
		}
		if limited, retryAfter := DetectRateLimit(resp.StatusCode, resp.Header, respBody); limited {
			apiErr.RateLimited = true
			apiErr.RetryAfter = retryAfter
		}
		return nil, resp.Header, apiErr
	}
	return respBody, resp.Header, nil
}
