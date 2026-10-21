package handler

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/statestore"
)

// writeConfigFileError is the error mapping the group and view writes use; a
// state-store failure must reach the caller as its own class (#2856), with a
// machine-readable code and without the ConfigMap's namespace/name.
func TestWriteConfigFileError_StateStoreErrors(t *testing.T) {
	t.Parallel()
	const backing = "configmap tenant-api/tenant-api-state"
	for _, tc := range []struct {
		err        error
		status     int
		code       string
		retryAfter bool
	}{
		{fmt.Errorf("%w (%s)", statestore.ErrConflict, backing), http.StatusConflict, string(errcode.Conflict), false},
		{fmt.Errorf("%w: get %s: timeout", statestore.ErrUnavailable, backing), http.StatusServiceUnavailable, string(errcode.Upstream), true},
		{fmt.Errorf("%w: %s would hold 950000 bytes (budget 921600)", statestore.ErrTooLarge, backing), http.StatusInsufficientStorage, string(errcode.StateFull), false},
	} {
		t.Run(http.StatusText(tc.status), func(t *testing.T) {
			t.Parallel()
			w := httptest.NewRecorder()
			writeConfigFileError(w, httptest.NewRequest(http.MethodPut, "/api/v1/groups/g", nil), tc.err)
			if w.Code != tc.status {
				t.Fatalf("status = %d, want %d; body=%s", w.Code, tc.status, w.Body.String())
			}
			var body ErrorResponse
			if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			if string(body.Code) != tc.code {
				t.Errorf("code = %q, want %q", body.Code, tc.code)
			}
			if got := w.Header().Get("Retry-After") != ""; got != tc.retryAfter {
				t.Errorf("Retry-After present = %v, want %v", got, tc.retryAfter)
			}
			if strings.Contains(w.Body.String(), "tenant-api-state") {
				t.Errorf("body names the backing ConfigMap: %s", w.Body.String())
			}
		})
	}
}
