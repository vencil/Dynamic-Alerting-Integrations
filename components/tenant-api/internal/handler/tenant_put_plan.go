package handler

// --- One refusal sequence for PUT, /diff and /validate (#3062) -----------
//
// PUT /tenants/{id}, POST /tenants/{id}/diff and POST /tenants/{id}/validate
// answer the same question — would this body be written, and as what? — so
// they refuse it by ONE sequence, in PUT's order, written once:
//
//  1. planPut, in the handler, on the serving tree: the id; the write
//     scope on {id} as the file holds it now (requireOrgWriteAt's rule);
//     {id}'s file not a regular file; the body; the write scope on {id} as
//     the body proposes it; a masked-credential placeholder (#1560).
//  2. gitops: the body-only pre-flight (validateBodyOnly — among others, the
//     body must be API-shaped: tenants.{id} and no other tenant, #3064), then
//     resolvePut on the tree the write replaces: the tenant file's
//     resolution (#1673, #2078, #2939); a git-managed tenant refused (#3064,
//     409 TENANT_GIT_MANAGED); validation. PUT runs it in the Writer — under
//     its lock on the serving tree (direct mode), or on the fresh base (PR
//     mode) — and the previews on the serving tree without a lock
//     (gitops.PreviewPut).
//
// Only the tree step 2 reads differs. A preview's refusal is PUT's, with the
// same status and body: /diff answers it as PUT does (planPut writes it, and
// writePutDirectError), /validate as its verdict — valid: false with the
// same text (validateWarnings), its convention for every refusal that is not
// about the request itself (the id, the body's size).
//
// ⛔ ORDER IS PART OF THE ANSWER. A check that runs earlier in one endpoint
// than in PUT answers a question PUT never answers. No endpoint has a check of
// its own here.
//
// Two places where a preview cannot be PUT, by design:
//
//   - /diff for a caller who may read {id} but not write it: the {id} write
//     scope refusing is not a 403 there but the masked preview (#1560): PUT
//     refuses that caller before anything else, which the caller knows. The
//     rest of the sequence runs unchanged (the proposed scope on {id} aside,
//     which only asks again what was refused); a git-managed tenant is the
//     same 409 TENANT_GIT_MANAGED its GET marks with managed_by: git.
//   - PR mode: PUT runs step 2 on the fresh base, under the writer lock;
//     the serving tree may lag it (#1718). /diff previews on the serving tree,
//     as it must to diff anything; /validate runs step 2's body-only part
//     only (gitops.DryRunValidateBodyOnly), so it never refuses on a stale
//     tree what the PR would accept — a git-managed tenant included.
//
// After the sequence, /diff decides what it may SHOW: for a masked preview,
// nothing it cannot mask with certainty. The file it shows is API-shaped —
// the tenant's own — or the sequence refused it (#3064).

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

// putEndpoint names the endpoint a planPut runs for.
type putEndpoint int

const (
	endpointPut putEndpoint = iota
	endpointValidate
	endpointDiff
)

// putPlan is what planPut established about a request: the body, and what
// the gates judged on it (for PR mode's re-judgement on the fresh base).
type putPlan struct {
	tenantID string
	body     string
	// r is the request carrying the #2405 bit (withReplaceUnparseable) and
	// the caller's TenantNames (withTenantNames): every later step reads
	// them from here.
	r *http.Request
	// masked: /diff only, the caller may not write {id} (see the file
	// comment).
	masked            bool
	gatePre, gatePost writeGate
}

// readPutBody reads the request body as one endpoint receives it, writing a
// refusal of the request itself (size, a malformed header or envelope) to rw.
type readPutBody func(rw http.ResponseWriter, r *http.Request) (string, bool)

// planPut runs step 1 of the sequence (see the file comment). Refusals of
// the request itself — the id, and whatever readBody refuses — go to rw;
// every other refusal goes to verdict (rw for PUT and /diff, a
// verdictCapture for /validate). ok is false once a refusal was written.
func planPut(rw, verdict http.ResponseWriter, r *http.Request, d *Deps, ep putEndpoint, readBody readPutBody) (*putPlan, bool) {
	tenantID := chi.URLParam(r, "id")
	if err := ValidateWritableTenantID(tenantID); err != nil {
		WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
		return nil, false
	}
	p := &putPlan{tenantID: tenantID}

	// ADR-027 / LD-6 P4b: org-scope-aware write gate, BEFORE the body is
	// read, so a denied caller triggers no side effect and learns nothing
	// from policy-violation details. Judged once (a second OrgAllowed would
	// count a second shadow would-deny).
	var allowed bool
	p.gatePre, allowed = orgWriteAt(r, d, tenantID)
	if !allowed {
		if ep != endpointDiff {
			WriteJSONErrorWithCode(verdict, r, http.StatusForbidden, CodeForbidden, orgWriteDeniedText(tenantID))
			return nil, false
		}
		p.masked = true // #1560: canSeeCredentials is this very predicate
	}
	// #2477: a tenant file that is not a regular file is refused here, at
	// once (409, config_error not_regular_file), before any step touches
	// it. A new tenant (no file yet) and every other resolve outcome go on.
	if existing, err := confd.ResolveTenantFile(d.ConfigDir, tenantID); err == nil {
		if errors.Is(confd.CheckRegularFile(existing), confd.ErrNotRegularFile) {
			writeTenantFileNotRegular(verdict, r, tenantID)
			return nil, false
		}
	}

	body, ok := readBody(rw, r)
	if !ok {
		return nil, false
	}
	p.body = body

	// #1597 follow-up: the gate above authorized against the tenant's
	// metadata AS IT IS ON DISK; the body can move it into an
	// environment/domain the caller does not administer, so the PROPOSED
	// state is authorized too. A masked /diff caller was refused the write
	// already.
	if !p.masked {
		if p.gatePost, ok = requireOrgWriteProposedAt(verdict, r, d, tenantID, rbac.PermWrite, body); !ok {
			return nil, false
		}
	}
	// #1560: a masked-credential placeholder written back is a 400.
	if err := gitops.PlaceholderPreflight(body); err != nil {
		writePlaceholderError(verdict, r, err)
		return nil, false
	}

	// #2405: replacing a current file that cannot be parsed needs write
	// permission on all tenants; the bit is read where the file is (step 2).
	// #3039: which other tenants a validation message may name.
	p.r = withTenantNames(withReplaceUnparseable(r, d), d)
	return p, true
}

