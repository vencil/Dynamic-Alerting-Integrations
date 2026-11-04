package handler

// The write's own judgement on a /validate preview (#3016): the route
// generator and the shared key verdict, asked as PUT asks them
// (gitops.Writer.JudgePreview), answered with PUT's text under PUT's
// disclosure rules.

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"

	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/gitops"
)

// Reasons of ValidateResponse.NotJudged that are the preview's own (the
// rest are the generator's, as PUT would be answered).
const (
	notJudgedRefused     = "refused"
	notJudgedPRMode      = "pr_mode"
	notJudgedUnchanged   = "unchanged"
	notJudgedPreviewBusy = "preview_busy"
	notJudgedNoValidator = "no_validator"
	notJudgedCanceled    = "canceled"
	notJudgedError       = "error"
)

// rejectionAboutTheWrite is a non-retryable GENERATOR_REJECTED reason that
// is a verdict on what would be written (its size, a path, its bytes after
// .gitattributes, a tree without a root platform file, a platform file):
// the PUT will not pass as it is, so the preview is judged and refused. Every
// other non-retryable reason (memory, incomplete, a key verdict that failed)
// is the container's own failure, which says nothing about the body.
var rejectionAboutTheWrite = map[string]bool{
	generator.ReasonTooLarge:               true,
	generator.ReasonInvalidRequest:         true,
	generator.ReasonNormalized:             true,
	generator.ReasonKeyVerdictUndecidable:  true,
	generator.ReasonKeyVerdictPlatformFile: true,
}

// judgePreview asks the write's judgement about res for tenantID: judged
// with the refusal PUT would answer (warnings: its 400 key-verdict lines,
// its 422 findings, or a GENERATOR_REJECTED that is a verdict on the write,
// rejectionAboutTheWrite) and the key verdict's advisory lines PUT returns
// on a pass; not judged with the reason — anything PUT would answer 429 or
// 503, a container that failed judging, a caller that went away — which
// says nothing about the body.
func judgePreview(d *Deps, r *http.Request, tenantID string, res gitops.PutResolution) (judged bool, notJudged string, warnings, advice []string) {
	advice, err := previewWriter(d).JudgePreview(r.Context(), tenantID, res)
	if err == nil {
		return true, "", nil, advice
	}
	var kve *gitops.KeyVerdictError
	var found *generator.FindingsError
	var rej *generator.RejectedError
	var unav *generator.UnavailableError
	switch {
	case errors.As(err, &kve):
		return true, "", kve.Lines(), nil
	case errors.As(err, &found):
		shown, withheld := disclosedFindings(d, r, found.Findings)
		for _, f := range shown {
			warnings = append(warnings, f.Message)
		}
		if withheld > 0 {
			warnings = append(warnings, fmt.Sprintf("the route generator reports %d more finding(s) "+
				"elsewhere in the tree that this caller may not see", withheld))
		}
		return true, "", warnings, nil
	case errors.As(err, &rej):
		if !rejectionAboutTheWrite[rej.Reason] {
			return false, generator.ClientReason(rej.Reason), nil, nil
		}
		return true, "", []string{generatorRejectedText(rej)}, nil
	case errors.As(err, &unav):
		slog.Warn("tenant dry-run: the generator container cannot judge", "tenant", tenantID,
			"code", unav.Code, "reason", unav.Reason, "error", err)
		return false, generator.ClientReason(unav.Reason), nil, nil
	case errors.Is(err, gitops.ErrPreviewUnchanged):
		return false, notJudgedUnchanged, nil, nil
	case errors.Is(err, gitops.ErrPreviewBusy):
		return false, notJudgedPreviewBusy, nil, nil
	case errors.Is(err, gitops.ErrNoValidator):
		return false, notJudgedNoValidator, nil, nil
	case errors.Is(err, context.Canceled):
		slog.Info("tenant dry-run: the caller went away before the judgement", "tenant", tenantID)
		return false, notJudgedCanceled, nil, nil
	default:
		slog.Error("tenant dry-run: the write's judgement failed", "tenant", tenantID, "error", err)
		return false, notJudgedError, nil, nil
	}
}
