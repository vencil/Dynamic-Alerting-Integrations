package gitops

// The generator's verdict on a PUT preview (#3016, POST
// /tenants/{id}/validate): the write's own judgement — the same
// WriteValidator and the same key-verdict refusal as validateProposed —
// asked without the writer lock and without committing anything.
//
//   - One preview is judged at a time (Writer.previewMu); a second is not
//     queued but answered ErrPreviewBusy, so previews never stand in the
//     generator's queue ahead of writes.
//   - The context is marked a preview (WithPreview): the validator then
//     neither counts its refusals among the writes'
//     (tenant_api_generator_rejections_total) nor judges the base a second
//     time to mark pre-existing findings.
//   - base is HEAD as read now. A write samples it before it reads the
//     tenant file (#1535 item 6); a preview judges the body it was given,
//     which replaces the file whole, so a commit landing between the read
//     and the sample changes only the rest of the tree it is judged in.

import (
	"context"
	"errors"
	"fmt"
)

// ErrPreviewBusy is a preview not judged because another preview is being
// judged (Writer.previewMu).
var ErrPreviewBusy = errors.New("another preview is being judged; retry")

// ErrPreviewUnchanged is a preview not judged because its body changes
// nothing: the write does not ask either (unchangedFile).
var ErrPreviewUnchanged = errors.New("the body changes nothing, so nothing is judged")

type previewKey struct{}

// WithPreview marks ctx as a preview's judgement (JudgePreview).
func WithPreview(ctx context.Context) context.Context {
	return context.WithValue(ctx, previewKey{}, true)
}

// IsPreview reports whether ctx is a preview's judgement (WithPreview).
func IsPreview(ctx context.Context) bool {
	v, _ := ctx.Value(previewKey{}).(bool)
	return v
}

// JudgePreview asks the write's judgement about res, the write PreviewPut
// resolved for tenantID: the key verdict's lines for ctx's caller
// (adviseKeyVerdict, as a write returns them), or the error the write would
// be refused with — a *KeyVerdictError, or the validator's own.
// ErrPreviewUnchanged for a body that changes nothing (the write does not
// ask either, unchangedFile); ErrPreviewBusy when another preview is being
// judged.
func (w *Writer) JudgePreview(ctx context.Context, tenantID string, res PutResolution) ([]string, error) {
	if w.unchangedFile(res.FilePath, res.Existing, res.Content) {
		return nil, ErrPreviewUnchanged
	}
	if !w.previewMu.TryLock() {
		return nil, ErrPreviewBusy
	}
	defer w.previewMu.Unlock()
	base, err := w.currentHEAD()
	if err != nil {
		return nil, fmt.Errorf("%w: %v", errValidatorBase, err)
	}
	adv, err := w.validateProposed(WithPreview(ctx), base, []ProposedFile{{
		Path: confRelKey(w.configDir, res.FilePath), Content: []byte(res.Content), Tenants: []string{tenantID},
	}})
	if err != nil {
		return nil, err
	}
	return adv.lines(), nil
}