// preview runs step 2 on the serving tree, without a lock
// (gitops.PreviewPut): what PUT would write, or its refusal — errs the
// validation set PUT wraps in ErrValidation, err any earlier refusal.
func (p *putPlan) preview(d *Deps) (gitops.PutResolution, []string, error) {
	return previewWriter(d).PreviewPut(p.r.Context(), p.tenantID, p.body)
}

// previewWriter is d.Writer, or for handler-test literals that set only
// ConfigDir a Writer on it: the same checks, without the production
// Writer's shared read walk and its stuck-walk breaker. Production always
// wires d.Writer (cmd/server/main.go).
func previewWriter(d *Deps) *gitops.Writer {
	if d.Writer != nil {
		return d.Writer
	}
	return gitops.NewWriter(d.ConfigDir, "")
}

// validationError is the error PUT's writer returns for a validation set.
func validationError(errs []string) error {
	return fmt.Errorf("%w: %s", gitops.ErrValidation, strings.Join(errs, "; "))
}

// writePutDirectError answers a direct-mode PUT whose write the Writer
// refused, and a /diff whose preview of it was refused (the same refusal,
// the same answer). fault reports a failure that is the server's (#3054).
func writePutDirectError(rw http.ResponseWriter, r *http.Request, d *Deps, tenantID string, err error) (fault bool) {
	// #2903: the generator container refused it (422 / 429 / 503).
	if writeGeneratorError(rw, r, d, err) {
		return false
	}
	if errors.Is(err, gitops.ErrWriteOverloaded) {
		WriteOverloaded(rw, r)
		return false
	}
	// #1723: the tree was on a PR branch and nothing was written —
	// retryable, not the 400 fallback below.
	if errors.Is(err, gitops.ErrTreeNotOnBase) {
		WriteTreeNotOnBase(rw, r, err)
		return false
	}
	// Same 409 + CONFLICT code as the custom-alerts base_hash check: from
	// the caller's side both mean "the file moved under you, re-read and
	// retry", and the current hash tells them what to re-read against.
	var pre *gitops.PreconditionError
	if errors.As(err, &pre) {
		extra := map[string]any{
			"message": "the tenant configuration was updated by someone else; refresh and retry",
		}
		// #3064: only the hash of a file GET /tenants/{id} shows — an
		// API-shaped one, judged on the very bytes it hashes.
		if pre.Current != "" && tenantFileAPIManaged(d, tenantID, pre.Current) {
			extra["current_source_hash"] = pre.Current
		}
		WriteErrorEnvelope(rw, r, http.StatusConflict, ErrorResponse{
			Error: gitops.ErrPrecondition.Error(),
			Code:  CodeConflict,
			Extra: extra,
		})
		return false
	}
	if errors.Is(err, gitops.ErrConflict) {
		WriteJSONError(rw, r, http.StatusConflict, err.Error())
		return false
	}
	// #1673: two files claim this tenant. The request is well-formed; the
	// on-disk state is ambiguous — 409, not the 400 fallback.
	if errors.Is(err, confd.ErrAmbiguousTenantFile) {
		WriteJSONError(rw, r, http.StatusConflict, err.Error())
		return false
	}
	// #2078: the id is declared by another conf.d file → 409
	// TENANT_DECLARED_ELSEWHERE (fixed message, path only in the log).
	if writeTenantPlacementError(rw, r, d, err) {
		return false
	}
	// #1700: the text is the caller's only when it is about the request; a
	// file / git failure keeps its path in the log.
	WriteJSONError(rw, r, http.StatusBadRequest, writeErrorText(r, err))
	return !writeErrorIsForClient(err)
}

// verdictCapture is the http.ResponseWriter /validate hands planPut for its
// verdicts: the refusal PUT would write, kept to be read back as the
// dry-run's warnings (validateWarnings) — the same text, never a second
// copy of it.
type verdictCapture struct {
	header http.Header
	status int
	body   bytes.Buffer
}

func (c *verdictCapture) Header() http.Header {
	if c.header == nil {
		c.header = http.Header{}
	}
	return c.header
}

func (c *verdictCapture) WriteHeader(status int) {
	if c.status == 0 {
		c.status = status
	}
}

func (c *verdictCapture) Write(b []byte) (int, error) {
	if c.status == 0 {
		c.status = http.StatusOK
	}
	return c.body.Write(b)
}

// validateWarnings is /validate's rendering of the refusal c holds: its
// violations one per line ("field: reason", as the dry-run has always listed
// them), or else its error text.
func (c *verdictCapture) validateWarnings() []string {
	var env struct {
		Error      string      `json:"error"`
		Violations []Violation `json:"violations"`
	}
	if err := json.Unmarshal(c.body.Bytes(), &env); err != nil || (env.Error == "" && len(env.Violations) == 0) {
		return []string{msgInternal}
	}
	if len(env.Violations) > 0 {
		out := make([]string, len(env.Violations))
		for i, v := range env.Violations {
			out[i] = v.Field + ": " + v.Reason
		}
		return out
	}
	return []string{env.Error}
}
