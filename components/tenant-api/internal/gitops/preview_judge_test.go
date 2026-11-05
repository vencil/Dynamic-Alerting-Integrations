package gitops

// JudgePreview (#3016): the write's judgement on a PUT preview, without
// the writer lock, one at a time, the context marked a preview; and the
// refusal reads the verdict's `blocks`, never the severity.

import (
	"context"
	"errors"
	"testing"
)

// ctxValidator records whether the context it was asked with is a preview.
type ctxValidator struct {
	recordingValidator
	preview bool
	entered chan struct{} // when set, closed once ValidateWrites is entered
	block   chan struct{} // when set, ValidateWrites waits on it
}

func (v *ctxValidator) ValidateWrites(ctx context.Context, base string, files []ProposedFile) (*KeyVerdict, error) {
	v.mu.Lock()
	v.preview = IsPreview(ctx)
	v.mu.Unlock()
	if v.entered != nil {
		close(v.entered)
	}
	if v.block != nil {
		<-v.block
	}
	return v.recordingValidator.ValidateWrites(ctx, base, files)
}

func previewOf(t *testing.T, w *Writer, tenant, body string) PutResolution {
	t.Helper()
	res, errs, err := w.PreviewPut(context.Background(), tenant, body)
	if err != nil || len(errs) > 0 {
		t.Fatalf("preview: errs %q err %v", errs, err)
	}
	return res
}

func TestJudgePreview_TheWritesJudgementWithoutTheLock(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	w := newTestWriter(dir, dir)
	v := &ctxValidator{}
	w.SetValidator(v)
	body := "tenants:\n  db-a:\n    mysql_threads_running: \"70\"\n"
	res := previewOf(t, w, "db-a", body)

	// Under the writer lock: a preview does not wait on it.
	w.mu.Lock()
	lines, err := w.JudgePreview(context.Background(), "db-a", res)
	w.mu.Unlock()
	if err != nil || lines != nil || !v.preview || len(v.calls) != 1 ||
		v.calls[0].files[0].Path != "db-a.yaml" || string(v.calls[0].files[0].Content) != body {
		t.Fatalf("lines %q err %v preview %v calls %+v", lines, err, v.preview, v.calls)
	}

	// A blocking finding refuses the preview as it refuses the write.
	v.verdict = &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{kvFinding("_x", "db-a.yaml", "db-a")}}
	var kve *KeyVerdictError
	if _, err := w.JudgePreview(context.Background(), "db-a", res); !errors.As(err, &kve) || len(kve.Lines()) != 1 {
		t.Fatalf("err = %v", err)
	}

	// The body the file already holds: nothing to judge.
	if _, err := w.Write(context.Background(), "db-a", "op@example.com", body); err == nil {
		v.calls = nil
		v.verdict = nil
		if _, err := w.JudgePreview(context.Background(), "db-a", previewOf(t, w, "db-a", body)); !errors.Is(err, ErrPreviewUnchanged) ||
			len(v.calls) != 0 {
			t.Fatalf("unchanged: err %v, calls %d", err, len(v.calls))
		}
	}
}

func TestJudgePreview_OneAtATime(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	w := newTestWriter(dir, dir)
	v := &ctxValidator{entered: make(chan struct{}), block: make(chan struct{})}
	w.SetValidator(v)
	res := previewOf(t, w, "db-a", "tenants:\n  db-a:\n    mysql_threads_running: \"70\"\n")
	done := make(chan error)
	go func() {
		_, err := w.JudgePreview(context.Background(), "db-a", res)
		done <- err
	}()
	<-v.entered // the first holds the slot, inside the validator
	if _, err := w.JudgePreview(context.Background(), "db-a", res); !errors.Is(err, ErrPreviewBusy) {
		t.Errorf("second preview: err = %v, want ErrPreviewBusy", err)
	}
	close(v.block)
	if err := <-done; err != nil {
		t.Errorf("first preview: %v", err)
	}
}

// The refusal reads the verdict's own `blocks` (#3016), not the severity.
func TestKeyVerdictRefusal_ReadsBlocks(t *testing.T) {
	t.Parallel()
	files := []ProposedFile{{Path: "db-a.yaml", Tenants: []string{"db-a"}}}
	notBlocking := kvFinding("_x", "db-a.yaml", "db-a")
	notBlocking.Blocks = false
	if r := keyVerdictRefusal(files, &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{notBlocking}}); r != nil {
		t.Errorf("an error finding that does not block refused: %v", r)
	}
	blocking := kvWarning("_x", "db-a.yaml", "db-a")
	blocking.Blocks = true
	if r := keyVerdictRefusal(files, &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{blocking}}); r == nil {
		t.Error("a finding that blocks did not refuse")
	}
}
