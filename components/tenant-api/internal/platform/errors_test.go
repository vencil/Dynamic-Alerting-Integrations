package platform

import (
	"errors"
	"net/http"
	"strings"
	"testing"
)

func TestAPIError_IsForbidden(t *testing.T) {
	t.Parallel()
	forbidden := &APIError{Provider: "GitHub", Method: "POST", Path: "/repos/o/r/pulls", StatusCode: http.StatusForbidden}
	if !errors.Is(forbidden, ErrForbidden) {
		t.Error("403 APIError should match ErrForbidden")
	}

	notForbidden := &APIError{Provider: "GitHub", Method: "GET", Path: "/x", StatusCode: http.StatusInternalServerError}
	if errors.Is(notForbidden, ErrForbidden) {
		t.Error("500 APIError should NOT match ErrForbidden")
	}
}

func TestAPIError_MessageHasCoordinatesOnly(t *testing.T) {
	t.Parallel()
	e := &APIError{Provider: "GitLab", Method: "POST", Path: "/api/v4/projects/x/merge_requests", StatusCode: 422}
	msg := e.Error()
	// The message carries status + coordinates only — never an upstream body.
	for _, want := range []string{"GitLab", "POST", "422"} {
		if !strings.Contains(msg, want) {
			t.Errorf("Error() = %q, missing %q", msg, want)
		}
	}
}
