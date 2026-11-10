package handler

import (
	"bytes"
	"fmt"
	"log/slog"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/generator"
)

// #1560 ⑤-b: a batch op the generator gave no verdict on reaches the log
// by its reason code only — never the container's message (free text that
// can quote a path or what the tenants wrote).
//
// Intentionally NOT t.Parallel(): it swaps the process-global default slog
// logger and restores it.
func TestGeneratorOpResult_LogsNoContainerMessage(t *testing.T) {
	// A test canary, not a credential.
	const canary = "CANARY-batch-log-5b"
	var buf bytes.Buffer
	orig := slog.Default()
	defer slog.SetDefault(orig)
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))

	for _, reason := range []string{generator.ReasonMemory, "a reason " + canary} {
		// Wrapped as the write paths hand it over (#1560 ⑤-b N-4): slog
		// resolves a LogValuer on the value it is given only, so a call site
		// that logs the wrapper logs Error() — the container's message.
		rej := fmt.Errorf("write: %w", &generator.RejectedError{Reason: reason, Message: "container: " + canary})
		// A batch op's result (generatorOpResult) …
		res, ok := generatorOpResult("db-a", rej)
		if !ok || res.Status != "error" {
			t.Fatalf("generatorOpResult = %+v, %v", res, ok)
		}
		if strings.Contains(res.Message, canary) {
			t.Errorf("the client text quotes the container's message: %q", res.Message)
		}
		// … and a single write's answer (renderKind) log alike.
		text, _ := renderKind(errkind.GeneratorRejected, httptest.NewRequest("PUT", "/", nil), &Deps{}, rej, errCtx{})
		if strings.Contains(text, canary) {
			t.Errorf("the client text quotes the container's message: %q", text)
		}
	}
	logs := buf.String()
	if strings.Contains(logs, "CANARY") {
		t.Errorf("the log quotes the container's text:\n%s", logs)
	}
	if !strings.Contains(logs, "batch op refused: the generator gave no verdict") ||
		!strings.Contains(logs, "write refused: the generator gave no verdict") ||
		!strings.Contains(logs, `"reason":"`+generator.ReasonMemory+`"`) ||
		!strings.Contains(logs, `"reason":"unknown"`) {
		t.Errorf("no structured refusal lines:\n%s", logs)
	}
}
